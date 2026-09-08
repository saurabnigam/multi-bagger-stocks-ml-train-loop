"""Proposal drafting, approval, human ratification, and governance state machine (C09)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

from quant.errors import Refused
from quant.knowledge.adr import write as write_adr
from quant.knowledge.review import factor as review_factor, model as review_model
from quant.types import Actor, Result

if TYPE_CHECKING:
    from quant.run import RunContext


TIER1_KINDS = {
    "promote_factor",
    "probation",
    "retire_factor",
    "accept_revision",
    "cost_model",
    "data_fix",
    "quarantine",
    "release_quarantine",
    "register_hypothesis",
}

TIER2_KINDS = {
    "promote_model",
    "demote_model",
    "rule_change",
    "taxonomy",
    "gate_override",
}


def _get_knowledge_dir(ctx: RunContext) -> Path:
    if hasattr(ctx.cfg, "paths") and hasattr(ctx.cfg.paths, "knowledge_dir"):
        return Path(ctx.cfg.paths.knowledge_dir)
    return Path("knowledge")


def draft(ctx: RunContext, as_of: str) -> list[str]:
    """Draft deduplicated proposals from active criteria reviews."""
    conn = ctx.conn
    if conn is None:
        return []

    run_id = getattr(ctx, "run_id", 1)
    timestamp = ctx.clock.now_iso() if getattr(ctx, "clock", None) else f"{as_of}T09:00:00.000000Z"
    drafted: list[str] = []

    # Check shadow / probation factors
    factors = conn.execute(
        "SELECT factor_id, name, version, status FROM factor_registry WHERE status IN ('shadow', 'probation')"
    ).fetchall()

    for f in factors:
        fid = f["factor_id"]
        check_res = review_factor(conn, fid, as_of, ctx.cfg)
        if check_res.eligible:
            # Check if existing open proposal
            existing = conn.execute(
                "SELECT proposal_id FROM proposals WHERE subject_id = ? AND status = 'proposed'",
                (fid,),
            ).fetchone()
            if not existing:
                seq = conn.execute(
                    "SELECT coalesce(max(rowid), 0) + 1 FROM proposals"
                ).fetchone()[0]
                pid = f"P-{as_of[:7]}-{seq:02d}"
                md_path = f"knowledge/proposals/{pid}.md"

                conn.execute(
                    """
                    INSERT INTO proposals (
                        proposal_id, created_run_id, as_of, kind, subject_id,
                        payload_json, evidence_json, rule_id, proposed_by, status, md_path
                    ) VALUES (?, ?, ?, 'promote_factor', ?, '{}', ?, 'factor_promotion_rule', 'system', 'proposed', ?)
                    """,
                    (pid, run_id, as_of, fid, json.dumps(check_res.evidence_ids), md_path),
                )
                drafted.append(pid)

    conn.commit()
    return drafted


def approve(ctx: RunContext, proposal_id: str, note: str = "") -> str:
    """Approve a proposal under Tier-1 (provisional for LLM) or Tier-2 (human only) authority."""
    conn = ctx.conn
    if conn is None:
        raise ValueError("RunContext connection required")

    actor = ctx.actor or Actor(kind="system", name="system")

    # Enforce governance rule: LLM actor claiming human prefix is refused
    if actor.kind == "llm" and actor.name.startswith("human:"):
        raise Refused("governance", f"LLM actor '{actor.name}' cannot claim human prefix")

    p_row = conn.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if not p_row:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    p = dict(p_row)
    kind = p["kind"]
    subject_id = p["subject_id"]

    tier = 2 if kind in TIER2_KINDS else 1

    # Enforce Tier 2 human-only authority
    if actor.kind == "llm" and tier == 2:
        raise Refused("governance", f"LLM actor cannot approve Tier 2 proposal '{kind}'; human authorization required")

    as_of = p["as_of"]
    timestamp = ctx.clock.now_iso() if getattr(ctx, "clock", None) else f"{as_of}T09:00:00.000000Z"
    git_sha = getattr(ctx, "git_sha", "unknown")

    # Tier 1 LLM approval yields provisional; human yields approved
    status = "provisional" if actor.kind == "llm" else "approved"

    seq = conn.execute(
        "SELECT coalesce(max(rowid), 0) + 1 FROM decisions"
    ).fetchone()[0]
    decision_id = f"D-{as_of[:7]}-{seq:02d}"

    k_dir = _get_knowledge_dir(ctx)
    adr_path = str(k_dir / "decisions" / f"ADR-{decision_id}.md")

    conn.execute(
        """
        INSERT INTO decisions (
            decision_id, proposal_id, kind, tier, subject_id, title, context,
            options_json, decision, evidence_refs_json, criteria_check_json,
            decided_on, decided_by, approver_kind, ratified_by, ratified_on,
            status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, NULL, NULL,
            ?, ?, NULL, ?, NULL, NULL, ?
        )
        """,
        (
            decision_id,
            proposal_id,
            kind,
            tier,
            subject_id,
            f"Decision for proposal {proposal_id} ({kind})",
            note or "Approved by governance workflow",
            json.dumps(["approve", "reject"]),
            "approve",
            p.get("evidence_json") or "[]",
            p.get("criteria_check_json"),
            timestamp,
            f"{actor.kind}:{actor.name}" if not actor.name.startswith(f"{actor.kind}:") else actor.name,
            actor.kind,
            status,
            as_of,
            adr_path,
            git_sha,
        ),
    )

    conn.execute(
        "UPDATE proposals SET status = 'approved', decided_on = ?, decided_by = ?, decision_id = ? WHERE proposal_id = ?",
        (timestamp, actor.name, decision_id, proposal_id),
    )
    conn.commit()

    write_adr(conn, decision_id, k_dir / "decisions")
    return decision_id


def reject(ctx: RunContext, proposal_id: str, note: str = "") -> str:
    """Reject a proposal with audit record."""
    conn = ctx.conn
    if conn is None:
        raise ValueError("RunContext connection required")

    actor = ctx.actor or Actor(kind="system", name="system")
    if actor.kind == "llm" and actor.name.startswith("human:"):
        raise Refused("governance", f"LLM actor '{actor.name}' cannot claim human prefix")

    p_row = conn.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if not p_row:
        raise ValueError(f"Proposal '{proposal_id}' not found")

    p = dict(p_row)
    as_of = p["as_of"]
    timestamp = ctx.clock.now_iso() if getattr(ctx, "clock", None) else f"{as_of}T09:00:00.000000Z"
    git_sha = getattr(ctx, "git_sha", "unknown")

    seq = conn.execute("SELECT coalesce(max(rowid), 0) + 1 FROM decisions").fetchone()[0]
    decision_id = f"D-{as_of[:7]}-{seq:02d}"

    k_dir = _get_knowledge_dir(ctx)
    adr_path = str(k_dir / "decisions" / f"ADR-{decision_id}.md")

    conn.execute(
        """
        INSERT INTO decisions (
            decision_id, proposal_id, kind, tier, subject_id, title, context,
            options_json, decision, evidence_refs_json, criteria_check_json,
            decided_on, decided_by, approver_kind, ratified_by, ratified_on,
            status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
        ) VALUES (
            ?, ?, ?, 1, ?, ?, ?,
            ?, 'reject', ?, ?,
            ?, ?, ?, NULL, NULL,
            'rejected', NULL, NULL, ?, NULL, NULL, ?
        )
        """,
        (
            decision_id,
            proposal_id,
            p["kind"],
            p["subject_id"],
            f"Rejection of proposal {proposal_id}",
            note or "Rejected by governance workflow",
            json.dumps(["approve", "reject"]),
            p.get("evidence_json") or "[]",
            p.get("criteria_check_json"),
            timestamp,
            f"{actor.kind}:{actor.name}" if not actor.name.startswith(f"{actor.kind}:") else actor.name,
            actor.kind,
            adr_path,
            git_sha,
        ),
    )

    conn.execute(
        "UPDATE proposals SET status = 'rejected', decided_on = ?, decided_by = ?, decision_id = ? WHERE proposal_id = ?",
        (timestamp, actor.name, decision_id, proposal_id),
    )
    conn.commit()

    write_adr(conn, decision_id, k_dir / "decisions")
    return decision_id


def ratify(ctx: RunContext, decision_id: str, note: str = "") -> Result:
    """Human co-signature ratifying an LLM provisional decision within 60 calendar days."""
    conn = ctx.conn
    if conn is None:
        raise ValueError("RunContext connection required")

    actor = ctx.actor or Actor(kind="system", name="system")
    if actor.kind != "human":
        raise Refused("governance", f"Ratification requires human actor, got '{actor.kind}'")

    d_row = conn.execute(
        "SELECT * FROM decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if not d_row:
        raise ValueError(f"Decision '{decision_id}' not found")

    d = dict(d_row)
    if d["status"] != "provisional":
        raise Refused("governance", f"Decision '{decision_id}' status is '{d['status']}', expected 'provisional'")

    decided_dt = datetime.fromisoformat(d["decided_on"].replace("Z", "+00:00"))
    now_dt = (
        ctx.clock.now()
        if (ctx and getattr(ctx, "clock", None))
        else datetime.now(timezone.utc)
    )
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    days_elapsed = (now_dt - decided_dt).total_seconds() / 86400.0
    if days_elapsed > 60.0:
        raise Refused("governance", f"Provisional decision '{decision_id}' expired ({days_elapsed:.1f} days > 60 days)")

    timestamp = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{now_dt.strftime('%Y-%m-%d')}T09:00:00.000000Z"
    )

    conn.execute(
        "UPDATE decisions SET status = 'approved', ratified_by = ?, ratified_on = ? WHERE decision_id = ?",
        (actor.name, timestamp, decision_id),
    )
    conn.commit()

    k_dir = _get_knowledge_dir(ctx)
    write_adr(conn, decision_id, k_dir / "decisions")
    return Result(status="ok", counts={"ratified": 1})


def apply(ctx: RunContext, as_of: str) -> Result:
    """Apply approved decisions prospectively and expire unratified provisional decisions."""
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"applied": 0})

    now_dt = (
        ctx.clock.now()
        if (ctx and getattr(ctx, "clock", None))
        else datetime.fromisoformat(f"{as_of}T09:00:00+00:00")
    )
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    now_iso = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{as_of}T09:00:00.000000Z"
    )
    git_sha = getattr(ctx, "git_sha", "unknown")
    k_dir = _get_knowledge_dir(ctx)

    # 1. Expiry check on provisional decisions
    prov_rows = conn.execute(
        "SELECT * FROM decisions WHERE status = 'provisional'"
    ).fetchall()

    for p_row in prov_rows:
        d = dict(p_row)
        decided_dt = datetime.fromisoformat(d["decided_on"].replace("Z", "+00:00"))
        days_elapsed = (now_dt - decided_dt).total_seconds() / 86400.0

        if days_elapsed > 60.0:
            # Expired! Create reversion decision
            rev_did = f"REV-{d['decision_id']}"
            adr_p = str(k_dir / "decisions" / f"ADR-{rev_did}.md")

            conn.execute(
                """
                INSERT OR IGNORE INTO decisions (
                    decision_id, proposal_id, kind, tier, subject_id, title, context,
                    options_json, decision, evidence_refs_json, criteria_check_json,
                    decided_on, decided_by, approver_kind, ratified_by, ratified_on,
                    status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
                ) VALUES (
                    ?, ?, 'reversion', 0, ?, ?, 'Automated reversion of unratified provisional decision after 60 days',
                    '[]', 'revert', '[]', NULL,
                    ?, 'system:expiry', 'system', NULL, NULL,
                    'applied', ?, ?, ?, ?, NULL, ?
                )
                """,
                (
                    rev_did,
                    d.get("proposal_id"),
                    d["subject_id"],
                    f"Reversion of expired provisional decision {d['decision_id']}",
                    now_iso,
                    as_of,
                    now_iso,
                    adr_p,
                    d["decision_id"],
                    git_sha,
                ),
            )

            conn.execute(
                "UPDATE decisions SET status = 'reverted', reverted_by = ? WHERE decision_id = ?",
                (rev_did, d["decision_id"]),
            )

            # Revert factor status in factor_registry if applicable
            if d["kind"] == "promote_factor":
                conn.execute(
                    "UPDATE factor_registry SET status = 'shadow', status_changed_on = ? WHERE factor_id = ?",
                    (as_of, d["subject_id"]),
                )

            # Append prospective model version if applicable
            conn.execute(
                """
                INSERT OR IGNORE INTO model_versions (
                    model_id, version, factor_set_json, weights_json, valid_from, valid_to, decision_id, note
                ) VALUES (
                    'EW_HIER_v1', 2, '[]', '{}', ?, NULL, ?, 'Automated reversion model version'
                )
                """,
                (as_of, rev_did),
            )

            write_adr(conn, rev_did, k_dir / "decisions")

    # 2. Apply approved decisions
    app_rows = conn.execute(
        "SELECT * FROM decisions WHERE status = 'approved' AND (applied_on IS NULL OR applied_on = '')"
    ).fetchall()

    applied_count = 0
    for a_row in app_rows:
        d = dict(a_row)
        if d["kind"] == "promote_factor":
            conn.execute(
                "UPDATE factor_registry SET status = 'active', status_changed_on = ? WHERE factor_id = ?",
                (as_of, d["subject_id"]),
            )
        conn.execute(
            "UPDATE decisions SET status = 'applied', applied_on = ? WHERE decision_id = ?",
            (now_iso, d["decision_id"]),
        )
        applied_count += 1

    conn.commit()
    return Result(status="ok", counts={"applied": applied_count})


def authorize(
    conn: sqlite3.Connection,
    actor: Actor,
    decision_id: str,
    kind: str,
    subject_id: str,
    now: str,
) -> None:
    """Validate that execution authority exists and is unexpired."""
    d_row = conn.execute(
        "SELECT * FROM decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if not d_row:
        raise Refused("governance", f"Decision '{decision_id}' not found")

    d = dict(d_row)
    if d["status"] not in ("approved", "provisional", "applied"):
        raise Refused("governance", f"Decision '{decision_id}' is not in valid status ({d['status']})")

    if d["status"] == "provisional":
        decided_dt = datetime.fromisoformat(d["decided_on"].replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if (now_dt - decided_dt).total_seconds() / 86400.0 > 60.0:
            raise Refused("governance", f"Provisional decision '{decision_id}' has expired")
