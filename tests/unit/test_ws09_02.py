"""Tests for WS09.02: Criteria and fixed review opportunities (C09)."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import pandas as pd
import pytest

from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.evaluation.stats import t_crit
from quant.knowledge.bootstrap import seed
from quant.knowledge.review import factor as review_factor, model as review_model
from quant.run import RunContext
from quant.types import Actor, Check, CriteriaCheck


@pytest.fixture
def golden_cases():
    cases_path = Path("docs/spec/contracts/golden_cases.json")
    with open(cases_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", data)


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test_review.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    # Base runs and cohorts
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-01', 'review', 'live', 1, '2026-09-01T09:00:00.000000Z', 'ok', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-01', 'live', '2026-09-01T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-01T09:00:00.000000Z', '2026-09-01T09:00:00.000000Z', 1, 1)"
        )
    return conn


def test_t_crit_promotion_budget_golden(golden_cases):
    """Verify t_crit matches golden case promotion_budget thresholds."""
    pb = golden_cases["promotion_budget"]
    trials = pb["trials"]  # [1, 10]
    expected = pb["expected_thresholds"]

    for m, exp in zip(trials, expected):
        tc = t_crit(m=m, looks=pb["max_looks"], alpha=pb["alpha"], floor=pb["t_floor"])
        assert pytest.approx(tc, abs=1e-5) == exp


def test_decision_vectors_test_each_criterion_independently(test_db, cfg):
    """Review checks each criterion independently and returns CriteriaCheck."""
    # Seed factor registry
    fid = "mom_12_1@1"
    with test_db:
        test_db.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES (?, 'mom_12_1', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'shadow', '2026-01-01', '2026-01-01')",
            (fid,),
        )
        test_db.execute(
            "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget, status, md_path) "
            "VALUES ('momentum', '[12, 24, 36]', 'fsha', 'H-2026-001', 'factor', ?, 'Mom Factor', 'Statement', 1, 3, 'ic', 'ic >= 0.02', 'ic < 0', '2026-01-01', 'system', '2026-01-31', 'csha', 2026, 1, 0, 'open', 'h.md')",
            (fid,),
        )

        # 12 months of evaluations
        dates = [f"2026-{m:02d}-28" for m in range(1, 13)]
        for i, dt in enumerate(dates):
            test_db.execute(
                "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, window_start, window_end, evidence_hash, revision) "
                "VALUES (?, 1, '2026-09-01T00:00:00Z', 'factor', 'mom_12_1', '1', ?, 3, 'full', 'live', 'oriented_ic', 0.04, 100, 33.3, 'ok', 'spearman', '', '', ?, 1)",
                (100 + i, dt, f"ev_{i}"),
            )

    check_res = review_factor(test_db, factor_id=fid, as_of="2026-12-31", cfg=cfg)
    assert isinstance(check_res, CriteriaCheck)
    assert check_res.subject_id == fid

    check_ids = {c.id for c in check_res.checks}
    # Verify required criteria IDs are present
    assert "mean_ic" in check_ids
    assert "sign_rate" in check_ids
    assert "hac_t" in check_ids
    assert "labeled_months" in check_ids
    assert "net_spread" in check_ids
    assert "ablation" in check_ids


def test_positive_oriented_ic_good_for_both_raw_directions(test_db, golden_cases, cfg):
    """Positive oriented IC passes mean_ic check for negative raw direction factor."""
    neg_case = golden_cases["negative_direction"]
    fid = "vol_252@1"

    with test_db:
        test_db.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES (?, 'vol_252', 1, 'low_risk', ?, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'shadow', '2026-01-01', '2026-01-01')",
            (fid, neg_case["direction"]),
        )
        test_db.execute(
            "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget, status, md_path) "
            "VALUES ('low_risk', '[12, 24, 36]', 'fsha', 'H-2026-002', 'factor', ?, 'Vol Factor', 'Statement', 1, 3, 'ic', 'ic >= 0.02', 'ic < 0', '2026-01-01', 'system', '2026-01-31', 'csha', 2026, 2, 0, 'open', 'h.md')",
            (fid,),
        )

        # 12 months with oriented IC = 0.035 (positive and >= 0.02)
        for i in range(1, 13):
            dt = f"2026-{i:02d}-28"
            test_db.execute(
                "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, window_start, window_end, evidence_hash, revision) "
                "VALUES (?, 1, '2026-09-01T00:00:00Z', 'factor', 'vol_252', '1', ?, 3, 'full', 'live', 'oriented_ic', 0.035, 100, 33.3, 'ok', 'spearman', '', '', ?, 1)",
                (200 + i, dt, f"ev_vol_{i}"),
            )

    check_res = review_factor(test_db, factor_id=fid, as_of="2026-12-31", cfg=cfg)
    mean_check = next(c for c in check_res.checks if c.id == "mean_ic")
    assert mean_check.status == "PASS"
    assert mean_check.observed >= 0.02


def test_unavailable_cost_and_ablation_is_unmet(test_db, cfg):
    """Missing net selection spread or ablation makes the criterion unmet."""
    fid = "roce@1"
    with test_db:
        test_db.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES (?, 'roce', 1, 'quality', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'shadow', '2026-01-01', '2026-01-01')",
            (fid,),
        )
        test_db.execute(
            "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget, status, md_path) "
            "VALUES ('quality', '[12, 24, 36]', 'fsha', 'H-2026-003', 'factor', ?, 'Roce Factor', 'Statement', 1, 3, 'ic', 'ic >= 0.02', 'ic < 0', '2026-01-01', 'system', '2026-01-31', 'csha', 2026, 3, 0, 'open', 'h.md')",
            (fid,),
        )

        for i in range(1, 13):
            test_db.execute(
                "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, window_start, window_end, evidence_hash, revision) "
                "VALUES (?, 1, '2026-09-01T00:00:00Z', 'factor', 'roce', '1', ?, 3, 'full', 'live', 'oriented_ic', 0.05, 100, 33.3, 'ok', 'spearman', '', '', ?, 1)",
                (300 + i, f"2026-{i:02d}-28", f"ev_roce_{i}"),
            )

    check_res = review_factor(test_db, factor_id=fid, as_of="2026-12-31", cfg=cfg)
    spread_check = next(c for c in check_res.checks if c.id == "net_spread")
    assert spread_check.status == "FAIL"

    ablation_check = next(c for c in check_res.checks if c.id == "ablation")
    assert ablation_check.status == "FAIL"

    # Overall eligible must be False when cost/ablation evidence is missing
    assert check_res.eligible is False


def test_factor_look_consumed_once_even_when_ancillary_fail(test_db, cfg):
    """Review at look 12 consumes that look; next review is 24 even if ancillary criteria fail."""
    fid = "accruals@1"
    with test_db:
        test_db.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES (?, 'accruals', 1, 'quality', -1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'shadow', '2026-01-01', '2026-01-01')",
            (fid,),
        )
        test_db.execute(
            "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget, status, md_path) "
            "VALUES ('quality', '[12, 24, 36]', 'fsha', 'H-2026-004', 'factor', ?, 'Accruals Factor', 'Statement', 1, 3, 'ic', 'ic >= 0.02', 'ic < 0', '2026-01-01', 'system', '2026-01-31', 'csha', 2026, 4, 0, 'open', 'h.md')",
            (fid,),
        )

        for i in range(1, 13):
            test_db.execute(
                "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, window_start, window_end, evidence_hash, revision) "
                "VALUES (?, 1, '2026-09-01T00:00:00Z', 'factor', 'accruals', '1', ?, 3, 'full', 'live', 'oriented_ic', 0.04, 100, 33.3, 'ok', 'spearman', '', '', ?, 1)",
                (400 + i, f"2026-{i:02d}-28", f"ev_acc_{i}"),
            )

    res1 = review_factor(test_db, factor_id=fid, as_of="2026-12-31", cfg=cfg)
    assert res1.eligible is False  # Failed ancillary checks
    assert res1.next_review == "24"  # Look 12 consumed; next look is 24


def test_model_review_paired_interval_and_looks(test_db, cfg):
    """Model review evaluates on the common paired interval and uses looks 24/36/48."""
    mid = "IC_SHRUNK_v1"
    ref_mid = "EW_HIER_v1"

    with test_db:
        test_db.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES (?, 'shrink', 'challenger', 'Shrunk Model', '{}', '2024-01-01')",
            (mid,),
        )
        test_db.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES (?, 'equal', 'champion', 'Champion Model', '{}', '2024-01-01')",
            (ref_mid,),
        )
        test_db.execute(
            "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget, status, md_path) "
            "VALUES ('composite', '[24, 36, 48]', 'msha', 'H-2024-001', 'model', ?, 'Shrunk Model', 'Statement', 1, 3, 'excess_net', 'excess_net > 0', 'excess_net <= 0', '2024-01-01', 'system', '2024-01-31', 'mcode', 2024, 1, 0, 'open', 'h_m.md')",
            (mid,),
        )

        dates = pd.date_range("2024-01-31", periods=24, freq="ME")
        for i, dt in enumerate(dates):
            m_end = dt.strftime("%Y-%m-%d")
            # 24 months of returns for both models
            test_db.execute(
                "INSERT INTO portfolio_returns (portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES ('IC_SHRUNK_v1_top30_buffer', ?, 1, ?, '2026-09-01T00:00:00Z', 0.025, 0.1, 0.001, 0.024, 0.023, 0.015, 30, '1')",
                (m_end, f"ev_shrunk_{i}"),
            )
            test_db.execute(
                "INSERT INTO portfolio_returns (portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, n_positions, cost_model_version) "
                "VALUES ('EW_HIER_v1_top30_buffer', ?, 1, ?, '2026-09-01T00:00:00Z', 0.016, 0.05, 0.0005, 0.0155, 0.015, 0.015, 30, '1')",
                (m_end, f"ev_hier_{i}"),
            )

    check_res = review_model(test_db, model_id=mid, as_of="2025-12-31", cfg=cfg)
    assert isinstance(check_res, CriteriaCheck)
    assert check_res.subject_id == mid
    assert check_res.next_review == "36"  # Look 24 consumed; next look is 36
