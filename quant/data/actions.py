"""Corporate action detection, manual additions, and decision verification."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd

from quant.data.identity import resolve_security_id
from quant.errors import Refused
from quant.knowledge.adr import write as write_adr
from quant.run import RunContext
from quant.types import Result

#: Kinds an operator can select from `data actions-resolve` (MASTER_SPEC 2.3, 4.3).
#: 'genuine' is not a stored kind; it records a reviewed-and-real move as manual_adj/1.0.
RESOLVE_VALUE_TRANSFER_KINDS = ("demerger", "rights", "scheme", "manual_adj")
RESOLVE_SPLIT_KINDS = ("split", "bonus")
RESOLVE_KINDS = RESOLVE_VALUE_TRANSFER_KINDS + RESOLVE_SPLIT_KINDS + ("genuine",)
JUMP_RATIO_BOUNDS = (1.0 / 1.40, 1.40)


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
    adj_factor = factor if kind in RESOLVE_VALUE_TRANSFER_KINDS else 1.0

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


def list_suspects(
    conn,
    price_store=None,
    security_ids: Optional[List[int]] = None,
    include_resolved: bool = False,
) -> List[Dict[str, Any]]:
    """Read-only operator review queue for suspected corporate actions (D3/T2).

    One row per suspected ``(security, ex_date)``. ``resolved`` is ``None`` while the
    only row for that key is the ``suspected`` one from :func:`detect`; once an
    operator has recorded an approved row for the same key via :func:`add` (or
    :func:`resolve`), that row's kind/value/decision_id is surfaced here instead --
    ``corporate_actions`` is append-only, so the suspect row itself is never edited
    (:func:`PriceStore.corporate_actions` resolves the same precedence). Unresolved
    rows are filtered out unless ``include_resolved`` is set (``data actions-list
    --all``). ``in_trailing_13m`` is True when ``ex_date`` falls within 13 months of
    that security's latest known price bar -- a still-truncated name in that window
    is actively distorting the current live cohort, not just historical replays.
    """
    query = "SELECT security_id, ex_date, observed_at, note FROM corporate_actions WHERE kind = 'suspected'"
    params: List[Any] = []
    if security_ids:
        placeholders = ",".join("?" for _ in security_ids)
        query += f" AND security_id IN ({placeholders})"
        params.extend(int(s) for s in security_ids)
    query += " ORDER BY security_id, ex_date"
    suspects = conn.execute(query, params).fetchall()

    out: List[Dict[str, Any]] = []
    for sid_raw, ex_date, observed_at, note in suspects:
        sid = int(sid_raw)
        resolution_row = conn.execute(
            "SELECT kind, ratio, adj_factor, decision_id, observed_at FROM corporate_actions "
            "WHERE security_id = ? AND ex_date = ? AND decision_id IS NOT NULL "
            "ORDER BY observed_at DESC LIMIT 1",
            (sid, ex_date),
        ).fetchone()
        if resolution_row and not include_resolved:
            continue

        sec_row = conn.execute("SELECT isin, name FROM securities WHERE security_id = ?", (sid,)).fetchone()
        sym_row = conn.execute(
            "SELECT nse_symbol FROM symbol_history WHERE security_id = ? ORDER BY valid_from DESC LIMIT 1",
            (sid,),
        ).fetchone()

        gross_factor = None
        if note:
            try:
                gross_factor = float(json.loads(note).get("gross_factor"))
            except (ValueError, TypeError, AttributeError):
                gross_factor = None

        in_trailing_13m = None
        if price_store is not None:
            with price_store.conn() as p_conn:
                latest = p_conn.execute(
                    "SELECT max(date) FROM prices_daily WHERE security_id = ?", (sid,)
                ).fetchone()[0]
            if latest:
                window_start = (pd.Timestamp(latest) - pd.DateOffset(months=13)).strftime("%Y-%m-%d")
                in_trailing_13m = bool(window_start <= ex_date <= str(latest))

        resolved = None
        if resolution_row:
            r_kind, r_ratio, r_adj, r_decision_id, r_observed_at = resolution_row
            resolved = {
                "kind": r_kind,
                "value": r_adj if r_adj is not None else r_ratio,
                "decision_id": r_decision_id,
                "observed_at": r_observed_at,
            }

        out.append({
            "security_id": sid,
            "isin": sec_row[0] if sec_row else None,
            "symbol": sym_row[0] if sym_row else None,
            "ex_date": ex_date,
            "gross_factor": gross_factor,
            "evidence_observed_at": observed_at,
            "in_trailing_13m": in_trailing_13m,
            "resolved": resolved,
        })
    return out


def _observed_gross_factor(ctx: RunContext, sid: int, ex_date: str) -> Optional[float]:
    """split * (close + dividend) / previous close on ``ex_date``, latest observed version."""
    from quant.data.prices import PriceStore

    store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    start = (pd.Timestamp(ex_date) - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    df = store._versioned([int(sid)], start, ex_date, ctx.clock.iso(), "close_raw, dividend_raw, split_ratio")
    if df.empty or str(df["date"].iloc[-1]) != ex_date or len(df) < 2:
        return None
    prev, cur = df.iloc[-2], df.iloc[-1]
    if not prev["close_raw"] or prev["close_raw"] <= 0:
        return None
    return float(cur["split_ratio"]) * (float(cur["close_raw"]) + float(cur["dividend_raw"] or 0.0)) / float(prev["close_raw"])


def resolve(
    ctx: RunContext,
    *,
    isin: Optional[str] = None,
    symbol: Optional[str] = None,
    ex_date: str,
    kind: str,
    factor: float,
    evidence: str,
    title: Optional[str] = None,
    force: bool = False,
) -> Result:
    """Operator resolution of a suspected corporate action (D3/T2; MASTER_SPEC 2.3, 4.3, 9.3).

    Human-only: a value-transfer factor is a Tier-1 judgment call read off a filing, and
    the spec's LLM-provisional path (9.3) is deliberately not offered here -- a wrong
    factor silently corrupts every downstream TRI and evaluation. Records a Tier-1
    ``data_fix`` decision at status ``approved`` (never ``provisional``), writes its ADR
    with the existing writer so ``kb check`` stays clean, then calls :func:`add` under
    that decision. ``kind='genuine'`` means the move was real: it is stored as
    ``manual_adj`` with ``adj_factor`` 1.0, which stops the truncation without touching
    the observed return. ``split``/``bonus`` take ``factor`` as the vendor-missed ratio
    (new shares per old); the other kinds take it as the multiplicative value-transfer
    adjustment. Refuses a second resolution of the same ``(security, ex_date)``, and an
    implausible combination of the evidenced jump and the chosen factor unless ``force``.
    """
    if ctx.actor.kind != "human":
        raise Refused(
            "governance",
            "data actions-resolve requires --actor-kind human: a corporate-action "
            f"value-transfer factor is never LLM-provisional (actor was '{ctx.actor.kind}')",
        )

    if kind not in RESOLVE_KINDS:
        raise Refused("invalid_kind", f"Invalid resolution kind '{kind}'; choose one of {RESOLVE_KINDS}")

    if kind == "genuine":
        stored_kind, stored_factor = "manual_adj", 1.0
    else:
        stored_kind, stored_factor = kind, float(factor)

    if not (0 < stored_factor <= 20):
        raise Refused("implausible_factor", f"Factor {stored_factor} is outside the sane range (0, 20]")

    sid = resolve_security_id(ctx.conn, isin=isin, symbol=symbol, cutoff=ctx.as_of)
    if sid is None:
        raise Refused("security_missing", f"Could not resolve isin={isin!r} symbol={symbol!r} at {ctx.as_of}")

    canonical_isin = isin
    if canonical_isin is None:
        row = ctx.conn.execute("SELECT isin FROM securities WHERE security_id = ?", (sid,)).fetchone()
        canonical_isin = row[0] if row else None

    existing = ctx.conn.execute(
        "SELECT decision_id FROM corporate_actions WHERE security_id = ? AND ex_date = ? "
        "AND decision_id IS NOT NULL",
        (sid, ex_date),
    ).fetchone()
    if existing:
        raise Refused(
            "already_resolved",
            f"{canonical_isin} {ex_date} was already resolved under decision {existing[0]}; "
            "corporate_actions is append-only and never re-resolved",
        )

    note_row = ctx.conn.execute(
        "SELECT note FROM corporate_actions WHERE security_id = ? AND ex_date = ? AND kind = 'suspected' "
        "ORDER BY observed_at DESC LIMIT 1",
        (sid, ex_date),
    ).fetchone()
    gross_factor = None
    if note_row and note_row[0]:
        try:
            gross_factor = float(json.loads(note_row[0]).get("gross_factor"))
        except (ValueError, TypeError, AttributeError):
            gross_factor = None
    if gross_factor is None:
        # No suspect recorded yet (resolved ahead of detection): measure the evidenced jump
        # from the price store rather than assuming 1.0, which would refuse every real fix.
        gross_factor = _observed_gross_factor(ctx, sid, ex_date)
    if gross_factor is None:
        if not force:
            raise Refused("no_price_evidence", f"No price bar on {ex_date} and the prior session for {canonical_isin}; "
                                               "check the ex-date or pass --force")
        gross_factor = 1.0

    combined = gross_factor * stored_factor
    lo, hi = JUMP_RATIO_BOUNDS
    warning = None
    # 'genuine' means the evidenced jump IS the real return -- it is not meant to cancel
    # gross_factor, so the plausibility check (which only makes sense for a corrective
    # value-transfer/split factor) does not apply to it.
    if kind != "genuine" and not (lo <= combined <= hi):
        if not force:
            raise Refused(
                "implausible_combination",
                f"gross_factor {gross_factor:.4f} x factor {stored_factor:.4f} = {combined:.4f} lies "
                f"outside [{lo:.4f}, {hi:.4f}]; pass --force to override",
            )
        warning = (
            f"gross_factor {gross_factor:.4f} x factor {stored_factor:.4f} = {combined:.4f} lies outside "
            f"[{lo:.4f}, {hi:.4f}]; proceeding under --force"
        )

    seq = ctx.conn.execute("SELECT coalesce(max(rowid), 0) + 1 FROM decisions").fetchone()[0]
    decision_id = f"D-{ctx.as_of[:7]}-{seq:02d}"
    k_dir = Path(ctx.cfg.paths.knowledge_dir)
    adr_path = str(k_dir / "decisions" / f"ADR-{decision_id}.md")
    timestamp = ctx.clock.iso()
    decided_by = ctx.actor.by

    ctx.conn.execute(
        """
        INSERT INTO decisions (
            decision_id, proposal_id, kind, tier, subject_id, title, context,
            options_json, decision, evidence_refs_json, criteria_check_json,
            decided_on, decided_by, approver_kind, ratified_by, ratified_on,
            status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
        ) VALUES (
            ?, NULL, 'data_fix', 1, ?, ?, ?,
            ?, ?, ?, NULL,
            ?, ?, ?, NULL, NULL,
            'approved', ?, NULL, ?, NULL, NULL, ?
        )
        """,
        (
            decision_id,
            canonical_isin,
            title or f"Corporate action resolution: {canonical_isin} {ex_date} ({stored_kind})",
            f"Suspected corporate action detected on {ex_date} with evidenced gross factor "
            f"{gross_factor:.4f}; reviewed against the operator-supplied evidence below.",
            json.dumps([{"kind": stored_kind, "factor": stored_factor}]),
            f"Record {stored_kind} with factor {stored_factor} for {canonical_isin} on {ex_date}",
            json.dumps([evidence]),
            timestamp,
            decided_by,
            ctx.actor.kind,
            ctx.as_of,
            adr_path,
            ctx.git_sha,
        ),
    )
    write_adr(ctx.conn, decision_id, k_dir / "decisions")

    add(ctx, isin=canonical_isin, ex_date=ex_date, kind=stored_kind, factor=stored_factor, decision_id=decision_id)

    return Result(
        status="ok",
        counts={"decisions": 1, "actions": 1},
        details={
            "decision_id": decision_id,
            "isin": canonical_isin,
            "security_id": sid,
            "ex_date": ex_date,
            "kind": stored_kind,
            "factor": stored_factor,
            "gross_factor": gross_factor,
            "adr_path": adr_path,
            "warning": warning,
        },
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
