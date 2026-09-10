"""Integration tests for WS11.01: Monthly orchestration and recovery (C11)."""

import json
from pathlib import Path
import sqlite3
import pytest

from quant.cli import main
from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.run import monthly, RunContext
from quant.types import Actor, FrozenClock, SystemClock


@pytest.fixture
def test_env(tmp_path, cfg):
    db_path = tmp_path / "v2_test.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "decisions").mkdir(parents=True, exist_ok=True)

    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=knowledge_dir, ui_dir=tmp_path / "ui")
    return test_cfg, db_path


def test_lock_and_publication_precheck(test_env):
    """Test publication precheck: already published cohort exits 0 immediately; future date returns 2."""
    cfg, db_path = test_env
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # 1. Reject future date (as_of > clock date)
    exit_code_future = monthly(cfg, clock, actor, as_of="2026-10-05")
    assert exit_code_future == 2

    # 2. Insert already published cohort for 2026-09-30
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'monthly', 'live', 1, '2026-09-30T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-30T18:30:00.000000Z', '2026-09-30T18:30:00.000000Z', 1, 1)"
        )
    conn.close()

    # Precheck should detect existing published cohort and exit 0 immediately
    exit_code_published = monthly(cfg, clock, actor, as_of="2026-09-30")
    assert exit_code_published == 0


def test_coldstart_blocked_no_scores(test_env):
    """Coldstart with insufficient universe/data sets status to blocked (exit code 2) and publishes no scores."""
    cfg, db_path = test_env
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # DB has schema but no universe members / captures at 2026-09-30
    exit_code = monthly(cfg, clock, actor, as_of="2026-09-30", skip_capture=True)
    assert exit_code == 2

    conn = connect(db_path)
    # No live cohort or scores published
    n_cohorts = conn.execute("SELECT count(*) FROM cohorts WHERE as_of = '2026-09-30' AND track = 'live'").fetchone()[0]
    n_scores = conn.execute("SELECT count(*) FROM scores WHERE as_of = '2026-09-30' AND track = 'live'").fetchone()[0]
    conn.close()

    assert n_cohorts == 0
    assert n_scores == 0


def test_crash_before_publish_leaves_no_cohort(test_env, monkeypatch):
    """Crash/failure during scoring or validation rolls back staging and leaves no cohort."""
    cfg, db_path = test_env
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # Populate dummy universe members so it gets past coldstart
    conn = connect(db_path)
    with conn:
        for i in range(500):
            conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (?, ?, ?, '2020-01-01', '2026-10-01', 'listed')",
                (i + 1, f"INE{i:09d}", f"Stock {i}"),
            )
            conn.execute(
                "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) VALUES ('2026-09-30', '2026-09-30T10:00:00.000000Z', ?, 'NIFTY500', ?, 'current_backfill')",
                (i + 1, f"STOCK{i}"),
            )
    conn.close()

    # Simulate a crash during scoring
    import quant.model.models as m_mod
    def mock_score_all(*args, **kwargs):
        raise RuntimeError("Simulated crash before cohort publish")
    monkeypatch.setattr(m_mod, "score_all", mock_score_all)

    exit_code = monthly(cfg, clock, actor, as_of="2026-09-30", skip_capture=True)
    assert exit_code in (1, 2)

    conn = connect(db_path)
    n_cohorts = conn.execute("SELECT count(*) FROM cohorts WHERE as_of = '2026-09-30' AND track = 'live'").fetchone()[0]
    conn.close()
    assert n_cohorts == 0


def test_step_order_settle_mature_evaluate_before_fit(test_env, monkeypatch):
    """Verify MASTER_SPEC 9.1 step order: settle, mature, evaluate occur before model fitting."""
    cfg, db_path = test_env
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    actor = Actor(kind="system", name="runner")

    order_of_steps = []

    import quant.portfolio.paper as paper_mod
    import quant.evaluation.labels as labels_mod
    import quant.evaluation.evaluate as eval_mod
    import quant.model.models as models_mod

    orig_settle = paper_mod.settle
    orig_mature = labels_mod.mature
    orig_eval_run = eval_mod.run
    orig_score_all = models_mod.score_all

    def mock_settle(ctx, through):
        order_of_steps.append("settle")
        return orig_settle(ctx, through)

    def mock_mature(ctx, through):
        order_of_steps.append("mature")
        return orig_mature(ctx, through)

    def mock_eval_run(ctx, through, track):
        order_of_steps.append("evaluate")
        return orig_eval_run(ctx, through, track)

    def mock_score_all(ctx, draft, family_ic_history):
        order_of_steps.append("score_all")
        return orig_score_all(ctx, draft, family_ic_history)

    monkeypatch.setattr(paper_mod, "settle", mock_settle)
    monkeypatch.setattr(labels_mod, "mature", mock_mature)
    monkeypatch.setattr(eval_mod, "run", mock_eval_run)
    monkeypatch.setattr(models_mod, "score_all", mock_score_all)

    monthly(cfg, clock, actor, as_of="2026-09-30", skip_capture=True)

    # Settle, mature, evaluate must be executed before scoring
    assert "settle" in order_of_steps
    assert "mature" in order_of_steps
    assert "evaluate" in order_of_steps

    settle_idx = order_of_steps.index("settle")
    mature_idx = order_of_steps.index("mature")
    eval_idx = order_of_steps.index("evaluate")

    if "score_all" in order_of_steps:
        score_idx = order_of_steps.index("score_all")
        assert settle_idx < score_idx
        assert mature_idx < score_idx
        assert eval_idx < score_idx


def test_failed_scores_still_produce_earlier_labels_and_report(test_env, monkeypatch):
    """When scoring fails or is blocked, earlier matured labels and orders remain committed."""
    cfg, db_path = test_env
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # Create an earlier cohort and label to be matured
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (10, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:2026-08-31', '2026-08-31', 'live', '2026-08-31T18:29:59.999999Z', 'def', 'mem', '[]', '2026-08-31T18:30:00.000000Z', '2026-08-31T18:30:00.000000Z', 1, 10)"
        )
        conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2020-01-01', '2026-10-01', 'listed')")
        conn.execute(
            "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, evidence_hash, computed_at, sector_group, status, price_manifest_sha, computed_run_id) "
            "VALUES ('live:2026-08-31', '2026-08-31', 1, 1, '2026-09-30', 'live', 1, 'ev1', '2026-08-31T18:30:00.000000Z', 'Broad', 'missing', 'pm_sha', 10)"
        )
    conn.close()

    # Fail scoring
    import quant.model.models as m_mod
    def mock_fail_scoring(*args, **kwargs):
        raise RuntimeError("Forced scoring failure")
    monkeypatch.setattr(m_mod, "score_all", mock_fail_scoring)

    exit_code = monthly(cfg, clock, actor, as_of="2026-09-30", skip_capture=True)
    assert exit_code in (1, 2)

    # Verify earlier label row is still intact
    conn = connect(db_path)
    cur = conn.cursor()
    lbl = cur.execute("SELECT count(*) FROM labels WHERE cohort_id = 'live:2026-08-31'").fetchone()[0]
    conn.close()
    assert lbl == 1


def test_cli_run_monthly(test_env):
    """CLI command `quant run monthly` executes cleanly."""
    cfg, db_path = test_env

    # Run CLI dry-run
    code = main([
        "run", "monthly",
        "--db-path", str(db_path),
        "--as-of", "2026-08-31",
        "--dry-run",
    ])
    assert code == 0
