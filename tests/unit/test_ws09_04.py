"""Tests for WS09.04: Reports and knowledge mirrors (C09)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pytest

from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.knowledge.lessons import record as record_lesson, sync_markdown as sync_lessons_markdown
from quant.knowledge.report import render, render_backfill
from quant.types import CheckReport


@pytest.fixture
def golden_cases():
    cases_path = Path("docs/spec/contracts/golden_cases.json")
    with open(cases_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", data)


@pytest.fixture
def test_env(tmp_path, cfg):
    db_path = tmp_path / "test_report.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=knowledge_dir)

    # Populate baseline run, cohort, securities, portfolios, evaluations, decisions, orders
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'monthly', 'live', 1, '2026-09-30T18:30:00.000000Z', 'ok', 'sha123', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def1', 'mem1', '[]', '2026-09-30T19:00:00.000000Z', '2026-09-30T18:45:00.000000Z', 1, 1)"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('backfill:2026-09-30', '2026-09-30', 'backfill', '2026-09-30T18:29:59.999999Z', 'def2', 'mem2', '[]', '2026-09-30T19:00:00.000000Z', '2026-09-30T18:45:00.000000Z', 1, 1)"
        )
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (101, 'INE001', 'Test Security', '2026-01-01', '2026-09-30', 'listed')"
        )
        conn.execute(
            """
            INSERT INTO portfolios (
                portfolio_id, model_id, subject_kind, subject_id, subject_version,
                cohort_id, rule, cadence, inception, rule_version
            ) VALUES (
                'TOP30_GROWTH', NULL, 'model', 'TOP30', '1',
                'live:2026-09-30', 'top30_buffer', 'monthly', '2026-09-01', '1'
            )
            """
        )

        # Evaluations: one estimable, one insufficient
        conn.execute(
            """
            INSERT INTO evaluations (
                eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
                as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
                status, method, window_start, window_end, evidence_hash, revision, supersedes_eval_id
            ) VALUES (
                1, 1, '2026-09-30T18:50:00.000000Z', 'factor', 'mom_12_1@1', '1',
                '2026-09-30', 3, 'universe', 'live', 'ic', 0.052, 500, 480.0, 0.02, 0.012, 0.092,
                'estimable', 'spearman', '2026-06-30', '2026-09-30', 'ev_hash_1', 1, NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO evaluations (
                eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
                as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
                status, method, window_start, window_end, evidence_hash, revision, supersedes_eval_id
            ) VALUES (
                2, 1, '2026-09-30T18:50:00.000000Z', 'factor', 'roce@1', '1',
                '2026-09-30', 3, 'universe', 'live', 'ic', 0.015, 3, 1.0, NULL, NULL, NULL,
                'insufficient', 'hac', '2026-06-30', '2026-09-30', 'ev_hash_2', 1, NULL
            )
            """
        )
        # Decision
        conn.execute(
            """
            INSERT INTO decisions (
                decision_id, kind, tier, subject_id, title, context, options_json,
                decision, evidence_refs_json, decided_on, decided_by, approver_kind,
                ratified_by, ratified_on, status, effective_from, applied_on, adr_path, git_sha
            ) VALUES (
                'D-2026-09-01', 'promote_factor', 1, 'roce@1', 'Promote Roce Factor', 'context note',
                '["approve", "reject"]', 'approve', '["ev_hash_2"]', '2026-09-01T09:00:00.000000Z',
                'llm:gemini-flash', 'llm', 'human:alice', '2026-09-05T10:00:00.000000Z', 'approved',
                '2026-09-01', '2026-09-05T10:00:00.000000Z', 'knowledge/decisions/ADR-D-2026-09-01.md', 'sha123'
            )
            """
        )
        # Pending orders in portfolio_orders
        conn.execute(
            """
            INSERT INTO portfolio_orders (
                order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at,
                purpose, side, target_weight, status, liquidity_bucket, decision_id
            ) VALUES (
                'ord_1', 'TOP30_GROWTH', 'live:2026-09-30', 101, '2026-09-30T18:45:00.000000Z',
                '2026-10-01T09:15:00.000000Z', 'rebalance', 'buy', 0.05, 'pending',
                'top_adv', 'D-2026-09-01'
            )
            """
        )

    return conn, test_cfg, knowledge_dir


def test_render_persisted_evidence_only(test_env):
    """Render report with persisted evidence, manifest, required footer, and cutoff times."""
    conn, cfg, knowledge_dir = test_env

    report_path = render(conn, "2026-09-30", cfg)
    assert report_path.exists()
    assert report_path.suffix == ".md"

    # Manifest json exists alongside report
    manifest_path = report_path.with_suffix(".json")
    assert manifest_path.exists()

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["as_of"] == "2026-09-30"
    assert manifest["track"] == "live"
    assert manifest["report_id"] == report_path.stem
    assert "known_at" in manifest
    assert "selected_evaluation_keys" in manifest
    assert "control_summaries" in manifest

    # Markdown content check
    content = report_path.read_text(encoding="utf-8")
    # Required footer
    assert "Small or dependent samples may not distinguish skill from noise." in content
    # Actual cutoff and publication times
    assert "2026-09-30T18:29:59.999999Z" in content
    assert "2026-09-30T19:00:00.000000Z" in content
    # Pending orders mentioned
    assert "TOP30_GROWTH" in content or "ord_1" in content or "pending" in content
    # Ratification mentioned
    assert "human:alice" in content
    # Estimable has CI band, insufficient does not fabricate finite band
    assert "[0.0120, 0.0920]" in content or "0.012" in content
    assert "insufficient" in content


def test_render_separate_tracks(test_env):
    """Render separates live track and backfill track."""
    conn, cfg, knowledge_dir = test_env

    live_p = render(conn, "2026-09-30", cfg)
    bf_p = render_backfill(conn, "2026-09-30", cfg)

    assert live_p != bf_p
    assert live_p.exists()
    assert bf_p.exists()

    with open(live_p.with_suffix(".json"), "r", encoding="utf-8") as f:
        live_m = json.load(f)
    with open(bf_p.with_suffix(".json"), "r", encoding="utf-8") as f:
        bf_m = json.load(f)

    assert live_m["track"] == "live"
    assert bf_m["track"] == "backfill"


def test_idempotent_render_no_changes(test_env):
    """Rendering same snapshot without DB changes produces identical report_id."""
    conn, cfg, _ = test_env

    p1 = render(conn, "2026-09-30", cfg)
    p2 = render(conn, "2026-09-30", cfg)
    assert p1 == p2


def test_reproduce_old_report_after_later_revisions(test_env):
    """Supplied snapshot reproduces exact original report even after revisions are logged."""
    conn, cfg, _ = test_env

    # 1. Initial report
    p1 = render(conn, "2026-09-30", cfg)
    with open(p1.with_suffix(".json"), "r", encoding="utf-8") as f:
        orig_snapshot = json.load(f)
    orig_text = p1.read_text(encoding="utf-8")

    # 2. Add evaluation revision 2 with revised value
    with conn:
        conn.execute(
            """
            INSERT INTO evaluations (
                eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
                as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
                status, method, window_start, window_end, evidence_hash, revision, supersedes_eval_id
            ) VALUES (
                3, 1, '2026-10-05T12:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
                '2026-09-30', 3, 'universe', 'live', 'ic', 0.088, 500, 480.0, 0.02, 0.040, 0.120,
                'estimable', 'spearman', '2026-06-30', '2026-09-30', 'ev_hash_rev2', 2, 1
            )
            """
        )

    # 3. Re-render with orig_snapshot reproduces exact orig_text
    reproduced_path = render(conn, "2026-09-30", cfg, snapshot=orig_snapshot)
    assert reproduced_path == p1
    assert p1.read_text(encoding="utf-8") == orig_text

    # 4. Rendering without snapshot creates a new report reflecting revision 2
    new_path = render(conn, "2026-09-30", cfg)
    assert new_path != p1
    new_text = new_path.read_text(encoding="utf-8")
    assert "0.088" in new_text


def test_lessons_ledger_and_markdown_mirror(test_env):
    """Lessons recorded to DB and mirrored to knowledge/lessons.md."""
    conn, cfg, knowledge_dir = test_env

    lid = record_lesson(
        conn=conn,
        knowledge_dir=knowledge_dir,
        recorded_on="2026-09-30",
        source="red_team_review",
        text="Dividends in yfinance were fractional in older versions but percent in v1.x; normalize yield.",
        evidence_refs=["ADR-0001-bootstrap"],
        decision_id="DEC_BOOTSTRAP",
        tags="data_quality,yield",
    )
    assert lid > 0

    lessons_md = knowledge_dir / "lessons.md"
    assert lessons_md.exists()
    content = lessons_md.read_text(encoding="utf-8")
    assert "normalize yield" in content
    assert "2026-09-30" in content


def test_knowledge_readme_exists():
    """Verify knowledge/README.md documentation exists."""
    readme_path = Path("knowledge/README.md")
    assert readme_path.exists()
    text = readme_path.read_text(encoding="utf-8")
    assert "knowledge" in text.lower()
    assert "adr" in text.lower() or "decisions" in text.lower()
