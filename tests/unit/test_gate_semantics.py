"""Gate semantics that keep a run from stopping for the wrong reason, and from
publishing a broken ranking for the right one.

Three real-world failure modes motivate this file:

1. A non-blocking data-quality warning (e.g. a vendor price gap on an
   otherwise-healthy month) must never itself halt the monthly run -- only a
   *blocking* gate failure may raise ``Blocked``. If ``gates.run`` ever drops
   the blocking/non-blocking distinction, every ordinary warning becomes an
   outage.
2. G9's historical replay is the only thing standing between "the factor
   pipeline is deterministic" and "nobody would notice if it silently
   drifted". Its exact-match tolerance has to be tight enough to catch a
   genuine regression while still tolerating float noise at the ULP level.
3. ``check_draft``'s rank-order invariant is the last line of defence against
   publishing a cohort where rank 1 is not actually the best-scored name --
   a bug here would be invisible in the UI (numbers still look plausible)
   but would silently mis-rank every published cohort.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.gates import run as run_gates
from quant.errors import Blocked
from quant.model.models import check_draft
from quant.run import RunContext, _g9_replay
from quant.types import Actor, Draft, FrozenClock


# --------------------------------------------------------------------------- (a) gates.run: blocking vs non-blocking


class _FakeStore:
    """Minimal store double: only the two ``close_raw`` call shapes gates.py makes,
    plus the G5 quarantine lookup. Real ``PriceStore`` is SQLite-backed and would
    need a populated prices database just to exercise a warning path."""

    def __init__(self, window_df: pd.DataFrame):
        self._window_df = window_df

    def close_raw(self, security_ids, start, end, vintage_at):
        if start == end:
            # G3's as-of-only lookup: full coverage, nothing missing.
            return pd.DataFrame([[100.0] * len(security_ids)], index=[end], columns=security_ids)
        # The trailing-63-session window W_PRICE_GAPS scans.
        return self._window_df.reindex(columns=security_ids)

    def quarantined_securities(self, since, as_of):
        return []


@pytest.fixture
def gate_ctx(tmp_path):
    """RunContext with a shrunk universe floor so a 5-member fixture can exercise
    every pre-gate without hand-building a 480-row draft."""
    cfg = load()

    db_path = tmp_path / "state.sqlite"
    from quant.db.core import apply_schema, connect

    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    # with_paths() rebuilds Config from the original raw toml, so any override must
    # be applied to the object actually handed to RunContext, after this call.
    cfg = cfg.with_paths(db=db_path)
    cfg.universe.min_rows = 5  # production default is 480; irrelevant to what this test checks
    clock = FrozenClock("2026-09-30T18:30:00.000000Z")
    actor = Actor(kind="system", name="test")

    run_ctx = RunContext(as_of="2026-09-30", kind="production", track="live", cfg=cfg, clock=clock, actor=actor)
    with run_ctx as c:
        yield c


def _healthy_draft_with_price_gaps() -> Draft:
    """5 members, every pre-gate evidenced to pass -- except two names go dark for
    the last 5 sessions of the trailing window, tripping W_PRICE_GAPS (2/5 = 40%
    of members missing a bar, above the 20% warning share)."""
    sids = [1, 2, 3, 4, 5]
    return Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": i} for i in sids]),
        groups=pd.Series({i: "Technology" for i in sids}),
        source_refs={
            "universe_capture_date": "2026-09-20",  # 10 days old, well under the 62-day stale limit
            "has_fundamentals": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )


def _gap_window_df() -> pd.DataFrame:
    # String-keyed dates: gates.py puts the raw index into a DQ event's detail_json,
    # and a real price store's index is a date string, not a Timestamp.
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=pd.Timestamp("2026-09-30"), periods=90)]
    df = pd.DataFrame(100.0, index=dates, columns=[1, 2, 3, 4, 5])
    df.loc[dates[-5:], [4, 5]] = np.nan  # names 4 and 5 go dark for the last week, after having traded
    return df


def test_non_blocking_gate_failure_is_recorded_but_does_not_block(gate_ctx):
    """A non-blocking FAIL (W_PRICE_GAPS) must not raise Blocked, and the warning
    must be persisted as a DQ event -- silence would defeat the point of a
    'warning' gate. Catches the mutation that drops '`and c.blocking`' from the
    Blocked-decision filter in gates.run."""
    gate_ctx.store = _FakeStore(_gap_window_df())
    draft = _healthy_draft_with_price_gaps()

    report = run_gates(gate_ctx, draft, phase="pre", strict=True)  # must not raise

    w_gaps = next(c for c in report.checks if c.id == "W_PRICE_GAPS")
    assert w_gaps.status == "FAIL"
    assert w_gaps.blocking is False, "a warning-only gate must never carry blocking=True on failure"

    # every other pre-gate genuinely passed -- this isn't "nothing was checked"
    others = [c for c in report.checks if c.id != "W_PRICE_GAPS"]
    assert others and all(c.status in ("PASS", "DEFERRED") for c in others)

    event = gate_ctx.conn.execute(
        "SELECT severity FROM data_quality_events WHERE code = 'PRICE_GAPS'"
    ).fetchone()
    assert event is not None, "the non-blocking failure must still leave a DQ event behind"
    assert event["severity"] == "WARN"


def test_blocking_gate_failure_still_raises(gate_ctx):
    """Sanity counterpart: a genuine blocking failure (too few members for G1)
    must still raise Blocked. Without this, a fix for the test above could
    trivially 'pass' by making gates.run never raise at all."""
    gate_ctx.store = _FakeStore(_gap_window_df())
    draft = _healthy_draft_with_price_gaps()
    draft.members = pd.DataFrame([{"security_id": 1}, {"security_id": 2}])  # 2 < min_rows(5)

    with pytest.raises(Blocked) as exc_info:
        run_gates(gate_ctx, draft, phase="pre", strict=True)
    assert "GATE_FAILURE" in str(exc_info.value) or "BOOTSTRAP_REQUIRED" in str(exc_info.value)


# --------------------------------------------------------------------------- (b) G9 replay tolerance


def _seed_g9_prior(ctx, factor_id: str, z_stored: float) -> None:
    """One active factor with one stored value in a published prior live cohort --
    the minimal fixture _g9_replay needs to have something to replay against."""
    now = ctx.clock.iso()
    ctx.conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'SYNG9', 'G9 Synthetic', '2026-01-01', '2026-09-30', 'listed')"
    )
    ctx.conn.execute(
        """
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level,
            hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
            backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
            module_path, status, registered_on, status_changed_on
        ) VALUES (?, ?, 1, 'growth', 1, 3, 'stock', 'H', 'f', '[]', 10, 1, 0, 0.5, 'e', NULL, 'sha', 'mod', 'active', ?, ?)
        """,
        (factor_id, factor_id.split("@")[0], now, now),
    )
    ctx.conn.execute(
        """
        INSERT INTO cohorts (
            cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash,
            source_refs_json, published_at, generated_at, is_clean, run_id
        ) VALUES ('C-G9-PRIOR', '2026-08-31', 'live', '2026-08-31T18:29:59.999999Z', 'defprior',
                  'memprior', '{}', ?, ?, 1, ?)
        """,
        (now, now, ctx.run_id),
    )
    ctx.conn.execute(
        """
        INSERT INTO factor_values (
            cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group,
            flags, input_refs_json, track, run_id
        ) VALUES ('C-G9-PRIOR', '2026-08-31', 1, ?, NULL, NULL, ?, 'Technology', '', '{}', 'live', ?)
        """,
        (factor_id, z_stored, ctx.run_id),
    )


def _new_draft() -> Draft:
    return Draft(
        cohort_id="C-NEW", as_of="2026-09-30", track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z", definition_hash="defnew",
    )


def test_g9_fails_on_a_1e_minus_6_drift(ctx, monkeypatch):
    """A replayed z that differs from the pinned value by 1e-6 is a real
    regression (float noise from double-precision arithmetic is many orders
    smaller), so G9 must FAIL and block."""
    z_stored = 1.234567
    _seed_g9_prior(ctx, "g9_drift_factor@1", z_stored)

    def fake_compute_all(_ctx, _draft):
        return pd.DataFrame([{"security_id": 1, "factor_id": "g9_drift_factor@1", "z": z_stored + 1e-6}])

    monkeypatch.setattr("quant.factors.registry.compute_all", fake_compute_all)

    check = _g9_replay(ctx, _new_draft())

    assert check.status == "FAIL", f"expected a 1e-6 drift to fail replay, got reason: {check.reason}"
    assert check.blocking is True
    assert check.observed["mismatches"] == 1


def test_g9_passes_on_a_1e_minus_12_drift(ctx, monkeypatch):
    """A replayed z within 1e-12 of the pinned value is ordinary floating-point
    noise (well under the 1e-9 exact-match tolerance) and must PASS, not block
    an otherwise-clean monthly run over noise."""
    z_stored = 1.234567
    _seed_g9_prior(ctx, "g9_noise_factor@1", z_stored)

    def fake_compute_all(_ctx, _draft):
        return pd.DataFrame([{"security_id": 1, "factor_id": "g9_noise_factor@1", "z": z_stored + 1e-12}])

    monkeypatch.setattr("quant.factors.registry.compute_all", fake_compute_all)

    check = _g9_replay(ctx, _new_draft())

    assert check.status == "PASS", f"expected a 1e-12 drift to pass replay, got reason: {check.reason}"
    assert check.blocking is False
    assert check.observed["mismatches"] == 0


# --------------------------------------------------------------------------- (c) check_draft rank-order invariant


def _scores(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_check_draft_flags_a_rank_that_disagrees_with_final_score():
    """Rank 1 must be the *best* scored name. A cohort where rank 1 carries a
    lower final score than rank 2 has its ranking and its score out of sync --
    exactly the kind of bug that would ship a wrong 'top pick' to a BI
    developer reading the UI's rank column instead of the raw score."""
    draft = Draft(
        cohort_id="C-BAD", as_of="2026-09-30", track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z", definition_hash="def",
        scores=_scores([
            {"cohort_id": "C-BAD", "security_id": 1, "model_id": "EW_HIER_v1",
             "eligible": 1, "scored": 1, "rank": 1, "final": 10.0},
            {"cohort_id": "C-BAD", "security_id": 2, "model_id": "EW_HIER_v1",
             "eligible": 1, "scored": 1, "rank": 2, "final": 20.0},  # higher final, worse rank
        ]),
    )

    report = check_draft(draft, cfg=None)

    rank_check = next(c for c in report.checks if c.id == "rank_monotone_EW_HIER_v1")
    assert rank_check.status == "FAIL", "rank 1 scoring below rank 2 must fail the rank-order invariant"
    assert rank_check.blocking is True
    assert rank_check.observed is False


def test_check_draft_passes_a_correctly_ordered_cohort():
    """Counterpart sanity check: when the ranks agree with final score (rank 1
    is the highest), the same invariant must PASS -- otherwise the check above
    could be satisfied by an invariant that just always fails."""
    draft = Draft(
        cohort_id="C-GOOD", as_of="2026-09-30", track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z", definition_hash="def",
        scores=_scores([
            {"cohort_id": "C-GOOD", "security_id": 1, "model_id": "EW_HIER_v1",
             "eligible": 1, "scored": 1, "rank": 1, "final": 20.0},
            {"cohort_id": "C-GOOD", "security_id": 2, "model_id": "EW_HIER_v1",
             "eligible": 1, "scored": 1, "rank": 2, "final": 10.0},
        ]),
    )

    report = check_draft(draft, cfg=None)

    rank_check = next(c for c in report.checks if c.id == "rank_monotone_EW_HIER_v1")
    assert rank_check.status == "PASS"
    assert rank_check.observed is True
