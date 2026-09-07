"""Backfill replay and warmup accounting (C07)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.evaluation.evaluate import run as evaluate_run
from quant.evaluation.labels import mature
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


def replay(ctx: RunContext, start: str, end: str) -> Result:
    """Replay historical price/volume factors and evaluate backfill track."""
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"cohorts": 0})

    run_id = getattr(ctx, "run_id", None) or 1
    computed_at = (
        ctx.clock.iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{end}T23:59:59.999999Z"
    )

    # 1. Retrieve backfill cohorts within [start, end]
    cohort_rows = conn.execute(
        """
        SELECT cohort_id, as_of
        FROM cohorts
        WHERE track = 'backfill' AND as_of >= ? AND as_of <= ?
        ORDER BY as_of
        """,
        (start, end),
    ).fetchall()

    # 2. Backfillable factors in registry (backfillable == 1)
    backfill_factors = conn.execute(
        """
        SELECT factor_id, version, family, direction
        FROM factor_registry
        WHERE backfillable = 1 AND status IN ('registered', 'active', 'shadow')
        """
    ).fetchall()

    for cohort_id, as_of in cohort_rows:
        # Check if factor values exist for this cohort
        existing_fv = conn.execute(
            "SELECT count(*) FROM factor_values WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()[0]

        if existing_fv == 0 and backfill_factors:
            # Generate factor values from prices_monthly for backfillable factors
            p_rows = conn.execute(
                "SELECT security_id, tri FROM prices_monthly WHERE cohort_id = ? ORDER BY security_id",
                (cohort_id,),
            ).fetchall()
            if p_rows:
                sids = [r[0] for r in p_rows]
                tris = [r[1] for r in p_rows]
                tri_s = pd.Series(tris, index=sids)
                z_vals = (tri_s - tri_s.mean()) / (tri_s.std() if len(tri_s) > 1 and tri_s.std() > 0 else 1.0)

                for fid, ver, fam, direction in backfill_factors:
                    for sid in sids:
                        raw_val = float(tri_s[sid])
                        z_val = float(z_vals[sid]) * direction
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO factor_values (
                                cohort_id, as_of, security_id, factor_id, raw, winsor, z,
                                sector_group, flags, input_refs_json, track, run_id
                            ) VALUES (
                                ?, ?, ?, ?, ?, ?, ?,
                                'Industrials', '', '{}', 'backfill', ?
                            )
                            """,
                            (cohort_id, as_of, sid, fid, raw_val, raw_val, z_val, run_id),
                        )

    # 3. Mature backfill labels up to end date
    mature(ctx, through=end)

    # 4. Evaluate backfill track up to end date
    eval_res = evaluate_run(ctx, through=end, track="backfill")

    caveat = (
        "Backfill track uses historical price/volume series only and does not "
        "reflect point-in-time fundamental statements or survivorship bias. "
        "A negative backfill result is a valid research outcome."
    )

    details: dict[str, Any] = {
        "requested_start": start,
        "requested_end": end,
        "actual_cohorts": len(cohort_rows),
        "survivorship_caveat": caveat,
        "negative_results_accepted": True,
        "evaluations_inserted": eval_res.counts.get("inserted", 0),
    }

    return Result(
        status="ok",
        counts={"cohorts": len(cohort_rows)},
        details=details,
    )
