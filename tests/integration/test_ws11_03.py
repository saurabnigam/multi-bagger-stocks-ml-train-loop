"""Integration tests for WS11.03: Verification, status, sign-off script and governance (C11)."""

import os
from pathlib import Path
import sqlite3
import subprocess
import pytest

from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.types import CheckReport
import quant.verify
import quant.status


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test_status.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    return conn, db_path


def test_status_read_reports_all_required_keys(tmp_path, cfg, test_db):
    """status.read includes publication, capture, blocked reason, orders, proposals, overdue ratifications, maturities, archive."""
    conn, db_path = test_db
    test_cfg = cfg.with_paths(db=db_path)

    # Empty state status
    st = quant.status.read(conn, test_cfg)
    assert isinstance(st, dict)
    for k in (
        "last_publication",
        "last_capture",
        "blocked_reason",
        "pending_orders",
        "pending_proposals",
        "overdue_ratifications",
        "next_possible_maturities",
        "source_archive_availability",
    ):
        assert k in st, f"Missing key in status: {k}"

    assert st["last_publication"] is None
    assert st["pending_orders"] == 0
    assert st["pending_proposals"] == 0
    assert st["overdue_ratifications"] == 0


def test_status_read_detects_overdue_ratifications_golden_governance(tmp_path, cfg, test_db):
    """Tier-1 LLM provisional decisions older than 60 days without ratification are marked overdue."""
    conn, db_path = test_db
    test_cfg = cfg.with_paths(db=db_path)

    # Insert a provisional Tier-1 decision from 70 days ago without ratification
    with conn:
        conn.execute(
            """
            INSERT INTO decisions (
                decision_id, kind, tier, subject_id, title, context, options_json,
                decision, evidence_refs_json, decided_on, decided_by, approver_kind,
                status, adr_path, git_sha
            ) VALUES (
                'D-2026-06-01', 'rule_change', 1, 'turnaround', 'Test Rule Change',
                'context', '[]', 'decision', '[]', '2026-06-01', 'llm:gemini-flash',
                'llm', 'provisional', 'knowledge/decisions/ADR-D-2026-06-01.md', 'sha'
            )
            """
        )

    st = quant.status.read(conn, test_cfg)
    assert st["overdue_ratifications"] >= 1


def test_verify_report_reproduces_evidence_refs(tmp_path, cfg, test_db):
    """verify.report verifies recorded evaluation evidence refs and returns CheckReport."""
    conn, db_path = test_db
    test_cfg = cfg.with_paths(db=db_path)

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'monthly', 'live', 1, '2026-09-30T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            """
            INSERT INTO evaluations (
                eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
                as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
                status, method, evidence_hash, revision
            ) VALUES (
                1, 1, '2026-09-30T18:30:00.000000Z', 'model', 'CHAMPION', '1',
                '2026-09-30', 3, 'universe', 'live', 'rank_ic', 0.05, 500, 480, 0.02, 0.01, 0.09,
                'estimable', 'spearman', 'ev_hash_1', 1
            )
            """
        )

    rep = quant.verify.report(conn, "2026-09-30", test_cfg)
    assert isinstance(rep, CheckReport)
    assert rep.passed


def test_verify_pit_returns_check_report(tmp_path, cfg, test_db):
    """verify.pit verifies point-in-time constraints over requested months and returns CheckReport."""
    conn, db_path = test_db
    test_cfg = cfg.with_paths(db=db_path)

    rep = quant.verify.pit(conn, 3, test_cfg)
    assert isinstance(rep, CheckReport)


def test_signoff_script_structure_and_no_automatic_pushes():
    """scripts/signoff.sh prints engineering/operational/longitudinal separately; monthly_cron.sh has no auto push."""
    signoff_sh = Path("scripts/signoff.sh")
    cron_sh = Path("monthly_cron.sh")

    assert signoff_sh.exists(), "scripts/signoff.sh must exist"
    signoff_content = signoff_sh.read_text(encoding="utf-8")
    assert "engineering" in signoff_content.lower()
    assert "operational" in signoff_content.lower()
    assert "longitudinal" in signoff_content.lower()
    assert "PASS" in signoff_content
    assert "DEFERRED" in signoff_content

    assert cron_sh.exists(), "monthly_cron.sh must exist"
    cron_content = cron_sh.read_text(encoding="utf-8")
    assert "git push" not in cron_content, "monthly_cron.sh must NOT contain automatic git push"


def test_root_docs_distinguish_legacy_invariants_from_v2():
    """README.md and AGENTS.md document V2 architecture and distinguish frozen legacy invariants."""
    readme = Path("README.md").read_text(encoding="utf-8")
    agents = Path("AGENTS.md").read_text(encoding="utf-8")

    for doc in (readme, agents):
        assert "quant_engine.db" in doc
        assert "03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8" in doc or "frozen" in doc.lower()
        assert "quant." in doc or "V2" in doc
