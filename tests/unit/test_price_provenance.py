"""Price provenance guard: a price row is DISHONEST at a vintage V when observed_at <= V but its capture is
registered (captures.captured_at) after V, so it is visible at V only because its record time precedes its
fetch time. Rows with an empty or unknown capture_id are not judged. All offline, on tmp_path databases.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.run import knowledge_cutoff
from quant.types import Draft, FrozenClock

AS_OF = "2026-08-31"
CUT = knowledge_cutoff(AS_OF)
HONEST_CAP = "2026-08-31T10:00:00.000000Z"
LATE_CAP = "2026-09-17T06:00:00.000000Z"            # registered 17 days after the cutoff
SCORE_AT = "2026-09-18T05:00:00.000000Z"
IDS = [1, 2, 3, 4, 5]


def _sessions(start, end):
    return list(pd.bdate_range(start, end).strftime("%Y-%m-%d"))


def _world(tmp_path, *, as_of_obs=CUT, as_of_capture="cap-late", late_at=LATE_CAP, with_history=True):
    """Five members with honest history to 2026-08-28; the as_of close carries ``as_of_obs`` / ``as_of_capture``."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "state.db")
    apply_schema(conn, kind="state")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, "
        "registry_sha256) VALUES (1, '2026-08-31', 'test', 'live', 1, '2026-08-31T00:00:00.000000Z', 'ok', 'g', 'c', 'q', 'r')")
    for cid, at in (("cap-honest", HONEST_CAP), ("cap-late", late_at)):
        conn.execute("INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                     "VALUES (?, ?, 'yahoo_prices', 'x', 's', 'v', 1)", (cid, at))
    for sid in IDS:
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2025-01-01', '2026-09-30', 'listed')", (sid, f"INE00000000{sid}", f"S{sid}"))
    store = PriceStore(tmp_path / "prices.db", state_conn=conn)
    rows = []
    if with_history:
        rows += [(sid, d, 100.0 + sid, f"{d}T10:00:00.000000Z", "cap-honest") for sid in IDS
                 for d in _sessions("2026-07-01", "2026-08-28")]
    rows += [(sid, AS_OF, 101.0 + sid, as_of_obs, as_of_capture) for sid in IDS]
    _put(store, rows)
    return conn, store


def _put(store, rows):
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, dividend_raw, "
            "split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, 1000000.0, 0.0, 1.0, ?, ?, ?, ?, 'sha')",
            [(s, d, p, p, p, p, p, p, obs, cap) for s, d, p, obs, cap in rows])


def _enable(conn):
    conn.execute(
        "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
        "evidence_refs_json, decided_on, decided_by, approver_kind, status, effective_from, adr_path, git_sha) "
        "VALUES ('D-1', 'rule_change', 2, 'late_price_admission', 't', 'c', '[]', 'd', '[]', "
        "'2026-08-30T00:00:00.000000Z', 'human:x', 'human', 'approved', '2026-08-30', 'x.md', 'sha')")


# ----------------------------------------------------------------------------- P1 dishonest_rows

def test_honest_rows_have_no_dishonest_rows_and_coverage_is_unchanged(tmp_path):
    from quant.data.price_admission import coverage, dishonest_rows

    conn, store = _world(tmp_path, as_of_obs="2026-08-31T10:00:00.000000Z", as_of_capture="cap-honest")
    out = dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, CUT)
    assert out == {"rows": 0, "securities": 0, "captures": [], "sample": []}
    assert coverage(store, IDS, AS_OF, CUT) == 1.0


def test_backdated_rows_are_dishonest_at_the_cutoff(tmp_path):
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path)
    out = dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, CUT)
    assert (out["rows"], out["securities"], out["captures"]) == (5, 5, ["cap-late"])
    assert len(out["sample"]) == 5
    assert set(out["sample"][0]) == {"security_id", "date", "observed_at", "capture_id", "captured_at"}
    assert out["sample"][0]["captured_at"] == LATE_CAP and out["sample"][0]["observed_at"] == CUT
    # the sample is capped at five
    assert len(dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, CUT)["sample"]) <= 5


def test_sample_is_capped_and_window_and_member_filters_apply(tmp_path):
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path)
    _put(store, [(sid, "2026-08-20", 1.0, CUT, "cap-late") for sid in IDS] + [(6, AS_OF, 1.0, CUT, "cap-late")])
    out = dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, CUT)
    assert out["rows"] == 10 and out["securities"] == 5 and len(out["sample"]) == 5      # sid 6 is not a member
    assert dishonest_rows(store, conn, IDS, "2026-08-25", AS_OF, CUT)["rows"] == 5       # window start excludes 08-20
    assert dishonest_rows(store, conn, [1], "2026-07-01", AS_OF, CUT)["rows"] == 2


def test_no_securities_gives_zeros(tmp_path):
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path)
    assert dishonest_rows(store, conn, [], "2026-07-01", AS_OF, CUT) == {"rows": 0, "securities": 0, "captures": [], "sample": []}


def test_the_same_rows_are_honest_at_a_vintage_after_their_capture(tmp_path):
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path)
    assert dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, SCORE_AT)["rows"] == 0
    assert dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, LATE_CAP)["rows"] == 0     # captured_at == V is not after V


def test_rows_with_an_empty_or_unknown_capture_are_not_judged(tmp_path):
    from quant.data.price_admission import coverage, dishonest_rows

    for cap in ("", "cap-unknown"):
        conn, store = _world(tmp_path / f"c{cap}", as_of_capture=cap)
        assert dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, CUT)["rows"] == 0
        assert coverage(store, IDS, AS_OF, CUT) == 1.0


def test_observed_after_captured_is_always_honest(tmp_path):
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path, as_of_obs="2026-09-17T07:00:00.000000Z")
    assert dishonest_rows(store, conn, IDS, "2026-07-01", AS_OF, "2026-09-30T00:00:00.000000Z")["rows"] == 0


def test_many_securities_and_days_run_in_few_statements(tmp_path):
    """500 members x 400 days: one grouped price query, no per-row lookups (statement count is bounded)."""
    from quant.data.price_admission import dishonest_rows

    conn, store = _world(tmp_path, with_history=False, as_of_obs="2026-08-31T10:00:00.000000Z", as_of_capture="cap-honest")
    ids = list(range(1, 501))
    dates = _sessions("2025-07-01", "2026-08-28")[:400]
    _put(store, [(s, d, 1.0, f"{d}T10:00:00.000000Z", "cap-honest") for s in ids for d in dates])
    n = {"sql": 0}
    real = store.conn

    from contextlib import contextmanager

    @contextmanager
    def counting():
        with real() as c:
            c.set_trace_callback(lambda _s: n.__setitem__("sql", n["sql"] + 1))
            yield c

    store.conn = counting
    out = dishonest_rows(store, conn, ids, "2025-07-01", AS_OF, CUT)
    assert out["rows"] == 0
    assert n["sql"] <= 3                                   # clean data: the grouped query only (chunked, never per row)


# ----------------------------------------------------------------------------- P2 coverage and decide

def test_backdated_bar_is_missing_in_coverage_and_decide_goes_late_or_blocked(tmp_path):
    from quant.data.price_admission import AdmittingPriceStore, coverage, decide

    ctxs = {}
    for name in ("enabled", "disabled"):
        conn, _ = _world(tmp_path / name)
        store = AdmittingPriceStore(tmp_path / name / "prices.db", state_conn=conn)
        ctxs[name] = SimpleNamespace(conn=conn, cfg=load_config(), clock=FrozenClock(SCORE_AT), store=store)
    _enable(ctxs["enabled"].conn)
    assert coverage(ctxs["enabled"].store, IDS, AS_OF, CUT) == 0.0                   # was 1.0 before the guard
    assert coverage(ctxs["enabled"].store, IDS, AS_OF, SCORE_AT) == 1.0              # honest once the capture exists
    adm = decide(ctxs["enabled"], AS_OF, CUT, IDS)
    assert adm.mode == "late" and adm.coverage_at_cutoff == 0.0 and adm.price_vintage == SCORE_AT
    adm = decide(ctxs["disabled"], AS_OF, CUT, IDS)
    assert adm.mode == "blocked" and "not enabled" in adm.reason


def test_partial_backdating_counts_only_the_clean_members(tmp_path):
    from quant.data.price_admission import coverage

    conn, store = _world(tmp_path, as_of_obs="2026-08-31T10:00:00.000000Z", as_of_capture="cap-honest")
    _put(store, [(1, AS_OF, 7.0, CUT, "cap-late")])                      # a second, backdated version for member 1
    assert coverage(store, IDS, AS_OF, CUT) == 1.0                       # member 1 still has its honest close
    conn2, store2 = _world(tmp_path / "b")
    _put(store2, [(1, AS_OF, 7.0, "2026-08-31T10:00:00.000000Z", "cap-honest")])
    assert coverage(store2, IDS, AS_OF, CUT) == pytest.approx(1 / 5)


def test_coverage_without_a_state_connection_behaves_as_before(tmp_path):
    from quant.data.price_admission import coverage

    conn, store = _world(tmp_path)
    store.state_conn = None
    assert coverage(store, IDS, AS_OF, CUT) == 1.0


# ----------------------------------------------------------------------------- P3 gate G3

def _g3(ctx_store, conn, *, cutoff=CUT, sids=IDS):
    from quant.data import gates

    cfg = load_config()
    ctx = SimpleNamespace(conn=conn, cfg=cfg, clock=FrozenClock(SCORE_AT), store=ctx_store)
    draft = Draft(cohort_id=f"live:{AS_OF}", as_of=AS_OF, track="live", knowledge_cutoff=cutoff, definition_hash="d",
                  members=pd.DataFrame([{"security_id": i} for i in sids]),
                  groups=pd.Series({i: "Technology" for i in sids}),
                  source_refs={"universe_capture_date": "2026-08-20", "has_fundamentals": True})
    return [c for c in gates._run_pre_gates(ctx, draft) if c.id == "G3"]


def test_g3_fails_on_backdated_rows_with_the_reason_text(tmp_path):
    conn, store = _world(tmp_path)
    (g3,) = _g3(store, conn)
    assert g3.status == "FAIL" and g3.blocking
    assert g3.reason.startswith("5 price rows of 5 members come from a capture registered after the price vintage "
                                "(record time earlier than fetch time): cap-late")
    assert not g3.reason.startswith("BOOTSTRAP_REQUIRED")


def test_g3_fails_on_dishonest_history_even_when_the_as_of_close_is_honest(tmp_path):
    conn, store = _world(tmp_path, as_of_obs="2026-08-31T10:00:00.000000Z", as_of_capture="cap-honest")
    _put(store, [(2, "2026-08-10", 5.0, "2026-08-10T11:00:00.000000Z", "cap-late")])
    (g3,) = _g3(store, conn)
    assert g3.status == "FAIL" and g3.reason.startswith("1 price rows of 1 members come from a capture registered after")


def test_g3_passes_on_honest_rows_and_ignores_unjudged_rows(tmp_path):
    for cap in ("cap-honest", "", "cap-unknown"):
        conn, store = _world(tmp_path / f"c{cap}", as_of_obs=CUT, as_of_capture=cap)
        (g3,) = _g3(store, conn)
        assert g3.status == "PASS", (cap, g3.reason)


def test_g3_is_honest_at_the_pin_so_a_late_scored_cohort_passes(tmp_path):
    from quant.data.price_admission import AdmittingPriceStore

    conn, _ = _world(tmp_path)
    store = AdmittingPriceStore(tmp_path / "prices.db", state_conn=conn)
    store.pin(CUT, SCORE_AT)
    (g3,) = _g3(store, conn)
    assert g3.status == "PASS" and g3.observed == 1.0


# ----------------------------------------------------------------------------- P4 verify pit

def _verify_world(tmp_path, *, as_of_obs, as_of_capture, published_at, enable=False, refs="{}"):
    conn, store = _world(tmp_path, as_of_obs=as_of_obs, as_of_capture=as_of_capture)
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, "
        "published_at, generated_at, is_clean, run_id) VALUES (?, ?, 'live', ?, 'def', 'm', ?, ?, ?, 1, 1)",
        (f"live:{AS_OF}", AS_OF, CUT, refs, published_at, published_at))
    for sid in IDS:
        conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
                     "VALUES (?, '2026-08-31T10:00:00.000000Z', ?, 'NIFTY500', ?, 'current_backfill')", (AS_OF, sid, f"T{sid}"))
    if enable:
        _enable(conn)
        for sid in IDS:
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, "
                "family_scores_json, scored, eligible, n_factors_used, input_hash, generated_at, track, run_id) "
                "VALUES (?, ?, ?, 'm', 1, 'g', 1, '{}', 1, 1, 1, 'h', ?, 'live', 1)", (f"live:{AS_OF}", AS_OF, sid, published_at))
    conn.close()
    cfg = load_config().with_paths(db=tmp_path / "state.db", prices_db=tmp_path / "prices.db")
    return cfg


def _pit(cfg):
    import quant.verify

    conn = connect(cfg.paths.db, readonly=True)
    try:
        rep = quant.verify.pit(conn, 3, cfg)
    finally:
        conn.close()
    return [c for c in rep.checks if c.id.endswith(":prices_no_backdated_capture")], rep


def test_verify_pit_fails_a_cohort_published_on_dishonest_rows(tmp_path):
    cfg = _verify_world(tmp_path, as_of_obs=CUT, as_of_capture="cap-late", published_at="2026-08-31T18:30:00.000000Z")
    checks, rep = _pit(cfg)
    assert [c.id for c in checks] == [f"verify:pit:live:{AS_OF}:prices_no_backdated_capture"]
    assert checks[0].status == "FAIL" and checks[0].blocking and checks[0].observed == 5
    assert "5 price rows of 5 members" in checks[0].reason and "cap-late" in checks[0].reason
    assert not rep.passed


def test_verify_pit_passes_a_clean_cohort(tmp_path):
    cfg = _verify_world(tmp_path, as_of_obs="2026-08-31T10:00:00.000000Z", as_of_capture="cap-honest",
                        published_at="2026-08-31T18:30:00.000000Z")
    checks, _ = _pit(cfg)
    assert checks[0].status == "PASS" and checks[0].observed == 0 and checks[0].blocking


def test_verify_pit_uses_the_derived_pin_so_a_late_scored_cohort_passes(tmp_path):
    """September's shape: rows recorded at the cutoff, registered 17 days later, cohort published after that."""
    cfg = _verify_world(tmp_path, as_of_obs=CUT, as_of_capture="cap-late", published_at=SCORE_AT, enable=True)
    checks, _ = _pit(cfg)
    assert checks[0].status == "PASS" and checks[0].observed == 0


def test_verify_pit_uses_a_stored_pin(tmp_path):
    cfg = _verify_world(tmp_path, as_of_obs=CUT, as_of_capture="cap-late", published_at=SCORE_AT,
                        refs=json.dumps({"price_vintage": SCORE_AT}))
    checks, _ = _pit(cfg)
    assert checks[0].status == "PASS"


def test_verify_pit_does_not_judge_unknown_captures(tmp_path):
    cfg = _verify_world(tmp_path, as_of_obs=CUT, as_of_capture="cap-unknown", published_at="2026-08-31T18:30:00.000000Z")
    checks, _ = _pit(cfg)
    assert checks[0].status == "PASS"


def test_verify_pit_defers_when_the_price_store_is_absent(tmp_path):
    cfg = _verify_world(tmp_path, as_of_obs=CUT, as_of_capture="cap-late", published_at="2026-08-31T18:30:00.000000Z")
    (tmp_path / "prices.db").unlink()
    checks, rep = _pit(cfg)
    assert checks[0].status == "DEFERRED" and not checks[0].blocking


# ----------------------------------------------------------------------------- P4 filled through the fallback chain

@pytest.fixture
def fworld(tmp_path):
    """The fallback test module's small world (auto mode, injected fetch), its run context open for the test."""
    from test_price_fallback import _build

    yield from _build(tmp_path, "auto")


def test_rows_filled_through_fill_missing_are_honest_at_the_fetch_time_and_invisible_before_it(fworld):
    from test_price_fallback import NOW, SESSION, enable, fill, isin, line
    from quant.data.price_admission import coverage, dishonest_rows

    w = fworld
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    assert dishonest_rows(w.store, w.ctx.conn, w.sids, "2026-11-01", SESSION, NOW)["rows"] == 0
    before = "2026-11-14T10:59:59.000000Z"                   # one second before the fetch
    assert coverage(w.store, w.sids, SESSION, before) == pytest.approx(4 / 6)       # only the four Yahoo members
    assert w.store.close_raw([5, 6], SESSION, SESSION, before).empty                # the filled rows are not visible yet
    assert coverage(w.store, w.sids, SESSION, NOW) == 1.0
