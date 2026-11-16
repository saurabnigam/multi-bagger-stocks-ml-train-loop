"""Regression tests for the paper_labels review fixes (C07 labels.mature, C08 paper).

Covers:
  - labels.mature() reading forward returns from the price store (TRI ratio,
    including a dividend event) instead of only the cohort's own
    prices_monthly panel.
  - labels.mature() producing a label row for every original cohort member,
    including one the price store (and prices_monthly) has no data for
    (survivorship; status 'missing').
  - paper.roll_forward() deriving a monthly return from actual price-store
    sessions (a TRI ratio), not calendar month-start/end dates that are
    usually not sessions.
  - paper.settle() never filling an order on a date that is not an observed
    session, even when nearby dates do have prices.
"""

from __future__ import annotations

import inspect
import math

import pytest

from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.evaluation.labels import mature
from quant.portfolio.paper import net_selection_spread, roll_forward, settle
from quant.run import RunContext
from quant.types import Actor, FrozenClock

EARLY_OBSERVED_AT = "2026-01-01T00:00:00.000000Z"


def _insert_security(conn, sid: int) -> None:
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (?, ?, ?, '2026-01-01', '2026-12-31', 'listed')",
        (sid, f"INE{sid:09d}", f"Stock {sid}"),
    )


def _insert_run(conn, run_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (?, '2026-01-01', 'test', 'live', 1, '2026-01-01T00:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')",
        (run_id,),
    )


def _insert_cohort(conn, cohort_id: str, as_of: str, run_id: int = 1, track: str = "live") -> None:
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
        "source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES (?, ?, ?, ?, 'def', 'mem', '{}', ?, ?, 1, ?)",
        (cohort_id, as_of, track, f"{as_of}T18:29:59.999999Z", f"{as_of}T09:00:00.000000Z",
         f"{as_of}T09:00:00.000000Z", run_id),
    )


def _insert_price(price_conn, sid: int, date: str, close: float, dividend: float = 0.0, split: float = 1.0) -> None:
    price_conn.execute(
        "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, 'cap', 'sha')",
        (sid, date, close, dividend, split, EARLY_OBSERVED_AT),
    )


@pytest.fixture
def labels_case(cfg):
    """A live cohort with 3 members: sid 1 has a dividend mid-horizon, sid 2 is
    a plain no-dividend mover, sid 3 has no price data anywhere."""
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    price_conn = connect(cfg.paths.prices_db)
    apply_schema(price_conn, kind="prices")

    with conn:
        _insert_run(conn, 1)
        for sid in (1, 2, 3):
            _insert_security(conn, sid)
        _insert_cohort(conn, "live:2026-03-31", "2026-03-31", run_id=1)
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('EW_HIER_v1', 1, '[]', '{}', '2026-01-01')"
        )
        for sid in (1, 2, 3):
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, "
                "group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, "
                "rank_all, rank, rank_group, decile, quintile, scored, eligible, n_factors_used, "
                "input_hash, generated_at, track, run_id) "
                "VALUES ('live:2026-03-31', '2026-03-31', ?, 'EW_HIER_v1', 1, 'GRP_A', 1, "
                "'{}', 0.5, 0.5, 0.0, 0.5, ?, ?, ?, 1, 1, 1, 1, 5, 'h', '2026-03-31T09:00:00.000000Z', 'live', 1)",
                (sid, sid, sid, sid),
            )

    with price_conn:
        # sid 1: a dividend on 2026-05-15 that a naive close-to-close return
        # would misread as a loss; TRI reinvests it, so the mid-leg is flat.
        _insert_price(price_conn, 1, "2026-03-31", 100.0)
        _insert_price(price_conn, 1, "2026-05-15", 95.0, dividend=5.0)
        _insert_price(price_conn, 1, "2026-06-30", 110.0)
        # sid 2: plain move, no corporate actions.
        _insert_price(price_conn, 2, "2026-03-31", 50.0)
        _insert_price(price_conn, 2, "2026-06-30", 55.0)
        # sid 3: deliberately no price data anywhere (store or prices_monthly).

    store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    ctx = RunContext(
        as_of="2026-06-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=FrozenClock("2026-07-01T00:00:00.000000Z"),
        actor=Actor(kind="system", name="test"),
    )
    ctx.conn = conn
    ctx.store = store
    ctx.run_id = 1
    return ctx, conn


def test_labels_from_store_match_hand_computed_log_tr_with_dividend(labels_case):
    """r_log for sid 1 equals the hand-computed TRI log return, i.e. the
    dividend-reinvested return (log(110/95)), not the naive close-to-close
    return (log(110/100)) that ignores the dividend."""
    ctx, conn = labels_case

    res = mature(ctx, through="2026-06-30")
    assert res.status == "ok"

    row = conn.execute(
        "SELECT r_log, r_arith, status FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND security_id = 1 AND horizon_m = 3"
    ).fetchone()
    assert row is not None
    assert row["status"] == "ok"

    # Hand computation of MASTER_SPEC 4.3's TRI recursion, independent of
    # PriceStore.tri(): TRI[d] = TRI[d-1]*split[d]*(close[d]+div[d])/close[d-1].
    tri0 = 100.0
    tri_mid = tri0 * 1.0 * (95.0 + 5.0) / 100.0  # == 100.0: dividend exactly offsets the close drop
    tri_end = tri_mid * 1.0 * (110.0 + 0.0) / 95.0
    expected_r_log = math.log(tri_end / tri0)

    assert expected_r_log == pytest.approx(math.log(110.0 / 95.0), abs=1e-12)
    assert row["r_log"] == pytest.approx(expected_r_log, abs=1e-9)
    # A naive close-to-close read (ignoring the dividend) would have given
    # log(110/100); confirm the implementation does NOT produce that instead.
    assert row["r_log"] != pytest.approx(math.log(110.0 / 100.0), abs=1e-6)
    assert row["r_arith"] == pytest.approx(math.exp(expected_r_log) - 1.0, abs=1e-9)


def test_every_original_member_gets_a_row_missing_status_without_prices(labels_case):
    """All 3 original cohort members get a 3M label row; sid 3 (no price data
    anywhere) is 'missing' with a NULL r_log, never assumed delisted or zero."""
    ctx, conn = labels_case

    mature(ctx, through="2026-06-30")

    rows = conn.execute(
        "SELECT security_id, status, r_log FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3"
    ).fetchall()
    by_sid = {r["security_id"]: r for r in rows}

    assert set(by_sid.keys()) == {1, 2, 3}
    assert by_sid[1]["status"] == "ok"
    assert by_sid[2]["status"] == "ok"
    assert by_sid[3]["status"] == "missing"
    assert by_sid[3]["r_log"] is None


def _insert_portfolio_scaffold(conn, cohort_id: str, portfolio_id: str, run_id: int = 1) -> None:
    _insert_run(conn, run_id)
    _insert_cohort(conn, cohort_id, cohort_id.split(":")[1], run_id=run_id)
    conn.execute(
        "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, cohort_id, rule, cadence, inception, rule_version) "
        "VALUES (?, 'model', 'M_TEST', '1', ?, 'top30_buffer', 'monthly', ?, '1')",
        (portfolio_id, cohort_id, cohort_id.split(":")[1]),
    )


def test_roll_forward_return_equals_tri_ratio_for_single_position(cfg):
    """A single fully-invested position's monthly return equals the TRI ratio
    between the inception session and the month-end session, including a
    mid-month dividend -- not a calendar-month-start/end close difference."""
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    price_conn = connect(cfg.paths.prices_db)
    apply_schema(price_conn, kind="prices")

    sid = 10
    cohort_id = "live:2026-03-08"
    portfolio_id = "P_SINGLE"
    order_id = "O_SINGLE_ENTRY"

    with conn:
        _insert_security(conn, sid)
        _insert_portfolio_scaffold(conn, cohort_id, portfolio_id)
        conn.execute(
            "INSERT INTO portfolio_orders (order_id, portfolio_id, cohort_id, security_id, created_at, "
            "earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
            "VALUES (?, ?, ?, ?, '2026-03-07T09:00:00.000000Z', '2026-03-08T10:00:00.000000Z', "
            "'entry', 'buy', 1.0, 'filled', 'A', NULL)",
            (order_id, portfolio_id, cohort_id, sid),
        )
        conn.execute(
            "INSERT INTO portfolio_trades (order_id, portfolio_id, cohort_id, exec_at, security_id, side, "
            "weight_delta, fill_price, cost_bps, liquidity_bucket, price_manifest_sha) "
            "VALUES (?, ?, ?, '2026-03-08T10:00:00.000000Z', ?, 'buy', 1.0, 100.0, 12.0, 'A', 'sha')",
            (order_id, portfolio_id, cohort_id, sid),
        )

    with price_conn:
        _insert_price(price_conn, sid, "2026-03-08", 100.0)
        # A dividend mid-month: a naive close-diff would understate the return.
        _insert_price(price_conn, sid, "2026-03-20", 97.0, dividend=3.0)
        _insert_price(price_conn, sid, "2026-03-31", 108.0)

    store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    ctx = RunContext(
        as_of="2026-03-31",
        kind="test",
        track="live",
        cfg=cfg,
        clock=FrozenClock("2026-04-01T00:00:00.000000Z"),
        actor=Actor(kind="system", name="test"),
    )
    ctx.conn = conn
    ctx.store = store

    res = roll_forward(ctx, through="2026-03-31")
    assert res.status == "ok"
    assert res.counts["returns_updated"] == 1

    row = conn.execute(
        "SELECT ret_gross, ret_net, n_positions FROM portfolio_returns "
        "WHERE portfolio_id = ? AND month_end = '2026-03-31'",
        (portfolio_id,),
    ).fetchone()
    assert row is not None

    tri0 = 100.0
    tri_mid = tri0 * 1.0 * (97.0 + 3.0) / 100.0
    tri_end = tri_mid * 1.0 * (108.0 + 0.0) / 97.0
    expected_ret = tri_end / tri0 - 1.0

    assert expected_ret != pytest.approx(0.0, abs=1e-6)
    assert row["ret_gross"] == pytest.approx(expected_ret, abs=1e-9)
    assert row["n_positions"] == 1

    # No pre-inception rows either (existing invariant, re-asserted here).
    pre_rows = conn.execute(
        "SELECT count(*) FROM portfolio_returns WHERE portfolio_id = ? AND month_end < '2026-03-08'",
        (portfolio_id,),
    ).fetchone()[0]
    assert pre_rows == 0


def test_unrelated_security_price_update_does_not_bump_revision(labels_case):
    """price_manifest_sha (and therefore evidence_hash) must be scoped to a
    cohort's own group members, not the whole price store: adding a new
    price row for a security that is NOT a member of this cohort must not
    create a spurious second revision for securities whose own r_log/status
    never changed.

    This reproduces the exact failure the adversarial review found in
    PriceStore.manifest_hash(vintage_at): that hash is a SHA256 over EVERY
    row in prices_daily up to vintage_at, so any monthly ingest anywhere in
    the ~500-name universe changes it -- and folding it into evidence_hash
    forced a brand-new label revision for every security/horizon/cohort on
    every run, even with byte-identical prices for that cohort.
    """
    ctx, conn = labels_case

    # Run once: sid 1 and sid 2 get their first (only) revision.
    mature(ctx, through="2026-06-30")
    first_rows = conn.execute(
        "SELECT security_id, revision, evidence_hash FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3 AND security_id IN (1, 2) "
        "ORDER BY security_id"
    ).fetchall()
    assert [r["revision"] for r in first_rows] == [1, 1]
    first_hashes = {r["security_id"]: r["evidence_hash"] for r in first_rows}

    # An UNRELATED security (999, not a cohort member at all) gets a new,
    # later-observed price row in the store -- nothing about sid 1/2's own
    # prices changes.
    price_conn = connect(ctx.cfg.paths.prices_db)
    with price_conn:
        _insert_security(conn, 999)  # not added to the cohort/scores
        price_conn.execute(
            "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
            "VALUES (999, '2026-06-15', 42.0, 0.0, 1.0, '2026-06-16T00:00:00.000000Z', 'cap', 'sha')"
        )

    # Run mature() again at a strictly later vintage/clock -- the price
    # vintage this pass reads at is genuinely later, so a whole-store hash
    # WOULD flip even though sid 1/2's own data is untouched.
    ctx2 = RunContext(
        as_of="2026-06-30",
        kind="test",
        track="live",
        cfg=ctx.cfg,
        clock=FrozenClock("2026-07-02T00:00:00.000000Z"),
        actor=Actor(kind="system", name="test"),
    )
    ctx2.conn = conn
    ctx2.store = PriceStore(ctx.cfg.paths.prices_db, state_conn=conn)
    ctx2.run_id = 1

    res2 = mature(ctx2, through="2026-06-30")
    assert res2.status == "ok"

    second_rows = conn.execute(
        "SELECT security_id, revision, evidence_hash, r_log FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3 AND security_id IN (1, 2) "
        "ORDER BY security_id"
    ).fetchall()
    # Still exactly one row (one revision) per security -- no spurious
    # duplicate revision was inserted.
    assert len(second_rows) == 2
    for r in second_rows:
        assert r["revision"] == 1
        assert r["evidence_hash"] == first_hashes[r["security_id"]]

    total_rows = conn.execute(
        "SELECT count(*) FROM labels WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3 "
        "AND security_id IN (1, 2)"
    ).fetchone()[0]
    assert total_rows == 2


def test_own_price_correction_still_bumps_revision(labels_case):
    """The scoped price hash must not over-suppress: a genuine correction to
    one of the cohort's OWN members' store prices must still produce a new
    revision for the affected group (MASTER_SPEC 2.3 'revisions are
    visible'), even though an unrelated security's price never does."""
    ctx, conn = labels_case

    mature(ctx, through="2026-06-30")
    rev1 = conn.execute(
        "SELECT security_id, revision FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3 ORDER BY security_id"
    ).fetchall()
    assert [r["revision"] for r in rev1] == [1, 1, 1]

    # A genuine correction to sid 1's OWN endpoint close, observed later.
    price_conn = connect(ctx.cfg.paths.prices_db)
    with price_conn:
        price_conn.execute(
            "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
            "VALUES (1, '2026-06-30', 115.0, 0.0, 1.0, '2026-07-05T00:00:00.000000Z', 'cap2', 'sha2')"
        )

    ctx2 = RunContext(
        as_of="2026-06-30",
        kind="test",
        track="live",
        cfg=ctx.cfg,
        clock=FrozenClock("2026-07-06T00:00:00.000000Z"),
        actor=Actor(kind="system", name="test"),
    )
    ctx2.conn = conn
    ctx2.store = PriceStore(ctx.cfg.paths.prices_db, state_conn=conn)
    ctx2.run_id = 1

    res2 = mature(ctx2, through="2026-06-30")
    assert res2.status == "ok"

    rev2 = conn.execute(
        "SELECT security_id, revision, r_log FROM labels "
        "WHERE cohort_id = 'live:2026-03-31' AND horizon_m = 3 AND revision = 2 ORDER BY security_id"
    ).fetchall()
    # sid 1, 2, 3 share one sector group in this fixture, so the correction
    # to sid 1's own price appends a group-wide revision 2 for all three.
    assert [r["security_id"] for r in rev2] == [1, 2, 3]

    sid1_rev2 = next(r for r in rev2 if r["security_id"] == 1)
    expected_r_log = math.log(115.0 / 95.0)
    assert sid1_rev2["r_log"] == pytest.approx(expected_r_log, abs=1e-9)


def test_net_selection_spread_signature_matches_interfaces_c08():
    """INTERFACES.md C08 contracts net_selection_spread(conn, cohort_id,
    subject_kind, subject_id, subject_version, horizon_m) -> dict with no
    extra parameters. A prior revision silently added an optional keyword-
    only ``cfg`` -- a real (if backward-compatible) deviation from the
    published public signature. Assert the signature is exactly the six
    contracted parameters, nothing more."""
    params = list(inspect.signature(net_selection_spread).parameters)
    assert params == [
        "conn",
        "cohort_id",
        "subject_kind",
        "subject_id",
        "subject_version",
        "horizon_m",
    ]


def test_settle_no_fill_on_non_session_date(cfg):
    """An order whose earliest_exec_at falls on a date the price store never
    observed a close for stays pending, even though nearby dates do have
    prices -- settle() never moves the execution date to a neighbour."""
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    price_conn = connect(cfg.paths.prices_db)
    apply_schema(price_conn, kind="prices")

    sid = 20
    cohort_id = "live:2026-04-01"
    portfolio_id = "P_NOFILL"
    order_id = "O_NOFILL"

    with conn:
        _insert_security(conn, sid)
        _insert_portfolio_scaffold(conn, cohort_id, portfolio_id)
        conn.execute(
            "INSERT INTO portfolio_orders (order_id, portfolio_id, cohort_id, security_id, created_at, "
            "earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
            "VALUES (?, ?, ?, ?, '2026-04-01T09:00:00.000000Z', '2026-04-04T10:00:00.000000Z', "
            "'rebalance', 'buy', 0.1, 'pending', 'A', NULL)",
            (order_id, portfolio_id, cohort_id, sid),
        )

    with price_conn:
        # Sessions exist either side of 2026-04-04 (a Saturday), never on it.
        _insert_price(price_conn, sid, "2026-04-03", 100.0)
        _insert_price(price_conn, sid, "2026-04-06", 101.0)

    store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    ctx = RunContext(
        as_of="2026-04-06",
        kind="test",
        track="live",
        cfg=cfg,
        clock=FrozenClock("2026-04-07T00:00:00.000000Z"),
        actor=Actor(kind="system", name="test"),
    )
    ctx.conn = conn
    ctx.store = store

    res = settle(ctx, through="2026-04-06T10:00:00.000000Z")
    assert res.counts["fills"] == 0

    order = conn.execute(
        "SELECT status FROM portfolio_orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    assert order["status"] == "pending"

    trades = conn.execute(
        "SELECT count(*) FROM portfolio_trades WHERE order_id = ?", (order_id,)
    ).fetchone()[0]
    assert trades == 0
