"""Walk-forward learning data retrieval and family IC history (C07)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from quant.evaluation.labels import _add_months


def family_ic_history(
    conn: sqlite3.Connection,
    model_version: dict[str, Any],
    as_of: str,
    known_at: str,
) -> pd.DataFrame:
    """Build causal, point-in-time family IC history for model fitting.

    Uses stored model/factor versions, clean live cohorts, completed endpoints (<= as_of),
    and contemporaneously known evaluation revisions (computed_at <= known_at).
    Cannot read backfill track. Preserves calendar dates without artificial compression.
    """
    # 1. Parse model_version
    weights = model_version.get("weights_json", {})
    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except Exception:
            weights = {}

    factor_set = model_version.get("factor_set_json", [])
    if isinstance(factor_set, str):
        try:
            factor_set = json.loads(factor_set)
        except Exception:
            factor_set = []

    families: list[str] = []
    if "family" in weights and isinstance(weights["family"], dict):
        families = list(weights["family"].keys())
    elif factor_set:
        families = sorted(list({f.get("family") for f in factor_set if f.get("family")}))
    else:
        families = ["momentum", "quality", "growth", "value", "low_risk", "flows"]

    horizon_m = model_version.get("horizon_m", 3)

    # Map family to factor IDs
    family_factors: dict[str, list[str]] = {fam: [] for fam in families}
    for item in factor_set:
        fam = item.get("family")
        fid = item.get("factor_id")
        if fam in family_factors and fid:
            family_factors[fam].append(fid)

    # 2. Query clean live cohorts
    cohort_rows = conn.execute(
        """
        SELECT cohort_id, as_of
        FROM cohorts
        WHERE track = 'live' AND is_clean = 1 AND as_of <= ?
        ORDER BY as_of
        """,
        (as_of,),
    ).fetchall()

    records = []
    valid_dates = []

    for cohort_id, c_as_of in cohort_rows:
        end_date = _add_months(c_as_of, horizon_m)
        if end_date > as_of:
            # Endpoint has not matured by as_of date
            continue

        valid_dates.append(c_as_of)
        row_values: dict[str, float] = {}

        for fam in families:
            fids = family_factors.get(fam, [])

            # Check evaluations for factors or directly for family
            query = """
            WITH ranked AS (
                SELECT subject_id, value, status,
                       ROW_NUMBER() OVER (
                           PARTITION BY subject_id
                           ORDER BY revision DESC, computed_at DESC
                       ) as rn
                FROM evaluations
                WHERE as_of = ?
                  AND horizon_m = ?
                  AND scope = 'eligible'
                  AND track = 'live'
                  AND metric = 'ic'
                  AND window_start = ''
                  AND window_end = ''
                  AND computed_at <= ?
            )
            SELECT subject_id, value, status
            FROM ranked
            WHERE rn = 1;
            """
            eval_rows = conn.execute(query, (c_as_of, horizon_m, known_at)).fetchall()
            eval_map = {r["subject_id"]: (r["value"], r["status"]) for r in eval_rows}

            # If family evaluated directly
            if fam in eval_map:
                val, stat = eval_map[fam]
                row_values[fam] = float(val) if (stat == "ok" and val is not None) else np.nan
            elif fids:
                vals = []
                for fid in fids:
                    if fid in eval_map:
                        v, st = eval_map[fid]
                        if st == "ok" and v is not None:
                            vals.append(float(v))
                row_values[fam] = float(np.mean(vals)) if vals else np.nan
            else:
                # Check factor_registry for factors belonging to this family
                reg_factors = [
                    r[0]
                    for r in conn.execute(
                        "SELECT factor_id FROM factor_registry WHERE family = ?", (fam,)
                    ).fetchall()
                ]
                vals = []
                for fid in reg_factors:
                    if fid in eval_map:
                        v, st = eval_map[fid]
                        if st == "ok" and v is not None:
                            vals.append(float(v))
                row_values[fam] = float(np.mean(vals)) if vals else np.nan

        records.append(row_values)

    if not records:
        return pd.DataFrame(columns=families)

    df = pd.DataFrame(records, index=valid_dates, dtype=float)
    return df
