"""Human Tier-2 rule-change decisions that switch on a governed engine rule (MASTER_SPEC R13).

A rule that changes what the engine admits is never a code default: it takes effect only where
a human decision for it is recorded in that database. This writes the ``decisions`` row and its
ADR the way ``quant.data.actions.resolve`` does, so ``kb check`` stays clean.
"""
from __future__ import annotations

import json
from pathlib import Path

from quant.errors import Refused
from quant.knowledge.adr import write as write_adr
from quant.run import RunContext

ALLOWED_RULE_SUBJECTS = {"late_price_admission", "price_fallback_sources"}


def record_rule_change(ctx: RunContext, *, subject_id: str, title: str, context: str, evidence: str) -> str:
    """Record an approved Tier-2 ``rule_change`` decision and its ADR; returns the decision id.

    Refused for a non-human actor, a subject outside ``ALLOWED_RULE_SUBJECTS``, or a subject that
    already has an approved/applied rule change (the message names it).
    """
    if ctx.actor.kind != "human":
        raise Refused(
            "governance",
            f"a rule change is a Tier-2 decision and needs --actor-kind human (actor was '{ctx.actor.kind}')",
        )
    if subject_id not in ALLOWED_RULE_SUBJECTS:
        raise Refused("unknown_subject",
                      f"'{subject_id}' is not a rule that can be enabled by decision; allowed: {sorted(ALLOWED_RULE_SUBJECTS)}")
    existing = ctx.conn.execute(
        "SELECT decision_id FROM decisions WHERE kind = 'rule_change' AND subject_id = ? "
        "AND status IN ('approved', 'applied') ORDER BY decided_on DESC, rowid DESC LIMIT 1",
        (subject_id,),
    ).fetchone()
    if existing:
        raise Refused("already_recorded", f"{subject_id} is already enabled under decision {existing[0]}")

    # the kb commands pass --by through whole ("human:NAME"); do not prefix the kind a second time
    decided_by = ctx.actor.name if ctx.actor.name.startswith(f"{ctx.actor.kind}:") else ctx.actor.by
    seq = ctx.conn.execute("SELECT coalesce(max(rowid), 0) + 1 FROM decisions").fetchone()[0]
    decision_id = f"D-{ctx.as_of[:7]}-{seq:02d}"
    k_dir = Path(ctx.cfg.paths.knowledge_dir)
    adr_path = str(k_dir / "decisions" / f"ADR-{decision_id}.md")
    ctx.conn.execute(
        """
        INSERT INTO decisions (
            decision_id, proposal_id, kind, tier, subject_id, title, context,
            options_json, decision, evidence_refs_json, criteria_check_json,
            decided_on, decided_by, approver_kind, ratified_by, ratified_on,
            status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
        ) VALUES (
            ?, NULL, 'rule_change', 2, ?, ?, ?,
            ?, ?, ?, NULL,
            ?, ?, ?, NULL, NULL,
            'approved', ?, NULL, ?, NULL, NULL, ?
        )
        """,
        (
            decision_id,
            subject_id,
            title,
            context,
            json.dumps([{"enable": subject_id}, {"keep": "strict cutoff for every input"}]),
            f"Enable {subject_id} from {ctx.as_of} (human Tier-2 rule change)",
            json.dumps([evidence]),
            ctx.clock.iso(),
            decided_by,
            ctx.actor.kind,
            ctx.as_of,
            adr_path,
            ctx.git_sha,
        ),
    )
    write_adr(ctx.conn, decision_id, k_dir / "decisions")
    return decision_id
