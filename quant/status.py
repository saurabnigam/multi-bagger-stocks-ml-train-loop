"""Quant engine operational status reporter (C11)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config

_CAPTURE_KINDS = ("nifty500", "yahoo_bundle", "yahoo_prices")
_TRACKED_HORIZON_FALLBACK = [1, 3, 6, 12, 24, 36]


def _end_month(as_of: str, horizon_m: int) -> str:
    """Calendar-month arithmetic matching quant.data.identity.tracked_securities."""
    y, m = int(as_of[:4]), int(as_of[5:7])
    total_m = m + horizon_m
    end_y = y + (total_m - 1) // 12
    end_m = (total_m - 1) % 12 + 1
    return f"{end_y:04d}-{end_m:02d}"


def read(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Read operational status: publication, capture, blocked reason, pending
    orders/proposals, overdue ratifications, next possible maturities and
    source archive availability -- all from persisted state (INTERFACES C11)."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # 1. Last publication
    last_pub = None
    if "cohorts" in tables:
        pub_row = cur.execute(
            "SELECT cohort_id, as_of, track, published_at FROM cohorts ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        last_pub = dict(pub_row) if pub_row else None

    # 2. Last capture per kind -- the real table is `captures`, not `source_captures`.
    last_capture: dict[str, Any] = {k: None for k in _CAPTURE_KINDS}
    if "captures" in tables:
        for kind in _CAPTURE_KINDS:
            cap_row = cur.execute(
                "SELECT capture_id, captured_at, archive_path, sha256 FROM captures "
                "WHERE kind = ? ORDER BY captured_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if cap_row:
                last_capture[kind] = dict(cap_row)

    # 3. Blocked reason: latest non-ok run, with the actual notes_json error/blocked detail.
    blocked_reason = None
    if "runs" in tables:
        run_row = cur.execute(
            "SELECT run_id, as_of, status, notes_json FROM runs "
            "WHERE status IN ('blocked', 'refused', 'failed') ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if run_row:
            detail = None
            if run_row["notes_json"]:
                try:
                    notes = json.loads(run_row["notes_json"])
                    detail = (
                        notes.get("blocked")
                        or notes.get("error")
                        or notes.get("capture_error")
                        or notes.get("report_error")
                    )
                except (TypeError, ValueError):
                    detail = None
            if not detail:
                detail = f"run {run_row['run_id']} for {run_row['as_of']}"
            blocked_reason = f"{run_row['status'].upper()}: {detail}"

    # 4. Pending orders
    pending_orders_count = 0
    if "portfolio_orders" in tables:
        pending_orders_count = cur.execute(
            "SELECT COUNT(*) FROM portfolio_orders WHERE status = 'pending'"
        ).fetchone()[0]

    # 5. Pending proposals
    pending_proposals_count = 0
    if "proposals" in tables:
        pending_proposals_count = cur.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'proposed'"
        ).fetchone()[0]

    # 6. Overdue ratifications: provisional Tier-1 LLM decisions past the ratification budget.
    # Schema comment on decisions.ratified_by: "human co-signature required for LLM Tier-1
    # decisions within 60 days" -- human-decided and Tier-0/2 decisions do not need this co-sign.
    overdue_ratifications_count = 0
    if "decisions" in tables:
        ratification_days = int(getattr(getattr(cfg, "budget", None), "llm_ratification_days", 60))
        dec_rows = cur.execute(
            "SELECT decided_on FROM decisions WHERE status = 'provisional' AND tier = 1 AND approver_kind = 'llm'"
        ).fetchall()
        now_dt = datetime.now(timezone.utc)
        for d in dec_rows:
            try:
                d_dt = datetime.fromisoformat(str(d["decided_on"]).replace("Z", "+00:00"))
                if d_dt.tzinfo is None:
                    d_dt = d_dt.replace(tzinfo=timezone.utc)
                if (now_dt - d_dt).days > ratification_days:
                    overdue_ratifications_count += 1
            except (AttributeError, ValueError):
                continue

    # 7. Next possible maturities: (cohort, tracked horizon) pairs whose window has already
    # closed but which have no label row yet -- i.e. maturation is now possible, not yet done.
    next_maturities: list[dict[str, Any]] = []
    if "cohorts" in tables and "labels" in tables:
        horizons = list(getattr(getattr(cfg, "horizons", None), "tracked_m", _TRACKED_HORIZON_FALLBACK))
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        cohort_rows = cur.execute(
            "SELECT cohort_id, as_of FROM cohorts WHERE track = 'live' ORDER BY as_of"
        ).fetchall()
        for c in cohort_rows:
            for h in horizons:
                target_month = _end_month(c["as_of"], int(h))
                if target_month > current_month:
                    continue  # horizon has not closed yet: not yet possible
                has_label = cur.execute(
                    "SELECT 1 FROM labels WHERE cohort_id = ? AND horizon_m = ? LIMIT 1",
                    (c["cohort_id"], int(h)),
                ).fetchone()
                if not has_label:
                    next_maturities.append(
                        {
                            "cohort_id": c["cohort_id"],
                            "as_of": c["as_of"],
                            "horizon_m": int(h),
                            "target_month": target_month,
                        }
                    )

    # 8. Source archive availability: do the files captures.archive_path points at still exist?
    archive_total = 0
    archive_available = 0
    archive_missing: list[str] = []
    if "captures" in tables:
        paths = cur.execute("SELECT DISTINCT archive_path FROM captures").fetchall()
        archive_total = len(paths)
        for p in paths:
            ap = p["archive_path"]
            if ap and Path(ap).exists():
                archive_available += 1
            else:
                archive_missing.append(ap)

    return {
        "last_publication": last_pub,
        "last_capture": last_capture,
        "blocked_reason": blocked_reason,
        "pending_orders": pending_orders_count,
        "pending_proposals": pending_proposals_count,
        "overdue_ratifications": overdue_ratifications_count,
        "next_possible_maturities": next_maturities,
        "source_archive_availability": {
            "total": archive_total,
            "available": archive_available,
            "missing": archive_missing,
        },
    }
