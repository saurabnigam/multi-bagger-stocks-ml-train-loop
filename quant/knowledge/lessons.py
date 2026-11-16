"""Lessons ledger recording and markdown mirroring (C09)."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


def record(
    conn: sqlite3.Connection,
    knowledge_dir: Path,
    recorded_on: str,
    source: str,
    text: str,
    evidence_refs: list[str] | None = None,
    decision_id: str | None = None,
    tags: str | None = None,
) -> int:
    """Record a lesson learned into the database and mirror to lessons.md."""
    evidence_json = json.dumps(evidence_refs or [])
    cur = conn.execute(
        """
        INSERT INTO lessons (recorded_on, source, text, evidence_refs_json, decision_id, tags)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (recorded_on, source, text, evidence_json, decision_id, tags),
    )
    lesson_id = cur.lastrowid or 0

    sync_markdown(conn, knowledge_dir)
    return lesson_id


def sync_markdown(conn: sqlite3.Connection, knowledge_dir: Path) -> Path:
    """Regenerate knowledge/lessons.md from the lessons table."""
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    lessons_path = knowledge_dir / "lessons.md"

    rows = conn.execute(
        "SELECT lesson_id, recorded_on, source, text, evidence_refs_json, decision_id, tags FROM lessons ORDER BY lesson_id ASC"
    ).fetchall()

    lines = [
        "# Knowledge Base: Lessons Ledger",
        "",
        "Append-only ledger of quantitative, data, and operational lessons learned.",
        "",
    ]

    for r in rows:
        lid = r["lesson_id"]
        dt = r["recorded_on"]
        src = r["source"]
        txt = r["text"]
        refs = json.loads(r["evidence_refs_json"] or "[]")
        did = r["decision_id"]
        t = r["tags"]

        meta_parts = [f"Source: {src}"]
        if did:
            meta_parts.append(f"Decision: {did}")
        if refs:
            meta_parts.append(f"Evidence: {', '.join(refs)}")
        if t:
            meta_parts.append(f"Tags: {t}")
        meta_str = " | ".join(meta_parts)

        lines.append(f"### Lesson {lid} ({dt})")
        lines.append(f"_{meta_str}_")
        lines.append("")
        lines.append(txt)
        lines.append("")

    content = "\n".join(lines)
    lessons_path.write_text(content, encoding="utf-8")
    return lessons_path


def list_lessons(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List all recorded lessons."""
    rows = conn.execute("SELECT * FROM lessons ORDER BY lesson_id ASC").fetchall()
    return [dict(r) for r in rows]
