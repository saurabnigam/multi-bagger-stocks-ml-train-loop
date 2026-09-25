"""Model definitions, versioning, staging and invariants."""

from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.data.actions import _validate_decision
from quant.errors import Refused
from quant.model.composite import compose
from quant.model.learn import allocate_units, fit_family_weights
from quant.model.screens import apply as apply_screens
from quant.types import Check, CheckReport, Result

if TYPE_CHECKING:
    import sqlite3
    from quant.types import Draft, RunContext


LAUNCH_FACTORS = [
    {"factor_id": "mom_12_1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "trend_200", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "vol_252", "family": "low_risk", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "roce", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
    {"factor_id": "accruals", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
    {"factor_id": "cash_conversion_3y", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
    {"factor_id": "earnings_yield", "family": "value", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "book_to_price", "family": "value", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "eps_growth_3y", "family": "growth", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "earn_mom", "family": "growth", "status_weight": 1.0, "nonfinancial": False},
]

MOM_FACTORS = [
    {"factor_id": "mom_12_1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    {"factor_id": "trend_200", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
]

# Challenger models registrable into an existing database via register_challenger()
# (MASTER_SPEC 6.4 decisions D6/D7). Not part of the launch-exemption set: registering
# one requires an approved decision, and it never gets a launch hypothesis.
CHALLENGER_DEFINITIONS: dict[str, dict[str, Any]] = {
    "EW_HIER_NR_v1": {
        "kind": "equal",
        "description": "Equal-weight hierarchical composite, ranked without within-sector re-neutralisation (D6)",
        "params": {"mode": "hierarchical_nr", "sleeve_weight": 0.0},
        "weights": {"families": ["momentum", "low_risk", "quality", "value", "growth"], "mode": "hierarchical_nr", "sleeve": 0.0},
        "note": "Challenger: drops the sector-size ceiling from composite_neutral (D6)",
    },
    "EW_HIER_COV_v1": {
        "kind": "equal",
        "description": "Equal-weight hierarchical composite, coverage-scaled by sqrt(n_used/n_applicable) (D7)",
        "params": {"mode": "hierarchical_cov", "sleeve_weight": 0.0},
        "weights": {"families": ["momentum", "low_risk", "quality", "value", "growth"], "mode": "hierarchical_cov", "sleeve": 0.0},
        "note": "Challenger: shrinks thin-evidence composites toward zero (D7)",
    },
}


def seed(ctx: RunContext, bootstrap_decision_id: str) -> Result:
    """Seed initial models and frozen version-1 definitions into models and model_versions."""
    conn = ctx.conn
    as_of = ctx.as_of or "2026-09-01"
    timestamp = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{as_of}T00:00:00.000000Z"
    )

    models_data = [
        (
            "EW_HIER_v1",
            "equal",
            "champion",
            "Equal-weight hierarchical composite across active families",
            json.dumps({"mode": "hierarchical", "sleeve_weight": 0.0}),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
        (
            "EW_FLAT_v1",
            "reference",
            "reference",
            "Equal-weight flat composite across all active factors directly",
            json.dumps({"mode": "flat", "sleeve_weight": 0.0}),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
        (
            "MOM_ONLY_v1",
            "reference",
            "reference",
            "Single-family momentum reference model",
            json.dumps({"mode": "mom_only", "sleeve_weight": 0.0}),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
        (
            "IC_SHRUNK_v1",
            "shrink",
            "challenger",
            "Shrunk family-weight challenger model (k_shrink=24, min_n_eff=4)",
            json.dumps({"mode": "hierarchical", "k_shrink": 24.0, "min_n_eff": 4.0, "sleeve_weight": 0.0}),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
        (
            "EW_HIER_NR_v1",
            CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["kind"],
            "challenger",
            CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["description"],
            json.dumps(CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["params"]),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
        (
            "EW_HIER_COV_v1",
            CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["kind"],
            "challenger",
            CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["description"],
            json.dumps(CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["params"]),
            None,
            timestamp,
            bootstrap_decision_id,
        ),
    ]

    for row in models_data:
        conn.execute(
            """
            INSERT OR IGNORE INTO models
            (model_id, kind, role, description, params_json, hypothesis_id, registered_on, decision_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    versions_data = [
        (
            "EW_HIER_v1",
            1,
            json.dumps(LAUNCH_FACTORS),
            json.dumps({"families": ["momentum", "low_risk", "quality", "value", "growth"], "mode": "hierarchical", "sleeve": 0.0}),
            as_of,
            None,
            bootstrap_decision_id,
            "Initial champion",
        ),
        (
            "EW_FLAT_v1",
            1,
            json.dumps(LAUNCH_FACTORS),
            json.dumps({"mode": "flat", "sleeve": 0.0}),
            as_of,
            None,
            bootstrap_decision_id,
            "Flat composite reference",
        ),
        (
            "MOM_ONLY_v1",
            1,
            json.dumps(MOM_FACTORS),
            json.dumps({"families": ["momentum"], "mode": "mom_only", "sleeve": 0.0}),
            as_of,
            None,
            bootstrap_decision_id,
            "Momentum reference",
        ),
        (
            "IC_SHRUNK_v1",
            1,
            json.dumps(LAUNCH_FACTORS),
            json.dumps({"mode": "hierarchical", "sleeve": 0.0}),
            as_of,
            None,
            bootstrap_decision_id,
            "Initial challenger",
        ),
        (
            "EW_HIER_NR_v1",
            1,
            json.dumps(LAUNCH_FACTORS),
            json.dumps(CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["weights"]),
            as_of,
            None,
            bootstrap_decision_id,
            CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["note"],
        ),
        (
            "EW_HIER_COV_v1",
            1,
            json.dumps(LAUNCH_FACTORS),
            json.dumps(CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["weights"]),
            as_of,
            None,
            bootstrap_decision_id,
            CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["note"],
        ),
    ]

    for row in versions_data:
        conn.execute(
            """
            INSERT OR IGNORE INTO model_versions
            (model_id, version, factor_set_json, weights_json, valid_from, valid_to, decision_id, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    return Result(status="ok", counts={"models": len(models_data), "versions": len(versions_data)})


def register_challenger(ctx: RunContext, model_id: str, decision_id: str) -> Result:
    """Register a known challenger model definition into an existing database.

    Unlike the launch set seeded by seed(), a challenger registered this way requires
    its own approved decision (MASTER_SPEC 6.4): _validate_decision (the same governance
    check quant.data.actions uses for corporate actions and quality-event clearances)
    confirms the decision exists, is approved, and matches the calling actor's kind; a
    Tier >= 1 decision must additionally have been approved by a human, since registering
    a challenger is not a launch-exempt, system-decided act. Idempotent: registering the
    same model_id twice under a decision that still validates is a no-op on content.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"models": 0, "versions": 0})

    defn = CHALLENGER_DEFINITIONS.get(model_id)
    if defn is None:
        raise Refused("unknown_model", f"'{model_id}' is not a registrable challenger definition")

    decision = _validate_decision(ctx, decision_id)
    if int(decision["tier"]) >= 1 and decision["approver_kind"] != "human":
        raise Refused(
            "governance",
            f"Decision {decision_id} is Tier {decision['tier']}; registering challenger "
            f"'{model_id}' requires a human-approved decision",
        )

    as_of = ctx.as_of or "2026-09-01"
    timestamp = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{as_of}T00:00:00.000000Z"
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO models
        (model_id, kind, role, description, params_json, hypothesis_id, registered_on, decision_id)
        VALUES (?, ?, 'challenger', ?, ?, NULL, ?, ?)
        """,
        (model_id, defn["kind"], defn["description"], json.dumps(defn["params"]), timestamp, decision_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO model_versions
        (model_id, version, factor_set_json, weights_json, valid_from, valid_to, decision_id, note)
        VALUES (?, 1, ?, ?, ?, NULL, ?, ?)
        """,
        (model_id, json.dumps(LAUNCH_FACTORS), json.dumps(defn["weights"]), as_of, decision_id, defn["note"]),
    )

    return Result(status="ok", counts={"models": 1, "versions": 1}, details={"model_id": model_id, "decision_id": decision_id})


def definition_at(conn: sqlite3.Connection, model_id: str, as_of: str) -> dict[str, Any]:
    """Retrieve version-frozen model definition active as of the given date."""
    query = """
        SELECT m.model_id, m.kind, m.role, m.description, m.params_json,
               v.version, v.factor_set_json, v.weights_json, v.valid_from, v.valid_to, v.decision_id
        FROM models m
        JOIN model_versions v ON m.model_id = v.model_id
        WHERE m.model_id = ?
          AND v.valid_from <= ?
          AND (v.valid_to IS NULL OR v.valid_to >= ?)
        ORDER BY v.version DESC
        LIMIT 1
    """
    row = conn.execute(query, (model_id, as_of, as_of)).fetchone()
    if row is None:
        raise KeyError(f"No definition found for model {model_id} as of {as_of}")

    return {
        "model_id": row[0],
        "kind": row[1],
        "role": row[2],
        "description": row[3],
        "params": json.loads(row[4]),
        "version": row[5],
        "factor_set": json.loads(row[6]),
        "weights": json.loads(row[7]),
        "valid_from": row[8],
        "valid_to": row[9],
        "decision_id": row[10],
    }


def score_all(
    ctx: RunContext, draft: Draft, family_ic_history: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score all non-retired models against the current draft and return (scores, model_weights)."""
    conn = ctx.conn
    as_of = draft.as_of

    # Retrieve non-retired model IDs
    if conn is not None:
        m_rows = conn.execute(
            "SELECT model_id FROM models WHERE role != 'retired' ORDER BY model_id"
        ).fetchall()
        model_ids = [r[0] for r in m_rows]
    else:
        model_ids = ["EW_HIER_v1", "EW_FLAT_v1", "MOM_ONLY_v1", "IC_SHRUNK_v1"]

    # Pivot factor z-scores
    factor_df = draft.factor_values
    if not factor_df.empty:
        # Normalize factor_id without version suffix if needed
        z_pivot = factor_df.pivot(index="security_id", columns="factor_id", values="z")
        # Also create copies with base factor names if versioned (e.g. mom_12_1@1 -> mom_12_1)
        for col in list(z_pivot.columns):
            base = col.split("@")[0]
            if base not in z_pivot.columns:
                z_pivot[base] = z_pivot[col]
    else:
        z_pivot = pd.DataFrame(index=draft.members.index if draft.members is not None else [])

    if draft.members is not None:
        z_pivot = z_pivot.reindex(draft.members.index)

    all_scores_list = []
    all_weights_list = []

    evidence_hash = (
        hashlib.sha256(family_ic_history.to_csv().encode()).hexdigest()
        if not family_ic_history.empty
        else "empty_history"
    )
    generated_at = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else draft.knowledge_cutoff
    )
    run_id = getattr(ctx, "run_id", 1)
    group_def_ver = int(
        getattr(getattr(ctx.cfg, "sectors", None), "group_def_version", 1)
    )

    for mid in model_ids:
        defn = definition_at(conn, mid, as_of) if conn is not None else None
        if defn is None:
            kind = "shrink" if "SHRUNK" in mid else ("flat" if "FLAT" in mid else ("mom" if "MOM" in mid else "equal"))
            factor_set = MOM_FACTORS if kind == "mom" else LAUNCH_FACTORS
            version = 1
            mode = "mom_only" if kind == "mom" else ("flat" if kind == "flat" else "hierarchical")
            sleeve_weight = 0.0
        else:
            kind = defn["kind"]
            factor_set = defn["factor_set"]
            version = defn["version"]
            mode = defn["params"].get("mode", "hierarchical")
            sleeve_weight = float(defn["params"].get("sleeve_weight", 0.0))

        # Factors excluded on actual coverage (G8) do not enter this cohort's composites.
        excluded = set(draft.source_refs.get("excluded_factors", []) or [])
        excluded |= {e.split("@")[0] for e in excluded}
        factor_set = [f for f in factor_set if f["factor_id"] not in excluded
                      and f["factor_id"].split("@")[0] not in excluded]
        if not factor_set:
            continue
        factor_defs_df = pd.DataFrame(factor_set)
        included_families = sorted(list({f["family"] for f in factor_set}))

        # Determine family weights
        if kind == "shrink":
            avail_cols = [c for c in family_ic_history.columns if c in included_families]
            if avail_cols:
                units, diag = fit_family_weights(family_ic_history[avail_cols], ctx.cfg)
                n_eff = diag["n_eff"]
                alpha = diag["alpha"]
                gate = diag["gate"]
            else:
                target = {fam: 1.0 / len(included_families) for fam in included_families}
                units = allocate_units(target)
                n_eff = 0.0
                alpha = 0.0
                gate = "closed"
        else:
            target = {fam: 1.0 / len(included_families) for fam in included_families}
            units = allocate_units(target)
            n_eff = None
            alpha = None
            gate = "closed"

        # Compose scores
        comp_df = compose(
            z_pivot,
            factor_defs_df,
            units,
            draft.groups,
            ctx.cfg,
            mode=mode,
            sleeve_weight=sleeve_weight,
        )

        # Apply screens
        screened_df = apply_screens(ctx, draft, comp_df, mid)

        # Canonical columns
        screened_df["cohort_id"] = draft.cohort_id
        screened_df["as_of"] = draft.as_of
        screened_df["model_id"] = mid
        screened_df["model_version"] = version
        screened_df["group_def_version"] = group_def_ver
        screened_df["dc_flag"] = 0
        screened_df["input_hash"] = draft.definition_hash
        screened_df["generated_at"] = generated_at
        screened_df["track"] = draft.track
        screened_df["run_id"] = run_id
        screened_df["security_id"] = screened_df.index

        all_scores_list.append(screened_df)

        # Build model_weights rows
        for fam, u in units.items():
            all_weights_list.append(
                {
                    "cohort_id": draft.cohort_id,
                    "model_id": mid,
                    "model_version": version,
                    "as_of": draft.as_of,
                    "family": fam,
                    "weight_units": u,
                    "n_eff": n_eff,
                    "alpha": alpha,
                    "gate": gate,
                    "evidence_hash": evidence_hash,
                    "run_id": run_id,
                }
            )

    all_scores = (
        pd.concat(all_scores_list, ignore_index=True)
        if all_scores_list
        else pd.DataFrame()
    )
    all_weights = pd.DataFrame(all_weights_list)

    draft.scores = all_scores
    draft.model_weights = all_weights
    return all_scores, all_weights


def check_draft(draft: Draft, cfg: Any) -> CheckReport:
    """Score/weight invariants on an unpublished draft (MASTER_SPEC 9.1 step 4)."""
    checks: list[Check] = []
    weights = draft.model_weights
    if weights is not None and len(weights):
        units_total = int(getattr(getattr(cfg, "learning", None), "weight_units", 10000))
        floor_mult = float(getattr(getattr(cfg, "learning", None), "floor_mult", 0.5))
        cap_mult = float(getattr(getattr(cfg, "learning", None), "cap_mult", 2.0))
        for mid, g in weights.groupby("model_id"):
            total = int(g["weight_units"].sum())
            f_count = int(len(g))
            lo = math.ceil(units_total * floor_mult / f_count)
            hi = math.floor(units_total * cap_mult / f_count)
            checks.append(Check(id=f"weights_sum_{mid}", status="PASS" if total == units_total else "FAIL",
                                observed=total, expected=units_total,
                                reason="Family weights must sum to exactly the unit total", blocking=True))
            in_bounds = bool(((g["weight_units"] >= lo) & (g["weight_units"] <= hi)).all())
            checks.append(Check(id=f"weights_bounds_{mid}", status="PASS" if in_bounds else "FAIL",
                                observed=(int(g["weight_units"].min()), int(g["weight_units"].max())),
                                expected=(lo, hi), reason="Family weights within [floor/F, cap/F]", blocking=True))
    scores = draft.scores
    if scores is None or len(scores) == 0:
        checks.append(Check(id="scores_present", status="FAIL", observed=0, expected=">0",
                            reason="Draft has no model scores", blocking=True))
        return CheckReport(checks=checks)
    dup = int(scores.duplicated(["cohort_id", "security_id", "model_id"]).sum())
    checks.append(Check(id="scores_unique", status="PASS" if dup == 0 else "FAIL", observed=dup, expected=0,
                        reason="One score row per (cohort, security, model)", blocking=True))
    bad_elig = int(((scores["eligible"] == 1) & (scores["scored"] == 0)).sum())
    checks.append(Check(id="eligible_subset_of_scored", status="PASS" if bad_elig == 0 else "FAIL",
                        observed=bad_elig, expected=0, reason="Eligible names must be scored", blocking=True))
    for mid, g in scores.groupby("model_id"):
        ranked = g[g["rank"].notna()].sort_values("rank")
        monotone = bool((ranked["final"].diff().dropna() <= 1e-12).all()) if len(ranked) > 1 else True
        checks.append(Check(id=f"rank_monotone_{mid}", status="PASS" if monotone else "FAIL",
                            observed=monotone, expected=True,
                            reason="Eligible rank must be descending in final score", blocking=True))
    champ = scores[scores["model_id"] == "EW_HIER_v1"]
    n_scored = int(champ["scored"].sum()) if len(champ) else 0
    checks.append(Check(id="champion_scored", status="PASS" if n_scored > 0 else "FAIL", observed=n_scored,
                        expected=">0", reason="Champion must score at least one name", blocking=True))
    return CheckReport(checks=checks)


def check(conn: sqlite3.Connection) -> CheckReport:
    """Validate model invariants across stored model versions and weights."""
    checks = []

    # 1. Weights sum to 10000 and bounds hold
    weight_groups = conn.execute(
        """
        SELECT cohort_id, model_id, sum(weight_units) as total_units, count(*) as f_count,
               min(weight_units) as min_u, max(weight_units) as max_u
        FROM model_weights
        GROUP BY cohort_id, model_id
        """
    ).fetchall()

    for cohort_id, model_id, total, f_count, min_u, max_u in weight_groups:
        sum_ok = total == 10000
        lower_b = math.ceil(10000 * 0.5 / f_count)
        upper_b = math.floor(10000 * 2.0 / f_count)
        bounds_ok = (min_u >= lower_b) and (max_u <= upper_b)

        checks.append(
            Check(
                id=f"weights_sum_{cohort_id}_{model_id}",
                status="PASS" if sum_ok else "FAIL",
                observed=total,
                expected=10000,
                reason="Family weights must sum to exactly 10000",
                blocking=True,
            )
        )
        checks.append(
            Check(
                id=f"weights_bounds_{cohort_id}_{model_id}",
                status="PASS" if bounds_ok else "FAIL",
                observed=(min_u, max_u),
                expected=(lower_b, upper_b),
                reason="Family weights must stay within [.5/F, 2/F]",
                blocking=True,
            )
        )

    # 2. Champion role is held by exactly one active model
    champs = conn.execute(
        "SELECT model_id FROM models WHERE role = 'champion'"
    ).fetchall()
    checks.append(
        Check(
            id="single_champion",
            status="PASS" if len(champs) == 1 else "FAIL",
            observed=len(champs),
            expected=1,
            reason="Exactly one champion model must exist",
            blocking=True,
        )
    )

    return CheckReport(checks=checks)
