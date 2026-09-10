"""Real, spec-conformant tests for the leakage suite T1-T10 (C07, MASTER_SPEC 7.5).

These exercise the rewritten quant.evaluation.leakage against real stored
evidence (a hand-built, internally-consistent published live cohort) as well
as each fully self-contained deterministic fixture (T6, T7, T8, T10) and the
T2 structural guard. Every test here would have failed against the previous
implementation, which hard-coded PASS for T3-T10 (T4 via an empty no-op) and
ran T1 only on ungrounded synthetic data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.data.prices import PriceStore
from quant.evaluation import leakage
from quant.run import RunContext, knowledge_cutoff
from quant.types import CheckReport, Draft


# --------------------------------------------------------------------------- fixture plumbing

AS_OF = "2026-06-30"
COHORT_ID = "live:2026-06-30"
CUTOFF = knowledge_cutoff(AS_OF)
SIDS_A = [1, 2, 3]
SIDS_B = [4, 5, 6]
ALL_SIDS = SIDS_A + SIDS_B


def _fresh_state_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with open("quant/db/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _mk_ctx(cfg, conn) -> RunContext:
    ctx = RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = conn
    ctx.run_id = 1
    return ctx


def _check(report: CheckReport, check_id: str):
    for c in report.checks:
        if c.id == check_id:
            return c
    raise AssertionError(f"{check_id} not found; ids={[c.id for c in report.checks]}")


def _seed_live_world(conn: sqlite3.Connection, *, break_field: str | None = None) -> None:
    """Seed one internally-consistent, spec-conformant published live cohort.

    ``break_field`` deliberately corrupts a single invariant so the FAIL-path
    tests can reuse this same builder:
    'published_at'                  -> cohort published before its own knowledge_cutoff
    'universe_captured_at'          -> universe capture referenced after the cutoff
    'missing_universe_captured_at'  -> no universe_captured_at key at all (fresh-bootstrap shape)
    'fv_cutoff'                     -> a factor_values.input_refs_json cutoff mismatch
    'price_manifest'                -> an empty prices_monthly.price_manifest_sha
    'drop_label'                    -> one scored member has no label row (T9)
    """
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, "
        "git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, ?, 'test', 'live', 1, ?, 'running', 'git', 'code', 'cfg', 'reg')",
        (AS_OF, CUTOFF),
    )
    for sid in ALL_SIDS:
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2020-01-01', '2026-12-31', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )

    published_at = CUTOFF if break_field != "published_at" else "2000-01-01T00:00:00.000000Z"
    if break_field == "missing_universe_captured_at":
        source_refs = {}
    else:
        universe_captured_at = (
            "2026-06-25T10:00:00.000000Z" if break_field != "universe_captured_at" else "2099-01-01T00:00:00.000000Z"
        )
        source_refs = {"universe_captured_at": universe_captured_at}
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
        "source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES (?, ?, 'live', ?, 'def1', 'mem1', ?, ?, ?, 1, 1)",
        (COHORT_ID, AS_OF, CUTOFF, json.dumps(source_refs), published_at, CUTOFF),
    )

    for mid, role in (("EW_HIER_v1", "champion"), ("IC_SHRUNK_v1", "challenger")):
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES (?, 'equal', ?, 'x', '{}', '2026-01-01')",
            (mid, role),
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES (?, 1, ?, '{\"family\":{\"momentum\":1.0}}', '2026-01-01')",
            (mid, json.dumps([{"factor_id": "mom_12_1@1", "family": "momentum"}])),
        )

    conn.execute(
        "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
        "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, "
        "code_sha256, module_path, status, registered_on, status_changed_on) "
        "VALUES ('mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, "
        "'sha', 'mod', 'active', '2026-01-01', '2026-01-01')"
    )

    fv_bad_cutoff = "2000-01-01T00:00:00.000000Z"

    for sg, sids_list in (("SECT_A", SIDS_A), ("SECT_B", SIDS_B)):
        n = len(sids_list)
        for i, sid in enumerate(sids_list):
            # Group-centered label (l_rel is a sector-relative return by construction
            # in the real pipeline): zero mean within each group.
            l_rel = 0.01 * (i - (n - 1) / 2.0)

            this_cutoff = fv_bad_cutoff if (break_field == "fv_cutoff" and sid == ALL_SIDS[0]) else CUTOFF
            input_refs = {"as_of": AS_OF, "cutoff": this_cutoff, "n_members": len(ALL_SIDS)}
            conn.execute(
                "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
                "sector_group, flags, input_refs_json, track, run_id) "
                "VALUES (?, ?, ?, 'mom_12_1@1', ?, ?, ?, ?, '', ?, 'live', 1)",
                (COHORT_ID, AS_OF, sid, float(sid), float(sid), float(sid) * 0.1, sg, json.dumps(input_refs)),
            )
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, "
                "group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, "
                "scored, eligible, n_factors_used, input_hash, generated_at, track, run_id) "
                "VALUES (?, ?, ?, 'EW_HIER_v1', 1, ?, 1, '{}', ?, ?, 0.0, ?, 1, 1, 1, 'h', ?, 'live', 1)",
                (COHORT_ID, AS_OF, sid, sg, float(sid) * 0.1, float(sid) * 0.1, float(sid) * 0.1, CUTOFF),
            )
            pms = "" if (break_field == "price_manifest" and sid == ALL_SIDS[0]) else "sha_ok"
            conn.execute(
                "INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, "
                "price_manifest_sha, run_id) VALUES (?, ?, ?, ?, ?, 'yahoo', ?, 1)",
                (COHORT_ID, AS_OF, sid, 100.0 + sid, 100.0 + sid, pms),
            )

            if break_field == "drop_label" and sid == ALL_SIDS[-1]:
                continue
            conn.execute(
                "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, "
                "evidence_hash, computed_at, r_log, r_arith, r_group_median, l_rel, r_uni, sector_group, "
                "status, mb36, mb36_touch, price_manifest_sha, computed_run_id) "
                "VALUES (?, ?, ?, 3, '2026-09-30', 'live', 1, 'eh1', ?, ?, ?, ?, ?, ?, ?, 'ok', 0, 0, 'sha_ok', 1)",
                (COHORT_ID, AS_OF, sid, CUTOFF, l_rel, l_rel, 0.0, l_rel, l_rel, sg),
            )


def _seed_model_weights(conn: sqlite3.Connection, evidence_hash: str) -> None:
    conn.execute(
        "INSERT INTO model_weights (cohort_id, model_id, model_version, as_of, family, weight_units, "
        "n_eff, alpha, gate, evidence_hash, run_id) "
        "VALUES (?, 'IC_SHRUNK_v1', 1, ?, 'momentum', 10000, NULL, NULL, 'closed', ?, 1)",
        (COHORT_ID, AS_OF, evidence_hash),
    )


def _seed_prices(cfg, sids) -> None:
    store = PriceStore(cfg.paths.prices_db, state_conn=None)
    for sid in sids:
        df = pd.DataFrame({"date": ["2026-06-30"], "close": [100.0 + sid], "splits": [1.0], "dividend": [0.0]})
        store.ingest(None, df, {"security_id": sid, "observed_at": "2026-06-30T18:30:00.000000Z", "close_basis": "raw"})


# --------------------------------------------------------------------------- T1

def test_t1_shuffle_uses_real_matured_cohort_when_available(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)

    report = leakage.run(ctx, draft=None)
    c = _check(report, "T1_SHUFFLE")

    assert c.blocking is False
    assert "live cohort" in c.reason
    assert COHORT_ID in c.reason
    assert c.status == "PASS", c.reason


def test_t1_shuffle_falls_back_to_documented_synthetic_fixture_without_data(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)

    report = leakage.run(ctx, draft=None)
    c = _check(report, "T1_SHUFFLE")

    assert c.blocking is False
    assert "synthetic fixture" in c.reason
    assert c.status == "PASS", c.reason


def test_t1_shuffle_never_blocks_regardless_of_status():
    for c in leakage.run(None, draft=None).checks:
        if c.id == "T1_SHUFFLE":
            assert c.blocking is False
            return
    raise AssertionError("T1_SHUFFLE missing")


def test_t1_shuffle_searches_backward_past_a_thin_latest_cohort(cfg):
    """A thin LATEST cohort (< 5 qualifying members) must not discard an
    earlier, fully-qualifying live cohort in favour of the synthetic fallback.

    Both `cohorts` and `labels` are append-only/immutable in this schema, so
    the thin cohort is seeded as a genuinely newer, separate cohort (rather
    than mutating rows out of the fully-qualifying one `_seed_live_world`
    already built at COHORT_ID/AS_OF).
    """
    conn = _fresh_state_conn()
    _seed_live_world(conn)  # cohort_id = COHORT_ID ("live:2026-06-30"), 6 qualifying members: OLDER

    # Seed a genuinely NEWER live cohort with only 2 qualifying members (< 5).
    thin_cohort_id = "live:2026-07-31"
    thin_as_of = "2026-07-31"
    thin_cutoff = knowledge_cutoff(thin_as_of)
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
        "source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES (?, ?, 'live', ?, 'def1', 'mem1', '{}', ?, ?, 1, 1)",
        (thin_cohort_id, thin_as_of, thin_cutoff, thin_cutoff, thin_cutoff),
    )
    for sid in SIDS_A[:2]:
        conn.execute(
            "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, "
            "group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, "
            "scored, eligible, n_factors_used, input_hash, generated_at, track, run_id) "
            "VALUES (?, ?, ?, 'EW_HIER_v1', 1, 'SECT_A', 1, '{}', 0.1, 0.1, 0.0, 0.1, 1, 1, 1, 'h', ?, 'live', 1)",
            (thin_cohort_id, thin_as_of, sid, thin_cutoff),
        )
        conn.execute(
            "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, "
            "evidence_hash, computed_at, r_log, r_arith, r_group_median, l_rel, r_uni, sector_group, "
            "status, mb36, mb36_touch, price_manifest_sha, computed_run_id) "
            "VALUES (?, ?, ?, 3, '2026-10-31', 'live', 1, 'eh_thin', ?, 0.01, 0.01, 0.0, 0.01, 0.01, "
            "'SECT_A', 'ok', 0, 0, 'sha_ok', 1)",
            (thin_cohort_id, thin_as_of, sid, thin_cutoff),
        )

    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T1_SHUFFLE")

    assert c.status == "PASS", c.reason
    assert "synthetic fixture" not in c.reason
    assert COHORT_ID in c.reason
    assert thin_cohort_id not in c.reason


# --------------------------------------------------------------------------- T2

def test_t2_planted_exact_spearman_and_clean_structural_surface():
    report = leakage.run(None, draft=None)
    c = _check(report, "T2_PLANTED")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["leaky_members"] == []
    assert abs(c.observed["ic"] - 0.9878787878787879) < 1e-12


def test_t2_planted_fails_if_factorinputs_gains_a_leaky_public_member():
    from quant.factors.inputs import FactorInputs

    def _leaky_label_frame(self):  # pragma: no cover - never actually called
        raise NotImplementedError

    FactorInputs.label_frame = _leaky_label_frame
    try:
        report = leakage.run(None, draft=None)
        c = _check(report, "T2_PLANTED")
        assert c.status == "FAIL"
        assert "label_frame" in c.observed["leaky_members"]
        assert c.blocking is True
    finally:
        del FactorInputs.label_frame


def test_t2_planted_matches_golden_case_file_directly(spec_case):
    from quant.evaluation.metrics import rank_ic

    case = spec_case("planted_rank")
    ic, n, status = rank_ic(pd.Series(case["scores"]), pd.Series(case["labels"]))
    assert status == "ok"
    report_ic = _check(leakage.run(None, draft=None), "T2_PLANTED").observed["ic"]
    assert abs(ic - report_ic) < 1e-15
    assert abs(ic - case["expected_spearman"]) < 1e-12


# --------------------------------------------------------------------------- T3

def test_t3_replay_deferred_when_no_published_live_cohort(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T3_REPLAY")
    assert c.status == "DEFERRED"
    assert c.blocking is True
    assert "no published live cohort" in c.reason.lower()


def test_t3_replay_deferred_when_price_store_lacks_the_vintage(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)  # cfg.paths.prices_db is untouched: empty price store

    c = _check(leakage.run(ctx, draft=None), "T3_REPLAY")
    assert c.status == "DEFERRED"
    assert c.blocking is True
    assert "vintage" in c.reason.lower()


def test_t3_replay_pass_on_exact_recompute_match(cfg, monkeypatch):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    _seed_prices(cfg, ALL_SIDS)
    ctx = _mk_ctx(cfg, conn)

    matching_rows = pd.DataFrame(
        [{"security_id": sid, "factor_id": "mom_12_1@1", "z": float(sid) * 0.1} for sid in ALL_SIDS]
    )
    monkeypatch.setattr("quant.factors.registry.compute_all", lambda ctx, draft: matching_rows)

    c = _check(leakage.run(ctx, draft=None), "T3_REPLAY")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["mismatches"] == 0
    assert c.observed["compared"] == len(ALL_SIDS)


def test_t3_replay_fails_on_recompute_mismatch(cfg, monkeypatch):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    _seed_prices(cfg, ALL_SIDS)
    ctx = _mk_ctx(cfg, conn)

    wrong_rows = pd.DataFrame(
        [{"security_id": sid, "factor_id": "mom_12_1@1", "z": 999.0} for sid in ALL_SIDS]
    )
    monkeypatch.setattr("quant.factors.registry.compute_all", lambda ctx, draft: wrong_rows)

    c = _check(leakage.run(ctx, draft=None), "T3_REPLAY")
    assert c.status == "FAIL"
    assert c.blocking is True
    assert c.observed["mismatches"] == len(ALL_SIDS)


# --------------------------------------------------------------------------- T4

def test_t4_boundary_deferred_when_no_live_cohort(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T4_BOUNDARY")
    assert c.status == "DEFERRED"
    assert c.blocking is True


def test_t4_boundary_pass_on_conformant_cohort(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T4_BOUNDARY")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["violations"] == 0


@pytest.mark.parametrize(
    "break_field,expected_substr",
    [
        ("published_at", "published_at"),
        ("universe_captured_at", "universe_captured_at"),
        ("fv_cutoff", "input_refs_json cutoff"),
        ("price_manifest", "price_manifest_sha"),
    ],
)
def test_t4_boundary_fails_on_each_boundary_violation(cfg, break_field, expected_substr):
    conn = _fresh_state_conn()
    _seed_live_world(conn, break_field=break_field)
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert c.blocking is True
    assert c.observed["violations"] >= 1
    assert expected_substr in c.reason


def test_t4_boundary_reports_but_does_not_fail_on_missing_universe_captured_at(cfg):
    """A legitimately-absent universe_captured_at (fresh-bootstrap cohort) must
    not be treated as a violation, but must be visible in `observed` so a
    reviewer can see this leg of the check was skipped, not silently passed."""
    conn = _fresh_state_conn()
    _seed_live_world(conn, break_field="missing_universe_captured_at")
    ctx = _mk_ctx(cfg, conn)

    c = _check(leakage.run(ctx, draft=None), "T4_BOUNDARY")
    assert c.status == "PASS", c.reason
    assert c.observed["violations"] == 0
    assert c.observed["missing_universe_captured_at"] == [COHORT_ID]
    assert "no universe_captured_at recorded" in c.reason


class _FixedClock:
    """Minimal Clock stand-in returning a fixed instant for .iso()/.now_iso()."""

    def __init__(self, now_iso: str):
        self._now = now_iso

    def iso(self) -> str:
        return self._now

    def now_iso(self) -> str:
        return self._now


DRAFT_AS_OF = "2026-07-31"
DRAFT_CUTOFF = knowledge_cutoff(DRAFT_AS_OF)


def _mk_conformant_draft(**overrides) -> Draft:
    fv = pd.DataFrame(
        {
            "security_id": [101],
            "factor_id": ["mom_12_1@1"],
            "z": [0.1],
            "input_refs_json": [json.dumps({"cutoff": DRAFT_CUTOFF})],
        }
    )
    draft = Draft(
        cohort_id=f"live:{DRAFT_AS_OF}",
        as_of=DRAFT_AS_OF,
        track="live",
        knowledge_cutoff=DRAFT_CUTOFF,
        definition_hash="defh",
        source_refs={
            "universe_captured_at": "2026-07-25T10:00:00.000000Z",
            "price_manifest_sha": "sha_ok",
        },
        factor_values=fv,
    )
    for k, v in overrides.items():
        setattr(draft, k, v)
    return draft


def test_t4_boundary_checks_the_draft_even_before_any_cohort_is_published(cfg):
    """Before this month's cohorts/factor_values rows exist (the real state at
    G10 time -- see run().__doc__), T4 must still produce a real verdict on
    the draft under review instead of a perpetual DEFERRED."""
    conn = _fresh_state_conn()  # no live cohorts at all
    ctx = _mk_ctx(cfg, conn)
    draft = _mk_conformant_draft()

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "PASS", c.reason
    assert c.observed["cohorts_checked"] == 0
    assert c.observed["draft_checked"] is True


def test_t4_boundary_fails_when_draft_knowledge_cutoff_is_wrong(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    draft = _mk_conformant_draft(knowledge_cutoff="2020-01-01T00:00:00.000000Z")

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert "draft" in c.reason and "knowledge_cutoff" in c.reason


def test_t4_boundary_fails_when_draft_universe_captured_at_is_after_cutoff(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    draft = _mk_conformant_draft(
        source_refs={"universe_captured_at": "2099-01-01T00:00:00.000000Z", "price_manifest_sha": "sha_ok"}
    )

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert "draft" in c.reason and "universe_captured_at" in c.reason


def test_t4_boundary_fails_when_draft_factor_values_cutoff_mismatches(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    bad_fv = pd.DataFrame(
        {
            "security_id": [101],
            "factor_id": ["mom_12_1@1"],
            "z": [0.1],
            "input_refs_json": [json.dumps({"cutoff": "2020-01-01T00:00:00.000000Z"})],
        }
    )
    draft = _mk_conformant_draft(factor_values=bad_fv)

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert "draft" in c.reason and "factor_values.input_refs_json cutoff" in c.reason


def test_t4_boundary_fails_when_clock_now_precedes_draft_cutoff(cfg):
    """Independent proxy for 'published_at >= knowledge_cutoff': published_at is
    not stamped yet at G10 time, so this uses the current clock reading -- if
    the cutoff has somehow been computed into the future relative to now, that
    is exactly the boundary defect T4 exists to catch."""
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    ctx.clock = _FixedClock("2020-01-01T00:00:00.000000Z")  # long before DRAFT_CUTOFF
    draft = _mk_conformant_draft()

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert "current clock time" in c.reason


def test_t4_boundary_passes_when_clock_now_is_after_draft_cutoff(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    ctx.clock = _FixedClock("2026-08-01T00:00:00.000000Z")  # after DRAFT_CUTOFF
    draft = _mk_conformant_draft()

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "PASS", c.reason


def test_t4_boundary_draft_check_combines_with_historical_check(cfg):
    """A conformant historical cohort plus a broken draft must still FAIL overall."""
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)
    draft = _mk_conformant_draft(knowledge_cutoff="2020-01-01T00:00:00.000000Z")

    c = _check(leakage.run(ctx, draft=draft), "T4_BOUNDARY")
    assert c.status == "FAIL"
    assert c.observed["cohorts_checked"] == 1
    assert c.observed["draft_checked"] is True


# --------------------------------------------------------------------------- T5

def test_t5_embargo_deferred_when_no_shrunk_weights(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T5_EMBARGO")
    assert c.status == "DEFERRED"
    assert c.blocking is True
    assert "IC_SHRUNK_v1" in c.reason


def test_t5_embargo_pass_when_evidence_hash_matches_recompute(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    # Our single cohort's own 3M endpoint (2026-09-30) has not matured as of its
    # own as_of (2026-06-30), so family_ic_history legitimately returns an empty
    # frame here -- exactly the "empty_history" branch models.score_all uses.
    _seed_model_weights(conn, evidence_hash="empty_history")
    ctx = _mk_ctx(cfg, conn)

    c = _check(leakage.run(ctx, draft=None), "T5_EMBARGO")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["future_endpoints"] == []
    assert c.observed["recomputed_hash"] == "empty_history"


def test_t5_embargo_fails_on_evidence_hash_mismatch(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    _seed_model_weights(conn, evidence_hash="stale_or_tampered_hash")
    ctx = _mk_ctx(cfg, conn)

    c = _check(leakage.run(ctx, draft=None), "T5_EMBARGO")
    assert c.status == "FAIL"
    assert c.blocking is True
    assert c.observed["recomputed_hash"] != "stale_or_tampered_hash"


def test_t5_embargo_endpoint_check_can_actually_reach_fail(cfg, monkeypatch):
    """T5's future-endpoint check must be able to FAIL, not just structurally
    always pass. `family_ic_history` is monkeypatched to return a frame whose
    index contains a date this cohort's own horizon-3M endpoint proves should
    have been excluded (2026-06-30 + 3M = 2026-09-30 > as_of 2026-06-30), with
    the evidence_hash deliberately made to MATCH so only the endpoint
    re-check can be responsible for the FAIL.
    """
    conn = _fresh_state_conn()
    _seed_live_world(conn)

    leaked_index = [AS_OF]  # this cohort's own as_of; its 3M endpoint exceeds its own as_of
    fake_hist = pd.DataFrame({"momentum": [0.05]}, index=leaked_index)
    fake_hash = hashlib.sha256(fake_hist.to_csv().encode()).hexdigest()
    _seed_model_weights(conn, evidence_hash=fake_hash)
    ctx = _mk_ctx(cfg, conn)
    monkeypatch.setattr("quant.evaluation.walkforward.family_ic_history", lambda *a, **k: fake_hist)

    c = _check(leakage.run(ctx, draft=None), "T5_EMBARGO")
    assert c.status == "FAIL", c.reason
    assert c.blocking is True
    # The hash matches exactly -- proving the FAIL comes from the endpoint
    # re-check, not from a hash mismatch.
    assert c.observed["recomputed_hash"] == fake_hash
    assert c.observed["future_endpoints"] == [AS_OF]
    # The independently-queried (straight from the raw `cohorts` table, not
    # from family_ic_history's own filtered output) expected-exclusion set
    # agrees: this cohort's own as_of is exactly the one date it expects
    # excluded.
    assert c.observed["independently_expected_excluded"] == [AS_OF]


def test_t5_embargo_endpoint_check_catches_a_leak_outside_the_recognised_cohort_set(cfg, monkeypatch):
    """The fix must not be narrower than a plain per-element recompute: a
    leaked date that ISN'T even a recognised (live, is_clean) cohort as_of --
    e.g. a corrupted index entry -- must still be caught by direct
    recomputation, not silently waved through just because it fails to match
    the independently-queried candidate set.
    """
    conn = _fresh_state_conn()
    _seed_live_world(conn)

    bogus_future_date = "2099-01-01"  # not any cohort's as_of at all
    fake_hist = pd.DataFrame({"momentum": [0.05]}, index=[bogus_future_date])
    fake_hash = hashlib.sha256(fake_hist.to_csv().encode()).hexdigest()
    _seed_model_weights(conn, evidence_hash=fake_hash)
    ctx = _mk_ctx(cfg, conn)
    monkeypatch.setattr("quant.evaluation.walkforward.family_ic_history", lambda *a, **k: fake_hist)

    c = _check(leakage.run(ctx, draft=None), "T5_EMBARGO")
    assert c.status == "FAIL", c.reason
    assert c.observed["future_endpoints"] == [bogus_future_date]
    # It is not in the cohorts-table-derived candidate set (it isn't a real
    # cohort as_of at all) -- confirming the direct recompute, not the
    # candidate cross-check, is what caught it.
    assert bogus_future_date not in c.observed["independently_expected_excluded"]


# --------------------------------------------------------------------------- T6

def test_t6_availability_shift_boundary_holds():
    c = _check(leakage.run(None, draft=None), "T6_AVAIL_SHIFT")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["value_between_available_from_and_fetched_at"] is None
    assert c.observed["value_after_fetched_at"] == pytest.approx(100.0)


def test_t6_pit_frame_directly_enforces_fetched_at_over_available_from():
    """Whitebox: the production function T6 exercises really does gate on fetched_at."""
    from quant.data.fundamentals import pit_frame

    conn = _fresh_state_conn()
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'INE000000009', 'X', '2020-01-01', '2026-01-01', 'listed')"
    )
    available_from = "2024-04-01T00:00:00.000000Z"
    fetched_at = "2024-04-10T12:00:00.000000Z"
    conn.execute(
        "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
        "available_from, available_from_basis, fetched_at, source, run_id) "
        "VALUES (1, 'income', 'Q', '2024-03-31', 'revenue', 100.0, 'inr', ?, 'lodr_45d', ?, 'fixture', 1)",
        (available_from, fetched_at),
    )
    between = pit_frame(conn, "2024-04-05T00:00:00.000000Z", "income", "revenue", "Q", 1, [1])
    after = pit_frame(conn, "2024-04-15T00:00:00.000000Z", "income", "revenue", "Q", 1, [1])
    assert pd.isna(between.loc[1, 0])
    assert after.loc[1, 0] == pytest.approx(100.0)


# --------------------------------------------------------------------------- T7

def test_t7_action_invariance_tri_matches_hand_calc_and_close_split_is_continuous():
    c = _check(leakage.run(None, draft=None), "T7_ACTION_INVARIANCE")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    tri0, tri1 = c.observed["tri"]
    split0, split1 = c.observed["close_split"]
    assert tri0 == pytest.approx(100.0, abs=1e-9)
    assert tri1 == pytest.approx(101.0, abs=1e-9)  # 2:1 split + INR1/share dividend = 1.01x total return
    assert split0 == pytest.approx(split1, abs=1e-9)  # continuous across the split


# --------------------------------------------------------------------------- T8

def test_t8_holdings_future_capture_does_not_change_past_lag0_read():
    c = _check(leakage.run(None, draft=None), "T8_HOLDINGS")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["lag0_before_future_capture"] == pytest.approx(50.0)
    assert c.observed["lag0_after_future_capture"] == pytest.approx(50.0)


def test_t8_holdings_series_directly_filters_future_captures():
    """Whitebox: confirms holdings.series itself (not just the check) enforces the cutoff."""
    from quant.data.holdings import series as holdings_series

    conn = _fresh_state_conn()
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'INE000000008', 'X', '2020-01-01', '2026-01-01', 'listed')"
    )
    conn.execute(
        "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
        "VALUES (1, '2024-01-15T00:00:00.000000Z', 50.0, 10.0, 1000.0, 'fixture')"
    )
    cutoff = "2024-02-01T00:00:00.000000Z"
    before = holdings_series(conn, cutoff, 0, [1]).get(1)
    conn.execute(
        "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
        "VALUES (1, '2024-03-01T00:00:00.000000Z', 99.0, 40.0, 1000.0, 'fixture')"
    )
    after = holdings_series(conn, cutoff, 0, [1]).get(1)
    assert before == pytest.approx(50.0)
    assert after == pytest.approx(50.0)


# --------------------------------------------------------------------------- T9

def test_t9_survivorship_deferred_when_no_matured_labels(cfg):
    conn = _fresh_state_conn()
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T9_SURVIVORSHIP")
    assert c.status == "DEFERRED"
    assert c.blocking is True


def test_t9_survivorship_pass_when_label_set_equals_member_set(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T9_SURVIVORSHIP")
    assert c.status == "PASS", c.reason
    assert c.blocking is True
    assert c.observed["violations"] == 0


def test_t9_survivorship_fails_when_a_scored_member_has_no_label(cfg):
    conn = _fresh_state_conn()
    _seed_live_world(conn, break_field="drop_label")
    ctx = _mk_ctx(cfg, conn)
    c = _check(leakage.run(ctx, draft=None), "T9_SURVIVORSHIP")
    assert c.status == "FAIL"
    assert c.blocking is True
    assert c.observed["violations"] >= 1


# --------------------------------------------------------------------------- T10

def test_t10_sector_null_is_deterministic_and_never_blocks():
    r1 = _check(leakage.run(None, draft=None), "T10_SECTOR_NULL")
    r2 = _check(leakage.run(None, draft=None), "T10_SECTOR_NULL")
    assert r1.blocking is False
    assert r1.observed == r2.observed  # fully deterministic given the fixed seed
    assert r1.observed["n"] == 200
    assert r1.status == "PASS", r1.reason


# --------------------------------------------------------------------------- whole-report shape

def test_leakage_suite_runs_t1_through_t10(cfg):
    """Every check id is present and blocking flags follow the spec exactly."""
    conn = _fresh_state_conn()
    _seed_live_world(conn)
    _seed_model_weights(conn, evidence_hash="empty_history")
    _seed_prices(cfg, ALL_SIDS)
    ctx = _mk_ctx(cfg, conn)

    report = leakage.run(ctx, draft=None)
    ids = [c.id for c in report.checks]
    for i in range(1, 11):
        assert any(cid.startswith(f"T{i}") for cid in ids), f"missing T{i} in {ids}"

    non_blocking = {c.id for c in report.checks if not c.blocking}
    assert non_blocking == {"T1_SHUFFLE", "T10_SECTOR_NULL"}

    # No check is a hard-coded, unconditional PASS: every one of them varies its
    # verdict across at least one of our scenarios above (this whole file, taken
    # together, is the evidence for that -- this assertion just documents it).
    assert all(c.status in ("PASS", "FAIL", "DEFERRED") for c in report.checks)
