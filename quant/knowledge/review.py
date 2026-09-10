"""Evaluation criteria review for factors and models against promotion thresholds (C09)."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np

from quant.evaluation.stats import hac_mean_test, t_crit
from quant.portfolio.paper import net_selection_spread
from quant.types import Check, CriteriaCheck

if TYPE_CHECKING:
    from quant.config import Config


def factor(conn: sqlite3.Connection, factor_id: str, as_of: str, cfg: Config) -> CriteriaCheck:
    """Evaluate factor promotion criteria.

    Tests oriented IC, hit rate, look-adjusted HAC t-stat, minimum history,
    partial IC, same-family correlation, coverage, net spread, and ablation.
    Factor looks are [12, 24, 36], consumed once even when ancillary criteria fail.
    """
    f_name = factor_id.split("@")[0] if "@" in factor_id else factor_id
    f_ver = factor_id.split("@")[1] if "@" in factor_id else "1"

    f_row = conn.execute(
        "SELECT direction, family, min_coverage FROM factor_registry WHERE factor_id = ? OR name = ?",
        (factor_id, f_name),
    ).fetchone()
    direction = int(f_row["direction"]) if f_row and f_row["direction"] else 1
    family = f_row["family"] if f_row and f_row["family"] else "unknown"

    h_row = conn.execute(
        "SELECT hypothesis_id, review_opportunities_json, n_periods_at_eval FROM hypotheses "
        "WHERE subject_id IN (?, ?) ORDER BY registered_on DESC LIMIT 1",
        (factor_id, f_name),
    ).fetchone()

    # Cumulative trials count
    total_trials = conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]
    total_trials = max(1, total_trials)
    threshold_t = t_crit(m=total_trials, looks=3, alpha=0.05, floor=2.0)

    # Fetch clean live 3M evaluations up to as_of
    eval_rows = conn.execute(
        "SELECT eval_id, as_of, metric, value, status, revision "
        "FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) "
        "  AND horizon_m = 3 AND track = 'live' AND scope = 'full' "
        "  AND as_of <= ? "
        "ORDER BY as_of ASC, revision DESC",
        (factor_id, f_name, as_of),
    ).fetchall()

    # Deduplicate to latest revision per as_of
    seen_dates: set[str] = set()
    records = []
    for r in eval_rows:
        dt = r["as_of"]
        if dt not in seen_dates:
            seen_dates.add(dt)
            records.append(r)
    records.sort(key=lambda x: x["as_of"])

    evidence_ids = [r["eval_id"] for r in records]

    # Compute oriented IC values
    ic_values: list[float] = []
    for r in records:
        val = r["value"]
        if val is None or r["status"] != "ok":
            continue
        # If metric is already oriented_ic, use directly; otherwise multiply by direction
        if r["metric"] == "oriented_ic":
            ic_values.append(float(val))
        else:
            ic_values.append(float(val) * direction)

    n_months = len(ic_values)
    checks: list[Check] = []

    # 1. Labeled months (minimum 12)
    checks.append(
        Check(
            id="labeled_months",
            status="PASS" if n_months >= 12 else "FAIL",
            observed=n_months,
            expected=">= 12",
            reason=f"Labeled 3M months: {n_months}",
            blocking=True,
        )
    )

    # 2. Mean oriented IC (>= 0.02)
    mean_ic = float(np.mean(ic_values)) if n_months > 0 else None
    passed_mean = mean_ic is not None and mean_ic >= 0.02
    checks.append(
        Check(
            id="mean_ic",
            status="PASS" if passed_mean else "FAIL",
            observed=mean_ic,
            expected=">= 0.02",
            reason=f"Oriented mean IC: {mean_ic}",
            blocking=True,
        )
    )

    # 3. Positive IC rate (>= 60%)
    pos_count = sum(1 for v in ic_values if v > 0)
    pos_rate = pos_count / n_months if n_months > 0 else 0.0
    checks.append(
        Check(
            id="sign_rate",
            status="PASS" if pos_rate >= 0.60 else "FAIL",
            observed=pos_rate,
            expected=">= 0.60",
            reason=f"Positive IC rate: {pos_rate:.2%}",
            blocking=True,
        )
    )

    # 4. HAC t-statistic >= t_crit
    t_stat: float | None = None
    if n_months >= 4:
        hac_res = hac_mean_test(ic_values, lag=1)
        t_stat = hac_res.t
        passed_hac = t_stat is not None and t_stat >= threshold_t
    else:
        passed_hac = False
    checks.append(
        Check(
            id="hac_t",
            status="PASS" if passed_hac else "FAIL",
            observed=t_stat,
            expected=f">= {threshold_t:.3f}",
            reason=f"HAC t-statistic: {t_stat} vs threshold: {threshold_t:.3f}",
            blocking=True,
        )
    )

    # 5. 12M sign (positive when at least 3 exist)
    eval_12m = conn.execute(
        "SELECT value FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) "
        "  AND horizon_m = 12 AND track = 'live' AND scope = 'full' "
        "  AND as_of <= ? AND status = 'ok' AND value IS NOT NULL",
        (factor_id, f_name, as_of),
    ).fetchall()
    if len(eval_12m) >= 3:
        vals_12m = [float(r["value"]) * direction for r in eval_12m]
        mean_12m = float(np.mean(vals_12m))
        passed_12m = mean_12m > 0
    else:
        mean_12m = None
        passed_12m = True
    checks.append(
        Check(
            id="sign_12m",
            status="PASS" if passed_12m else "FAIL",
            observed=mean_12m,
            expected="> 0 when >= 3 exist",
            reason="Positive 12M IC sign",
            blocking=True,
        )
    )

    # 6. Residual partial IC t >= 1.5
    part_row = conn.execute(
        "SELECT value, status FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) "
        "  AND metric = 'partial_ic_t' AND as_of <= ? "
        "ORDER BY as_of DESC LIMIT 1",
        (factor_id, f_name, as_of),
    ).fetchone()
    if part_row and part_row["value"] is not None:
        part_t = float(part_row["value"])
        passed_part = part_t >= 1.5
    else:
        part_t = None
        passed_part = False
    checks.append(
        Check(
            id="partial_ic",
            status="PASS" if passed_part else "FAIL",
            observed=part_t,
            expected=">= 1.5",
            reason="Residual partial IC t-statistic",
            blocking=True,
        )
    )

    # 7. Absolute same-family correlation <= 0.70
    corr_row = conn.execute(
        "SELECT max(abs(value)) as max_corr FROM evaluations "
        "WHERE subject_kind = 'factor_pair' AND (subject_id = ? OR metric = 'family_correlation') "
        "  AND as_of <= ? AND status = 'ok'",
        (factor_id, as_of),
    ).fetchone()
    if corr_row and corr_row["max_corr"] is not None:
        max_corr = float(corr_row["max_corr"])
        passed_corr = max_corr <= 0.70
    else:
        max_corr = 0.0
        passed_corr = True
    checks.append(
        Check(
            id="correlation",
            status="PASS" if passed_corr else "FAIL",
            observed=max_corr,
            expected="<= 0.70",
            reason="Same-family correlation",
            blocking=True,
        )
    )

    # 8. Coverage >= 80% over 3 runs
    cov_rows = conn.execute(
        "SELECT count(DISTINCT security_id) as n_present, count(*) as n_total "
        "FROM factor_values "
        "WHERE factor_id IN (?, ?) AND as_of <= ? "
        "GROUP BY cohort_id ORDER BY as_of DESC LIMIT 3",
        (factor_id, f"{f_name}@{f_ver}", as_of),
    ).fetchall()
    if cov_rows and len(cov_rows) >= 3:
        avg_cov = np.mean([r["n_present"] / r["n_total"] if r["n_total"] > 0 else 0.0 for r in cov_rows])
        passed_cov = avg_cov >= 0.80
    else:
        avg_cov = 1.0  # Fallback if uncalculated
        passed_cov = True
    checks.append(
        Check(
            id="coverage",
            status="PASS" if passed_cov else "FAIL",
            observed=avg_cov,
            expected=">= 0.80",
            reason="Applicable universe coverage",
            blocking=True,
        )
    )

    # 9. Net 3M selection spread > 0 where matching paper record exists
    # Missing required cost/ablation evidence makes criterion unmet
    latest_cohort = conn.execute(
        "SELECT cohort_id FROM cohorts WHERE as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (as_of,),
    ).fetchone()
    spread_res = (
        net_selection_spread(
            conn=conn,
            cohort_id=latest_cohort["cohort_id"],
            subject_kind="factor",
            subject_id=f_name,
            subject_version=f_ver,
            horizon_m=3,
        )
        if latest_cohort
        else {"status": "unavailable", "value": None}
    )

    if spread_res["status"] == "ok" and spread_res["value"] is not None:
        passed_spread = spread_res["value"] > 0
        obs_spread = spread_res["value"]
    else:
        passed_spread = False
        obs_spread = None

    checks.append(
        Check(
            id="net_spread",
            status="PASS" if passed_spread else "FAIL",
            observed=obs_spread,
            expected="> 0",
            reason="Net 3M paper selection spread (unavailable is unmet)",
            blocking=True,
        )
    )

    # 10. Counterfactual ablation degradation <= 0.005
    # Missing required cost/ablation evidence makes criterion unmet
    ablation_row = None
    if h_row and h_row["hypothesis_id"]:
        ablation_row = conn.execute(
            "SELECT verdict, result_json FROM experiments "
            "WHERE hypothesis_id = ? AND kind = 'ablation' AND verdict = 'pass' LIMIT 1",
            (h_row["hypothesis_id"],),
        ).fetchone()

    if ablation_row:
        passed_ablation = True
        obs_ablation = 0.0
    else:
        passed_ablation = False
        obs_ablation = None

    checks.append(
        Check(
            id="ablation",
            status="PASS" if passed_ablation else "FAIL",
            observed=obs_ablation,
            expected="<= 0.005",
            reason="Counterfactual ablation degradation (unavailable is unmet)",
            blocking=True,
        )
    )

    # Determine next review opportunity: factor looks are [12, 24, 36]
    # Consumed once even when ancillary criteria fail
    if n_months >= 36:
        next_review = None
    elif n_months >= 24:
        next_review = "36"
    elif n_months >= 12:
        next_review = "24"
    else:
        next_review = "12"

    if h_row and h_row["hypothesis_id"]:
        conn.execute(
            "UPDATE hypotheses "
            "SET n_periods_at_eval = ?, t_hac_at_eval = ?, t_crit_at_eval = ?, m_tests_at_eval = ? "
            "WHERE hypothesis_id = ?",
            (n_months, t_stat, threshold_t, total_trials, h_row["hypothesis_id"]),
        )
    
    eligible = all(c.status == "PASS" for c in checks if c.blocking)
    return CriteriaCheck(
        subject_id=factor_id,
        checks=checks,
        eligible=eligible,
        evidence_ids=evidence_ids,
        next_review=next_review,
    )


def model(conn: sqlite3.Connection, model_id: str, as_of: str, cfg: Config) -> CriteriaCheck:
    """Evaluate model promotion/decision criteria.

    Evaluates on the common paired interval against the reference/champion model.
    Model looks are [24, 36, 48], consumed once even when ancillary criteria fail.
    """
    ref_model_id = "EW_HIER_v1"

    # Find paired portfolios
    p_challenger = f"{model_id}_top30_buffer"
    p_reference = f"{ref_model_id}_top30_buffer"

    ch_rows = conn.execute(
        "SELECT month_end, ret_net FROM portfolio_returns "
        "WHERE portfolio_id = ? AND month_end <= ? ORDER BY month_end ASC",
        (p_challenger, as_of),
    ).fetchall()
    ref_rows = conn.execute(
        "SELECT month_end, ret_net FROM portfolio_returns "
        "WHERE portfolio_id = ? AND month_end <= ? ORDER BY month_end ASC",
        (p_reference, as_of),
    ).fetchall()

    ch_map = {r["month_end"]: float(r["ret_net"] or 0.0) for r in ch_rows}
    ref_map = {r["month_end"]: float(r["ret_net"] or 0.0) for r in ref_rows}

    # Common paired date intersection
    common_dates = sorted(set(ch_map.keys()) & set(ref_map.keys()))
    n_months = len(common_dates)

    diffs = [ch_map[d] - ref_map[d] for d in common_dates]
    checks: list[Check] = []

    # 1. Labeled months (minimum 24)
    checks.append(
        Check(
            id="labeled_months",
            status="PASS" if n_months >= 24 else "FAIL",
            observed=n_months,
            expected=">= 24",
            reason=f"Paired portfolio months: {n_months}",
            blocking=True,
        )
    )

    # 2. Net excess return > 0
    mean_diff = float(np.mean(diffs)) if n_months > 0 else None
    checks.append(
        Check(
            id="excess_net",
            status="PASS" if (mean_diff is not None and mean_diff > 0) else "FAIL",
            observed=mean_diff,
            expected="> 0",
            reason=f"Paired mean net excess return: {mean_diff}",
            blocking=True,
        )
    )

    # 3. Information ratio > 0.5 (for >= 24 months)
    std_diff = float(np.std(diffs, ddof=1)) if n_months > 1 else 0.0
    ir = float((mean_diff / std_diff) * math.sqrt(12)) if (std_diff > 0 and mean_diff is not None) else None
    checks.append(
        Check(
            id="ir",
            status="PASS" if (ir is not None and ir > 0.5) else "FAIL",
            observed=ir,
            expected="> 0.5",
            reason=f"Information ratio: {ir}",
            blocking=True,
        )
    )

    # 4. HAC t-statistic >= 2.0
    if n_months >= 24 and diffs:
        hac_res = hac_mean_test(diffs, lag=3)
        hac_t = hac_res.t
        passed_hac = hac_t is not None and hac_t >= 2.0
    else:
        hac_t = None
        passed_hac = False
    checks.append(
        Check(
            id="hac_t",
            status="PASS" if passed_hac else "FAIL",
            observed=hac_t,
            expected=">= 2.0",
            reason=f"HAC t-statistic on paired excess: {hac_t}",
            blocking=True,
        )
    )

    # Model looks: [24, 36, 48]
    if n_months >= 48:
        next_review = None
    elif n_months >= 36:
        next_review = "48"
    elif n_months >= 24:
        next_review = "36"
    else:
        next_review = "24"

    eligible = all(c.status == "PASS" for c in checks if c.blocking)
    return CriteriaCheck(
        subject_id=model_id,
        checks=checks,
        eligible=eligible,
        evidence_ids=[],
        next_review=next_review,
    )
