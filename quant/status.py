"""Quant engine operational status reporter (C11)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config


def read(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Read operational status including publication, capture, pending orders, ratifications."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    # 1. Last publication
    pub_row = cur.execute(
        "SELECT cohort_id, as_of, published_at, track FROM cohorts ORDER BY published_at DESC LIMIT 1"
    ).fetchone()
    last_pub = dict(pub_row) if pub_row else None

    # 2. Last capture
    last_cap = None
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "source_captures" in tables:
        cap_row = cur.execute("SELECT observed_at FROM source_captures ORDER BY observed_at DESC LIMIT 1").fetchone()
        if cap_row:
            last_cap = cap_row["observed_at"]

    # 3. Blocked reason
    blocked_reason = None
    if "runs" in tables:
        run_row = cur.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if run_row and run_row["status"] in ("blocked", "refused", "failed"):
            blocked_reason = f"{run_row['status'].upper()}: run {run_row['run_id']} for {run_row['as_of']}"

    # 4. Pending orders
    pending_orders_count = 0
    if "portfolio_orders" in tables:
        o_row = cur.execute("SELECT COUNT(*) as c FROM portfolio_orders WHERE status = 'pending'").fetchone()
        pending_orders_count = o_row["c"] if o_row else 0

    # 5. Pending proposals
    pending_proposals_count = 0
    if "proposals" in tables:
        p_row = cur.execute("SELECT COUNT(*) as c FROM proposals WHERE status = 'proposed'").fetchone()
        pending_proposals_count = p_row["c"] if p_row else 0

    # 6. Overdue ratifications (governance golden case: Tier-1 decisions > 60 days unratified)
    overdue_ratifications_count = 0
    if "decisions" in tables:
        dec_rows = cur.execute(
            "SELECT decided_on, tier, status, approver_kind, ratified_on FROM decisions WHERE status = 'provisional'"
        ).fetchall()
        now_dt = datetime.now(timezone.utc)
        for d in dec_rows:
            dec_on = d["decided_on"]
            try:
                # Support YYYY-MM-DD or ISO formats
                d_dt = datetime.fromisoformat(dec_on.replace("Z", "+00:00"))
                if d_dt.tzinfo is None:
                    d_dt = d_dt.replace(tzinfo=timezone.utc)
                age_days = (now_dt - d_dt).days
                if age_days > 60:
                    overdue_ratifications_count += 1
            except Exception:
                pass

    # 7. Next possible maturities
    next_maturities: list[str] = []
    if "cohorts" in tables and "evaluations" in tables:
        cohorts = cur.execute("SELECT as_of FROM cohorts ORDER BY as_of DESC LIMIT 3").fetchall()
        for c in cohorts:
            next_maturities.append(f"{c['as_of']}+3m")

    # 8. Source archive availability
    archive_dir = Path(getattr(cfg.paths, "raw_dir", "data/raw"))
    archive_avail = archive_dir.exists()

    return {
        "last_publication": last_pub,
        "last_capture": last_cap,
        "blocked_reason": blocked_reason,
        "pending_orders": pending_orders_count,
        "pending_proposals": pending_proposals_count,
        "overdue_ratifications": overdue_ratifications_count,
        "next_possible_maturities": next_maturities,
        "source_archive_availability": archive_avail,
    }
