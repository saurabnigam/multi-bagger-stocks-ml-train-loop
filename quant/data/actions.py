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


def detect(ctx: RunContext, security_ids: List[int], since: str, store=None) -> Result:
    """Flag unexplained one-day total-return jumps as ``suspected`` corporate actions.

    A day's gross total-return factor split*(close+dividend)/prev_close outside
    [1/jump_ratio, jump_ratio] (config [returns].jump_ratio, 1.40) with no corporate-action
    row for that security and ex-date is recorded as kind 'suspected', source 'inferred'.
    ``observed_at`` is the observation time of the price bar that evidences the jump (not
    the run clock): the inference is a deterministic function of admissible data, so it is
    admissible at any cutoff that could see that bar, and later detections do not change
    earlier replays. The price store then truncates history at unresolved suspects until a
    decision records a value-transfer factor (or 1.0 = genuine move) via ``add``.
    """
    import numpy as np

    from quant.data.prices import PriceStore

    jump = float(getattr(getattr(ctx.cfg, "returns", None), "jump_ratio", 1.40))
    store = store or getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    vintage = ctx.clock.iso()
    sids = sorted({int(x) for x in security_ids})
    if not sids:
        return Result(status="ok", counts={"suspected": 0, "scanned": 0}, details={"since": since})
    known = {
        (int(r[0]), str(r[1]))
        for r in ctx.conn.execute("SELECT security_id, ex_date FROM corporate_actions").fetchall()
    }
    start = (pd.Timestamp(since) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = vintage[:10]
    new_rows = []
    for i in range(0, len(sids), 100):
        chunk = sids[i:i + 100]
        df = store._versioned(chunk, start, end, vintage, "close_raw, dividend_raw, split_ratio, observed_at")
        for sid, g in df.groupby("security_id"):
            g = g.sort_values("date")
            c = g["close_raw"].to_numpy(float)
            d = g["dividend_raw"].to_numpy(float)
            sp = g["split_ratio"].to_numpy(float)
            if len(c) < 2:
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                f = sp[1:] * (c[1:] + d[1:]) / c[:-1]
            bad = np.where(np.isfinite(f) & ((f > jump) | (f < 1.0 / jump)))[0] + 1
            dates = g["date"].to_numpy(str)
            obs = g["observed_at"].to_numpy(str)
            for k in bad:
                ex = dates[k]
                if ex < since or (int(sid), ex) in known:
                    continue
                new_rows.append((int(sid), ex, float(f[k - 1]), obs[k]))
                known.add((int(sid), ex))
    for sid, ex, factor, obs in new_rows:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, "
            "source, observed_at, decision_id, note) VALUES (?, ?, 'suspected', NULL, NULL, NULL, 'inferred', ?, NULL, ?)",
            (sid, ex, obs, json.dumps({"gross_factor": round(factor, 6), "jump_ratio": jump})),
        )
    if new_rows:
        from quant.data.gates import record_event
        record_event(ctx, code="SUSPECTED_CORPORATE_ACTIONS", severity="WARN",
                     detail={"count": len(new_rows),
                             "actions": [{"security_id": s_, "ex_date": e_, "gross_factor": round(f_, 4)}
                                         for s_, e_, f_, _ in new_rows[:50]]})
    return Result(status="ok", counts={"suspected": len(new_rows), "scanned": len(sids)},
                  details={"since": since, "new": [(s_, e_, round(f_, 4)) for s_, e_, f_, _ in new_rows]})


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
