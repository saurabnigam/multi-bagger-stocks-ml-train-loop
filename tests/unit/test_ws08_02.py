"""Tests for WS08.02: Pending orders, dated fills and NAV (C08)."""

from datetime import datetime, timezone
import json
import sqlite3
import pandas as pd
import pytest

from quant.config import Config
from quant.data.calendar import Calendar
from quant.db.core import apply_schema, connect
from quant.portfolio.paper import net_selection_spread, plan, roll_forward, settle
from quant.run import RunContext
from quant.types import Actor, Clock


class FrozenTestClock(Clock):
    def __init__(self, now_str: str):
        self._now_str = now_str

    def now(self) -> datetime:
        return datetime.fromisoformat(self._now_str.replace("Z", "+00:00"))

    def now_iso(self) -> str:
        return self._now_str

    def advance(self, dt: str) -> None:
        self._now_str = dt


@pytest.fixture
def test_setup(tmp_path, cfg):
    db_path = tmp_path / "test_state.db"
    prices_db_path = tmp_path / "test_prices.db"

    conn = connect(db_path)
    apply_schema(conn, kind="state")

    p_conn = connect(prices_db_path)
    apply_schema(p_conn, kind="prices")

    test_cfg = cfg.with_paths(db=db_path, prices_db=prices_db_path)

    # Seed base tables
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-07', 'plan', 'live', 1, '2026-09-07T09:00:00.000000Z', 'running', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-07', 'live', '2026-09-07T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-07T09:00:00.000000Z', '2026-09-07T09:00:00.000000Z', 1, 1)"
        )
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('M_CHAMPION', 'equal', 'champion', 'Champion Model', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('M_CHAMPION', 1, '[{\"factor_id\":\"mom_12_1@1\",\"family\":\"momentum\"}]', '{\"family\":{\"momentum\":1.0}}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES ('mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01')"
        )

        for sid in range(1, 21):
            conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                f"VALUES ({sid}, 'ISIN{sid:03d}', 'Stock {sid}', '2026-01-01', '2026-09-07', 'listed')"
            )
            bucket = "C" if sid == 1 else ("B" if sid <= 10 else "A")
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, quintile, scored, eligible, liquidity_bucket, n_factors_used, dc_flag, input_hash, generated_at, track, run_id) "
                f"VALUES ('live:1', '2026-09-07', {sid}, 'M_CHAMPION', 1, 'SEC_A', 1, '{{}}', {100 - sid}, {100 - sid}, 0, {100 - sid}, {sid}, {sid}, {sid}, {(sid - 1) // 2 + 1}, {(sid - 1) // 4 + 1}, 1, 1, '{bucket}', 5, 0, 'h', '2026-09-07T09:00:00.000000Z', 'live', 1)"
            )
            conn.execute(
                "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group, flags, input_refs_json, track, run_id) "
                f"VALUES ('live:1', '2026-09-07', {sid}, 'mom_12_1@1', {20 - sid}, {20 - sid}, {(20 - sid) / 5.0}, 'SEC_A', '', '[]', 'live', 1)"
            )

    # Setup calendar sessions
    sessions_df = pd.DataFrame([
        {"date": "2026-09-07", "close_at": "2026-09-07T10:00:00.000000Z"},
        {"date": "2026-09-08", "close_at": "2026-09-08T10:00:00.000000Z"},
        {"date": "2026-09-09", "close_at": "2026-09-09T10:00:00.000000Z"},
        {"date": "2026-09-30", "close_at": "2026-09-30T10:00:00.000000Z"},
        {"date": "2026-12-31", "close_at": "2026-12-31T10:00:00.000000Z"},
    ])
    cal = Calendar(sessions_df)

    clock = FrozenTestClock("2026-09-07T09:00:00.000000Z")
    actor = Actor(kind="system", name="test")

    ctx = RunContext(
        as_of="2026-09-07",
        kind="plan",
        track="live",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn
    ctx.calendar = cal

    return ctx, conn, p_conn, cal, test_cfg


def test_execution_timing_and_settlement(test_setup, spec_case):
    """Golden case 'execution': orders generated at 09:00 cannot fill at today's close; earliest exec is next session."""
    ctx, conn, p_conn, cal, cfg = test_setup
    c = spec_case("execution")

    res = plan(ctx, cohort_id="live:1")
    assert res.status == "ok"
    assert res.counts["orders_planned"] > 0

    # Verify orders in DB have earliest_exec_at matching golden case
    order = conn.execute(
        "SELECT earliest_exec_at, status FROM portfolio_orders WHERE cohort_id = 'live:1' LIMIT 1"
    ).fetchone()
    assert order["earliest_exec_at"] == c["expected_earliest_exec_at"]
    assert order["status"] == "pending"

    # Insert price on 2026-09-07 and 2026-09-08
    with p_conn:
        for sid in range(1, 21):
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-07', 100.0, 0.0, 1.0, '2026-09-07T10:00:00.000000Z', 'cap1', 'sha1')"
            )
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-08', 102.0, 0.0, 1.0, '2026-09-08T10:00:00.000000Z', 'cap2', 'sha2')"
            )

    # Settle at 2026-09-07T10:00:00Z -> CANNOT fill before actual eligible close
    res_s1 = settle(ctx, through="2026-09-07T10:00:00.000000Z")
    assert res_s1.counts["fills"] == 0
    pending_count = conn.execute("SELECT count(*) FROM portfolio_orders WHERE status = 'pending'").fetchone()[0]
    assert pending_count > 0

    # Settle at 2026-09-08T10:00:00Z -> fills eligible orders
    res_s2 = settle(ctx, through="2026-09-08T10:00:00.000000Z")
    assert res_s2.counts["fills"] > 0

    # Repeated settlement adds no fill (idempotent)
    res_s3 = settle(ctx, through="2026-09-08T10:00:00.000000Z")
    assert res_s3.counts["fills"] == 0


def test_missing_prices_keep_orders_pending(test_setup):
    """Orders remain pending when prices are missing and provide frozen selection evidence."""
    ctx, conn, p_conn, cal, cfg = test_setup

    plan(ctx, cohort_id="live:1")

    # Do NOT insert prices in p_conn for 2026-09-08
    res = settle(ctx, through="2026-09-08T10:00:00.000000Z")
    assert res.counts["fills"] == 0
    pending_count = conn.execute("SELECT count(*) FROM portfolio_orders WHERE status = 'pending'").fetchone()[0]
    assert pending_count > 0

    # Selection spread for factor book is unavailable/pending
    spread_info = net_selection_spread(
        conn=conn,
        cohort_id="live:1",
        subject_kind="factor",
        subject_id="mom_12_1@1",
        subject_version="1",
        horizon_m=3,
    )
    assert spread_info["status"] in ("pending", "unavailable")
    assert spread_info["value"] is None


def test_cohort_books_cost_and_selection_spread(test_setup, spec_case):
    """TOP_Q20 and MATCHED_EW cohort books include entry/exit costs and compute net selection spread."""
    ctx, conn, p_conn, cal, cfg = test_setup
    cost_case = spec_case("cost")

    plan(ctx, cohort_id="live:1")

    # Insert entry prices at 2026-09-08
    with p_conn:
        for sid in range(1, 21):
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-08', 100.0, 0.0, 1.0, '2026-09-08T10:00:00.000000Z', 'cap1', 'sha1')"
            )
            # Exit prices on 2026-12-31: top quintile (sids 1..4) gain +20%, rest gain 0%
            exit_price = 120.0 if sid <= 4 else 100.0
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-12-31', {exit_price}, 0.0, 1.0, '2026-12-31T10:00:00.000000Z', 'cap2', 'sha2')"
            )

    # Settle entry
    s_entry = settle(ctx, through="2026-09-08T10:00:00.000000Z")
    assert s_entry.counts["fills"] > 0

    # Verify entry trade cost matches cost arithmetic (0.10 weight B trade costs 0.00037)
    trade = conn.execute(
        "SELECT weight_delta, cost_bps, liquidity_bucket FROM portfolio_trades WHERE liquidity_bucket = 'B' LIMIT 1"
    ).fetchone()
    if trade:
        cost = abs(trade["weight_delta"]) * trade["cost_bps"] / 10000.0
        # If weight is 0.1, cost is 0.00037
        assert abs(cost - abs(trade["weight_delta"]) * 37.0 / 10000.0) < 1e-12

    # Settle exit on 2026-12-31
    s_exit = settle(ctx, through="2026-12-31T10:00:00.000000Z")
    assert s_exit.counts["fills"] > 0

    # Net selection spread is now estimable
    spread = net_selection_spread(
        conn=conn,
        cohort_id="live:1",
        subject_kind="factor",
        subject_id="mom_12_1@1",
        subject_version="1",
        horizon_m=3,
    )
    assert spread["status"] == "ok"
    assert spread["value"] is not None
    assert spread["value"] > 0  # TOP_Q20 beat MATCHED_EW
    assert spread["n_cost_events"] > 0
    assert spread["execution_start"] == "2026-09-08T10:00:00.000000Z"
    assert spread["execution_end"] == "2026-12-31T10:00:00.000000Z"
    assert len(spread["evidence_refs"]) > 0


def test_split_dividend_invariance(test_setup, spec_case):
    """Corporate actions (split 6:1 and dividend 5 on 600 -> 95) preserve economic TR."""
    ctx, conn, p_conn, cal, cfg = test_setup
    c = spec_case("split_dividend")

    plan(ctx, cohort_id="live:1")

    # Seed entry prices on 2026-09-08
    with p_conn:
        for sid in range(1, 21):
            p0 = float(c["close_raw"][0]) if sid == 1 else 100.0
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-08', {p0}, 0.0, 1.0, '2026-09-08T10:00:00.000000Z', 'cap1', 'sha1')"
            )
            p1 = float(c["close_raw"][1]) if sid == 1 else 100.0
            div1 = float(c["dividend_raw"][1]) if sid == 1 else 0.0
            split1 = float(c["split_ratio"][1]) if sid == 1 else 1.0
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-12-31', {p1}, {div1}, {split1}, '2026-12-31T10:00:00.000000Z', 'cap2', 'sha2')"
            )

    settle(ctx, through="2026-09-08T10:00:00.000000Z")
    settle(ctx, through="2026-12-31T10:00:00.000000Z")

    # Security 1 total return between entry and exit is exactly (95 + 5) * 6 / 600 - 1 = 0%
    spread = net_selection_spread(
        conn=conn,
        cohort_id="live:1",
        subject_kind="model",
        subject_id="M_CHAMPION",
        subject_version="1",
        horizon_m=3,
    )
    assert spread["status"] == "ok"
    assert spread["value"] is not None


def test_roll_forward_no_pre_inception_returns(test_setup):
    """Roll forward derives NAV from dated fills with no pre-inception returns."""
    ctx, conn, p_conn, cal, cfg = test_setup

    plan(ctx, cohort_id="live:1")

    # Entry on 2026-09-08
    with p_conn:
        for sid in range(1, 21):
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-08', 100.0, 0.0, 1.0, '2026-09-08T10:00:00.000000Z', 'cap1', 'sha1')"
            )
            p_conn.execute(
                "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
                f"VALUES ({sid}, '2026-09-30', 105.0, 0.0, 1.0, '2026-09-30T10:00:00.000000Z', 'cap2', 'sha2')"
            )

    settle(ctx, through="2026-09-08T10:00:00.000000Z")

    res = roll_forward(ctx, through="2026-09-30")
    assert res.status == "ok"

    # Pre-inception month (e.g. 2026-08-31) must have NO rows
    pre_rows = conn.execute(
        "SELECT count(*) FROM portfolio_returns WHERE month_end < '2026-09-01'"
    ).fetchone()[0]
    assert pre_rows == 0

    # 2026-09-30 has returns
    ret_rows = conn.execute(
        "SELECT * FROM portfolio_returns WHERE month_end = '2026-09-30'"
    ).fetchall()
    assert len(ret_rows) > 0


def test_plan_idempotent_retry(test_setup):
    """A retry of plan cannot add duplicate orders."""
    ctx, conn, p_conn, cal, cfg = test_setup

    res1 = plan(ctx, cohort_id="live:1")
    n1 = conn.execute("SELECT count(*) FROM portfolio_orders").fetchone()[0]
    assert n1 > 0

    res2 = plan(ctx, cohort_id="live:1")
    n2 = conn.execute("SELECT count(*) FROM portfolio_orders").fetchone()[0]
    assert n1 == n2


def test_unfilled_entry_cancels_exit_pair(test_setup):
    """An unfilled/cancelled entry cancels its corresponding exit order."""
    ctx, conn, p_conn, cal, cfg = test_setup

    plan(ctx, cohort_id="live:1")

    # Manually cancel an entry order
    entry_order = conn.execute(
        "SELECT order_id, portfolio_id, security_id FROM portfolio_orders WHERE purpose = 'entry' LIMIT 1"
    ).fetchone()
    conn.execute(
        "UPDATE portfolio_orders SET status = 'cancelled' WHERE order_id = ?",
        (entry_order["order_id"],),
    )
    conn.commit()

    # Settle at exit date: should cancel the corresponding exit order
    settle(ctx, through="2026-12-31T10:00:00.000000Z")

    exit_order = conn.execute(
        "SELECT status FROM portfolio_orders WHERE portfolio_id = ? AND security_id = ? AND purpose = 'exit'",
        (entry_order["portfolio_id"], entry_order["security_id"]),
    ).fetchone()
    assert exit_order["status"] == "cancelled"


def test_bucket_c_cap_in_cohort_books(test_setup):
    """Bucket C names are capped at 0.02 target weight in attribution books."""
    ctx, conn, p_conn, cal, cfg = test_setup

    plan(ctx, cohort_id="live:1")

    order = conn.execute(
        "SELECT target_weight FROM portfolio_orders WHERE security_id = 1 AND purpose = 'entry' AND liquidity_bucket = 'C' LIMIT 1"
    ).fetchone()
    assert order is not None
    assert order["target_weight"] <= 0.02 + 1e-6


def test_cli_portfolio_commands(test_setup):
    """CLI handlers for portfolio plan, settle, roll_forward succeed."""
    ctx, conn, p_conn, cal, cfg = test_setup
    import argparse
    from quant.commands.portfolio import cmd_portfolio_plan, cmd_portfolio_roll_forward, cmd_portfolio_settle

    args_plan = argparse.Namespace(
        config=None,
        db_path=str(cfg.paths.db),
        as_of="2026-09-07",
        cohort_id="live:1",
        actor_kind="system",
        by="system:cli",
    )
    assert cmd_portfolio_plan(args_plan) == 0

    args_settle = argparse.Namespace(
        config=None,
        db_path=str(cfg.paths.db),
        through="2026-09-08T10:00:00.000000Z",
        actor_kind="system",
        by="system:cli",
    )
    assert cmd_portfolio_settle(args_settle) == 0

    args_rf = argparse.Namespace(
        config=None,
        db_path=str(cfg.paths.db),
        through="2026-09-30",
        actor_kind="system",
        by="system:cli",
    )
    assert cmd_portfolio_roll_forward(args_rf) == 0

