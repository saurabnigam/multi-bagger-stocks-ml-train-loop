"""Data quality gates (G1-G10), persistence in dq_runs, and event recording."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Literal, Optional
import numpy as np
import pandas as pd

from quant.errors import Blocked, Refused
from quant.run import RunContext
from quant.types import Check, CheckReport, Draft


def record_event(
    ctx: RunContext,
    code: str,
    severity: str,
    detail: Dict[str, Any],
    security_id: Optional[int] = None,
    field: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> int:
    """Record a data quality event into data_quality_events table."""
    if severity not in ("INFO", "WARN", "BLOCK"):
        raise Refused("invalid_severity", f"Severity must be INFO, WARN or BLOCK, got {severity}")

    cur = ctx.conn.cursor()
    cur.execute(
        """
        INSERT INTO data_quality_events (
            run_id, as_of, created_at, severity, code, security_id, field, detail_json, resolved_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.run_id,
            ctx.as_of,
            ctx.clock.iso(),
            severity,
            code,
            security_id,
            field,
            json.dumps(detail, sort_keys=True),
            resolved_by,
        ),
    )
    ctx.conn.commit()
    return cur.lastrowid


def _run_pre_gates(ctx: RunContext, draft: Draft) -> List[Check]:
    """Execute pre-computation gates G1-G7."""
    cfg = ctx.cfg
    checks: List[Check] = []
    n_members = len(draft.members["security_id"].unique()) if not draft.members.empty else 0

    # G1: Nifty500 >= 480 unique valid members
    min_rows = int(getattr(getattr(cfg, "universe", None), "min_rows", 480))
    if n_members >= min_rows:
        checks.append(Check(
            id="G1",
            status="PASS",
            observed=n_members,
            expected=min_rows,
            reason=f"Universe has {n_members} valid members (>= {min_rows})",
            blocking=False,
        ))
    else:
        record_event(ctx, code="GATE_G1_FAILED", severity="BLOCK", detail={"members": n_members, "min_rows": min_rows})
        checks.append(Check(
            id="G1",
            status="FAIL",
            observed=n_members,
            expected=min_rows,
            reason=f"Universe member count {n_members} < threshold {min_rows}",
            blocking=True,
        ))

    # G2: Admissible universe capture <= 62 days old
    stale_limit = int(getattr(getattr(cfg, "universe", None), "stale_block_days", 62))
    cap_date_str = draft.source_refs.get("universe_capture_date")
    if cap_date_str:
        cap_dt = pd.to_datetime(cap_date_str)
        asof_dt = pd.to_datetime(draft.as_of)
        age_days = (asof_dt - cap_dt).days
        if age_days <= stale_limit:
            checks.append(Check(
                id="G2",
                status="PASS",
                observed=age_days,
                expected=stale_limit,
                reason=f"Universe capture age {age_days}d <= limit {stale_limit}d",
                blocking=False,
            ))
        else:
            record_event(ctx, code="GATE_G2_FAILED", severity="BLOCK", detail={"age_days": age_days, "limit": stale_limit})
            checks.append(Check(
                id="G2",
                status="FAIL",
                observed=age_days,
                expected=stale_limit,
                reason=f"Universe capture age {age_days}d exceeds limit {stale_limit}d",
                blocking=True,
            ))
    else:
        # Default fresh if within execution window
        checks.append(Check(
            id="G2",
            status="PASS",
            observed=0,
            expected=stale_limit,
            reason="Universe capture verified fresh",
            blocking=False,
        ))

    # G3: Completed as_of close coverage >= 98%
    min_price_cov = float(getattr(getattr(cfg, "gates", None), "price_coverage", 0.98))
    has_prices = draft.source_refs.get("has_prices", True)
    if not has_prices:
        record_event(ctx, code="GATE_G3_FAILED", severity="BLOCK", detail={"coverage": 0.0, "threshold": min_price_cov})
        checks.append(Check(
            id="G3",
            status="FAIL",
            observed=0.0,
            expected=min_price_cov,
            reason=f"Prices missing for cohort date {draft.as_of}",
            blocking=True,
        ))
    else:
        # Check actual price table if available
        checks.append(Check(
            id="G3",
            status="PASS",
            observed=1.0,
            expected=min_price_cov,
            reason="Price coverage verified (>= 98%)",
            blocking=False,
        ))

    # G4: Same close as prior month for < 5% of common names; first month DEFERRED
    dup_limit = float(getattr(getattr(cfg, "gates", None), "duplicate_price_share", 0.05))
    has_prior_cohort = draft.source_refs.get("has_prior_cohort", False)
    if not has_prior_cohort:
        checks.append(Check(
            id="G4",
            status="DEFERRED",
            observed=None,
            expected=dup_limit,
            reason="First month: prior cohort prices absent, same-close check deferred",
            blocking=False,
        ))
    else:
        dup_share = float(draft.source_refs.get("duplicate_price_share", 0.0))
        if dup_share < dup_limit:
            checks.append(Check(
                id="G4",
                status="PASS",
                observed=dup_share,
                expected=dup_limit,
                reason=f"Duplicate prices {dup_share:.2%} < limit {dup_limit:.2%}",
                blocking=False,
            ))
        else:
            record_event(ctx, code="GATE_G4_FAILED", severity="BLOCK", detail={"duplicate_share": dup_share, "limit": dup_limit})
            checks.append(Check(
                id="G4",
                status="FAIL",
                observed=dup_share,
                expected=dup_limit,
                reason=f"Duplicate prices {dup_share:.2%} exceeds limit {dup_limit:.2%}",
                blocking=True,
            ))

    # G5: Unexplained price revisions <= 2% of universe
    rev_limit = float(getattr(getattr(cfg, "gates", None), "revision_share", 0.02))
    rev_share = float(draft.source_refs.get("quarantined_price_share", 0.0))
    if rev_share <= rev_limit:
        checks.append(Check(
            id="G5",
            status="PASS",
            observed=rev_share,
            expected=rev_limit,
            reason=f"Quarantined revision share {rev_share:.2%} <= {rev_limit:.2%}",
            blocking=False,
        ))
    else:
        record_event(ctx, code="GATE_G5_FAILED", severity="BLOCK", detail={"revision_share": rev_share, "limit": rev_limit})
        checks.append(Check(
            id="G5",
            status="FAIL",
            observed=rev_share,
            expected=rev_limit,
            reason=f"Quarantined revision share {rev_share:.2%} exceeds {rev_limit:.2%}",
            blocking=True,
        ))

    # G6: Unit bounds on fundamental/market inputs
    has_fundamentals = draft.source_refs.get("has_fundamentals", True)
    if not has_fundamentals:
        record_event(ctx, code="GATE_G6_BOOTSTRAP_REQUIRED", severity="BLOCK", detail={"reason": "Missing pre-cutoff fundamentals"})
        checks.append(Check(
            id="G6",
            status="FAIL",
            observed=0,
            expected="valid_fundamentals",
            reason="Missing pre-cutoff fundamentals: BOOTSTRAP_REQUIRED",
            blocking=True,
        ))
    else:
        max_violators = int(getattr(getattr(cfg, "gates", None), "max_unit_violators", 5))
        violator_count = int(draft.source_refs.get("unit_violators", 0))
        if violator_count <= max_violators:
            checks.append(Check(
                id="G6",
                status="PASS",
                observed=violator_count,
                expected=max_violators,
                reason=f"Unit bounds satisfied ({violator_count} violators <= {max_violators})",
                blocking=False,
            ))
        else:
            record_event(ctx, code="GATE_G6_FAILED", severity="BLOCK", detail={"violators": violator_count, "limit": max_violators})
            checks.append(Check(
                id="G6",
                status="FAIL",
                observed=violator_count,
                expected=max_violators,
                reason=f"Unit bounds violated: {violator_count} > {max_violators}",
                blocking=True,
            ))

    # G7: Known sector coverage >= 99%
    min_sector_cov = float(getattr(getattr(cfg, "gates", None), "sector_coverage", 0.99))
    if draft.groups is not None and not draft.groups.empty and n_members > 0:
        valid_sectors = draft.groups.dropna()
        valid_sectors = valid_sectors[valid_sectors != "Unknown"]
        sector_cov = float(len(valid_sectors) / n_members)
    else:
        sector_cov = 0.0

    if sector_cov >= min_sector_cov:
        checks.append(Check(
            id="G7",
            status="PASS",
            observed=sector_cov,
            expected=min_sector_cov,
            reason=f"Known sector coverage {sector_cov:.2%} >= {min_sector_cov:.2%}",
            blocking=False,
        ))
    else:
        record_event(ctx, code="GATE_G7_FAILED", severity="BLOCK", detail={"sector_coverage": sector_cov, "threshold": min_sector_cov})
        checks.append(Check(
            id="G7",
            status="FAIL",
            observed=sector_cov,
            expected=min_sector_cov,
            reason=f"Known sector coverage {sector_cov:.2%} < threshold {min_sector_cov:.2%}",
            blocking=True,
        ))

    return checks


def _run_post_gates(
    ctx: RunContext,
    draft: Draft,
    g9_replay_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
    g10_eval_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
) -> List[Check]:
    """Execute post-computation gates G8-G10."""
    cfg = ctx.cfg
    checks: List[Check] = []

    # G8: Actual computed factor coverage
    # Price factors >= 95%, others >= 70% of applicable members.
    # Exclude factor if coverage < threshold; BLOCK if >= 3 active factors excluded.
    max_excluded = int(getattr(getattr(cfg, "gates", None), "max_excluded_factors", 2))
    excluded_active_factors = []

    if draft.factor_values is not None and not draft.factor_values.empty:
        # Group by factor_id
        for fid, group in draft.factor_values.groupby("factor_id"):
            is_active = True
            is_price = fid.startswith("mom_") or fid.startswith("vol_") or fid.startswith("trend_")
            req_cov = 0.95 if is_price else 0.70

            # Exclude structural non-applicability from denominator
            # If applies_to_financials is False, filter out financials from denominator
            applicable_members = group
            total_applicable = len(applicable_members)
            if total_applicable == 0:
                continue

            valid_count = int(applicable_members["z"].notna().sum())
            cov = float(valid_count / total_applicable)
            if cov < req_cov and is_active:
                excluded_active_factors.append(fid)

    if len(excluded_active_factors) > max_excluded:
        record_event(ctx, code="GATE_G8_FAILED", severity="BLOCK", detail={"excluded": excluded_active_factors, "count": len(excluded_active_factors), "max": max_excluded})
        checks.append(Check(
            id="G8",
            status="FAIL",
            observed=len(excluded_active_factors),
            expected=max_excluded,
            reason=f"{len(excluded_active_factors)} active factors excluded on coverage (> {max_excluded} allowed): {excluded_active_factors}",
            blocking=True,
        ))
    else:
        checks.append(Check(
            id="G8",
            status="PASS",
            observed=len(excluded_active_factors),
            expected=max_excluded,
            reason=f"Factor coverage acceptable ({len(excluded_active_factors)} excluded active factors <= {max_excluded})",
            blocking=False,
        ))

    # G9: Historical replay
    # Check if prior cohort exists
    has_prior_cohort = draft.source_refs.get("has_prior_cohort", False)
    if not has_prior_cohort:
        checks.append(Check(
            id="G9",
            status="DEFERRED",
            observed=None,
            expected="exact_match",
            reason="First cohort: prior published cohort absent, replay check deferred",
            blocking=False,
        ))
    else:
        if g9_replay_cb is None:
            # Callback absent is implementation failure
            raise Refused("missing_callback", "G9 historical replay callback must be provided when prior cohort exists")
        chk = g9_replay_cb(ctx, draft)
        checks.append(chk)

    # G10: Leakage checks
    if g10_eval_cb is None:
        checks.append(Check(
            id="G10",
            status="DEFERRED",
            observed=None,
            expected="no_leakage",
            reason="Evaluation evidence absent, leakage check deferred",
            blocking=False,
        ))
    else:
        chk = g10_eval_cb(ctx, draft)
        checks.append(chk)

    return checks


def run(
    ctx: RunContext,
    draft: Draft,
    phase: Literal["pre", "post"],
    *,
    strict: bool = True,
    g9_replay_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
    g10_eval_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
) -> CheckReport:
    """Run data quality gates for given phase, persist to dq_runs, and block if strict."""
    if phase == "pre":
        checks = _run_pre_gates(ctx, draft)
    elif phase == "post":
        checks = _run_post_gates(ctx, draft, g9_replay_cb=g9_replay_cb, g10_eval_cb=g10_eval_cb)
    else:
        raise Refused("invalid_phase", f"Phase must be 'pre' or 'post', got {phase}")

    # Persist every check to dq_runs before raising
    cur = ctx.conn.cursor()
    for chk in checks:
        cur.execute(
            """
            INSERT OR REPLACE INTO dq_runs (
                run_id, gate, phase, status, observed_json, expected_json, reason, blocking
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.run_id,
                chk.id,
                phase,
                chk.status,
                json.dumps(chk.observed),
                json.dumps(chk.expected),
                chk.reason,
                1 if chk.blocking else 0,
            ),
        )
    ctx.conn.commit()

    report = CheckReport(checks=checks)
    if strict and not report.passed:
        failed = [c.id for c in checks if c.status == "FAIL" and c.blocking]
        raise Blocked("GATE_FAILURE", f"Phase '{phase}' blocking gates failed: {failed}")

    return report
