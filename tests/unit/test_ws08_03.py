"""Tests for WS08.03: Matched benchmarks, spreads and scoreboard (C08)."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.config import Config
from quant.data.calendar import Calendar
from quant.db.core import apply_schema, connect
from quant.evaluation.stats import hac_mean_test
from quant.portfolio.paper import net_selection_spread
from quant.portfolio.scoreboard import compute
from quant.run import RunContext
from quant.types import Actor, Clock


@pytest.fixture
def golden_cases():
    cases_path = Path("docs/spec/contracts/golden_cases.json")
    with open(cases_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", data)


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test_scoreboard.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    # Base runs and cohorts
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-07', 'scoreboard', 'live', 1, '2026-09-07T09:00:00.000000Z', 'ok', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-07', 'live', '2026-09-07T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-07T09:00:00.000000Z', '2026-09-07T09:00:00.000000Z', 1, 1)"
        )
    return conn


def test_golden_hac_insufficient(golden_cases):
    """Verify golden case hac_insufficient produces status insufficient and None se."""
    case = golden_cases["hac_insufficient"]
    res = hac_mean_test(case["values"], lag=case["lag"])
    assert res.n == case["expected_n"]
    assert res.n_eff == case["expected_n_eff"]
    assert res.status == case["expected_status"]
    assert res.se is None
    assert res.t is None


def test_golden_cost_events_summed(test_db, golden_cases, cfg):
    """Verify cost events inside full interval are summed (golden case cost)."""
    cost_case = golden_cases["cost"]
    expected_cost = cost_case["expected_cost_fraction"]  # 0.00037

    pid = "P_COST_TEST"
    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M1', '1', 'top30_buffer', 'monthly', '2026-01-31', '1')",
            (pid,),
        )
        # 3 months, each with cost equal to expected_cost
        for i, m_end in enumerate(["2026-01-31", "2026-02-28", "2026-03-31"]):
            ev_hash = f"cost_hash_{i}"
            test_db.execute(
                "INSERT INTO portfolio_returns "
                "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, '2026-09-07T00:00:00Z', 0.02, 0.1, ?, ?, 0.015, 0.01, 30, '1')",
                (pid, m_end, ev_hash, expected_cost, 0.02 - expected_cost),
            )

    df = compute(test_db, through="2026-03-31", cfg=cfg)
    assert not df.empty
    row = df[df["portfolio_id"] == pid].iloc[0]
    assert row["n_months"] == 3
    assert pytest.approx(row["cost_drag"], abs=1e-7) == expected_cost * 3
    assert len(row["evidence_refs"]) == 3


def test_matched_ew_minus_itself_is_zero(test_db, cfg):
    """Verify comparing net with net for matched EW minus itself is strictly 0."""
    ew_pid = "M1_matched_ew"
    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M1', '1', 'cohort_matched_ew', '3M', '2026-01-31', '1')",
            (ew_pid,),
        )
        for i, m_end in enumerate(["2026-01-31", "2026-02-28", "2026-03-31"]):
            ev_hash = f"ew_hash_{i}"
            test_db.execute(
                "INSERT INTO portfolio_returns "
                "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, '2026-09-07T00:00:00Z', 0.03, 0.05, 0.001, 0.029, 0.028, 0.029, 50, '1')",
                (ew_pid, m_end, ev_hash),
            )

    df = compute(test_db, through="2026-03-31", cfg=cfg)
    assert not df.empty
    row = df[df["portfolio_id"] == ew_pid].iloc[0]
    assert row["excess_net"] == 0.0


def test_ir_absent_below_24_months_and_present_at_24(test_db, cfg):
    """IR and annualized inference are absent (None, verdict='insufficient') below 24 months."""
    pid = "P_TRACK_RECORD"
    bm_pid = "P_TRACK_RECORD_matched_ew"

    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M_EXP', '1', 'top30_buffer', 'monthly', '2024-01-31', '1')",
            (pid,),
        )
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M_EXP', '1', 'cohort_matched_ew', 'monthly', '2024-01-31', '1')",
            (bm_pid,),
        )

        dates = pd.date_range("2024-01-31", periods=24, freq="ME")
        for i, dt in enumerate(dates):
            m_end = dt.strftime("%Y-%m-%d")
            p_net = 0.02 + 0.005 * np.sin(i)
            bm_net = 0.01

            test_db.execute(
                "INSERT INTO portfolio_returns "
                "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, '2026-09-07T00:00:00Z', ?, 0.1, 0.001, ?, ?, 0.01, 30, '1')",
                (pid, m_end, f"ev_p_{i}", p_net + 0.001, p_net, p_net - 0.0005),
            )
            test_db.execute(
                "INSERT INTO portfolio_returns "
                "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, '2026-09-07T00:00:00Z', ?, 0.05, 0.0005, ?, ?, 0.01, 100, '1')",
                (bm_pid, m_end, f"ev_bm_{i}", bm_net + 0.0005, bm_net, bm_net - 0.0002),
            )

    # Test at 23 months: through month 23
    through_23 = dates[22].strftime("%Y-%m-%d")
    df_23 = compute(test_db, through=through_23, cfg=cfg)
    row_23 = df_23[df_23["portfolio_id"] == pid].iloc[0]
    assert row_23["n_months"] == 23
    assert row_23["ir"] is None
    assert row_23["hac_t"] is None
    assert row_23["ci_lo"] is None
    assert row_23["ci_hi"] is None
    assert row_23["verdict"] == "insufficient"

    # Test at 24 months: through month 24
    through_24 = dates[23].strftime("%Y-%m-%d")
    df_24 = compute(test_db, through=through_24, cfg=cfg)
    row_24 = df_24[df_24["portfolio_id"] == pid].iloc[0]
    assert row_24["n_months"] == 24
    assert row_24["ir"] is not None
    assert row_24["ir"] > 0
    assert row_24["hac_t"] is not None
    assert row_24["ci_lo"] is not None
    assert row_24["ci_hi"] is not None
    assert row_24["verdict"] in ("positive", "weak positive")


def test_no_simulated_quintile_book_means_unavailable(test_db, cfg):
    """No simulated quintile book means unavailable net spread and insufficient verdict."""
    res = net_selection_spread(
        conn=test_db,
        cohort_id="live:1",
        subject_kind="factor",
        subject_id="nonexistent",
        subject_version="1",
        horizon_m=3,
    )
    assert res["status"] == "unavailable"
    assert res["value"] is None

    # Portfolio with no benchmark and no returns
    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES ('P_UNSIM', 'factor', 'f1', '1', 'cohort_top_quintile', '3M', '2026-09-07', '1')"
        )

    df = compute(test_db, through="2026-09-07", cfg=cfg)
    row = df[df["portfolio_id"] == "P_UNSIM"].iloc[0]
    assert row["n_months"] == 0
    assert row["ret_net"] is None
    assert row["excess_net"] is None
    assert row["verdict"] == "insufficient"


def test_no_forced_top30_turnover_below_dec10_inequality(test_db, cfg):
    """Scoreboard allows TOP30 turnover to be higher than DEC10 turnover without error or forced clamp."""
    p_top30 = "P_TOP30"
    p_dec10 = "P_DEC10"

    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M1', '1', 'top30_buffer', 'monthly', '2026-01-31', '1')",
            (p_top30,),
        )
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M1', '1', 'decile', 'monthly', '2026-01-31', '1')",
            (p_dec10,),
        )

        # TOP30 has high turnover (0.60), DEC10 has lower turnover (0.20)
        test_db.execute(
            "INSERT INTO portfolio_returns "
            "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
            "VALUES (?, '2026-01-31', 1, 'h_top30', '2026-09-07T00:00:00Z', 0.05, 0.60, 0.002, 0.048, 0.046, 0.01, 30, '1')",
            (p_top30,),
        )
        test_db.execute(
            "INSERT INTO portfolio_returns "
            "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
            "VALUES (?, '2026-01-31', 1, 'h_dec10', '2026-09-07T00:00:00Z', 0.04, 0.20, 0.001, 0.039, 0.038, 0.01, 10, '1')",
            (p_dec10,),
        )

    df = compute(test_db, through="2026-01-31", cfg=cfg)
    r_top30 = df[df["portfolio_id"] == p_top30].iloc[0]
    r_dec10 = df[df["portfolio_id"] == p_dec10].iloc[0]

    assert r_top30["turnover"] == 0.60
    assert r_dec10["turnover"] == 0.20
    # Explicitly confirm no assertion or inequality forced top30 < dec10
    assert r_top30["turnover"] > r_dec10["turnover"]


def test_scoreboard_exact_columns_and_max_drawdown(test_db, cfg):
    """Verify exact 15 columns returned and correct max_drawdown computation."""
    pid = "P_DRAWDOWN"
    with test_db:
        test_db.execute(
            "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, inception, rule_version) "
            "VALUES (?, 'model', 'M1', '1', 'top30_buffer', 'monthly', '2026-01-31', '1')",
            (pid,),
        )
        # Month 1: +10% -> NAV 1.10 (peak 1.10, dd 0)
        # Month 2: -20% -> NAV 0.88 (peak 1.10, dd (1.10 - 0.88)/1.10 = 0.20)
        # Month 3: +10% -> NAV 0.968 (peak 1.10, dd (1.10 - 0.968)/1.10 = 0.12)
        returns = [0.10, -0.20, 0.10]
        dates = ["2026-01-31", "2026-02-28", "2026-03-31"]
        for i, (r, dt) in enumerate(zip(returns, dates)):
            test_db.execute(
                "INSERT INTO portfolio_returns "
                "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, '2026-09-07T00:00:00Z', ?, 0.1, 0.0, ?, ?, 0.0, 30, '1')",
                (pid, dt, f"h_dd_{i}", r, r, r),
            )

    df = compute(test_db, through="2026-03-31", cfg=cfg)
    expected_cols = [
        "portfolio_id",
        "window_start",
        "window_end",
        "n_months",
        "ret_net",
        "excess_net",
        "ir",
        "hac_t",
        "ci_lo",
        "ci_hi",
        "turnover",
        "cost_drag",
        "max_drawdown",
        "verdict",
        "evidence_refs",
    ]
    assert list(df.columns) == expected_cols

    row = df[df["portfolio_id"] == pid].iloc[0]
    assert row["window_start"] == "2026-01-31"
    assert row["window_end"] == "2026-03-31"
    assert row["n_months"] == 3
    assert pytest.approx(row["max_drawdown"], abs=1e-5) == 0.20


def test_cli_portfolio_scoreboard(test_db, tmp_path, capsys):
    """Test CLI portfolio scoreboard command handler."""
    import argparse
    from quant.commands.portfolio import cmd_portfolio_scoreboard

    # Query db path
    db_file = test_db.execute("PRAGMA database_list").fetchall()[0]["file"]

    args = argparse.Namespace(
        db_path=db_file,
        through="2026-03-31",
        config=None,
    )
    rc = cmd_portfolio_scoreboard(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "portfolio_id" in out or "No portfolios found" in out
