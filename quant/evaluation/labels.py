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


def _price_store(ctx: RunContext) -> Any:
    """The PriceStore to read from: ``ctx.store`` when set, else one opened at
    ``cfg.paths.prices_db``. Returns None when no price data is reachable, in
    which case callers fall back to the ``prices_monthly`` panel."""
    store = getattr(ctx, "store", None)
    if store is not None:
        return store
    cfg = getattr(ctx, "cfg", None)
    if cfg is None or not hasattr(cfg, "paths") or not hasattr(cfg.paths, "prices_db"):
        return None
    try:
        from quant.data.prices import PriceStore

        return PriceStore(cfg.paths.prices_db, state_conn=getattr(ctx, "conn", None))
    except Exception:
        return None


def _scoped_price_hash(tri_df: pd.DataFrame | None, sids: list[int], dates: list[str | None]) -> str:
    """Content hash of exactly the tri() values read for these securities at
    these dates.

    Scoped to one cohort/horizon/group's own read, never the whole price
    store: a whole-store hash (``PriceStore.manifest_hash``) changes on
    every monthly ingest of the full universe, which would flip every
    group's evidence_hash even when that group's own prices never moved --
    forcing a spurious new label revision every single run, forever. This
    hash only changes when one of THESE rows' own TRI values changes (a
    genuine correction/backfill observed later).
    """
    rows: list[list[Any]] = []
    if tri_df is not None and not tri_df.empty:
        for d in dates:
            if d is None or d not in tri_df.index:
                continue
            row = tri_df.loc[d]
            for sid in sids:
                v = row.get(sid)
                if pd.notna(v):
                    rows.append([int(sid), d, float(v)])
    rows.sort(key=lambda r: (r[0], r[1]))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


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
    # The price vintage this maturation pass reads at: "the label computation
    # time" (MASTER_SPEC 2.3). Revisions become visible exactly when a later
    # mature() call runs at a later vintage_at and sees revised store prices.
    vintage_at = computed_at

    # Configured horizons
    horizons = [1, 3, 6, 12, 24, 36]
    if ctx.cfg and hasattr(ctx.cfg, "horizons") and hasattr(ctx.cfg.horizons, "tracked_m"):
        horizons = list(ctx.cfg.horizons.tracked_m)

    mb_multiple = 2.0
    if ctx.cfg and hasattr(ctx.cfg, "horizons") and hasattr(ctx.cfg.horizons, "multibagger_multiple"):
        mb_multiple = float(ctx.cfg.horizons.multibagger_multiple)
    mb_threshold = mb_multiple - 1.0

    history_start = "2015-01-01"
    if ctx.cfg and hasattr(ctx.cfg, "yahoo") and hasattr(ctx.cfg.yahoo, "history_start"):
        history_start = str(ctx.cfg.yahoo.history_start)

    store = _price_store(ctx)

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

        # Grouping is fixed for the cohort (independent of horizon); compute
        # it once here so the per-horizon loop can build a scoped price hash
        # per group without re-deriving membership each time.
        by_group: dict[str, list[int]] = {}
        for sid in sids:
            by_group.setdefault(member_groups.get(sid, "UNKNOWN"), []).append(sid)

        # Start prices (at cohort as_of), same-track fallback for members the
        # price store has no data for.
        start_pm_rows = conn.execute(
            "SELECT security_id, tri, price_manifest_sha FROM prices_monthly WHERE cohort_id = ?",
            (cohort_id,),
        ).fetchall()
        start_prices_pm = {r[0]: r[1] for r in start_pm_rows}
        start_manifest_pm = {r[0]: r[2] for r in start_pm_rows}

        for h in horizons:
            end_date = _add_months(as_of, h)
            if end_date > through:
                # Horizon endpoint not yet reached
                continue

            # (1) Forward returns from the price store: one tri() call per
            # cohort/horizon over [history_start, end_date] at vintage_at, so
            # both the start and endpoint TRI come from the same revision.
            store_start_tri: dict[int, float] = {}
            store_end_tri: dict[int, float] = {}
            endpoint_session: str | None = None
            tri_df: pd.DataFrame | None = None
            if store is not None:
                try:
                    endpoint_sessions = store.session_dates(as_of, end_date, min_securities=1)
                except Exception:
                    endpoint_sessions = []
                endpoint_session = endpoint_sessions[-1] if endpoint_sessions else None
                if endpoint_session is not None:
                    try:
                        tri_df = store.tri(sids, start=history_start, end=end_date, vintage_at=vintage_at)
                    except Exception:
                        tri_df = None
                    if tri_df is not None and not tri_df.empty:
                        if as_of in tri_df.index:
                            row_s = tri_df.loc[as_of]
                            store_start_tri = {
                                sid: float(row_s.get(sid)) for sid in sids if pd.notna(row_s.get(sid))
                            }
                        if endpoint_session in tri_df.index:
                            row_e = tri_df.loc[endpoint_session]
                            store_end_tri = {
                                sid: float(row_e.get(sid)) for sid in sids if pd.notna(row_e.get(sid))
                            }

            # Scoped price provenance for store-sourced members: one hash per
            # sector group, built ONLY from the tri() rows read above for
            # that group's own members at (as_of, endpoint_session). Never a
            # whole-store hash -- see _scoped_price_hash docstring.
            group_store_hash: dict[str, str] = {
                grp: _scoped_price_hash(tri_df, grp_sids, [as_of, endpoint_session])
                for grp, grp_sids in by_group.items()
            }

            # (2) Same-track prices_monthly fallback for members the store
            # lacks: the endpoint cohort for this track and end_date, else a
            # direct as_of match. Never mixes tracks.
            end_cohort = conn.execute(
                "SELECT cohort_id FROM cohorts WHERE track = ? AND as_of = ? ORDER BY published_at DESC, cohort_id DESC",
                (track, end_date),
            ).fetchone()

            end_pm_rows: list
            if end_cohort:
                end_pm_rows = conn.execute(
                    "SELECT security_id, tri, price_manifest_sha FROM prices_monthly WHERE cohort_id = ?",
                    (end_cohort[0],),
                ).fetchall()
            else:
                end_pm_rows = conn.execute(
                    "SELECT security_id, tri, price_manifest_sha FROM prices_monthly WHERE as_of = ?",
                    (end_date,),
                ).fetchall()
            end_prices_pm = {r[0]: r[1] for r in end_pm_rows}
            end_manifest_pm = {r[0]: r[2] for r in end_pm_rows}

            # Compute returns per member
            member_returns: dict[int, dict[str, Any]] = {}
            for sid in sids:
                p_start = store_start_tri.get(sid)
                p_end = store_end_tri.get(sid)
                from_store = p_start is not None and p_end is not None

                if p_start is None:
                    p_start = start_prices_pm.get(sid)
                if p_end is None:
                    p_end = end_prices_pm.get(sid)

                if from_store:
                    manifest_sha = group_store_hash.get(member_groups.get(sid, "UNKNOWN"), "")
                else:
                    manifest_sha = (
                        end_manifest_pm.get(sid)
                        or start_manifest_pm.get(sid)
                        or ""
                    )

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
                    "price_manifest_sha": manifest_sha,
                }

            # Universe median across ok members
            ok_logs = [m["r_log"] for m in member_returns.values() if m["status"] == "ok"]
            uni_median = float(np.median(ok_logs)) if ok_logs else 0.0

            # Group medians and evidence hashes (by_group was built once
            # above, before the horizon loop -- it does not depend on h)
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
                        # (4) mb36 is only defined at the 36-month horizon
                        # (arith return >= configured multiple - 1); NULL at
                        # every other horizon. mb36_touch (whether the path
                        # ever touched the multiple, not just the endpoint)
                        # stays NULL: it needs a max-TRI-along-the-path check
                        # against daily/monthly bars between as_of and the
                        # endpoint, which is not implemented here.
                        ret["mb36"] = (1 if ret["r_arith"] >= mb_threshold else 0) if h == 36 else None
                        ret["mb36_touch"] = None
                    else:
                        ret["r_group_median"] = None
                        ret["l_rel"] = None
                        ret["r_uni"] = None
                        ret["mb36"] = None
                        ret["mb36_touch"] = None

                # (5) Evidence hash includes each member's price_manifest_sha
                # (MASTER_SPEC 2.3: "the evidence hash includes all group
                # members' price versions"). That manifest sha is a content
                # hash of the price data actually used, so it is the price
                # vintage: it changes only when the underlying prices behind
                # this row change (a store correction observed by a later
                # mature() call), never merely because wall-clock time moved
                # on. Folding the raw vintage_at timestamp in here instead
                # would force a new label revision every monthly run forever,
                # even with byte-identical prices -- not "revisions are
                # visible", just unbounded revision growth.
                #
                # Critically, price_manifest_sha itself must be scoped to
                # THIS group's own members (group_store_hash, built from
                # _scoped_price_hash above) rather than a whole-price-store
                # hash: a whole-store hash changes on every monthly ingest of
                # the ~500-name universe regardless of whether this group's
                # own prices moved, which reproduces the exact same
                # unbounded-revision-growth failure through a different
                # input. Scoping to the group's own read makes this group's
                # evidence_hash change if and only if this group's own
                # r_log/status/price data changed.
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
                            member_returns[s]["price_manifest_sha"],
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
                            ret["price_manifest_sha"] or "",
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
