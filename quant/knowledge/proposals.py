"""Proposal drafting, approval, human ratification, and governance state machine (C09)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.db.core import append_rows, update_control
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

# Forward transition applied prospectively when a decision of this kind takes effect.
_APPLY_TARGET_STATUS = {
    "promote_factor": "active",
    "probation": "probation",
    "retire_factor": "retired",
}

# Reversion plan (spec 9.3): undo exactly the recorded effect, one lifecycle step back.
_REVERSION_TARGET_STATUS = {
    "promote_factor": "shadow",
    "probation": "active",
    "retire_factor": "probation",
}


def _get_knowledge_dir(ctx: RunContext) -> Path:
    if hasattr(ctx.cfg, "paths") and hasattr(ctx.cfg.paths, "knowledge_dir"):
        return Path(ctx.cfg.paths.knowledge_dir)
    return Path("knowledge")


def _ratification_days(ctx: RunContext) -> float:
    budget = getattr(ctx.cfg, "budget", None) if ctx and getattr(ctx, "cfg", None) else None
    days = getattr(budget, "llm_ratification_days", None) if budget is not None else None
    return float(days) if days else 60.0


def _has_open_governance_action(conn: sqlite3.Connection, kind: str, subject_id: str) -> bool:
    """True if `subject_id` already has a proposed proposal or an unresolved decision of `kind`.

    Dedup key is (kind, subject_id): a proposal or decision of a different kind for the same
    subject (or vice versa) never blocks drafting.
    """
    existing_proposal = conn.execute(
        "SELECT 1 FROM proposals WHERE kind = ? AND subject_id = ? AND status = 'proposed'",
        (kind, subject_id),
    ).fetchone()
    if existing_proposal:
        return True
    existing_decision = conn.execute(
        "SELECT 1 FROM decisions WHERE kind = ? AND subject_id = ? AND status IN ('provisional', 'approved')",
        (kind, subject_id),
    ).fetchone()
    return existing_decision is not None


def _factor_status_history_row(factor_id: str, effective_from: str, status: str, decision_id: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "factor_id": factor_id,
        "effective_from": effective_from,
        "status": status,
        "decision_id": decision_id,
    }])


def _apply_factor_transition(
    ctx: RunContext, factor_id: str, new_status: str, as_of: str, decision_id: str
) -> None:
    """Move a factor to `new_status` via the allowlisted control-update path and append the
    matching factor_status_history row (append-only; never touched by a raw UPDATE).

    effective_from is the prospective as_of date (spec 9.3: "applying at month-end is
    prospective"), not a wall-clock timestamp, so at most one status change per factor per
    run is representable -- two conflicting transitions for the same factor on the same as_of
    correctly raise ImmutableConflict as a genuinely inconsistent governance state.
    """
    conn = ctx.conn
    row = conn.execute("SELECT factor_id FROM factor_registry WHERE factor_id = ?", (factor_id,)).fetchone()
    if row is None:
        return
    update_control(
        ctx,
        "factor_registry",
        {"factor_id": factor_id},
        {"status": new_status, "status_changed_on": as_of, "status_decision_id": decision_id},
    )
    append_rows(
        ctx,
        "factor_status_history",
        _factor_status_history_row(factor_id, as_of, new_status, decision_id),
        ["factor_id", "effective_from"],
    )


def _close_factor_from_model_versions(ctx: RunContext, factor_id: str, as_of: str, decision_id: str) -> None:
    """Close every model's current version that includes `factor_id` and append a NEW version
    without it (factor_set_json minus this factor; weights_json copied unchanged). The prior
    version's valid_to is set via update_control; a new version row is appended, never an
    existing one rewritten (spec 9.3 reversion plan)."""
    conn = ctx.conn
    current_versions = conn.execute(
        "SELECT model_id, version, factor_set_json, weights_json FROM model_versions WHERE valid_to IS NULL"
    ).fetchall()
    for row in current_versions:
        try:
            factor_set = json.loads(row["factor_set_json"] or "[]")
        except (ValueError, TypeError):
            factor_set = []
        if not isinstance(factor_set, list):
            continue
        if not any(isinstance(item, dict) and item.get("factor_id") == factor_id for item in factor_set):
            continue

        new_factor_set = [item for item in factor_set if not (isinstance(item, dict) and item.get("factor_id") == factor_id)]
        new_version = int(row["version"]) + 1

        update_control(
            ctx,
            "model_versions",
            {"model_id": row["model_id"], "version": row["version"]},
            {"valid_to": as_of},
        )
        new_row = pd.DataFrame([{
            "model_id": row["model_id"],
            "version": new_version,
            "factor_set_json": json.dumps(new_factor_set),
            "weights_json": row["weights_json"],
            "valid_from": as_of,
            "valid_to": None,
            "decision_id": decision_id,
            "note": f"Reversion of {factor_id}: dropped from factor_set",
        }])
        append_rows(ctx, "model_versions", new_row, ["model_id", "version"])


def _apply_decision_effect(ctx: RunContext, d: dict[str, Any], as_of: str) -> None:
    """Apply the prospective effect of an approved/provisional decision (spec 9.1 step 2).

    Only factor lifecycle transitions (promote/probation/retire) are owned by this module;
    other Tier-1/Tier-2 kinds (data fixes, cost recalibration, quarantine, taxonomy, model
    promotion/demotion, ...) have their substantive effects applied by the modules that own
    those tables, so only bookkeeping (applied_on/status) happens here for them.
    """
    kind = d["kind"]
    target_status = _APPLY_TARGET_STATUS.get(kind)
    if target_status is not None:
        _apply_factor_transition(ctx, d["subject_id"], target_status, as_of, d["decision_id"])


def _draft_one(
    conn: sqlite3.Connection,
    run_id: int,
    as_of: str,
    kind: str,
    subject_id: str,
    rule_id: str,
    evidence_ids: list[Any],
) -> str | None:
    if _has_open_governance_action(conn, kind, subject_id):
        return None
    seq = conn.execute("SELECT coalesce(max(rowid), 0) + 1 FROM proposals").fetchone()[0]
    pid = f"P-{as_of[:7]}-{seq:02d}"
    md_path = f"knowledge/proposals/{pid}.md"
    conn.execute(
        """
        INSERT INTO proposals (
            proposal_id, created_run_id, as_of, kind, subject_id,
            payload_json, evidence_json, rule_id, proposed_by, status, md_path
        ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'system', 'proposed', ?)
        """,
        (pid, run_id, as_of, kind, subject_id, json.dumps(evidence_ids), rule_id, md_path),
    )
    return pid


def draft(ctx: RunContext, as_of: str) -> list[str]:
    """Draft deduplicated proposals from active criteria reviews.

    Dedup key is (kind, subject_id) across proposals already 'proposed' and decisions still
    'provisional' or 'approved' (approved-but-not-yet-applied): a subject with any such
    in-flight governance action for that kind is never proposed again. Reviews both shadow/
    probation factors (promote_factor) and challenger models (promote_model).
    """
    conn = ctx.conn
    if conn is None:
        return []

    run_id = getattr(ctx, "run_id", None) or 1
    drafted: list[str] = []

    # Shadow / probation factors: promote_factor proposals.
    factors = conn.execute(
        "SELECT factor_id FROM factor_registry WHERE status IN ('shadow', 'probation')"
    ).fetchall()
    for f in factors:
        fid = f["factor_id"]
        check_res = review_factor(conn, fid, as_of, ctx.cfg)
        if check_res.eligible:
            pid = _draft_one(conn, run_id, as_of, "promote_factor", fid, "factor_promotion_rule", check_res.evidence_ids)
            if pid:
                drafted.append(pid)

    # Challenger models: promote_model proposals.
    models = conn.execute("SELECT model_id FROM models WHERE role = 'challenger'").fetchall()
    for m in models:
        mid = m["model_id"]
        check_res = review_model(conn, mid, as_of, ctx.cfg)
        if check_res.eligible:
            pid = _draft_one(conn, run_id, as_of, "promote_model", mid, "model_promotion_rule", check_res.evidence_ids)
            if pid:
                drafted.append(pid)

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

    ratification_days = _ratification_days(ctx)
    days_elapsed = (now_dt - decided_dt).total_seconds() / 86400.0
    if days_elapsed > ratification_days:
        raise Refused(
            "governance",
            f"Provisional decision '{decision_id}' expired ({days_elapsed:.1f} days > {ratification_days:.0f} days)",
        )

    timestamp = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{now_dt.strftime('%Y-%m-%d')}T09:00:00.000000Z"
    )

    conn.execute(
        "UPDATE decisions SET status = 'approved', ratified_by = ?, ratified_on = ? WHERE decision_id = ?",
        (actor.name, timestamp, decision_id),
    )

    k_dir = _get_knowledge_dir(ctx)
    write_adr(conn, decision_id, k_dir / "decisions")
    return Result(status="ok", counts={"ratified": 1})


def apply(ctx: RunContext, as_of: str) -> Result:
    """Apply approved/provisional decisions prospectively; expire unratified provisional ones.

    Order matches spec 9.1 step 2 ("process already authorized expiry/reversion effects
    before selecting definitions"): expiry runs first, then not-yet-applied effects.

    A Tier-1 LLM approval is provisional but takes effect prospectively immediately, same as
    a human approval -- only its `status` stays 'provisional' until a human ratifies it
    (`applied_on` records when the effect actually landed). If it is never ratified within
    `cfg.budget.llm_ratification_days`, the effect is reverted here: the reversion plan is the
    exact inverse lifecycle step (promote_factor -> shadow, probation -> active, retire_factor
    -> probation), recorded with its own factor_status_history row, and any model whose current
    factor_set includes the factor gets a new version (never a rewritten one) with the factor
    dropped and the old version's valid_to closed.

    A provisional decision whose effect was never actually staged (`applied_on` still NULL --
    e.g. this is the first `apply()` call since it was decided, or an intervening run was
    blocked per spec 9.1) has nothing recorded to invert: fabricating a reversion decision and
    a new model_version for an effect that was never applied would misrepresent what happened.
    Spec 9.3 is silent on this case, so the conservative reading is taken: such a decision is
    simply closed out as a terminal non-effect (status 'rejected') without touching
    factor_registry, factor_status_history, or model_versions.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"applied": 0, "reverted": 0})

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
    ratification_days = _ratification_days(ctx)

    # 1. Expiry: unratified provisional decisions past the ratification window are reverted
    # before any of this run's own new effects are applied.
    prov_rows = conn.execute("SELECT * FROM decisions WHERE status = 'provisional'").fetchall()

    reverted_count = 0
    for p_row in prov_rows:
        d = dict(p_row)
        decided_dt = datetime.fromisoformat(d["decided_on"].replace("Z", "+00:00"))
        days_elapsed = (now_dt - decided_dt).total_seconds() / 86400.0
        if days_elapsed <= ratification_days:
            continue

        if not d.get("applied_on"):
            # Nothing was ever staged for this decision, so there is no recorded effect to
            # revert -- close it out without touching factor_registry/model_versions.
            update_control(ctx, "decisions", {"decision_id": d["decision_id"]}, {"status": "rejected"})
            continue

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
                ?, ?, 'reversion', 0, ?, ?,
                'Automated reversion of unratified provisional decision after the ratification window',
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

        target_status = _REVERSION_TARGET_STATUS.get(d["kind"])
        if target_status is not None:
            _apply_factor_transition(ctx, d["subject_id"], target_status, as_of, rev_did)
            _close_factor_from_model_versions(ctx, d["subject_id"], as_of, rev_did)

        update_control(ctx, "decisions", {"decision_id": d["decision_id"]}, {"status": "reverted", "reverted_by": rev_did})

        write_adr(conn, rev_did, k_dir / "decisions")
        reverted_count += 1

    # 2. Apply not-yet-applied approved/provisional decisions prospectively. A human 'approved'
    # decision becomes terminal 'applied'; a Tier-1 LLM 'provisional' decision keeps its status
    # (pending ratification) but still gets its effect applied and its applied_on recorded, so a
    # later ratification (or expiry) never re-applies or re-reverts the same effect twice.
    pending_rows = conn.execute(
        "SELECT * FROM decisions WHERE status IN ('approved', 'provisional') AND (applied_on IS NULL OR applied_on = '')"
    ).fetchall()

    applied_count = 0
    for a_row in pending_rows:
        d = dict(a_row)
        _apply_decision_effect(ctx, d, as_of)
        changes: dict[str, Any] = {"applied_on": now_iso}
        if d["status"] == "approved":
            changes["status"] = "applied"
        update_control(ctx, "decisions", {"decision_id": d["decision_id"]}, changes)
        applied_count += 1

    return Result(status="ok", counts={"applied": applied_count, "reverted": reverted_count})


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
