"""Evaluation criteria review for factors and models against promotion thresholds (C09)."""

from __future__ import annotations

import json
import math
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np

from quant.db.core import update_control
from quant.evaluation.stats import hac_mean_test, t_crit
from quant.portfolio.paper import net_selection_spread
from quant.types import Check, CriteriaCheck

if TYPE_CHECKING:
    from quant.config import Config


def _parse_opportunities(h_row: sqlite3.Row | None, default: list[int]) -> list[int]:
    """Parse the fixed review_opportunities_json registered on the hypothesis.

    Falls back to `default` (the configured review months) when unregistered or malformed.
    """
    if h_row is not None:
        try:
            raw = h_row["review_opportunities_json"]
        except (IndexError, KeyError):
            raw = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    return sorted(int(x) for x in parsed)
            except (ValueError, TypeError):
                pass
    return sorted(default)


def _due_look(stored: int | None, n_available: int, opportunities: list[int]) -> tuple[int, bool]:
    """Determine the review window for this call.

    Returns (window_size, is_new_look). The earliest still-unconsumed opportunity that
    `n_available` has now reached is a new look, evaluated using exactly that many of the
    earliest eligible cohorts (spec 7.3). When no new opportunity is due, replay the
    previously stored look's exact window (or, if none was ever consumed, the currently
    available count) so criteria/eligibility never double-consume a look.
    """
    for opp in opportunities:
        if n_available >= opp and (stored is None or opp > stored):
            return opp, True
    if stored is not None:
        return stored, False
    return n_available, False


def factor(conn: sqlite3.Connection, factor_id: str, as_of: str, cfg: Config) -> CriteriaCheck:
    """Evaluate factor promotion criteria.

    Tests oriented IC, hit rate, look-adjusted HAC t-stat, minimum history,
    partial IC, same-family correlation, coverage, net spread, and ablation.
    Factor looks are registered at [12, 24, 36] eligible labeled cohorts (spec 7.3),
    consumed once even when ancillary criteria fail; a call that lands between
    opportunities (or after the last one) replays the previously stored look and is
    never eligible for a new look.
    """
    f_name = factor_id.split("@")[0] if "@" in factor_id else factor_id
    f_ver = factor_id.split("@")[1] if "@" in factor_id else "1"

    f_row = conn.execute(
        "SELECT direction, family, min_coverage, applies_to_financials "
        "FROM factor_registry WHERE factor_id = ? OR name = ?",
        (factor_id, f_name),
    ).fetchone()
    direction = int(f_row["direction"]) if f_row and f_row["direction"] else 1
    family = f_row["family"] if f_row and f_row["family"] else "unknown"
    applies_to_financials = bool(f_row["applies_to_financials"]) if f_row and f_row["applies_to_financials"] is not None else True

    h_row = conn.execute(
        "SELECT hypothesis_id, review_opportunities_json, n_periods_at_eval FROM hypotheses "
        "WHERE subject_id IN (?, ?) ORDER BY registered_on DESC LIMIT 1",
        (factor_id, f_name),
    ).fetchone()

    # Cumulative trials count: every hypothesis ever registered, launch set included (spec 5.3).
    total_trials = conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]
    total_trials = max(1, total_trials)
    threshold_t = t_crit(m=total_trials, looks=3, alpha=0.05, floor=2.0)

    default_opportunities = list(getattr(getattr(cfg, "budget", None), "review_labelled_months", [12, 24, 36]) or [12, 24, 36])
    opportunities = _parse_opportunities(h_row, default_opportunities)

    # Fetch clean live 3M evaluations up to as_of. evaluate.run() only ever writes scope
    # 'all' or 'eligible' (never 'full'); promotion evidence is the eligible cohort scope.
    eval_rows = conn.execute(
        "SELECT eval_id, as_of, horizon_m, metric, value, status, revision "
        "FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) "
        "  AND horizon_m = 3 AND track = 'live' AND scope = 'eligible' "
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

    # Oriented IC values, chronologically ordered, paired with their originating row so a
    # truncated look window keeps evidence_ids and HAC lag consistent with the values used.
    usable: list[tuple[sqlite3.Row, float]] = []
    for r in records:
        val = r["value"]
        if val is None or r["status"] != "ok":
            continue
        # If metric is already oriented_ic, use directly; otherwise multiply by direction
        if r["metric"] == "oriented_ic":
            oriented = float(val)
        else:
            oriented = float(val) * direction
        usable.append((r, oriented))

    n_available = len(usable)
    stored_look = int(h_row["n_periods_at_eval"]) if h_row and h_row["n_periods_at_eval"] is not None else None
    window_size, is_new_look = _due_look(stored_look, n_available, opportunities)

    usable_window = usable[:window_size]
    evidence_ids = [r["eval_id"] for r, _ in usable_window]
    ic_values = [v for _, v in usable_window]
    # Horizon comes from the evaluation rows actually used (all 3 by the WHERE clause above,
    # but read back rather than assumed); HAC lag is h-1 (spec 7.3), e.g. lag 2 for h=3.
    horizon_used = int(usable_window[0][0]["horizon_m"]) if usable_window else 3
    lag = max(0, horizon_used - 1)

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

    # 4. HAC t-statistic >= t_crit (lag = horizon - 1, e.g. lag 2 for the 3M horizon)
    t_stat: float | None = None
    if n_months >= 4:
        hac_res = hac_mean_test(ic_values, lag=lag)
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
        "  AND horizon_m = 12 AND track = 'live' AND scope = 'eligible' "
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

    # 6. Residual partial IC: HAC t (lag h-1) of the per-date residual partial IC series
    #    written by evaluate.run (metric 'partial_ic', eligible scope, 3M) must be >= 1.5.
    part_rows = conn.execute(
        "SELECT as_of, value, status, revision FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) "
        "  AND metric = 'partial_ic' AND horizon_m = 3 AND track = 'live' AND scope = 'eligible' "
        "  AND as_of <= ? ORDER BY as_of ASC, revision DESC",
        (factor_id, f_name, as_of),
    ).fetchall()
    seen_p: set[str] = set()
    part_series: list[float] = []
    for r in part_rows:
        if r["as_of"] in seen_p:
            continue
        seen_p.add(r["as_of"])
        if r["status"] == "ok" and r["value"] is not None:
            part_series.append(float(r["value"]) * direction)
    part_series = part_series[:window_size]
    if len(part_series) >= 4:
        part_t = hac_mean_test(part_series, lag=lag).t
        passed_part = part_t is not None and part_t >= 1.5
    else:
        part_t = None
        passed_part = False
    checks.append(
        Check(
            id="partial_ic",
            status="PASS" if passed_part else "FAIL",
            observed=part_t,
            expected=">= 1.5",
            reason=f"HAC t of residual partial IC over {len(part_series)} months (lag {lag}); unavailable is unmet",
            blocking=True,
        )
    )

    # 7. Absolute same-family correlation <= 0.70 (max over the last 3 cohorts' evidence
    #    written by evaluate.run: metric 'family_correlation', label-free, horizon 0)
    corr_rows = conn.execute(
        "SELECT as_of, value, status, revision FROM evaluations "
        "WHERE subject_kind = 'factor' AND subject_id IN (?, ?) AND metric = 'family_correlation' "
        "  AND track = 'live' AND as_of <= ? ORDER BY as_of DESC, revision DESC",
        (factor_id, f_name, as_of),
    ).fetchall()
    seen_c: set[str] = set()
    corr_vals: list[float] = []
    for r in corr_rows:
        if r["as_of"] in seen_c or len(seen_c) >= 3:
            continue
        seen_c.add(r["as_of"])
        if r["status"] == "ok" and r["value"] is not None:
            corr_vals.append(abs(float(r["value"])))
    if corr_vals:
        max_corr = max(corr_vals)
        passed_corr = max_corr <= 0.70
    else:
        # Missing evidence makes the criterion unmet, not a free pass.
        max_corr = None
        passed_corr = False
    checks.append(
        Check(
            id="correlation",
            status="PASS" if passed_corr else "FAIL",
            observed=max_corr,
            expected="<= 0.70",
            reason=f"Max same-family |corr| over {len(corr_vals)} recent cohorts (unavailable is unmet)",
            blocking=True,
        )
    )

    # 8. Coverage >= 80% of applicable members (nonfinancial-only denominator for
    # nonfinancial factors), averaged over up to the last 3 published runs (spec 9.4).
    cov_value_rows = conn.execute(
        "SELECT cohort_id, sector_group, z FROM factor_values "
        "WHERE factor_id IN (?, ?) AND track = 'live' AND as_of <= ? "
        "ORDER BY as_of DESC",
        (factor_id, f"{f_name}@{f_ver}", as_of),
    ).fetchall()

    cov_by_cohort: dict[str, list[tuple[str, Any]]] = {}
    cohort_order: list[str] = []
    for r in cov_value_rows:
        cid = r["cohort_id"]
        if cid not in cov_by_cohort:
            cov_by_cohort[cid] = []
            cohort_order.append(cid)
        cov_by_cohort[cid].append((r["sector_group"], r["z"]))

    cohort_coverages: list[float] = []
    for cid in cohort_order[:3]:
        rows = cov_by_cohort[cid]
        applicable = rows if applies_to_financials else [rr for rr in rows if rr[0] != "Financial Services"]
        n_app = len(applicable)
        if n_app == 0:
            continue
        n_present = sum(1 for _, z in applicable if z is not None)
        cohort_coverages.append(n_present / n_app)

    if cohort_coverages:
        avg_cov = float(np.mean(cohort_coverages))
        passed_cov = avg_cov >= 0.80
    else:
        # No computed factor_values history yet: unmet, not a free pass.
        avg_cov = None
        passed_cov = False
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

    # Next review opportunity: the first registered opportunity beyond the window just used
    # (whether that window is a freshly consumed look or a replayed prior one).
    next_review = None
    for opp in opportunities:
        if opp > window_size:
            next_review = str(opp)
            break

    # Store the look actually consumed via the allowlisted control-update path (never a raw
    # UPDATE) so a subsequent call at the same or lower cohort count cannot re-consume it.
    if is_new_look and h_row and h_row["hypothesis_id"]:
        update_control(
            SimpleNamespace(conn=conn),
            "hypotheses",
            {"hypothesis_id": h_row["hypothesis_id"]},
            {
                "n_periods_at_eval": window_size,
                "t_hac_at_eval": t_stat,
                "t_crit_at_eval": threshold_t,
                "m_tests_at_eval": total_trials,
            },
        )

    # A call that does not consume a new registered opportunity is never eligible for
    # promotion, regardless of how the replayed criteria happen to score (spec 7.3: each
    # opportunity is evaluated once; later calls before the next one create no new look).
    criteria_pass = all(c.status == "PASS" for c in checks if c.blocking)
    eligible = is_new_look and criteria_pass
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
        hac_res = hac_mean_test(diffs, lag=int(getattr(getattr(cfg, 'evaluation', None), 'portfolio_hac_lag', 3)))
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

    # Model looks are fixed at registration (cfg.budget.model_review_labelled_months, default
    # [24, 36, 48]); each is consumed once via the model's hypothesis row, like factor looks.
    opportunities = list(getattr(getattr(cfg, "budget", None), "model_review_labelled_months", None) or [24, 36, 48])
    h_row = conn.execute(
        "SELECT hypothesis_id, n_periods_at_eval FROM hypotheses WHERE subject_id = ? AND kind = 'model' "
        "ORDER BY registered_on DESC LIMIT 1",
        (model_id,),
    ).fetchone()
    stored_look = int(h_row["n_periods_at_eval"]) if h_row and h_row["n_periods_at_eval"] is not None else None
    window_size, is_new_look = _due_look(stored_look, n_months, opportunities)
    next_review = None
    for opp in opportunities:
        if opp > window_size:
            next_review = str(opp)
            break
    if is_new_look and h_row and h_row["hypothesis_id"]:
        update_control(
            SimpleNamespace(conn=conn),
            "hypotheses",
            {"hypothesis_id": h_row["hypothesis_id"]},
            {"n_periods_at_eval": window_size, "t_hac_at_eval": hac_t, "t_crit_at_eval": 2.0},
        )

    criteria_pass = all(c.status == "PASS" for c in checks if c.blocking)
    eligible = is_new_look and criteria_pass
    return CriteriaCheck(
        subject_id=model_id,
        checks=checks,
        eligible=eligible,
        evidence_ids=[],
        next_review=next_review,
    )
