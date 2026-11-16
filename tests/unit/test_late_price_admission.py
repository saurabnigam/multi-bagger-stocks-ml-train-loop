"""Late price admission (MASTER_SPEC R13): a closing price is admissible by its trading date.

When the as_of closes are not all recorded by the knowledge cutoff, a cohort may be scored
late: its price bars (dated <= as_of) are read at a pinned price vintage, while everything
else keeps the cutoff. These tests cover the price store subclass (pins, corporate-action and
split handling), the admission decision, the enabling decision as a precondition, the monthly
run end to end on a synthetic world, replay of a late cohort, and the exclusion of late
cohorts from clean-live evidence. Everything is offline, on tmp_path databases.
"""
from __future__ import annotations

import copy
import json
import shutil
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.run import RunContext, knowledge_cutoff, monthly
from quant.types import Actor, FrozenClock

AS_OF = "2026-08-31"
CUT = knowledge_cutoff(AS_OF)                       # 2026-08-31T18:29:59.999999Z
LATE_OBS = "2026-09-01T06:00:00.000000Z"            # the vendor delivers the as_of close a day late
APPROVED_AT = "2026-09-01T12:00:00.000000Z"         # a human approval recorded after the cutoff
SCORE_AT = "2026-09-02T05:00:00.000000Z"            # when the late cohort is scored
HUMAN = Actor(kind="human", name="owner")


# ----------------------------------------------------------------------------- store-level world

def _paths(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    return conn, tmp_path / "prices.db"


def _put(store, rows):
    """rows: (security_id, date, close, observed_at[, split_ratio[, volume]])."""
    recs = []
    for r in rows:
        sid, date, close, obs = r[:4]
        split = r[4] if len(r) > 4 else 1.0
        vol = r[5] if len(r) > 5 else 1_000_000.0
        recs.append((sid, date, close, close, close, close, vol, 0.0, split, close, close, obs, "cap", "sha"))
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            "dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)


def _action(conn, sid, ex_date, kind, observed_at, *, decision_id=None, ratio=None, adj=None):
    conn.execute(
        "INSERT INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, source, "
        "observed_at, decision_id, note) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL)",
        (sid, ex_date, kind, ratio, adj, "inferred" if kind == "suspected" else "manual", observed_at, decision_id))


def _sessions(start, end):
    return list(pd.bdate_range(start, end).strftime("%Y-%m-%d"))


def _mini(tmp_path):
    """Three securities, bars to 2026-08-28 on time; the as_of bar (and a bar dated after as_of) arrive late.

    sid 1: vendor split on the late as_of bar; a suspect before the cutoff approved after it.
    sid 2: a 40% drop on the late as_of bar (a suspect evidenced only by a late bar); an approved split
           recorded after the cutoff.
    sid 3: an approved value transfer recorded after the cutoff, never suspected; an approved split before it.
    """
    conn, prices = _paths(tmp_path)
    for sid in (1, 2, 3):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2025-01-01', '2026-09-01', 'listed')", (sid, f"INE00000000{sid}", f"S{sid}"))
    store = PriceStore(prices, state_conn=conn)
    rng = np.random.default_rng(7)
    rows = []
    for sid in (1, 2, 3):
        px = 100.0
        for d in _sessions("2026-06-01", "2026-08-28"):
            px *= 1.0 + float(rng.normal(0.0, 0.004))
            rows.append((sid, d, round(px, 4), f"{d}T10:00:00.000000Z"))
    _put(store, rows)
    last = {s: [r for r in rows if r[0] == s][-1][2] for s in (1, 2, 3)}
    _put(store, [
        (1, AS_OF, last[1] / 2.0, LATE_OBS, 2.0),     # vendor split 2:1, close quoted on the new basis
        (2, AS_OF, last[2] * 0.6, LATE_OBS),          # 40% drop: beyond the jump ratio
        (3, AS_OF, last[3] * 1.001, LATE_OBS),
        (1, "2026-09-01", last[1] / 2.0, LATE_OBS),   # dated after as_of: never admissible for this cohort
        (2, "2026-09-01", last[2] * 0.6, LATE_OBS),
        (3, "2026-09-01", last[3], LATE_OBS),
    ])
    _action(conn, 1, "2026-07-15", "suspected", "2026-07-16T10:00:00.000000Z")
    _action(conn, 1, "2026-07-15", "manual_adj", APPROVED_AT, decision_id="D-X", adj=0.9)
    _action(conn, 2, AS_OF, "suspected", LATE_OBS)
    _action(conn, 2, "2026-07-01", "split", APPROVED_AT, decision_id="D-X", ratio=3.0)
    _action(conn, 3, "2026-08-20", "manual_adj", APPROVED_AT, decision_id="D-X", adj=0.95)
    _action(conn, 3, "2026-07-20", "split", "2026-07-21T10:00:00.000000Z", decision_id="D-X", ratio=2.0)
    return conn, prices


def _stores(conn, prices):
    from quant.data.price_admission import AdmittingPriceStore

    return PriceStore(prices, state_conn=conn), AdmittingPriceStore(prices, state_conn=conn)


# ----------------------------------------------------------------------------- 1. no pins: identical

def test_without_pins_the_subclass_equals_the_plain_store(tmp_path):
    conn, prices = _mini(tmp_path)
    plain, adm = _stores(conn, prices)
    ids = [1, 2, 3]
    for vintage in (CUT, "2026-09-03T00:00:00.000000Z"):
        for name in ("tri", "close_split", "close_raw", "volume"):
            a = getattr(plain, name)(ids, "2026-06-01", "2026-09-05", vintage)
            b = getattr(adm, name)(ids, "2026-06-01", "2026-09-05", vintage)
            pd.testing.assert_frame_equal(a, b)
        pd.testing.assert_frame_equal(plain.adv_inr(ids, "2026-09-05", vintage), adm.adv_inr(ids, "2026-09-05", vintage))
        assert plain.split_events(ids, "2026-06-01", "2026-09-05", vintage) == \
            adm.split_events(ids, "2026-06-01", "2026-09-05", vintage)
        assert plain.corporate_actions(ids, vintage) == adm.corporate_actions(ids, vintage)
        assert plain.manifest_hash(vintage) == adm.manifest_hash(vintage)
    assert adm.pins() == {}


# ----------------------------------------------------------------------------- pin bookkeeping

def test_pin_validation_idempotence_and_copy(tmp_path):
    from quant.data.price_admission import RULE_BARS_AND_SUSPECTS, RULE_BARS_ONLY

    conn, prices = _paths(tmp_path)
    _, adm = _stores(conn, prices)
    with pytest.raises(ValueError):
        adm.pin(CUT, CUT)                                   # price vintage must be after the cutoff
    with pytest.raises(ValueError):
        adm.pin(CUT, "2026-08-31T00:00:00.000000Z")
    with pytest.raises(ValueError):
        adm.pin(CUT, SCORE_AT, rule="bar_date/9")
    adm.pin(CUT, SCORE_AT)
    adm.pin(CUT, SCORE_AT)                                  # idempotent
    assert adm.pins()[CUT]["rule"] == RULE_BARS_AND_SUSPECTS
    with pytest.raises(ValueError):
        adm.pin(CUT, "2026-09-03T00:00:00.000000Z")         # a conflicting re-pin
    with pytest.raises(ValueError):
        adm.pin(CUT, SCORE_AT, rule=RULE_BARS_ONLY)
    snapshot = adm.pins()
    snapshot[CUT]["rule"] = "tampered"
    snapshot["other"] = {}
    assert adm.pins() == {CUT: {"price_vintage": SCORE_AT, "rule": RULE_BARS_AND_SUSPECTS}}


# ----------------------------------------------------------------------------- 2. pinned bars

def test_pinned_bars_follow_the_price_vintage_and_never_pass_as_of(tmp_path):
    from quant.data.price_admission import coverage

    conn, prices = _mini(tmp_path)
    plain, adm = _stores(conn, prices)
    ids = [1, 2, 3]
    assert plain.close_raw(ids, AS_OF, AS_OF, CUT).empty or plain.close_raw(ids, AS_OF, AS_OF, CUT).isna().all().all()
    adm.pin(CUT, SCORE_AT)
    admitted = adm.close_raw(ids, AS_OF, AS_OF, CUT)
    assert list(admitted.index) == [AS_OF] and admitted.notna().all().all()
    # the same bars a plain store reads at the price vintage, capped by the requested end date
    pd.testing.assert_frame_equal(admitted, plain.close_raw(ids, AS_OF, AS_OF, SCORE_AT))
    # a bar dated after as_of never appears, whatever end date the caller asks for
    window = adm.close_raw(ids, "2026-08-24", "2026-09-05", CUT)
    assert window.index.max() == AS_OF
    assert plain.close_raw(ids, "2026-08-24", "2026-09-05", SCORE_AT).index.max() == "2026-09-01"
    # adv_inr reads the admitted as_of bar too (the 63-day window slides by one session); another vintage is untouched
    pinned_adv = adm.adv_inr(ids, "2026-09-05", CUT)
    pd.testing.assert_frame_equal(pinned_adv, plain.adv_inr(ids, AS_OF, SCORE_AT))
    assert not pinned_adv.equals(plain.adv_inr(ids, AS_OF, CUT))
    other = "2026-08-30T00:00:00.000000Z"
    pd.testing.assert_frame_equal(adm.close_raw(ids, "2026-08-24", AS_OF, other), plain.close_raw(ids, "2026-08-24", AS_OF, other))
    # the manifest of the pinned cutoff covers the admitted bars and nothing dated after as_of
    assert adm.manifest_hash(CUT) != plain.manifest_hash(CUT)
    assert max(r[1] for r in adm._manifest_rows(CUT)) == AS_OF
    assert max(r[1] for r in plain._manifest_rows(SCORE_AT)) == "2026-09-01"
    # coverage ignores pins: it is the strict read
    assert coverage(adm, ids, AS_OF, CUT) == 0.0
    assert coverage(adm, ids, AS_OF, SCORE_AT) == 1.0
    assert coverage(adm, [], AS_OF, SCORE_AT) == 0.0
    assert coverage(adm, ids + [99], AS_OF, SCORE_AT) == pytest.approx(0.75)


# ----------------------------------------------------------------------------- 3. corporate actions

def test_corporate_actions_under_a_pin(tmp_path):
    from quant.data.price_admission import RULE_BARS_ONLY, RULE_BARS_AND_SUSPECTS

    conn, prices = _mini(tmp_path)
    plain, adm = _stores(conn, prices)
    ids = [1, 2, 3]
    strict = plain.corporate_actions(ids, CUT)
    assert strict == {1: [{"ex_date": "2026-07-15", "mode": "truncate"}],
                      3: [{"ex_date": "2026-07-20", "mode": "split", "mult": 2.0}]}

    adm.pin(CUT, SCORE_AT, rule=RULE_BARS_AND_SUSPECTS)
    pinned = adm.corporate_actions(ids, CUT)
    # sid 1: the approval recorded after the cutoff is ignored, the suspect still truncates
    assert pinned[1] == [{"ex_date": "2026-07-15", "mode": "truncate"}]
    # sid 2: a jump evidenced only by a late bar is an unresolved suspect; the split approved after the cutoff
    # is not applied (no "split" entry) but, unknown at the cutoff, is a suspect too
    assert pinned[2] == [{"ex_date": "2026-07-01", "mode": "truncate"}, {"ex_date": AS_OF, "mode": "truncate"}]
    # sid 3: an approval after the cutoff with no suspect before it is also treated as unresolved, ordered by ex_date
    assert pinned[3] == [{"ex_date": "2026-07-20", "mode": "split", "mult": 2.0},
                         {"ex_date": "2026-08-20", "mode": "truncate"}]
    # the plain store at the same vintage would have honoured the late approval and the late split
    late_view = plain.corporate_actions(ids, SCORE_AT)
    assert late_view[1] == [{"ex_date": "2026-07-15", "mode": "adjust", "mult": 0.9}]

    # tri follows: sid 2 restarts at the late ex-date, so earlier levels are unusable
    tri = adm.tri(ids, "2026-06-01", AS_OF, CUT)
    assert tri[2].iloc[:-1].isna().all() and tri[2].notna().iloc[-1]
    assert tri[1].iloc[:-1].isna().any()                     # sid 1 truncated at 2026-07-15
    assert adm.unresolved_actions(ids, "2026-06-01", AS_OF, CUT)[2] == ["2026-07-01", AS_OF]

    # rule bar_date/0: bars only, the strict action view unchanged
    adm0 = _stores(conn, prices)[1]
    adm0.pin(CUT, SCORE_AT, rule=RULE_BARS_ONLY)
    assert adm0.corporate_actions(ids, CUT) == strict
    tri0 = adm0.tri(ids, "2026-06-01", AS_OF, CUT)
    assert tri0[2].notna().all()


def test_split_events_under_a_pin_keep_approved_splits_strict(tmp_path):
    conn, prices = _mini(tmp_path)
    plain, adm = _stores(conn, prices)
    ids = [1, 2, 3]
    window = ("2026-06-01", "2026-09-05")
    strict = plain.split_events(ids, *window, CUT)
    assert strict == {3: [("2026-07-20", 2.0)]}
    adm.pin(CUT, SCORE_AT)
    pinned = adm.split_events(ids, *window, CUT)
    # the vendor split on the late as_of bar is admitted; the approved split recorded after the cutoff is not
    assert pinned == {1: [(AS_OF, 2.0)], 3: [("2026-07-20", 2.0)]}
    assert 2 not in pinned
    # the plain store at the price vintage shows what a naive implementation would have used
    assert plain.split_events(ids, *window, SCORE_AT)[2] == [("2026-07-01", 3.0)]


def test_late_truncate_is_only_for_ex_dates_up_to_the_as_of(tmp_path):
    conn, prices = _mini(tmp_path)
    _action(conn, 1, "2026-09-01", "manual_adj", APPROVED_AT, decision_id="D-X", adj=0.8)     # ex-date after as_of
    plain, adm = _stores(conn, prices)
    assert any(a["ex_date"] == "2026-09-01" for a in plain.corporate_actions([1], SCORE_AT)[1])   # the late view has it
    adm.pin(CUT, SCORE_AT)
    assert [a["ex_date"] for a in adm.corporate_actions([1, 2, 3], CUT)[1]] == ["2026-07-15"]
    assert [a["ex_date"] for a in adm.corporate_actions([1, 2, 3], CUT)[2]] == ["2026-07-01", AS_OF]    # == as_of stays


def test_split_events_under_a_pin_equal_the_plain_store_when_every_row_is_visible_at_the_cutoff(tmp_path):
    """Drift guard: the pinned split query mirrors the base query, so a change to the base must be mirrored."""
    conn, prices = _paths(tmp_path)
    for sid in (1, 2):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2025-01-01', '2026-09-01', 'listed')", (sid, f"INE00000000{sid}", f"S{sid}"))
    store = PriceStore(prices, state_conn=conn)
    rows = [(s, d, 100.0 + s, f"{d}T10:00:00.000000Z") for s in (1, 2) for d in _sessions("2026-06-01", AS_OF)]
    rows = [r if not (r[0] == 1 and r[1] == "2026-07-10") else (1, r[1], r[2], r[3], 2.0) for r in rows]   # vendor split
    _put(store, rows)
    _action(conn, 2, "2026-07-20", "split", "2026-07-21T10:00:00.000000Z", decision_id="D-X", ratio=3.0)   # approved split
    plain, adm = _stores(conn, prices)
    args = ([1, 2], "2026-06-01", "2026-09-05", CUT)
    expected = plain.split_events(*args)
    assert expected == {1: [("2026-07-10", 2.0)], 2: [("2026-07-20", 3.0)]}
    adm.pin(CUT, SCORE_AT)
    assert adm.split_events(*args) == expected


# ----------------------------------------------------------------------------- 4. decide()

def _decision(conn, *, status="approved", approver="human", subject="late_price_admission", decided_on="2026-08-30T00:00:00.000000Z",
              decision_id="D-2026-08-01", tier=2):
    conn.execute(
        "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
        "evidence_refs_json, decided_on, decided_by, approver_kind, status, effective_from, adr_path, git_sha) "
        "VALUES (?, 'rule_change', ?, ?, 't', 'c', '[]', 'd', '[]', ?, ?, ?, ?, '2026-08-30', 'x.md', 'sha')",
        (decision_id, tier, subject, decided_on, f"{approver}:someone", approver, status))
    return decision_id


def _decide_world(tmp_path, *, as_of_obs, now):
    """Five securities with a history; the as_of close of each is observed at ``as_of_obs``."""
    conn, prices = _paths(tmp_path)
    store = _stores(conn, prices)[1]
    rows = []
    for sid in range(1, 6):
        for d in _sessions("2026-07-01", "2026-08-28"):
            rows.append((sid, d, 100.0 + sid, f"{d}T10:00:00.000000Z"))
        rows.append((sid, AS_OF, 101.0 + sid, as_of_obs))
    _put(store, rows)
    cfg = load_config()
    ctx = SimpleNamespace(conn=conn, cfg=cfg, clock=FrozenClock(now), store=store)
    return ctx


def test_enabled_decision_selection(tmp_path):
    from quant.data.price_admission import enabled_decision

    conn, _ = _paths(tmp_path)
    assert enabled_decision(conn) is None
    _decision(conn, approver="llm", decision_id="D-1")                       # not human
    _decision(conn, subject="something_else", decision_id="D-2")             # other subject
    _decision(conn, status="provisional", decision_id="D-3")                 # not approved/applied
    assert enabled_decision(conn) is None
    _decision(conn, tier=1, decision_id="D-3b")                              # a human Tier-1 row is not the Tier-2 decision
    assert enabled_decision(conn) is None
    _decision(conn, decision_id="D-4", decided_on="2026-08-01T00:00:00.000000Z")
    _decision(conn, status="applied", decision_id="D-5", decided_on="2026-08-02T00:00:00.000000Z")
    assert enabled_decision(conn) == "D-5"                                    # the newest


def test_decide_on_time_late_and_blocked(tmp_path):
    from quant.data.price_admission import decide

    ids = [1, 2, 3, 4, 5]
    # on time: the as_of closes were recorded before the cutoff, whether or not the decision exists
    ctx = _decide_world(tmp_path / "a", as_of_obs=f"{AS_OF}T10:00:00.000000Z", now=SCORE_AT)
    adm = decide(ctx, AS_OF, CUT, ids)
    assert adm.mode == "on_time" and adm.coverage_at_cutoff == 1.0 and adm.price_vintage is None

    # late: closes arrived after the cutoff, decision recorded, closes present now
    ctx = _decide_world(tmp_path / "b", as_of_obs=LATE_OBS, now=SCORE_AT)
    _decision(ctx.conn)
    adm = decide(ctx, AS_OF, CUT, ids)
    assert adm.mode == "late"
    assert (adm.coverage_at_cutoff, adm.coverage_at_vintage) == (0.0, 1.0)
    assert adm.price_vintage == SCORE_AT and adm.decision_id == "D-2026-08-01"

    # blocked: no decision
    ctx = _decide_world(tmp_path / "c", as_of_obs=LATE_OBS, now=SCORE_AT)
    adm = decide(ctx, AS_OF, CUT, ids)
    assert adm.mode == "blocked" and adm.decision_id is None and adm.price_vintage is None
    assert "not enabled" in adm.reason and "still missing" not in adm.reason

    # blocked: decision present but the closes are still missing at the scoring time
    ctx = _decide_world(tmp_path / "d", as_of_obs=LATE_OBS, now="2026-09-01T00:00:00.000000Z")
    _decision(ctx.conn)
    adm = decide(ctx, AS_OF, CUT, ids)
    assert adm.mode == "blocked" and adm.decision_id == "D-2026-08-01"
    assert "still missing" in adm.reason

    # a partial gap below the G3 threshold is not on time (5 of 6 members priced is 83%)
    ctx = _decide_world(tmp_path / "e", as_of_obs=f"{AS_OF}T10:00:00.000000Z", now=SCORE_AT)
    adm = decide(ctx, AS_OF, CUT, ids + [6])
    assert adm.mode == "blocked" and adm.coverage_at_cutoff == pytest.approx(5 / 6)


# ----------------------------------------------------------------------------- monthly() world

SECTORS = ["Capital Goods", "Chemicals", "Healthcare"]
N_SEC = 30
B_ASOF = "2026-09-30"
A_CLOCK = FrozenClock(SCORE_AT)                         # the August run, a day after the cutoff
B_CLOCK = FrozenClock("2026-10-01T05:00:00.000000Z")    # the September run


def _world_cfg(tmp_path):
    cfg = load_config().with_paths(db=tmp_path / "state.db", prices_db=tmp_path / "prices.db",
                                   data_dir=tmp_path / "data", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", ui_dir=tmp_path / "ui")
    # a 60-name synthetic universe with price, ROCE and value inputs only: shrink the floors that
    # are about the size and breadth of the real universe, not about prices
    cfg.universe.min_rows = 25
    cfg.evaluation.shuffle_permutations = 10      # the leakage suite's own cost, not what is tested here
    cfg.gates.max_excluded_factors = 99
    cfg.standardise.min_families = 2
    return cfg


def _bars(rng, sid_list, sessions, start_px):
    rows = []
    px = dict(start_px)
    for d in sessions:
        for sid in sid_list:
            px[sid] *= 1.0 + float(rng.normal(0.0003, 0.01))
            rows.append((sid, d, round(px[sid], 4), None))
    return rows, px


def _build_world(root):
    """State and price databases for two monthly runs (August as_of 2026-08-31, September as_of 2026-09-30).

    Every bar is recorded on time (at 10:00 UTC of its own date); a test restamps the as_of bars to make
    the closes late. September's bars are added by ``_add_september`` after the August run.
    """
    cfg = _world_cfg(root)
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    conn.close()
    with RunContext(as_of="2026-08-20", kind="bootstrap", track="live", cfg=cfg,
                    clock=FrozenClock("2026-08-20T10:00:00.000000Z"), actor=Actor(kind="system", name="seed")) as ctx:
        from quant.knowledge.bootstrap import seed
        seed(ctx, spec_sha256="test")
        at = "2026-08-20T10:00:00.000000Z"
        ctx.conn.execute("INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                         "VALUES ('cap_u1', ?, 'nifty500', 'x', 'sha', 'test', ?)", (at, ctx.run_id))
        for sid in range(1, N_SEC + 1):
            ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                             "VALUES (?, ?, ?, '2020-01-01', '2026-08-20', 'listed')", (sid, f"INE{sid:09d}", f"Stock {sid}"))
            ctx.conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, "
                             "nse_sector, series, source) VALUES ('2026-08-20', ?, ?, 'NIFTY500', ?, ?, 'EQ', 'nse_csv')",
                             (at, sid, f"SYM{sid}", SECTORS[(sid - 1) // 10]))
            ctx.conn.execute("INSERT INTO security_attributes (captured_at, security_id, mcap_inr, shares_out, float_shares, ev_inr, "
                             "trailing_pe, price_to_book, dividend_rate_inr, beta, source_sha256) "
                             "VALUES (?, ?, 5e10, 5e8, 3e8, 6e10, ?, ?, 1.0, 1.0, 'sha')", (at, sid, 8.0 + sid % 17, 1.5 + sid % 9))
            ctx.conn.execute("INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
                             "VALUES (?, ?, 0.3, 0.5, 5e8, 'test')", (sid, at))
            for statement, field, val in (("income", "EBIT", 1.0e9 * (1 + sid / N_SEC)),
                                          ("balance", "Total Assets", 1.0e10),
                                          ("balance", "Current Liabilities", 2.0e9)):
                ctx.conn.execute(
                    "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, available_from, "
                    "available_from_basis, fetched_at, source, run_id) VALUES (?, ?, 'A', '2026-03-31', ?, ?, 'inr', "
                    "'2026-06-15', 'lodr_45d', ?, 'test', ?)", (sid, statement, field, val, at, ctx.run_id))
    state = connect(cfg.paths.db)
    store = PriceStore(cfg.paths.prices_db, state_conn=state)
    rng = np.random.default_rng(11)
    rows, px = _bars(rng, list(range(1, N_SEC + 1)), _sessions("2025-10-01", AS_OF), {s: 100.0 + s for s in range(1, N_SEC + 1)})
    _put(store, [(sid, d, c, f"{d}T10:00:00.000000Z", 1.0, 2_000_000.0) for sid, d, c, _ in rows])
    state.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    state.close()
    return px, rng


@pytest.fixture(scope="module")
def world_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("world_template")
    px, rng = _build_world(root)
    return root, px, rng


def _clone_world(template, tmp_path):
    """A private copy of the template world: (cfg, last prices, rng) with every path under tmp_path."""
    root, px, rng = template
    shutil.copytree(root, tmp_path / "w")
    return _world_cfg(tmp_path / "w"), dict(px), copy.deepcopy(rng)


def _restamp_asof_bars(cfg, observed_at):
    """What happened on the real database: the as_of closes are recorded after the cutoff."""
    with PriceStore(cfg.paths.prices_db, state_conn=None).conn() as c:
        c.execute("UPDATE prices_daily SET observed_at = ? WHERE date = ?", (observed_at, AS_OF))


def _add_september(cfg, px, rng):
    store = PriceStore(cfg.paths.prices_db, state_conn=connect(cfg.paths.db))
    rows, _ = _bars(rng, list(range(1, N_SEC + 1)), _sessions("2026-09-01", B_ASOF), px)
    _put(store, [(s, d, c, f"{d}T10:00:00.000000Z", 1.0, 2_000_000.0) for s, d, c, _ in rows])


def _run_month(cfg, as_of, clock):
    return monthly(cfg, clock, Actor(kind="system", name="runner"), as_of=as_of, skip_capture=True, allow_mid_month=True)


def _q(cfg, sql, params=()):
    conn = connect(cfg.paths.db, readonly=True)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _enable(cfg, clock=None):
    from quant.knowledge.decisions import record_rule_change

    with RunContext(as_of="2026-09-01", kind="decide", track="live", cfg=cfg, clock=clock or FrozenClock("2026-09-01T09:00:00.000000Z"),
                    actor=HUMAN) as ctx:
        return record_rule_change(ctx, subject_id="late_price_admission", title="Admit closes by trading date",
                                  context="The vendor delivers closes a day late.", evidence="MASTER_SPEC R13")


def _late_fundamental(cfg, sid, value, fetched_at):
    """A restated Total Assets row that is 'available from' before the cutoff but only fetched after it."""
    conn = connect(cfg.paths.db)
    try:
        run_id = conn.execute("SELECT min(run_id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, available_from, "
            "available_from_basis, fetched_at, source, run_id) VALUES (?, 'balance', 'A', '2026-03-31', 'Total Assets', ?, "
            "'inr', '2026-06-15', 'lodr_45d', ?, 'test', ?)", (sid, value, fetched_at, run_id))
    finally:
        conn.close()


def _journaled(cfg, at="2026-09-01T12:00:00.000000Z"):
    """A RunContext for rows written outside the pipeline, so the ledger stays consistent with the database."""
    return RunContext(as_of="2026-09-01", kind="action", track="live", cfg=cfg, clock=FrozenClock(at), actor=HUMAN)


def _after_cutoff_inputs(cfg):
    """Inputs recorded after the cutoff that a late cohort must not use: attributes, holdings, index membership and a
    human corporate-action approval. Security 1 gets four monthly holdings captures (to the cutoff) so a flows
    factor exists for it; security 2 has a suspected action before the cutoff that is approved after it."""
    with _journaled(cfg) as ctx:
        conn = ctx.conn
        conn.execute("INSERT INTO security_attributes (captured_at, security_id, mcap_inr, shares_out, float_shares, ev_inr, "
                     "trailing_pe, price_to_book, dividend_rate_inr, beta, source_sha256) "
                     "VALUES (?, 1, 5e12, 5e8, 3e8, 6e12, 3.0, 0.5, 9.0, 1.0, 'sha')", (APPROVED_AT,))
        for at, pct in (("2026-05-20T10:00:00.000000Z", 0.10), ("2026-06-20T10:00:00.000000Z", 0.20),
                        ("2026-07-20T10:00:00.000000Z", 0.25), (APPROVED_AT, 0.90)):
            conn.execute("INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
                         "VALUES (1, ?, ?, 0.5, 5e8, 'test')", (at, pct))
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (31, 'INE000000031', 'Late joiner', '2020-01-01', '2026-09-01', 'listed')")
        conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, series, source) "
                     "VALUES ('2026-08-20', ?, 31, 'NIFTY500', 'SYM31', 'Capital Goods', 'EQ', 'nse_csv')", (APPROVED_AT,))
        conn.execute("INSERT INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, source, observed_at, "
                     "decision_id, note) VALUES (2, '2026-08-14', 'suspected', NULL, NULL, NULL, 'inferred', "
                     "'2026-08-17T10:00:00.000000Z', NULL, NULL)")
        conn.execute("INSERT INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, source, observed_at, "
                     "decision_id, note) VALUES (2, '2026-08-14', 'manual_adj', NULL, NULL, 0.9, 'manual', ?, 'D-X', NULL)",
                     (APPROVED_AT,))


def _later_price_state(cfg):
    """After the late cohort is published: revised old bars, a later suspect and a later approval."""
    later = "2026-09-10T10:00:00.000000Z"
    store = PriceStore(cfg.paths.prices_db, state_conn=None)
    with store.conn() as c:
        old = c.execute("SELECT close_raw FROM prices_daily WHERE security_id = 5 AND date = '2026-08-20' LIMIT 1").fetchone()[0]
        asof = c.execute("SELECT close_raw FROM prices_daily WHERE security_id = 6 AND date = ? LIMIT 1", (AS_OF,)).fetchone()[0]
    _put(store, [(5, "2026-08-20", old * 1.25, later), (6, AS_OF, asof * 0.5, later, 2.0)])
    with _journaled(cfg, "2026-09-14T00:00:00.000000Z") as ctx:
        conn = ctx.conn
        for kind, adj, dec, obs in (("suspected", None, None, "2026-09-12T10:00:00.000000Z"),
                                    ("manual_adj", 0.9, "D-X", "2026-09-13T10:00:00.000000Z")):
            conn.execute("INSERT INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, source, "
                         "observed_at, decision_id, note) VALUES (4, '2026-08-25', ?, NULL, NULL, ?, ?, ?, ?, NULL)",
                         (kind, adj, "inferred" if kind == "suspected" else "manual", obs, dec))


def _gate_status(cfg, gate, *, phase=None, last=True):
    rows = _q(cfg, "SELECT d.status, d.reason FROM dq_runs d JOIN runs r ON r.run_id = d.run_id "
                   "WHERE r.kind = 'monthly' AND d.gate = ? ORDER BY d.run_id", (gate,))
    return rows[-1] if last else rows


# ----------------------------------------------------------------------------- 5. monthly(), late closes

def test_monthly_late_closes_blocked_without_the_decision_published_with_it(tmp_path, world_template, monkeypatch):
    cfg, px, rng = _clone_world(world_template, tmp_path)
    _restamp_asof_bars(cfg, LATE_OBS)
    _late_fundamental(cfg, 1, 1.0e11, "2026-09-01T00:00:00.000000Z")
    _after_cutoff_inputs(cfg)

    # without the enabling decision the engine behaves as it did: blocked at G3, nothing published
    assert _run_month(cfg, AS_OF, A_CLOCK) == 2
    assert _q(cfg, "SELECT count(*) FROM cohorts") == [(0,)]
    status, reason = _gate_status(cfg, "G3")
    assert status == "FAIL" and reason.startswith("BOOTSTRAP_REQUIRED")
    notes = json.loads(_q(cfg, "SELECT notes_json FROM runs WHERE kind = 'monthly'")[0][0])
    assert notes["blocked"].startswith("BOOTSTRAP_REQUIRED") and "not enabled" in notes["late_price_admission"]
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'") == [(0,)]

    decision_id = _enable(cfg)
    assert _run_month(cfg, AS_OF, A_CLOCK) == 0
    cohort_id, is_clean, refs_json = _q(cfg, "SELECT cohort_id, is_clean, source_refs_json FROM cohorts")[0]
    refs = json.loads(refs_json)
    assert (cohort_id, is_clean) == (f"live:{AS_OF}", 0)
    assert refs["price_vintage"] == SCORE_AT and refs["scored_late"] is True
    assert refs["late_price_admission"]["rule"] == "bar_date/1"
    assert refs["late_price_admission"]["decision_id"] == decision_id
    assert refs["late_price_admission"]["coverage_at_cutoff"] == 0.0
    assert refs["late_price_admission"]["coverage_at_vintage"] == 1.0
    assert _gate_status(cfg, "G3")[0] == "PASS"
    assert _q(cfg, "SELECT count(*) FROM prices_monthly WHERE close_raw IS NOT NULL") == [(N_SEC,)]
    run_notes = json.loads(_q(cfg, "SELECT notes_json FROM runs WHERE kind = 'monthly' ORDER BY run_id DESC LIMIT 1")[0][0])
    assert run_notes["scored_late"] is True and "late_price_admission" in run_notes

    # fundamentals stay at the cutoff: the restated assets (fetched after it) are not used for security 1
    raw = _q(cfg, "SELECT raw FROM factor_values WHERE factor_id = 'roce@1' AND security_id = 1")[0][0]
    assert raw == pytest.approx(1.0e9 * (1 + 1 / N_SEC) / (1.0e10 - 2.0e9))

    # attributes, holdings and index membership recorded after the cutoff are not used either
    def factor(factor_id, sid):
        return _q(cfg, "SELECT raw FROM factor_values WHERE factor_id = ? AND security_id = ?", (factor_id, sid))[0][0]
    assert factor("size@1", 1) == pytest.approx(np.log(5e10))                          # not the later 5e12
    assert factor("earnings_yield@1", 1) == pytest.approx(1.0e9 * (1 + 1 / N_SEC) / 6e10)   # not the later EV 6e12
    assert factor("inst_hold_chg_3m@1", 1) == pytest.approx(0.30 - 0.10)               # not 0.90 - 0.20
    assert _q(cfg, "SELECT count(*) FROM scores WHERE cohort_id = ? AND security_id = 31", (cohort_id,)) == [(0,)]
    assert _q(cfg, "SELECT count(DISTINCT security_id) FROM scores WHERE cohort_id = ?", (cohort_id,)) == [(N_SEC,)]
    # an approval recorded after the cutoff does not lift the truncation at the suspect: security 2 has no long-history factors
    assert factor("mom_6_1@1", 2) is None and factor("trend_200@1", 2) is None
    assert factor("mom_6_1@1", 3) is not None and factor("trend_200@1", 3) is not None

    # the price manifest of the late cohort verifies through the CLI, which a plain price store cannot do
    from quant.commands import prices as prices_cmd
    from argparse import Namespace

    monkeypatch.setattr(prices_cmd, "_cfg", lambda args: cfg)
    manifest = cfg.paths.data_dir / "manifests" / f"prices_{AS_OF}.json"
    assert manifest.is_file()
    assert prices_cmd.prices_manifest_cmd(Namespace(verify=True, as_of=AS_OF)) == 0
    state = connect(cfg.paths.db, readonly=True)
    try:
        assert PriceStore(cfg.paths.prices_db, state_conn=state).manifest_verify(manifest).status == "FAIL"
    finally:
        state.close()

    # later price captures (revised old bars, new versions of the as_of bar), a later suspect and a later approval
    # leave its replay unchanged
    _later_price_state(cfg)

    # the following month, on time: it replays the late cohort from its stored pin and G9 passes
    _add_september(cfg, px, rng)
    assert _run_month(cfg, B_ASOF, B_CLOCK) == 0
    assert _gate_status(cfg, "G9")[0] == "PASS"
    b_clean, b_refs = _q(cfg, f"SELECT is_clean, source_refs_json FROM cohorts WHERE cohort_id = 'live:{B_ASOF}'")[0]
    assert b_clean == 1
    assert not {"price_vintage", "scored_late", "late_price_admission"} & set(json.loads(b_refs))
    # the late cohort is the only one flagged; the pin was replayed, not re-derived
    assert _q(cfg, "SELECT cohort_id FROM cohorts WHERE is_clean = 0") == [(f"live:{AS_OF}",)]
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'") == [(0,)]


# ----------------------------------------------------------------------------- 6. a cohort published before this change

def test_cohort_published_before_the_change_replays_only_under_the_decision(tmp_path, world_template, monkeypatch):
    from quant.cli import main
    from quant.commands import kb as kb_cmd
    from quant.data.price_admission import published_pins, RULE_BARS_ONLY

    cfg, px, rng = _clone_world(world_template, tmp_path)
    assert _run_month(cfg, AS_OF, A_CLOCK) == 0                      # published on time, as before this change
    a_refs = json.loads(_q(cfg, "SELECT source_refs_json FROM cohorts")[0][0])
    assert _q(cfg, "SELECT is_clean FROM cohorts") == [(1,)]
    assert not {"price_vintage", "scored_late", "late_price_admission"} & set(a_refs)
    published_at = _q(cfg, "SELECT published_at FROM cohorts")[0][0]

    # what happened on the real database: the as_of bars were later restamped as observed after the cutoff
    _restamp_asof_bars(cfg, LATE_OBS)
    _add_september(cfg, px, rng)

    # without the decision nothing is derived, and the replay of August that September's G9 runs fails
    from quant.data.price_admission import AdmittingPriceStore
    from quant.run import _g9_replay
    from quant.types import Draft

    with RunContext(as_of=B_ASOF, kind="verify", track="live", cfg=cfg, clock=B_CLOCK, actor=Actor(kind="system", name="t")) as ctx:
        ctx.store = AdmittingPriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        assert published_pins(ctx, ctx.store, write=True) == {}
        draft = Draft(cohort_id=f"live:{B_ASOF}", as_of=B_ASOF, track="live", knowledge_cutoff=knowledge_cutoff(B_ASOF),
                      definition_hash="d", members=pd.DataFrame({"security_id": []}), groups=pd.Series(dtype=object))
        g9 = _g9_replay(ctx, draft)
        assert g9.status == "FAIL" and g9.blocking and g9.observed["mismatches"] > 0
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'") == [(0,)]

    # recording the decision flags the older cohort at once, in the same run, before any monthly run
    monkeypatch.setattr(kb_cmd, "_cfg", lambda args: cfg)
    assert main(["kb", "decide", "--kind", "rule_change", "--subject", "late_price_admission", "--title", "Admit closes by trading date",
                 "--evidence", "MASTER_SPEC R13", "--actor-kind", "human", "--by", "human:owner", "--as-of", "2026-10-01"]) == 0
    (decision_id,) = [r[0] for r in _q(cfg, "SELECT decision_id FROM decisions WHERE kind = 'rule_change'")]
    events = _q(cfg, "SELECT severity, detail_json FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'")
    assert len(events) == 1 and events[0][0] == "INFO"
    detail = json.loads(events[0][1])
    assert detail == {"cohort_id": f"live:{AS_OF}", "price_vintage": published_at, "rule": RULE_BARS_ONLY,
                      "basis": "derived", "decision_id": decision_id}

    # clean-live evidence (walkforward here, evaluate below) excludes it at once, though the row is still is_clean = 1
    from quant.evaluation import evaluate
    from quant.evaluation.walkforward import family_ic_history

    conn = connect(cfg.paths.db, readonly=True)
    try:
        model = {"weights_json": {}, "factor_set_json": [], "horizon_m": 3}
        assert list(family_ic_history(conn, model, "2026-12-31", "2026-12-31T00:00:00.000000Z").index) == []
    finally:
        conn.close()

    # a later monthly run does not add a second event, and G9 passes through the derived pin
    assert _run_month(cfg, B_ASOF, FrozenClock("2026-10-01T07:00:00.000000Z")) == 0
    assert _gate_status(cfg, "G9")[0] == "PASS"
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'") == [(1,)]

    # deriving the pin again writes nothing new, and a read-only derivation writes nothing at all
    with RunContext(as_of=B_ASOF, kind="verify", track="live", cfg=cfg, clock=FrozenClock("2026-10-02T00:00:00.000000Z"),
                    actor=Actor(kind="system", name="t")) as ctx:
        from quant.data.price_admission import AdmittingPriceStore
        ctx.store = AdmittingPriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        pins = published_pins(ctx, ctx.store, write=True)
        assert pins[f"live:{AS_OF}"]["price_vintage"] == published_at and pins[f"live:{AS_OF}"]["basis"] == "derived"
        assert f"live:{B_ASOF}" not in pins
        published_pins(ctx, ctx.store, write=False)
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE'") == [(1,)]

    # evaluation ignores the August cohort although it is still is_clean = 1
    seen = []
    orig = evaluate._cohort_factor_frame
    evaluate._cohort_factor_frame = lambda conn, cid: (seen.append(cid) or orig(conn, cid))
    try:
        with RunContext(as_of=B_ASOF, kind="evaluate", track="live", cfg=cfg, clock=FrozenClock("2026-12-31T00:00:00.000000Z"),
                        actor=Actor(kind="system", name="t")) as ctx:
            evaluate.run(ctx, through="2026-12-31", track="live")
    finally:
        evaluate._cohort_factor_frame = orig
    assert seen == [f"live:{B_ASOF}"]


# ----------------------------------------------------------------------------- evidence exclusion (units)

def _cohort(conn, cohort_id, as_of, *, clean=1, refs="{}"):
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, "
        "published_at, generated_at, is_clean, run_id) VALUES (?, ?, 'live', ?, ?, 'm', ?, ?, ?, ?, 1)",
        (cohort_id, as_of, knowledge_cutoff(as_of), f"def-{cohort_id}", refs, f"{as_of}T23:59:59.999999Z", f"{as_of}T23:59:59.999999Z", clean))


def _late_event(conn, cohort_id, code="COHORT_SCORED_LATE"):
    conn.execute(
        "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, field, detail_json, resolved_by) "
        "VALUES (1, '2026-09-30', '2026-10-01T00:00:00.000000Z', 'INFO', ?, NULL, NULL, ?, NULL)",
        (code, json.dumps({"cohort_id": cohort_id})))


def _evidence_db(tmp_path, event_code=None):
    """State database with three clean live cohorts (and, optionally, a late event naming the second)."""
    conn, prices = _paths(tmp_path)
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, "
        "registry_sha256) VALUES (1, '2026-01-30', 'test', 'live', 1, '2026-01-30T00:00:00.000000Z', 'ok', 'g', 'c', 'q', 'r')")
    _cohort(conn, "live:2026-01-30", "2026-01-30")
    _cohort(conn, "live:2026-02-27", "2026-02-27")
    _cohort(conn, "live:2026-06-30", "2026-06-30")
    # an event without a cohort id must not wipe out every cohort (NOT IN with a NULL)
    conn.execute("INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, detail_json) "
                 "VALUES (1, '2026-06-30', '2026-07-01T00:00:00.000000Z', 'INFO', 'COHORT_SCORED_LATE', '{}')")
    if event_code:
        _late_event(conn, "live:2026-02-27", event_code)
    return conn


def test_walkforward_ignores_cohorts_named_by_a_late_event(tmp_path):
    from quant.evaluation.walkforward import family_ic_history

    model = {"weights_json": {}, "factor_set_json": [], "horizon_m": 3}
    conn = _evidence_db(tmp_path / "plain")
    assert list(family_ic_history(conn, model, "2026-12-31", "2026-12-31T00:00:00.000000Z").index) == \
        ["2026-01-30", "2026-02-27", "2026-06-30"]
    conn = _evidence_db(tmp_path / "late", "COHORT_SCORED_LATE")
    assert list(family_ic_history(conn, model, "2026-12-31", "2026-12-31T00:00:00.000000Z").index) == \
        ["2026-01-30", "2026-06-30"]


@pytest.mark.parametrize("code", ["PRICE_GAPS", "DQ_PRICE_PROVENANCE_FAIL", "COHORT_INVALIDATED"])
def test_other_event_codes_do_not_exclude(tmp_path, code):
    from quant.evaluation.walkforward import family_ic_history

    conn = _evidence_db(tmp_path, code)
    model = {"weights_json": {}, "factor_set_json": [], "horizon_m": 3}
    assert len(family_ic_history(conn, model, "2026-12-31", "2026-12-31T00:00:00.000000Z").index) == 3


@pytest.mark.parametrize("late", [False, True])
def test_learning_curve_training_end_ignores_late_cohorts(tmp_path, late):
    from quant.evaluation import curves

    conn = _evidence_db(tmp_path, "COHORT_SCORED_LATE" if late else None)
    conn.execute(
        "INSERT INTO evaluations (computed_run_id, computed_at, subject_kind, subject_id, subject_version, as_of, horizon_m, scope, "
        "track, metric, value, n, status, method, evidence_hash, revision) VALUES (1, '2026-07-01T00:00:00.000000Z', 'model', "
        "'M_CHAMPION', '1', '2026-06-30', 3, 'eligible', 'live', 'ic', 0.1, 50, 'ok', 'rank', 'h1', 1)")
    conn.close()
    cfg = load_config().with_paths(db=tmp_path / "state.db", prices_db=tmp_path / "prices.db")
    with RunContext(as_of="2026-07-01", kind="evaluate", track="live", cfg=cfg, clock=FrozenClock("2026-07-01T00:00:00.000000Z"),
                    actor=Actor(kind="system", name="t")) as ctx:
        curves.update(ctx, through="2026-07-01")
        train_end = ctx.conn.execute("SELECT train_end FROM learning_curve_points").fetchone()[0]
    assert train_end == ("2026-01-30" if late else "2026-02-27")


def test_an_event_without_a_cohort_id_excludes_no_cohort(tmp_path):
    """NOT IN over a set holding a NULL is never true: the fragment must not wipe out every cohort (the shape the
    leakage embargo, evaluate and curves queries use)."""
    from quant.data.price_admission import excluded_late_cohorts_sql

    conn = _evidence_db(tmp_path)                      # holds a COHORT_SCORED_LATE event whose detail has no cohort_id
    assert [r[0] for r in conn.execute("SELECT json_extract(detail_json, '$.cohort_id') FROM data_quality_events")] == [None]
    query = f"SELECT DISTINCT as_of FROM cohorts WHERE track = 'live' AND is_clean = 1 AND as_of <= ? AND {excluded_late_cohorts_sql()}"
    assert [r[0] for r in conn.execute(query + " ORDER BY as_of", ("2026-12-31",))] == ["2026-01-30", "2026-02-27", "2026-06-30"]
    _late_event(conn, "live:2026-02-27")
    assert [r[0] for r in conn.execute(query + " ORDER BY as_of", ("2026-12-31",))] == ["2026-01-30", "2026-06-30"]


def test_exclusion_fragment_names_exactly_the_late_code():
    from quant.data.price_admission import LATE_EVENT_CODES, excluded_late_cohorts_sql

    assert LATE_EVENT_CODES == ("COHORT_SCORED_LATE",)
    sql = excluded_late_cohorts_sql("c.cohort_id")
    assert sql.startswith("c.cohort_id NOT IN") and "'COHORT_SCORED_LATE'" in sql
    assert "DQ_PRICE_PROVENANCE_FAIL" not in sql and "COHORT_INVALIDATED" not in sql


# ----------------------------------------------------------------------------- malformed stored pins

def _pin_world(tmp_path, refs_by_cohort):
    conn, prices = _paths(tmp_path)
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, "
        "registry_sha256) VALUES (1, '2026-01-30', 'test', 'live', 1, '2026-01-30T00:00:00.000000Z', 'ok', 'g', 'c', 'q', 'r')")
    for cohort_id, refs in refs_by_cohort.items():
        _cohort(conn, cohort_id, cohort_id.split(":")[1], refs=json.dumps(refs))
    conn.close()
    return load_config().with_paths(db=tmp_path / "state.db", prices_db=prices)


def test_a_stored_pin_with_a_non_dict_rule_block_uses_the_default_rule(tmp_path):
    from quant.data.price_admission import AdmittingPriceStore, RULE_BARS_AND_SUSPECTS, published_pins

    cfg = _pin_world(tmp_path, {"live:2026-01-30": {"price_vintage": "2026-02-05T00:00:00.000000Z",
                                                    "late_price_admission": "oops"}})
    with RunContext(as_of="2026-02-06", kind="verify", track="live", cfg=cfg, clock=FrozenClock("2026-02-06T00:00:00.000000Z"),
                    actor=Actor(kind="system", name="t")) as ctx:
        ctx.store = AdmittingPriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        pins = published_pins(ctx, ctx.store, write=True)
    assert pins == {"live:2026-01-30": {"price_vintage": "2026-02-05T00:00:00.000000Z", "rule": RULE_BARS_AND_SUSPECTS,
                                        "basis": "stored"}}
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_PIN_INVALID'") == [(0,)]


def test_an_invalid_stored_price_vintage_is_skipped_and_warned_once_per_cohort(tmp_path):
    from quant.data.price_admission import AdmittingPriceStore, published_pins

    cfg = _pin_world(tmp_path, {"live:2026-01-30": {"price_vintage": 12345},                              # not a string
                                "live:2026-02-27": {"price_vintage": "2026-02-01T00:00:00.000000Z"},      # not after the cutoff
                                "live:2026-06-30": {"price_vintage": "2026-07-02T00:00:00.000000Z"}})     # valid
    def run(write, at):
        with RunContext(as_of="2026-07-03", kind="verify", track="live", cfg=cfg, clock=FrozenClock(at),
                        actor=Actor(kind="system", name="t")) as ctx:
            ctx.store = AdmittingPriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
            return published_pins(ctx, ctx.store, write=write)

    pins = run(False, "2026-07-03T00:00:00.000000Z")
    assert set(pins) == {"live:2026-06-30"}
    assert _q(cfg, "SELECT count(*) FROM data_quality_events WHERE code = 'COHORT_PIN_INVALID'") == [(0,)]   # read-only writes nothing
    assert set(run(True, "2026-07-03T01:00:00.000000Z")) == {"live:2026-06-30"}
    run(True, "2026-07-03T02:00:00.000000Z")                                                              # no second warning
    rows = _q(cfg, "SELECT severity, detail_json FROM data_quality_events WHERE code = 'COHORT_PIN_INVALID' ORDER BY detail_json")
    assert [r[0] for r in rows] == ["WARN", "WARN"]
    details = [json.loads(r[1]) for r in rows]
    assert [d["cohort_id"] for d in details] == ["live:2026-01-30", "live:2026-02-27"]
    assert details[0]["value"] == 12345 and details[1]["value"] == "2026-02-01T00:00:00.000000Z"
