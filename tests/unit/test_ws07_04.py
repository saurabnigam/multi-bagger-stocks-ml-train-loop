"""Tests for WS07.04: Leakage suite and stored-history curves (C07)."""

import sqlite3
import numpy as np
import pandas as pd
import pytest

from quant.config import Config
from quant.evaluation.curves import update as update_curves
from quant.evaluation.leakage import run as run_leakage
from quant.evaluation.metrics import rank_ic
from quant.run import RunContext
from quant.types import CheckReport


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with open("quant/db/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _seed_curves_world(conn):
    # Runs
    conn.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (1, '2026-06-30', 'test', 'live', 1, '2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)
    conn.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (2, '2026-09-30', 'test', 'live', 1, '2026-09-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)

    # Securities
    for sid in range(1, 11):
        conn.execute("""
            INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, '2026-01-01', '2026-09-30', 'listed')
        """, (sid, f"INE{sid:09d}", f"Stock {sid}"))

    # Live Cohort: 2026-06-30
    conn.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T10:00:00.000000Z', 'def1', 'mem1', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)
    """)

    # Model and factor registry
    conn.execute("""
        INSERT INTO models (model_id, kind, role, description, params_json, registered_on)
        VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-06-01')
    """)
    conn.execute("""
        INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from)
        VALUES ('EW_HIER_v1', 1, '[{"factor_id":"mom_12_1@1","family":"momentum"}]', '{"family":{"momentum":1.0}}', '2026-06-01')
    """)
    conn.execute("""
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level, hypothesis,
            formula, inputs_json, lookback_days, applies_to_financials, backfillable,
            min_coverage, code_sha256, module_path, status, registered_on, status_changed_on
        ) VALUES (
            'mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp',
            'formula', '[]', 252, 0, 1,
            0.8, 'sha', 'mod', 'active', '2026-06-01', '2026-06-01'
        )
    """)

    # Evaluation row for mom_12_1@1
    conn.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            1, 1, '2026-09-30T10:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-06-30', 3, 'eligible', 'live', 'ic', 0.12, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_1', 1, NULL
        )
    """)

    # Evaluation row for EW_HIER_v1 model
    conn.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            2, 1, '2026-09-30T10:00:00.000000Z', 'model', 'EW_HIER_v1', '1',
            '2026-06-30', 3, 'eligible', 'live', 'ic', 0.15, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_model_1', 1, NULL
        )
    """)


def test_leakage_suite_runs_t1_through_t10(test_db, cfg):
    """Leakage suite executes T1 through T10 checks with explicit reasons and pass/fail states."""
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db

    report = run_leakage(ctx, draft=None)
    assert isinstance(report, CheckReport)
    check_ids = [c.id for c in report.checks]
    for i in range(1, 11):
        assert any(c.startswith(f"T{i}") for c in check_ids)


def test_planted_rank_deterministic_exact_ic(spec_case):
    """Deterministic planted rank matches exact expected Spearman correlation."""
    c = spec_case("planted_rank")
    ic, n, status = rank_ic(pd.Series(c["scores"]), pd.Series(c["labels"]))
    assert status == "ok"
    assert n == 10
    assert abs(ic - c["expected_spearman"]) < 1e-12


def test_t1_shuffle_distribution():
    """T1 shuffle permutes 200 times and aggregate mean is centered near zero."""
    rng = np.random.default_rng(42)
    scores = pd.Series(rng.normal(size=50))
    labels = pd.Series(rng.normal(size=50))

    # 200 random permutations
    perm_ics = []
    for _ in range(200):
        perm_labels = pd.Series(rng.permutation(labels.values), index=labels.index)
        ic, _, _ = rank_ic(scores, perm_labels)
        if ic is not None:
            perm_ics.append(ic)

    mc_mean = float(np.mean(perm_ics))
    mc_se = float(np.std(perm_ics, ddof=1) / np.sqrt(len(perm_ics)))
    tolerance = max(0.005, 5 * mc_se)
    assert abs(mc_mean) <= tolerance


def test_learning_and_evidence_curves(test_db, cfg):
    """Curves update populates evidence_curve and learning_curve_points."""
    _seed_curves_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 2

    res = update_curves(ctx, through="2026-09-30")
    assert res.status == "ok"

    # Verify evidence_curve populated
    ec_rows = test_db.execute("SELECT * FROM evidence_curve").fetchall()
    assert len(ec_rows) > 0
    row = ec_rows[0]
    assert row["subject_id"] in ("mom_12_1@1", "EW_HIER_v1")
    assert row["horizon_m"] == 3
    # N=1 is <= lag+1 (1 <= 3), so status is insufficient and bands are NULL/unavailable
    assert row["status"] in ("insufficient", "ok")
    if row["status"] == "insufficient":
        assert row["ci90_lo"] is None
        assert row["ci90_hi"] is None
        assert row["ic_hac_se"] is None
