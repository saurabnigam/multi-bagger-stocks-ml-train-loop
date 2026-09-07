"""Cohort labels calculation, endpoint maturity, and appended revisions."""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.types import Result

if TYPE_CHECKING:
    import sqlite3
    from quant.run import RunContext


def _add_months(as_of: str, months: int) -> str:
    """Calculate endpoint month and return the last calendar day in that month."""
    dt = datetime.date.fromisoformat(as_of)
    month = dt.month + months
    year = dt.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def mature(ctx: RunContext, through: str) -> Result:
    """Mature cohorts up to through date and compute/revise forward-return labels.

    Only matures cohorts whose endpoint <= through.
    Appends new revisions for affected sector groups when returns or corporate actions change.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"matured": 0})

    run_id = getattr(ctx, "run_id", None) or 1
    computed_at = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{ctx.as_of or through}T00:00:00.000000Z"
    )

    # Configured horizons
    horizons = [1, 3, 6, 12, 24, 36]
    if ctx.cfg and hasattr(ctx.cfg, "horizons") and hasattr(ctx.cfg.horizons, "tracked_m"):
        horizons = list(ctx.cfg.horizons.tracked_m)

    # Find cohorts published on or before through
    cohort_rows = conn.execute(
        "SELECT cohort_id, as_of, track FROM cohorts WHERE as_of <= ? ORDER BY as_of, cohort_id",
        (through,),
    ).fetchall()

    total_inserted = 0

    for cohort_id, as_of, track in cohort_rows:
        # Get cohort members and sector groups
        score_members = conn.execute(
            """
            SELECT DISTINCT security_id, sector_group
            FROM scores
            WHERE cohort_id = ?
            ORDER BY security_id
            """,
            (cohort_id,),
        ).fetchall()

        if not score_members:
            # Fallback to prices_monthly if scores not present
            score_members = conn.execute(
                """
                SELECT DISTINCT security_id, 'UNKNOWN' as sector_group
                FROM prices_monthly
                WHERE cohort_id = ?
                ORDER BY security_id
                """,
                (cohort_id,),
            ).fetchall()

        if not score_members:
            continue

        sids = [r[0] for r in score_members]
        member_groups = {r[0]: r[1] for r in score_members}

        # Start prices (at cohort as_of)
        start_prices = dict(
            conn.execute(
                "SELECT security_id, tri FROM prices_monthly WHERE cohort_id = ?",
                (cohort_id,),
            ).fetchall()
        )

        for h in horizons:
            end_date = _add_months(as_of, h)
            if end_date > through:
                # Horizon endpoint not yet reached
                continue

            # Fetch endpoint prices for the same track
            # Look for cohort matching this track and end_date
            end_cohort = conn.execute(
                "SELECT cohort_id FROM cohorts WHERE track = ? AND as_of = ? ORDER BY published_at DESC, cohort_id DESC",
                (track, end_date),
            ).fetchone()

            end_prices = {}
            if end_cohort:
                end_prices = dict(
                    conn.execute(
                        "SELECT security_id, tri FROM prices_monthly WHERE cohort_id = ?",
                        (end_cohort[0],),
                    ).fetchall()
                )
            else:
                # Try finding in prices_monthly by as_of date directly
                rows = conn.execute(
                    "SELECT security_id, tri FROM prices_monthly WHERE as_of = ?",
                    (end_date,),
                ).fetchall()
                end_prices = dict(rows)

            # Compute returns per member
            member_returns: dict[int, dict[str, Any]] = {}
            for sid in sids:
                p_start = start_prices.get(sid)
                p_end = end_prices.get(sid)

                if (
                    p_start is not None
                    and p_end is not None
                    and pd.notna(p_start)
                    and pd.notna(p_end)
                    and p_start > 0
                    and p_end > 0
                ):
                    r_log = float(np.log(p_end / p_start))
                    r_arith = float(np.exp(r_log) - 1.0)
                    status = "ok"
                else:
                    r_log = None
                    r_arith = None
                    status = "missing"

                member_returns[sid] = {
                    "r_log": r_log,
                    "r_arith": r_arith,
                    "status": status,
                    "p_start": p_start,
                    "p_end": p_end,
                }

            # Universe median across ok members
            ok_logs = [m["r_log"] for m in member_returns.values() if m["status"] == "ok"]
            uni_median = float(np.median(ok_logs)) if ok_logs else 0.0

            # Group by sector_group
            by_group: dict[str, list[int]] = {}
            for sid in sids:
                grp = member_groups.get(sid, "UNKNOWN")
                by_group.setdefault(grp, []).append(sid)

            # Group medians and evidence hashes
            for grp, grp_sids in by_group.items():
                grp_ok_logs = [
                    member_returns[s]["r_log"]
                    for s in grp_sids
                    if member_returns[s]["status"] == "ok"
                ]
                grp_median = float(np.median(grp_ok_logs)) if grp_ok_logs else 0.0

                for s in grp_sids:
                    ret = member_returns[s]
                    if ret["status"] == "ok":
                        ret["r_group_median"] = grp_median
                        ret["l_rel"] = float(ret["r_log"] - grp_median)
                        ret["r_uni"] = float(ret["r_log"] - uni_median)
                        ret["mb36"] = 1 if ret["r_arith"] >= 1.0 else 0
                        ret["mb36_touch"] = 0
                    else:
                        ret["r_group_median"] = None
                        ret["l_rel"] = None
                        ret["r_uni"] = None
                        ret["mb36"] = None
                        ret["mb36_touch"] = None

                # Compute evidence hash for this group
                hash_data = {
                    "cohort_id": cohort_id,
                    "horizon_m": h,
                    "end_date": end_date,
                    "group": grp,
                    "members": [
                        (
                            s,
                            member_returns[s]["status"],
                            member_returns[s]["r_log"],
                            member_returns[s]["p_end"],
                        )
                        for s in sorted(grp_sids)
                    ],
                }
                grp_evidence_hash = hashlib.sha256(
                    json.dumps(hash_data, sort_keys=True).encode()
                ).hexdigest()

                # Check existing revision for this group's members
                placeholders = ",".join("?" for _ in grp_sids)
                existing = conn.execute(
                    f"""
                    SELECT security_id, revision, evidence_hash
                    FROM labels
                    WHERE cohort_id = ? AND horizon_m = ? AND security_id IN ({placeholders})
                    ORDER BY revision DESC
                    """,
                    [cohort_id, h] + grp_sids,
                ).fetchall()

                # Get latest revision per security
                latest_revs: dict[int, tuple[int, str]] = {}
                for sid, rev, eh in existing:
                    if sid not in latest_revs:
                        latest_revs[sid] = (rev, eh)

                # If all members in group already have identical evidence_hash, skip insertion
                if latest_revs and all(
                    latest_revs.get(s, (0, ""))[1] == grp_evidence_hash for s in grp_sids
                ):
                    continue

                # Determine next revision number
                all_revs = [rev for _, rev, _ in existing]
                next_rev = (max(all_revs) + 1) if all_revs else 1

                for s in grp_sids:
                    ret = member_returns[s]
                    sup_rev = latest_revs.get(s, (None, None))[0]

                    conn.execute(
                        """
                        INSERT INTO labels (
                            cohort_id, as_of, security_id, horizon_m, end_date, track,
                            revision, evidence_hash, computed_at,
                            r_log, r_arith, r_group_median, l_rel, r_uni, sector_group,
                            status, mb36, mb36_touch, price_manifest_sha, computed_run_id,
                            decision_id, supersedes_revision
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?
                        )
                        """,
                        (
                            cohort_id,
                            as_of,
                            s,
                            h,
                            end_date,
                            track,
                            next_rev,
                            grp_evidence_hash,
                            computed_at,
                            ret["r_log"],
                            ret["r_arith"],
                            ret["r_group_median"],
                            ret["l_rel"],
                            ret["r_uni"],
                            grp,
                            ret["status"],
                            ret["mb36"],
                            ret["mb36_touch"],
                            grp_evidence_hash,
                            run_id,
                            None,
                            sup_rev,
                        ),
                    )
                    total_inserted += 1

    return Result(status="ok", counts={"matured": total_inserted})


def frame(
    conn: sqlite3.Connection,
    cohort_id: str,
    horizon_m: int,
    known_at: str,
    *,
    scope: str = "all",
    model_id: str = "EW_HIER_v1",
) -> pd.DataFrame:
    """Retrieve forward-return labels for cohort_id and horizon_m as of known_at.

    Selects latest revision known_at per security key before applying status/eligibility filters.
    """
    query = """
    WITH ranked_revisions AS (
        SELECT l.security_id, l.l_rel, l.r_log, l.r_arith, l.status, l.sector_group,
               l.revision, l.evidence_hash, l.computed_at,
               ROW_NUMBER() OVER (
                   PARTITION BY l.security_id
                   ORDER BY l.revision DESC, l.computed_at DESC
               ) as rn
        FROM labels l
        WHERE l.cohort_id = ?
          AND l.horizon_m = ?
          AND l.computed_at <= ?
    )
    SELECT r.security_id, r.l_rel, r.r_log, r.r_arith, r.status, r.sector_group,
           COALESCE(s.eligible, 1) as eligible, r.revision, r.evidence_hash
    FROM ranked_revisions r
    LEFT JOIN scores s ON s.cohort_id = ? AND s.security_id = r.security_id AND s.model_id = ?
    WHERE r.rn = 1
    ORDER BY r.security_id
    """
    df = pd.read_sql_query(
        query,
        conn,
        params=[cohort_id, horizon_m, known_at, cohort_id, model_id],
    )

    if scope == "eligible":
        df = df[df["eligible"] == 1].reset_index(drop=True)

    return df
