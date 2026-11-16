"""Architecture Decision Record (ADR) authoring, storage, and consistency verification (C09)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.types import Check, CheckReport


def write(conn: sqlite3.Connection, decision_id: str, output_dir: Path) -> Path:
    """Write an immutable ADR markdown document for a recorded decision."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Decision '{decision_id}' not found in decisions table")

    d = dict(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    adr_path = output_dir / f"ADR-{decision_id}.md"

    md_lines = [
        f"# ADR: {d['title']} ({decision_id})",
        "",
        f"- **Decision ID:** {decision_id}",
        f"- **Date:** {d['decided_on']}",
        f"- **Tier:** Tier {d['tier']}",
        f"- **Kind:** {d['kind']}",
        f"- **Subject ID:** {d['subject_id']}",
        f"- **Approver Kind:** {d['approver_kind']}",
        f"- **Decided By:** {d['decided_by']}",
        f"- **Status:** {d['status']}",
        f"- **Effective From:** {d.get('effective_from') or 'N/A'}",
        f"- **Ratified By:** {d.get('ratified_by') or 'N/A'}",
        f"- **Ratified On:** {d.get('ratified_on') or 'N/A'}",
        f"- **Git SHA:** {d.get('git_sha') or 'unknown'}",
        "",
        "## Context",
        d.get("context") or "N/A",
        "",
        "## Options Considered",
        d.get("options_json") or "[]",
        "",
        "## Decision",
        d.get("decision") or "N/A",
        "",
        "## Evidence References",
        d.get("evidence_refs_json") or "[]",
        "",
        "## Criteria Check",
        d.get("criteria_check_json") or "N/A",
        "",
    ]

    content = "\n".join(md_lines)
    adr_path.write_text(content, encoding="utf-8")

    conn.execute(
        "UPDATE decisions SET adr_path = ? WHERE decision_id = ?",
        (str(adr_path), decision_id),
    )
    return adr_path


def check(conn: sqlite3.Connection, knowledge_dir: Path) -> CheckReport:
    """Verify that every active/applied decision has an existing ADR file on disk."""
    d_rows = conn.execute(
        "SELECT decision_id, adr_path, status, title FROM decisions "
        "WHERE status IN ('approved', 'provisional', 'applied', 'reverted')"
    ).fetchall()

    checks: list[Check] = []
    for r in d_rows:
        did = r["decision_id"]
        adr_p_str = r["adr_path"]
        p = Path(adr_p_str) if adr_p_str else None

        if p and not p.is_absolute():
            candidates = [
                knowledge_dir.parent / p,
                knowledge_dir / "decisions" / p.name,
                knowledge_dir / p.name,
                knowledge_dir / p,
            ]
            p = next((c for c in candidates if c.exists()), candidates[0])

        if p and p.exists():
            checks.append(
                Check(
                    id=f"adr_{did}",
                    status="PASS",
                    observed=str(p),
                    expected="exists",
                    reason=f"ADR exists on disk for {did}",
                    blocking=True,
                )
            )
        else:
            checks.append(
                Check(
                    id=f"adr_{did}",
                    status="FAIL",
                    observed=str(p) if p else None,
                    expected="exists",
                    reason=f"Missing ADR markdown file on disk for decision {did}",
                    blocking=True,
                )
            )

    return CheckReport(checks=checks)
