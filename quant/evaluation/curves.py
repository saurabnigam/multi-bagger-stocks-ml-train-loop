"""Evidence curves and learning curve points tracking (C07)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.evaluation.evaluate import ic_series
from quant.evaluation.labels import _add_months
from quant.evaluation.stats import hac_mean_test
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


def update(ctx: RunContext, through: str) -> Result:
    """Update evidence_curve and learning_curve_points tables up to through date."""
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"evidence_curves": 0, "learning_points": 0})

    run_id = getattr(ctx, "run_id", None) or 1
    if ctx and getattr(ctx, "clock", None):
        computed_at = ctx.clock.iso()
    else:
        as_of_val = getattr(ctx, "as_of", None) or through
        computed_at = as_of_val if "T" in as_of_val else f"{as_of_val}T23:59:59.999999Z"

    # 1. Evidence curves
    distinct_subjects = conn.execute(
        """
        SELECT DISTINCT subject_kind, subject_id, subject_version, horizon_m, track
        FROM evaluations
        WHERE metric = 'ic' AND window_start = '' AND window_end = '' AND as_of <= ?
        ORDER BY subject_kind, subject_id, horizon_m
        """,
        (through,),
    ).fetchall()

    ec_inserted = 0

    for kind, sid, ver, h, track in distinct_subjects:
        s = ic_series(
            conn,
            subject_kind=kind,
            subject_id=sid,
            subject_version=ver,
            horizon_m=h,
            scope="eligible",
            track=track,
            through=through,
            known_at=computed_at,
        )
        if s.empty:
            continue

        valid_s = s.dropna()
        months_clean = len(s)
        n_labelled = len(valid_s)
        n_eff = float(n_labelled / h) if h > 0 else float(n_labelled)

        hac_res = hac_mean_test(valid_s.tolist(), lag=max(0, h - 1))
        cusum_val = float(valid_s.sum()) if n_labelled > 0 else 0.0

        hash_payload = {
            "kind": kind,
            "id": sid,
            "ver": ver,
            "h": h,
            "track": track,
            "series": [(d, None if pd.isna(v) else float(v)) for d, v in s.items()],
        }
        eh = hashlib.sha256(json.dumps(hash_payload, sort_keys=True).encode()).hexdigest()

        # Insert or ignore (evidence_curve is immutable once written)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO evidence_curve (
                computed_at, subject_kind, subject_id, subject_version,
                horizon_m, track, evidence_hash,
                months_clean, n_labelled, n_eff,
                ic_cum_mean, ic_hac_se, ic_hac_t, ci90_lo, ci90_hi, status,
                cusum_ic, spread_net_cum, slope_24
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, 0.0, 0.0
            )
            """,
            (
                computed_at,
                kind,
                sid,
                ver,
                h,
                track,
                eh,
                months_clean,
                n_labelled,
                n_eff,
                hac_res.mean,
                hac_res.se,
                hac_res.t,
                hac_res.ci_lo,
                hac_res.ci_hi,
                hac_res.status,
                cusum_val,
            ),
        )
        if cur.rowcount > 0:
            ec_inserted += 1

    # 2. Learning curve points
    lp_inserted = 0
    model_evals = conn.execute(
        """
        SELECT e.subject_id, e.subject_version, e.as_of, e.horizon_m, e.track, e.value, e.n, c.cohort_id
        FROM evaluations e
        JOIN cohorts c ON c.as_of = e.as_of AND c.track = e.track
        WHERE e.subject_kind = 'model' AND e.metric = 'ic' AND e.window_start = '' AND e.window_end = ''
          AND e.as_of <= ? AND e.status = 'ok'
        ORDER BY e.as_of
        """,
        (through,),
    ).fetchall()

    # Clean live cohort as_of dates per track, used to find the last matured cohort (the one
    # whose h-month endpoint is already known) actually available to train each fit.
    matured_cache: dict[str, list[str]] = {}

    def _last_matured_cohort(track_: str, h_: int, test_as_of_: str) -> str:
        if track_ not in matured_cache:
            matured_cache[track_] = [
                r[0]
                for r in conn.execute(
                    "SELECT as_of FROM cohorts WHERE track = ? AND (is_clean = 1 OR track != 'live') ORDER BY as_of",
                    (track_,),
                ).fetchall()
            ]
        candidates = [c for c in matured_cache[track_] if _add_months(c, h_) <= test_as_of_]
        # No prior matured cohort (e.g. the very first live cohort): nothing was trained on.
        return candidates[-1] if candidates else test_as_of_

    for mid, mver, as_of, h, track, ic_val, n_scored, cid in model_evals:
        # Benchmark equal weight model IC on same cohort
        ew_row = conn.execute(
            """
            SELECT value FROM evaluations
            WHERE subject_kind = 'model' AND subject_id = 'EW_HIER_v1' AND as_of = ? AND horizon_m = ?
              AND scope = 'eligible' AND track = ? AND metric = 'ic' AND status = 'ok'
            ORDER BY revision DESC LIMIT 1
            """,
            (as_of, h, track),
        ).fetchone()
        ew_ic = float(ew_row[0]) if ew_row and ew_row[0] is not None else None

        # Model weights
        mv_row = conn.execute(
            "SELECT weights_json FROM model_versions WHERE model_id = ? AND version = ?",
            (mid, int(mver)),
        ).fetchone()
        w_json = mv_row[0] if mv_row else "{}"

        # k = number of matured cohorts actually used in fitting at this as_of, recovered from
        # the stored fit's n_eff = k/h (model_weights), not re-derived by refitting.
        mw_row = conn.execute(
            "SELECT n_eff FROM model_weights WHERE cohort_id = ? AND model_id = ? LIMIT 1",
            (cid, mid),
        ).fetchone()
        k = int(round(float(mw_row[0]) * h)) if mw_row and mw_row[0] is not None else 0

        train_end = _last_matured_cohort(track, h, as_of)

        lp_hash = hashlib.sha256(
            f"{mid}:{mver}:{cid}:{h}:{ic_val}:{ew_ic}".encode()
        ).hexdigest()

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO learning_curve_points (
                model_id, model_version, cohort_id, horizon_m, track, k,
                train_end, test_as_of, realised_as_of, weights_json,
                oos_ic, ew_oos_ic, n, evidence_hash, computed_run_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                mid,
                int(mver),
                cid,
                h,
                track,
                k,
                train_end,
                as_of,
                through,
                w_json,
                ic_val,
                ew_ic,
                n_scored,
                lp_hash,
                run_id,
            ),
        )
        if cur.rowcount > 0:
            lp_inserted += 1

    return Result(
        status="ok",
        counts={"evidence_curves": ec_inserted, "learning_points": lp_inserted},
    )
