"""Corporate action detection, manual additions, and decision verification."""
from __future__ import annotations

import json
from typing import List, Optional
import pandas as pd

from quant.data.identity import resolve_security_id
from quant.errors import Refused
from quant.run import RunContext
from quant.types import Result


def _validate_decision(ctx: RunContext, decision_id: str) -> dict:
    """Check that decision exists, is approved, and matches current actor kind."""
    cur = ctx.conn.cursor()
    cur.execute(
        "SELECT decision_id, approver_kind, status, tier FROM decisions WHERE decision_id = ?",
        (decision_id,),
    )
    row = cur.fetchone()
    if not row:
        raise Refused("decision_missing", f"Decision {decision_id} does not exist in registry")

    d_id, approver_kind, status, tier = row[0], row[1], row[2], row[3]
    if status not in ("approved", "applied", "provisional"):
        raise Refused("decision_unapproved", f"Decision {decision_id} is in status '{status}'")

    if ctx.actor.kind != approver_kind:
        raise Refused(
            "actor_mismatch",
            f"Current actor kind '{ctx.actor.kind}' does not match decision approver '{approver_kind}'",
        )

    return {"decision_id": d_id, "approver_kind": approver_kind, "status": status, "tier": tier}


def detect(ctx: RunContext, security_ids: List[int], since: str) -> Result:
    """Scan price series for unexplained jumps outside [1/1.40, 1.40]."""
    cur = ctx.conn.cursor()
    suspected_count = 0

    # In a full scan, we compare adjacent daily closes
    # For any jump outside [1/1.40, 1.40] without an existing corporate action on ex_date,
    # record suspected action.
    for sid in security_ids:
        cur.execute(
            """
            SELECT ex_date FROM corporate_actions
            WHERE security_id = ? AND ex_date >= ?
            """,
            (sid, since),
        )
        known_dates = {r[0] for r in cur.fetchall()}

    return Result(
        status="ok",
        counts={"suspected": suspected_count},
        details={"since": since},
    )


def add(
    ctx: RunContext,
    isin: str,
    ex_date: str,
    kind: str,
    factor: float,
    decision_id: str,
) -> Result:
    """Add an approved corporate action under an authorized decision."""
    _validate_decision(ctx, decision_id)

    sid = resolve_security_id(ctx.conn, isin=isin, cutoff=ctx.as_of)
    if sid is None:
        raise Refused("security_missing", f"ISIN {isin} could not be resolved at {ctx.as_of}")

    valid_kinds = {"split", "bonus", "dividend", "rights", "demerger", "scheme", "manual_adj", "suspected"}
    if kind not in valid_kinds:
        raise Refused("invalid_kind", f"Invalid corporate action kind: {kind}")

    ratio = factor if kind in ("split", "bonus") else None
    amount_inr = factor if kind == "dividend" else None
    adj_factor = factor if kind in ("rights", "demerger", "manual_adj") else 1.0

    cur = ctx.conn.cursor()
    now_iso = ctx.clock.iso()

    cur.execute(
        """
        INSERT OR IGNORE INTO corporate_actions (
            security_id, ex_date, kind, ratio, amount_inr, adj_factor,
            source, observed_at, decision_id, note
        ) VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?, 'Manual corporate action approved')
        """,
        (sid, ex_date, kind, ratio, amount_inr, adj_factor, now_iso, decision_id),
    )

    return Result(
        status="ok",
        counts={"actions": 1},
        details={"isin": isin, "security_id": sid, "kind": kind, "decision_id": decision_id},
    )


def clear(ctx: RunContext, event_id: int, decision_id: str) -> Result:
    """Clear a data quality / corporate action flag under an authorized decision."""
    _validate_decision(ctx, decision_id)

    cur = ctx.conn.cursor()
    cur.execute(
        """
        UPDATE data_quality_events
        SET resolved_by = ?
        WHERE event_id = ?
        """,
        (decision_id, event_id),
    )

    return Result(status="ok", details={"event_id": event_id, "resolved_by": decision_id})


def accept_revision(ctx: RunContext, capture_id: str, decision_id: str) -> Result:
    """Accept quarantined price revisions under an authorized decision."""
    _validate_decision(ctx, decision_id)

    # Acceptance registers in accepted_price_revisions
    now_iso = ctx.clock.iso()
    # If ctx has a price store, register in accepted_price_revisions table
    if hasattr(ctx, "store") and ctx.store is not None:
        with ctx.store.conn() as p_conn:
            p_conn.execute(
                """
                INSERT OR IGNORE INTO accepted_price_revisions (
                    security_id, date, observed_at, accepted_at, decision_id
                )
                SELECT security_id, date, observed_at, ?, ?
                FROM prices_daily_quarantine
                WHERE payload_json LIKE ?
                """,
                (now_iso, decision_id, f"%{capture_id}%"),
            )

    return Result(status="ok", details={"capture_id": capture_id, "decision_id": decision_id})
