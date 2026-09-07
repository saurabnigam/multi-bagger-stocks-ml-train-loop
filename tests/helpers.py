from __future__ import annotations

import sqlite3
from typing import Any


def seed_minimal_state(conn: sqlite3.Connection) -> None:
    """Insert minimal valid rows satisfying foreign key constraints for tests."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'test', 'live', 1, '2026-10-01T00:00:00.000000Z', 'running', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'SYN1', 'Synthetic Security 1', '2026-01-01', '2026-09-30', 'listed')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, name, status, registered_at, registered_decision_id) "
            "VALUES ('quality', '[]', 'f1', 'H_TEST', 'Test Hypothesis', 'active', '2026-01-01T00:00:00.000000Z', 'DEC1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO models (model_id, name, status, role, created_at) "
            "VALUES ('M_CHAMPION', 'Champion Model', 'active', 'champion', '2026-01-01T00:00:00.000000Z')"
        )
