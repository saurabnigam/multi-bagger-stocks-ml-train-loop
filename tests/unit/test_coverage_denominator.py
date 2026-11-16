"""Price-factor coverage denominator (MASTER_SPEC R16): members listed long enough.

The market's session list is the sorted set of dates on which any member has a close up to as_of at
the cohort vintage. A member is seasoned for a price factor when its FIRST bar is on or before the
session ``lookback_days`` sessions before as_of (mom_12_1 could have 253 bars). Holes after the first
bar do not make a member unseasoned: its missing value counts as missing. Unseasoned members are
outside the denominator and reported, unless they are more than UNSEASONED_MAX_SHARE (10%) of the
applicable members: then the strict denominator is used and G8 says so. Offline, on tmp_path databases.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data import gates
from quant.data.price_admission import AdmittingPriceStore
from quant.data.prices import PriceStore
from quant.db import lock as dblock
from quant.db.core import apply_schema, connect
from quant.run import RunContext, knowledge_cutoff
from quant.types import Actor, Draft, FrozenClock

AS_OF = "2026-09-30"
CUT = knowledge_cutoff(AS_OF)
LONG = "mom_12_1@1"      # lookback 252
SHORT = "rev_1m@1"       # lookback 21
SESS = list(pd.bdate_range(end=AS_OF, periods=300).strftime("%Y-%m-%d"))


_TEMPLATE = {}


@pytest.fixture(autouse=True)
def _release_leaked_locks():
    """These tests enter a RunContext without exiting it; free its write lock so later tests start clean."""
    yield
    for key, entry in list(dblock._registry.items()):
        try:
            os.close(entry.fd)
        except OSError:
            pass
        dblock._registry.pop(key, None)


def _template():
    """State and price databases built once per module; each test copies them (schema build is ~0.8 s)."""
    if not _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="cov_tpl_"))
        conn = connect(d / "state.db")
        apply_schema(conn, kind="state")
        PriceStore(d / "p.db", state_conn=conn)
        conn.close()
        _TEMPLATE["dir"] = d
    return _TEMPLATE["dir"]


def _ctx(tmp_path, store_cls=PriceStore, with_store=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "state.db"
    shutil.copy(_template() / "state.db", db)
    shutil.copy(_template() / "p.db", tmp_path / "p.db")
    cfg = load().with_paths(db=db, prices_db=tmp_path / "p.db")
    ctx = RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg,
                     clock=FrozenClock("2026-10-01T12:00:00.000000Z"), actor=Actor(kind="system", name="t"))
    ctx.__enter__()
    ctx.store = store_cls(cfg.paths.prices_db, state_conn=ctx.conn) if with_store else None
    return ctx


def _bars(store, counts, observed="2026-09-30T10:00:00.000000Z"):
    """counts: {security_id: number of most recent sessions with a bar}; one write for all members."""
    recs = [(sid, d, 100.0, 100.0, 100.0, 100.0, 1e6, 0.0, 1.0, 100.0, 100.0, observed, "cap", "sha")
            for sid, n in counts.items() if n for d in SESS[-n:]]
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            "dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)


def _bars_on(store, dates_by_sid, observed="2026-09-30T10:00:00.000000Z"):
    """dates_by_sid: {security_id: [explicit session dates]}."""
    recs = [(sid, d, 100.0, 100.0, 100.0, 100.0, 1e6, 0.0, 1.0, 100.0, 100.0, observed, "cap", "sha")
            for sid, ds in dates_by_sid.items() for d in ds]
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            "dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)


def _draft(n, factor_ids, missing=(), group="A"):
    rows = []
    for sid in range(n):
        for fid in factor_ids:
            rows.append({"security_id": sid, "factor_id": fid, "z": np.nan if sid in missing else 1.0,
                         "sector_group": group})
    return Draft(cohort_id="live:2026-09-30", as_of=AS_OF, track="live", knowledge_cutoff=CUT, definition_hash="d",
                 members=pd.DataFrame({"security_id": range(n)}), groups=pd.Series({i: group for i in range(n)}),
                 source_refs={"has_prior_cohort": False}, factor_values=pd.DataFrame(rows))


def _row(cov, fid):
    return cov[cov["factor_id"] == fid].iloc[0]


def _world(ctx, n, bars):
    _bars(ctx.store, {sid: bars(sid) for sid in range(n)})


def test_a_young_listings_leave_the_denominator(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 40 if s < 6 else 300)
    draft = _draft(100, [LONG], missing=range(6))       # the 6 young members have no value
    cov = gates.factor_coverage(ctx, draft)
    r = _row(cov, LONG)
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (94, 6, 94)
    assert r["coverage"] == 1.0 and not r["excluded"]
    assert LONG not in gates.excluded_factors(ctx, draft)
    ctx.status = "ok"


def test_b_seasoned_missing_values_still_count(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 300)
    cov = gates.factor_coverage(ctx, _draft(100, [LONG], missing=range(8)))
    r = _row(cov, LONG)
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (100, 0, 92)
    assert abs(r["coverage"] - 0.92) < 1e-12 and r["excluded"]
    ctx.status = "ok"


def test_c_zero_bars_is_unseasoned_not_missing(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 0 if s < 3 else 300)
    r = _row(gates.factor_coverage(ctx, _draft(100, [LONG], missing=range(3))), LONG)
    assert (r["applicable"], r["unseasoned"]) == (97, 3) and r["coverage"] == 1.0 and not r["excluded"]
    ctx.status = "ok"


def test_d_short_lookback_keeps_young_members(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 40 if s < 6 else 300)    # 40 >= 21 bars: seasoned for rev_1m, not for mom_12_1
    cov = gates.factor_coverage(ctx, _draft(100, [LONG, SHORT], missing=range(6)))
    short, long_ = _row(cov, SHORT), _row(cov, LONG)
    assert (short["applicable"], short["unseasoned"], short["finite"]) == (100, 0, 94)
    assert abs(short["coverage"] - 0.94) < 1e-12 and short["excluded"]     # a real gap for the short window
    assert (long_["applicable"], long_["unseasoned"]) == (94, 6) and not long_["excluded"]
    ctx.status = "ok"


def test_d2_boundary_lookback_plus_one_bars(tmp_path):
    """lookback_days 21 needs 22 bars (first bar on or before the session 21 sessions before as_of)."""
    ctx = _ctx(tmp_path / "s21")
    _world(ctx, 100, lambda s: 21 if s < 5 else 300)
    r = _row(gates.factor_coverage(ctx, _draft(100, [SHORT])), SHORT)
    assert (r["applicable"], r["unseasoned"]) == (95, 5)
    ctx2 = _ctx(tmp_path / "s22")
    _world(ctx2, 100, lambda s: 22 if s < 5 else 300)
    r2 = _row(gates.factor_coverage(ctx2, _draft(100, [SHORT])), SHORT)
    assert (r2["applicable"], r2["unseasoned"]) == (100, 0)
    ctx.status = "ok"
    ctx2.status = "ok"


def test_d3_boundary_252_vs_253_for_mom_12_1(tmp_path):
    ctx = _ctx(tmp_path / "b252")
    _world(ctx, 100, lambda s: 252 if s < 5 else 300)
    r = _row(gates.factor_coverage(ctx, _draft(100, [LONG])), LONG)
    assert (r["applicable"], r["unseasoned"]) == (95, 5)             # 252 bars is one short of 253
    ctx2 = _ctx(tmp_path / "b253")
    _world(ctx2, 100, lambda s: 253 if s < 5 else 300)
    r2 = _row(gates.factor_coverage(ctx2, _draft(100, [LONG])), LONG)
    assert (r2["applicable"], r2["unseasoned"]) == (100, 0)
    ctx.status = "ok"
    ctx2.status = "ok"


def test_d4_holes_after_the_first_bar_do_not_make_a_member_unseasoned(tmp_path):
    ctx = _ctx(tmp_path)
    dates = {sid: SESS[-300:] for sid in range(6, 100)}
    for sid in range(6):                                     # listed 300 sessions ago, but only 30 bars survive
        dates[sid] = [SESS[-300]] + SESS[-29:]
    _bars_on(ctx.store, dates)
    r = _row(gates.factor_coverage(ctx, _draft(100, [LONG], missing=range(6))), LONG)
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (100, 0, 94)
    assert abs(r["coverage"] - 0.94) < 1e-12 and r["excluded"]       # the holes count as missing values
    ctx.status = "ok"


def test_d5_session_list_is_the_union_over_members(tmp_path):
    """A session only some members traded still counts as a session."""
    ctx = _ctx(tmp_path)
    dates = {sid: SESS[-300:] for sid in range(100)}
    dates[0] = [d for d in SESS[-300:] if d != SESS[-253]]            # one member has a hole exactly on the threshold
    _bars_on(ctx.store, dates)
    r = _row(gates.factor_coverage(ctx, _draft(100, [LONG])), LONG)
    assert (r["applicable"], r["unseasoned"]) == (100, 0)            # first bar SESS[-300] <= SESS[-253]
    ctx.status = "ok"


def test_e_non_price_and_no_store_unchanged(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 40 if s < 6 else 300)
    cov = gates.factor_coverage(ctx, _draft(100, ["roce@1"], missing=range(6)))     # non-price: 94% of 100 vs 70%
    r = _row(cov, "roce@1")
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (100, 0, 94) and not r["excluded"]
    bare = _ctx(tmp_path / "bare", with_store=False)
    r2 = _row(gates.factor_coverage(bare, _draft(100, [LONG], missing=range(6))), LONG)
    assert (r2["applicable"], r2["unseasoned"], r2["finite"]) == (100, 0, 94)
    assert abs(r2["coverage"] - 0.94) < 1e-12 and r2["excluded"]
    ctx.status = "ok"
    bare.status = "ok"


def test_f_all_new_universe_uses_the_strict_denominator_and_is_excluded(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 20, lambda s: 30)                       # all-new universe: far above the 10% guard
    r = _row(gates.factor_coverage(ctx, _draft(20, [LONG], missing=range(20))), LONG)
    assert (r["applicable"], r["unseasoned"], r["finite"], r["coverage"]) == (20, 20, 0, 0.0) and r["excluded"]
    assert bool(r["strict_fallback"]) is True
    ctx.status = "ok"


def test_f2_lookback_missing_on_spec_means_everyone_seasoned(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _world(ctx, 10, lambda s: 5)
    monkeypatch.setattr(gates, "_factor_lookback", lambda fid: 0)
    r = _row(gates.factor_coverage(ctx, _draft(10, [LONG])), LONG)
    assert (r["applicable"], r["unseasoned"]) == (10, 0) and not r["excluded"]
    ctx.status = "ok"


def test_g_g8_reason_carries_unseasoned_count(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 40 if s < 6 else 300)
    draft = _draft(100, [LONG], missing=range(6))
    report = gates.run(ctx, draft, phase="post", strict=False)
    g8 = next(c for c in report.checks if c.id == "G8")
    assert "price-factor coverage counts members with enough history" in g8.reason and "6 unseasoned" in g8.reason
    assert g8.status == "PASS" and draft.source_refs["excluded_factors"] == []
    ctx.status = "ok"


def test_g2_reason_has_no_clause_without_unseasoned(tmp_path):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 300)
    report = gates.run(ctx, _draft(100, [LONG]), phase="post", strict=False)
    assert "unseasoned" not in next(c for c in report.checks if c.id == "G8").reason
    ctx.status = "ok"


def test_h_pinned_vintage_bars_count_toward_seasoning(tmp_path):
    ctx = _ctx(tmp_path, AdmittingPriceStore)
    late = "2026-10-01T06:00:00.000000Z"                # recorded after the cutoff
    _bars(ctx.store, {sid: 252 for sid in range(100)})                     # on time: 252 bars each
    _bars_on(ctx.store, {sid: [SESS[-253]] for sid in range(95)}, observed=late)    # 95 get a 253rd bar late
    draft = _draft(100, [LONG])
    before = _row(gates.factor_coverage(ctx, draft), LONG)
    assert before["unseasoned"] == 100                  # unpinned: the cutoff sees 252 sessions, nobody has 253
    ctx.store.pin(CUT, late)
    draft2 = _draft(100, [LONG])                        # fresh draft: the listing info is cached per draft
    after = _row(gates.factor_coverage(ctx, draft2), LONG)
    assert (after["applicable"], after["unseasoned"]) == (95, 5)
    ctx.status = "ok"


def _guard_case(tmp_path, n_young):
    ctx = _ctx(tmp_path)
    _world(ctx, 100, lambda s: 40 if s < n_young else 300)
    draft = _draft(100, [LONG], missing=range(n_young))
    cov = gates.factor_coverage(ctx, draft)
    report = gates.run(ctx, draft, phase="post", strict=False)
    g8 = next(c for c in report.checks if c.id == "G8")
    ctx.status = "ok"
    return _row(cov, LONG), g8


def test_i_guard_exactly_ten_percent_still_uses_the_seasoned_denominator(tmp_path):
    r, g8 = _guard_case(tmp_path, 10)
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (90, 10, 90) and not r["excluded"]
    assert bool(r["strict_fallback"]) is False
    assert "strict denominator used" not in g8.reason


def test_i_guard_above_ten_percent_falls_back_to_the_strict_denominator(tmp_path):
    r, g8 = _guard_case(tmp_path, 11)
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (100, 11, 89) and r["excluded"]
    assert bool(r["strict_fallback"]) is True
    assert f"{LONG}: unseasoned share 11.0% exceeds 10%: strict denominator used" in g8.reason
    assert "price-factor coverage counts members with enough history" not in g8.reason


def test_i_guard_constant_and_no_config_key():
    assert gates.UNSEASONED_MAX_SHARE == 0.10
    from quant.config import load as _load
    assert "unseasoned" not in str(_load().to_dict()).lower()


def test_j_unknown_price_factor_gets_lookback_zero_and_is_named_in_g8(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.conn.execute(
        "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, "
        "formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, "
        "module_path, status, registered_on, status_changed_on) VALUES ('zz_custom@1', 'zz_custom', 1, 'momentum', "
        "1, 3, 'stock', 'h', 'f', '[\"tri\"]', 300, 1, 1, 0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01')")
    _world(ctx, 100, lambda s: 40 if s < 6 else 300)
    draft = _draft(100, ["zz_custom@1", LONG], missing=range(6))
    cov = gates.factor_coverage(ctx, draft)
    r = _row(cov, "zz_custom@1")
    assert (r["applicable"], r["unseasoned"], r["finite"]) == (100, 0, 94)      # lookback 0: everyone seasoned
    assert bool(r["lookback_unknown"]) is True and not bool(_row(cov, LONG)["lookback_unknown"])
    g8 = next(c for c in gates.run(ctx, draft, phase="post", strict=False).checks if c.id == "G8")
    assert g8.reason.count("lookback unknown") == 1 and "zz_custom@1" in g8.reason
    ctx.status = "ok"
