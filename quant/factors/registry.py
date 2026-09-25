"""Factor registry, version-tracked hypotheses, and staging compute."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from quant.errors import Blocked, Refused
from quant.factors.base import Factor, FactorSpec
from quant.factors.controls import Beta252, Liq, Size
from quant.factors.flows import InstHoldChg3m
from quant.factors.growth import EarnMom, EpsGrowth3y, RevGrowth3y
from quant.factors.inputs import FactorInputs, build as build_inputs
from quant.factors.legacy import DcFlag
from quant.factors.low_risk import MaxRet21, Vol252
from quant.factors.momentum import Dist52wHigh, Mom12_1, Mom6_1, Rev1m, Trend200
from quant.factors.quality import Accruals, CashConversion3y, Leverage, Roce, RoeStability3y
from quant.factors.standardise import transform
from quant.factors.value import BookToPrice, DivYield, EarningsYield, FcfYield
from quant.run import RunContext
from quant.types import Draft, Result

LAUNCH_FACTOR_CLASSES: Dict[str, type[Factor]] = {
    "mom_12_1": Mom12_1,
    "trend_200": Trend200,
    "vol_252": Vol252,
    "roce": Roce,
    "accruals": Accruals,
    "cash_conversion_3y": CashConversion3y,
    "earnings_yield": EarningsYield,
    "book_to_price": BookToPrice,
    "eps_growth_3y": EpsGrowth3y,
    "earn_mom": EarnMom,
    "inst_hold_chg_3m": InstHoldChg3m,
    "mom_6_1": Mom6_1,
    "dist_52w_high": Dist52wHigh,
    "rev_1m": Rev1m,
    "max_ret_21": MaxRet21,
    "leverage": Leverage,
    "roe_stability_3y": RoeStability3y,
    "fcf_yield": FcfYield,
    "div_yield": DivYield,
    "rev_growth_3y": RevGrowth3y,
    "size": Size,
    "liq": Liq,
    "beta_252": Beta252,
    "dc_flag": DcFlag,
}

ACTIVE_LAUNCH_FACTORS = {
    "mom_12_1", "trend_200", "vol_252", "roce", "accruals",
    "cash_conversion_3y", "earnings_yield", "book_to_price",
    "eps_growth_3y", "earn_mom", "inst_hold_chg_3m"
}


def _calc_code_sha(spec: FactorSpec) -> str:
    """Deterministic hash of factor formula, direction, inputs and hypothesis."""
    content = f"{spec.name}:{spec.version}:{spec.formula}:{','.join(sorted(spec.inputs))}:{spec.direction}:{spec.hypothesis}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_bootstrap_decision(conn: sqlite3.Connection, timestamp: str, git_sha: str) -> None:
    """Ensure Tier-0 bootstrap decision exists for initial factor registration."""
    conn.execute(
        """
        INSERT OR IGNORE INTO decisions (
            decision_id, kind, tier, subject_id, title, context, options_json,
            decision, evidence_refs_json, decided_on, decided_by, approver_kind,
            status, adr_path, git_sha
        ) VALUES (
            'DEC_BOOTSTRAP', 'factor_registration', 0, 'factors', 'Bootstrap launch factor set',
            'Initial factor registration', '[]', 'approve', '[]', ?, 'system', 'system',
            'approved', 'docs/adr/0001-bootstrap.md', ?
        )
        """,
        (timestamp, git_sha),
    )


def sync(ctx: RunContext, definitions: List[FactorSpec]) -> Result:
    """Sync registered factor definitions to factor_registry and factor_status_history.
    
    Rejects changed code/helper hashes without affected version bumps.
    Assigns valid lifecycle shadow to controls and legacy diagnostics.
    """
    cur = ctx.conn.cursor()
    _ensure_bootstrap_decision(ctx.conn, ctx.clock.iso(), ctx.git_sha)

    synced_count = 0
    now_iso = ctx.clock.iso()

    for spec in definitions:
        code_sha = _calc_code_sha(spec)

        cur.execute("SELECT code_sha256, formula FROM factor_registry WHERE factor_id = ?", (spec.factor_id,))
        existing = cur.fetchone()

        if existing is not None:
            existing_sha, existing_formula = existing
            if existing_sha != code_sha or existing_formula != spec.formula:
                raise Refused(
                    "CODE_CHANGED_WITHOUT_VERSION_BUMP",
                    f"Factor {spec.factor_id} code or formula changed without version bump (sha {existing_sha} != {code_sha})",
                )
            continue
        if spec.hypothesis_id:
            cur.execute(
                """
                INSERT OR IGNORE INTO hypotheses (
                    hypothesis_id, family, review_opportunities_json, formula_sha256,
                    kind, subject_id, title, statement, expected_sign, horizon_m,
                    primary_metric, success_criterion, failure_criterion,
                    registered_on, registered_by, first_oos_as_of, budget_year,
                    sequence_in_year, counts_toward_budget, status, md_path
                ) VALUES (
                    ?, ?, '[]', ?, 'factor', ?, ?, ?, ?, ?,
                    'spearman_ic', 'ic>0', 'ic<=0',
                    ?, 'system', ?, 2026, 0, 0, 'open', ''
                )
                """,
                (
                    spec.hypothesis_id,
                    spec.family,
                    code_sha,
                    spec.factor_id,
                    spec.name,
                    spec.hypothesis,
                    spec.direction,
                    spec.horizon_m,
                    now_iso,
                    ctx.as_of,
                ),
            )

        # Determine launch lifecycle status
        if spec.family in ("control", "legacy"):
            status = "shadow"
        elif spec.name in ACTIVE_LAUNCH_FACTORS:
            status = "active"
        else:
            status = "shadow"

        cls = LAUNCH_FACTOR_CLASSES.get(spec.name)
        module_path = f"{cls.__module__}.{cls.__name__}" if cls else f"quant.factors.{spec.family}.{spec.name}"

        cur.execute(
            """
            INSERT INTO factor_registry (
                factor_id, name, version, family, direction, horizon_m, level,
                hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
                backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
                module_path, status, registered_on, first_live_as_of,
                status_changed_on, status_decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'DEC_BOOTSTRAP')
            """,
            (
                spec.factor_id,
                spec.name,
                spec.version,
                spec.family,
                spec.direction,
                spec.horizon_m,
                spec.level,
                spec.hypothesis,
                spec.formula,
                json.dumps(list(spec.inputs)),
                spec.lookback_days,
                1 if spec.applies_to_financials else 0,
                1 if spec.backfillable else 0,
                spec.min_coverage,
                spec.evidence,
                spec.hypothesis_id,
                code_sha,
                module_path,
                status,
                now_iso,
                now_iso,
            ),
        )

        cur.execute(
            """
            INSERT OR IGNORE INTO factor_status_history (
                factor_id, effective_from, status, decision_id
            ) VALUES (?, ?, ?, 'DEC_BOOTSTRAP')
            """,
            (spec.factor_id, now_iso, status),
        )
        synced_count += 1

    return Result(
        status="ok",
        counts={"synced": synced_count},
        details={"message": f"Synced {synced_count} factors"},
    )


def compute_all(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    """Compute all active, shadow, and probation factors for draft cohort.
    
    Returns staging rows only (does NOT persist into factor_values).
    Excludes future hypotheses whose registration date is after knowledge_cutoff.
    """
    cur = ctx.conn.cursor()
    cur.execute(
        """
        SELECT factor_id, name, version, family, direction, module_path
        FROM factor_registry
        WHERE registered_on <= ?
          AND status IN ('active', 'shadow', 'probation')
        ORDER BY factor_id
        """,
        (draft.knowledge_cutoff,),
    )
    factor_meta = cur.fetchall()

    empty_cols = [
        "cohort_id", "as_of", "security_id", "factor_id",
        "raw", "winsor", "z", "sector_group", "flags",
        "input_refs_json", "track", "run_id"
    ]
    if not factor_meta:
        return pd.DataFrame(columns=empty_cols)

    inputs = build_inputs(ctx, draft)
    staging_frames = []

    for fid, name, ver, family, direction, mod_path in factor_meta:
        # Resolve class
        cls = LAUNCH_FACTOR_CLASSES.get(name)
        if cls is None:
            continue

        factor_obj = cls()
        raw = factor_obj.compute(inputs)

        # Task T9 / decision D10: a nonfinancial-only factor is structurally not
        # applicable to Financial Services names (raw is already NaN for them, per
        # each factor's own _is_financial() mask) -- distinct from a security that
        # is merely missing data. Tell transform() which securities are applicable
        # so it flags not_applicable rather than folding them into small_group.
        # Same "group != Financial Services" idiom as quant.model.composite and
        # quant.data.gates use for the same denominator exclusion (MASTER_SPEC §5.2).
        if factor_obj.spec.applies_to_financials:
            applicable = None
        else:
            applicable = inputs.sector_group != "Financial Services"

        # Standardise per sector group
        std_df = transform(raw, draft.groups, direction, ctx.cfg, applicable=applicable)

        frame = pd.DataFrame({
            "cohort_id": draft.cohort_id,
            "as_of": draft.as_of,
            "security_id": inputs.members,
            "factor_id": fid,
            "raw": std_df["raw"].values,
            "winsor": std_df["winsor"].values,
            "z": std_df["z"].values,
            "sector_group": draft.groups.reindex(inputs.members).values,
            "flags": std_df["flags"].values,
            "input_refs_json": json.dumps(inputs.provenance()),
            "track": draft.track,
            "run_id": ctx.run_id,
        })
        staging_frames.append(frame)

    if not staging_frames:
        return pd.DataFrame(columns=empty_cols)

    return pd.concat(staging_frames, ignore_index=True)


def values_frame(
    conn: sqlite3.Connection,
    cohort_id: str,
    factor_ids: List[str],
) -> pd.DataFrame:
    """Retrieve factor values matrix for a given cohort and list of factor IDs.
    
    Rows are security_id, columns are factor_id with values = z.
    """
    if not factor_ids:
        return pd.DataFrame()

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in factor_ids)
    query = f"""
    SELECT security_id, factor_id, z
    FROM factor_values
    WHERE cohort_id = ?
      AND factor_id IN ({placeholders})
    ORDER BY security_id, factor_id
    """
    cur.execute(query, (cohort_id, *factor_ids))
    rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=factor_ids)

    df = pd.DataFrame(rows, columns=["security_id", "factor_id", "z"])
    pivoted = df.pivot(index="security_id", columns="factor_id", values="z")
    pivoted.index.name = "security_id"
    return pivoted.reindex(columns=factor_ids)
