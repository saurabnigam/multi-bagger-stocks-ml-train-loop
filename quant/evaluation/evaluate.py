"""Evaluation runner, metrics computation, and time-series extraction (C07)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.evaluation.labels import _add_months, frame as labels_frame
from quant.evaluation.metrics import rank_ic
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


def _eval_evidence_hash(
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    as_of: str,
    horizon_m: int,
    scope: str,
    track: str,
    metric: str,
    method: str,
    pairs: list[tuple[int, float, float]],
    label_evidence_hashes: list[str],
) -> str:
    payload = {
        "kind": subject_kind,
        "id": subject_id,
        "version": subject_version,
        "as_of": as_of,
        "horizon_m": horizon_m,
        "scope": scope,
        "track": track,
        "metric": metric,
        "method": method,
        "pairs": pairs,
        "label_hashes": sorted(set(label_evidence_hashes)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _upsert_evaluation(
    conn: sqlite3.Connection,
    run_id: int,
    computed_at: str,
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    as_of: str,
    horizon_m: int,
    scope: str,
    track: str,
    metric: str,
    value: float | None,
    n: int,
    n_eff: float,
    status: str,
    method: str,
    evidence_hash: str,
    decision_id: str | None = None,
) -> bool:
    """Insert evaluation if new or if evidence changed. Returns True if inserted, False if skipped."""
    existing = conn.execute(
        """
        SELECT eval_id, revision, evidence_hash
        FROM evaluations
        WHERE subject_kind = ? AND subject_id = ? AND subject_version = ?
          AND as_of = ? AND horizon_m = ? AND scope = ? AND track = ?
          AND metric = ? AND method = ? AND window_start = '' AND window_end = ''
        ORDER BY revision DESC
        """,
        (subject_kind, subject_id, subject_version, as_of, horizon_m, scope, track, metric, method),
    ).fetchall()

    if existing:
        latest = existing[0]
        if latest["evidence_hash"] == evidence_hash:
            return False
        next_rev = latest["revision"] + 1
        sup_id = latest["eval_id"]
    else:
        next_rev = 1
        sup_id = None

    cursor = conn.execute(
        """
        INSERT INTO evaluations (
            computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            '', '', ?, ?, ?
        )
        """,
        (
            run_id,
            computed_at,
            subject_kind,
            subject_id,
            subject_version,
            as_of,
            horizon_m,
            scope,
            track,
            metric,
            value,
            n,
            n_eff,
            status,
            method,
            evidence_hash,
            next_rev,
            sup_id,
        ),
    )
    new_eval_id = cursor.lastrowid

    if sup_id is not None:
        conn.execute(
            """
            INSERT INTO evaluations_log (old_eval_id, new_eval_id, reason, decision_id, changed_at)
            VALUES (?, ?, 'revised evidence', ?, ?)
            """,
            (sup_id, new_eval_id, decision_id, computed_at),
        )

    return True


def run(ctx: RunContext, through: str, track: str) -> Result:
    """Evaluate factors and models for all completed horizons up to through date."""
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"inserted": 0})

    run_id = getattr(ctx, "run_id", None) or 1
    if ctx and getattr(ctx, "clock", None):
        computed_at = ctx.clock.iso()
    else:
        as_of_val = getattr(ctx, "as_of", None) or through
        computed_at = as_of_val if "T" in as_of_val else f"{as_of_val}T23:59:59.999999Z"

    horizons = [1, 3, 6, 12, 24, 36]
    if ctx.cfg and hasattr(ctx.cfg, "horizons") and hasattr(ctx.cfg.horizons, "tracked_m"):
        horizons = list(ctx.cfg.horizons.tracked_m)

    cohort_rows = conn.execute(
        "SELECT cohort_id, as_of FROM cohorts WHERE track = ? AND as_of <= ? AND is_clean = 1 ORDER BY as_of",
        (track, through),
    ).fetchall()

    total_inserted = 0

    for cohort_id, as_of in cohort_rows:
        for h in horizons:
            end_date = _add_months(as_of, h)
            if end_date > through:
                continue

            for scope in ["all", "eligible"]:
                lbl_df = labels_frame(conn, cohort_id, h, known_at=computed_at, scope=scope)
                if lbl_df.empty:
                    continue

                lbl_dict = dict(zip(lbl_df["security_id"], lbl_df["l_rel"]))
                lbl_hashes = list(lbl_df["evidence_hash"].unique())

                # 1. Evaluate Factors
                factor_rows = conn.execute(
                    """
                    SELECT DISTINCT f.factor_id, f.version, fv.security_id, fv.z
                    FROM factor_registry f
                    JOIN factor_values fv ON fv.factor_id = f.factor_id
                    WHERE fv.cohort_id = ?
                    ORDER BY f.factor_id, fv.security_id
                    """,
                    (cohort_id,),
                ).fetchall()

                by_factor: dict[tuple[str, int], list[tuple[int, float]]] = {}
                for fid, ver, sid, z in factor_rows:
                    if sid in lbl_dict and z is not None and pd.notna(z):
                        by_factor.setdefault((fid, ver), []).append((sid, float(z)))

                for (fid, ver), z_pairs in by_factor.items():
                    sids = [p[0] for p in z_pairs]
                    scores = [p[1] for p in z_pairs]
                    labels = [lbl_dict[s] for s in sids]

                    score_s = pd.Series(scores, index=sids)
                    label_s = pd.Series(labels, index=sids)

                    ic_val, n, status = rank_ic(score_s, label_s)
                    n_eff = float(n / h) if h > 0 else float(n)

                    pairs_for_hash = [
                        (sids[i], scores[i], labels[i])
                        for i in range(len(sids))
                        if pd.notna(scores[i]) and pd.notna(labels[i])
                    ]
                    eh = _eval_evidence_hash(
                        "factor",
                        fid,
                        str(ver),
                        as_of,
                        h,
                        scope,
                        track,
                        "ic",
                        "spearman",
                        pairs_for_hash,
                        lbl_hashes,
                    )

                    inserted = _upsert_evaluation(
                        conn,
                        run_id,
                        computed_at,
                        "factor",
                        fid,
                        str(ver),
                        as_of,
                        h,
                        scope,
                        track,
                        "ic",
                        ic_val,
                        n,
                        n_eff,
                        status,
                        "spearman",
                        eh,
                    )
                    if inserted:
                        total_inserted += 1

                # 2. Evaluate Models
                model_rows = conn.execute(
                    """
                    SELECT DISTINCT m.model_id, s.model_version, s.security_id, s.final
                    FROM scores s
                    JOIN models m ON m.model_id = s.model_id
                    WHERE s.cohort_id = ?
                    ORDER BY m.model_id, s.security_id
                    """,
                    (cohort_id,),
                ).fetchall()

                by_model: dict[tuple[str, int], list[tuple[int, float]]] = {}
                for mid, ver, sid, final in model_rows:
                    if sid in lbl_dict and final is not None and pd.notna(final):
                        by_model.setdefault((mid, ver), []).append((sid, float(final)))

                for (mid, ver), m_pairs in by_model.items():
                    sids = [p[0] for p in m_pairs]
                    scores = [p[1] for p in m_pairs]
                    labels = [lbl_dict[s] for s in sids]

                    score_s = pd.Series(scores, index=sids)
                    label_s = pd.Series(labels, index=sids)

                    ic_val, n, status = rank_ic(score_s, label_s)
                    n_eff = float(n / h) if h > 0 else float(n)

                    pairs_for_hash = [
                        (sids[i], scores[i], labels[i])
                        for i in range(len(sids))
                        if pd.notna(scores[i]) and pd.notna(labels[i])
                    ]
                    eh = _eval_evidence_hash(
                        "model",
                        mid,
                        str(ver),
                        as_of,
                        h,
                        scope,
                        track,
                        "ic",
                        "spearman",
                        pairs_for_hash,
                        lbl_hashes,
                    )

                    inserted = _upsert_evaluation(
                        conn,
                        run_id,
                        computed_at,
                        "model",
                        mid,
                        str(ver),
                        as_of,
                        h,
                        scope,
                        track,
                        "ic",
                        ic_val,
                        n,
                        n_eff,
                        status,
                        "spearman",
                        eh,
                    )
                    if inserted:
                        total_inserted += 1

    return Result(status="ok", counts={"inserted": total_inserted})


def ic_series(
    conn: sqlite3.Connection,
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    horizon_m: int,
    scope: str,
    track: str,
    through: str,
    known_at: str,
) -> pd.Series:
    """Retrieve monthly IC series for subject through date known as of known_at.

    Selects latest revision known_at before status filtering; non-ok status yields NaN.
    """
    query = """
    WITH ranked AS (
        SELECT as_of, value, status, revision, computed_at,
               ROW_NUMBER() OVER (
                   PARTITION BY as_of
                   ORDER BY revision DESC, computed_at DESC
               ) as rn
        FROM evaluations
        WHERE subject_kind = ?
          AND subject_id = ?
          AND subject_version = ?
          AND horizon_m = ?
          AND scope = ?
          AND track = ?
          AND metric = 'ic'
          AND window_start = ''
          AND window_end = ''
          AND as_of <= ?
          AND computed_at <= ?
    )
    SELECT as_of, value, status
    FROM ranked
    WHERE rn = 1
    ORDER BY as_of;
    """
    rows = conn.execute(
        query,
        (
            subject_kind,
            subject_id,
            subject_version,
            horizon_m,
            scope,
            track,
            through,
            known_at,
        ),
    ).fetchall()

    if not rows:
        return pd.Series(dtype=float)

    dates = []
    values = []
    for r in rows:
        dates.append(r["as_of"])
        if r["status"] == "ok" and r["value"] is not None:
            values.append(float(r["value"]))
        else:
            values.append(float("nan"))

    return pd.Series(values, index=dates, dtype=float)
