"""System bootstrap seeding of launch factors, models, hypotheses, and decision (C09)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

from quant.factors.registry import LAUNCH_FACTOR_CLASSES, sync as sync_factors
from quant.model.models import seed as seed_models
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


def _calc_code_sha(spec: Any) -> str:
    content = f"{spec.name}:{spec.version}:{spec.formula}:{','.join(sorted(spec.inputs))}:{spec.direction}:{spec.hypothesis}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def seed(ctx: RunContext, spec_sha256: str) -> Result:
    """Seed only exact launch definitions with system/spec-hash decision, idempotently.

    - Creates Tier-0 system decision 'DEC_BOOTSTRAP' referencing spec_sha256.
    - Seeds launch models and frozen version-1 definitions.
    - Seeds launch factor definitions into factor_registry.
    - Seeds launch hypotheses with counts_toward_budget = 0 (launch exemption).
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"seeded": 0})

    as_of = ctx.as_of or "2026-09-01"
    timestamp = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{as_of}T00:00:00.000000Z"
    )
    git_sha = getattr(ctx, "git_sha", "unknown")

    # 1. Tier-0 system bootstrap decision
    conn.execute(
        """
        INSERT OR IGNORE INTO decisions (
            decision_id, kind, tier, subject_id, title, context, options_json,
            decision, evidence_refs_json, decided_on, decided_by, approver_kind,
            status, adr_path, git_sha
        ) VALUES (
            'DEC_BOOTSTRAP', 'system_bootstrap', 0, 'launch_set', 'System bootstrap launch set',
            'Initial system bootstrap referencing spec hash', '["bootstrap"]', 'approved',
            ?, ?, 'system', 'system',
            'applied', 'knowledge/decisions/ADR-0001-bootstrap.md', ?
        )
        """,
        (json.dumps([spec_sha256]), timestamp, git_sha),
    )

    k_dir = Path(ctx.cfg.paths.knowledge_dir) if hasattr(ctx.cfg, "paths") and hasattr(ctx.cfg.paths, "knowledge_dir") else Path("knowledge")
    adr_file = k_dir / "decisions" / "ADR-0001-bootstrap.md"
    adr_file.parent.mkdir(parents=True, exist_ok=True)
    if not adr_file.exists():
        adr_file.write_text(
            f"# ADR: System bootstrap launch set (DEC_BOOTSTRAP)\n\n"
            f"- **Decision ID:** DEC_BOOTSTRAP\n"
            f"- **Date:** {timestamp}\n"
            f"- **Tier:** Tier 0\n"
            f"- **Kind:** system_bootstrap\n"
            f"- **Subject ID:** launch_set\n"
            f"- **Approver Kind:** system\n"
            f"- **Decided By:** system\n"
            f"- **Status:** applied\n\n"
            f"## Context\nInitial system bootstrap referencing spec hash {spec_sha256}.\n\n"
            f"## Decision\napproved\n",
            encoding="utf-8",
        )

    # 2. Seed launch models
    seed_models(ctx, "DEC_BOOTSTRAP")

    # 3. Seed launch factors
    factor_specs = [cls().spec for cls in LAUNCH_FACTOR_CLASSES.values()]
    sync_factors(ctx, factor_specs)

    # 4. Seed launch hypotheses (counts_toward_budget = 0)
    budget_year = int(as_of[:4])
    hypotheses_seeded = 0

    # 4a. Factor hypotheses
    for spec in factor_specs:
        hid = f"H-LAUNCH-{spec.name}"
        formula_sha = hashlib.sha256(spec.formula.encode("utf-8")).hexdigest()
        code_sha = _calc_code_sha(spec)

        seq_row = conn.execute(
            "SELECT coalesce(max(sequence_in_year), 0) + 1 FROM hypotheses WHERE budget_year = ?",
            (budget_year,),
        ).fetchone()
        seq = seq_row[0]

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO hypotheses (
                family, review_opportunities_json, formula_sha256, hypothesis_id, kind,
                subject_id, title, statement, expected_sign, horizon_m, primary_metric,
                success_criterion, failure_criterion, registered_on, registered_by,
                first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget,
                status, decision_id, md_path
            ) VALUES (
                ?, ?, ?, ?, 'factor',
                ?, ?, ?, ?, 3, 'ic',
                'ic >= 0.02', 'ic < 0', ?, 'system',
                ?, ?, ?, ?, 0,
                'open', 'DEC_BOOTSTRAP', ?
            )
            """,
            (
                spec.family,
                json.dumps([12, 24, 36]),
                formula_sha,
                hid,
                spec.factor_id,
                f"Launch factor {spec.name}",
                spec.hypothesis,
                spec.direction,
                timestamp,
                as_of,
                code_sha,
                budget_year,
                seq,
                f"knowledge/hypotheses/{hid}.md",
            ),
        )
        if cur.rowcount > 0:
            hypotheses_seeded += 1

    # 4b. Model hypotheses
    launch_model_ids = ["EW_HIER_v1", "EW_FLAT_v1", "MOM_ONLY_v1", "IC_SHRUNK_v1"]
    for mid in launch_model_ids:
        hid = f"H-LAUNCH-{mid}"
        m_sha = hashlib.sha256(mid.encode("utf-8")).hexdigest()

        seq_row = conn.execute(
            "SELECT coalesce(max(sequence_in_year), 0) + 1 FROM hypotheses WHERE budget_year = ?",
            (budget_year,),
        ).fetchone()
        seq = seq_row[0]

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO hypotheses (
                family, review_opportunities_json, formula_sha256, hypothesis_id, kind,
                subject_id, title, statement, expected_sign, horizon_m, primary_metric,
                success_criterion, failure_criterion, registered_on, registered_by,
                first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget,
                status, decision_id, md_path
            ) VALUES (
                'composite', ?, ?, ?, 'model',
                ?, ?, ?, 1, 3, 'excess_net',
                'excess_net > 0', 'excess_net <= 0', ?, 'system',
                ?, ?, ?, ?, 0,
                'open', 'DEC_BOOTSTRAP', ?
            )
            """,
            (
                json.dumps([24, 36, 48]),
                m_sha,
                hid,
                mid,
                f"Launch model {mid}",
                f"Launch composite model {mid}",
                timestamp,
                as_of,
                m_sha,
                budget_year,
                seq,
                f"knowledge/hypotheses/{hid}.md",
            ),
        )
        if cur.rowcount > 0:
            hypotheses_seeded += 1

    conn.commit()
    return Result(status="ok", counts={"hypotheses_seeded": hypotheses_seeded})
