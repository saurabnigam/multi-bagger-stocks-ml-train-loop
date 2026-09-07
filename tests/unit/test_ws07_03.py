"""Tests for WS07.03: Revision selection and causal training history (C07)."""

import sqlite3
import pandas as pd
import pytest

from quant.config import Config
from quant.evaluation.evaluate import ic_series, run as evaluate_run
from quant.evaluation.walkforward import family_ic_history
from quant.run import RunContext


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with open("quant/db/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _seed_eval_world(conn):
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

    # Live Cohort 1: 2026-06-30
    conn.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T10:00:00.000000Z', 'def1', 'mem1', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)
    """)

    # Live Cohort 2: 2026-09-30
    conn.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T10:00:00.000000Z', 'def2', 'mem2', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 2)
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

    # Factor values and scores for 2026-06-30
    for sid in range(1, 11):
        conn.execute("""
            INSERT INTO factor_values (
                cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group, flags, input_refs_json, track, run_id
            ) VALUES (
                'live:2026-06-30', '2026-06-30', ?, 'mom_12_1@1', ?, ?, ?, 'Industrials', '', '{}', 'live', 1
            )
        """, (sid, float(sid), float(sid), float(sid) - 5.5))
        conn.execute("""
            INSERT INTO scores (
                cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version,
                family_scores_json, composite, composite_neutral, sector_tilt, final,
                rank_all, rank, rank_group, decile, quintile,
                scored, eligible, exclusion_reason, liquidity_bucket,
                n_factors_used, dc_flag, input_hash, generated_at,
                track, run_id
            ) VALUES (
                'live:2026-06-30', '2026-06-30', ?, 'EW_HIER_v1', 1, 'Industrials', 1,
                '{}', ?, ?, 0.0, ?,
                ?, ?, ?, 1, 1,
                1, 1, NULL, 'liquid',
                1, 0, 'hash', '2026-07-01T00:00:00.000000Z',
                'live', 1
            )
        """, (sid, float(sid), float(sid), float(sid), sid, sid, sid))

        # Labels for 2026-06-30 (horizon 3 -> end_date 2026-09-30)
        conn.execute("""
            INSERT INTO labels (
                cohort_id, as_of, security_id, horizon_m, end_date, track,
                revision, evidence_hash, computed_at,
                r_log, r_arith, r_group_median, l_rel, r_uni, sector_group,
                status, mb36, mb36_touch, price_manifest_sha, computed_run_id,
                decision_id, supersedes_revision
            ) VALUES (
                'live:2026-06-30', '2026-06-30', ?, 3, '2026-09-30', 'live',
                1, 'eh_labels_1', '2026-09-30T10:00:00.000000Z',
                ?, ?, 0.0, ?, ?, 'Industrials',
                'ok', 0, 0, 'sha', 1,
                NULL, NULL
            )
        """, (sid, float(sid) * 0.02, float(sid) * 0.02, float(sid) * 0.02, float(sid) * 0.02))


def test_monthly_window_keys_use_empty_strings(test_db, cfg):
    """Monthly evaluations use empty strings for window_start and window_end, not NULL."""
    _seed_eval_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 2

    res = evaluate_run(ctx, through="2026-09-30", track="live")
    assert res.status == "ok"

    rows = test_db.execute("SELECT window_start, window_end FROM evaluations").fetchall()
    assert len(rows) > 0
    for r in rows:
        assert r["window_start"] == ""
        assert r["window_end"] == ""


def test_unchanged_evaluation_inserts_zero_rows(test_db, cfg, spec_case):
    """Re-running evaluation with unchanged evidence inserts 0 rows."""
    c = spec_case("evaluation_revisions")
    _seed_eval_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 2

    # Attempt 1
    res1 = evaluate_run(ctx, through="2026-09-30", track="live")
    n1 = test_db.execute("SELECT count(*) FROM evaluations").fetchone()[0]
    assert n1 > 0

    # Attempt 2 (identical attempts)
    res2 = evaluate_run(ctx, through="2026-09-30", track="live")
    assert res2.counts.get("inserted", 0) == 0
    n2 = test_db.execute("SELECT count(*) FROM evaluations").fetchone()[0]
    assert n2 == n1


def test_changed_evidence_appends_revision_with_supersedes(test_db, cfg, spec_case):
    """Changed evidence appends revision 2 with supersedes_eval_id and logs change."""
    c = spec_case("evaluation_revisions")
    _seed_eval_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 2

    evaluate_run(ctx, through="2026-09-30", track="live")

    # Get factor evaluation row
    row1 = test_db.execute("""
        SELECT eval_id, revision, evidence_hash, value
        FROM evaluations
        WHERE subject_id = 'mom_12_1@1' AND horizon_m = 3 AND revision = 1
    """).fetchone()
    assert row1 is not None

    # Simulate revised labels appended in labels table
    for sid in range(1, 11):
        test_db.execute("""
            INSERT INTO labels (
                cohort_id, as_of, security_id, horizon_m, end_date, track,
                revision, evidence_hash, computed_at,
                r_log, r_arith, r_group_median, l_rel, r_uni, sector_group,
                status, mb36, mb36_touch, price_manifest_sha, computed_run_id,
                decision_id, supersedes_revision
            ) VALUES (
                'live:2026-06-30', '2026-06-30', ?, 3, '2026-09-30', 'live',
                2, 'eh_labels_rev2', '2026-10-15T00:00:00.000000Z',
                ?, ?, 0.0, ?, ?, 'Industrials',
                'ok', 0, 0, 'sha_rev2', 2,
                NULL, 1
            )
        """, (sid, float(11 - sid) * 0.02, float(11 - sid) * 0.02, float(11 - sid) * 0.02, float(11 - sid) * 0.02))

    ctx2 = RunContext(as_of="2026-10-31", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx2.conn = test_db
    ctx2.run_id = 2

    evaluate_run(ctx2, through="2026-09-30", track="live")

    row2 = test_db.execute("""
        SELECT eval_id, revision, evidence_hash, value, supersedes_eval_id
        FROM evaluations
        WHERE subject_id = 'mom_12_1@1' AND horizon_m = 3 AND revision = 2
    """).fetchone()
    assert row2 is not None
    assert row2["revision"] == c["changed_evidence_revision"]
    assert row2["supersedes_eval_id"] == row1["eval_id"]

    # Old row must be completely unchanged
    row1_after = test_db.execute("""
        SELECT eval_id, revision, evidence_hash, value
        FROM evaluations
        WHERE eval_id = ?
    """, (row1["eval_id"],)).fetchone()
    assert row1_after["evidence_hash"] == row1["evidence_hash"]
    assert row1_after["value"] == row1["value"]

    # Log table entry exists
    log_entry = test_db.execute("SELECT * FROM evaluations_log WHERE old_eval_id = ?", (row1["eval_id"],)).fetchone()
    assert log_entry is not None
    assert log_entry["new_eval_id"] == row2["eval_id"]


def test_select_latest_known_revision_before_status_filtering(test_db):
    """Selects latest revision known_at before status filters; an excluded revision cannot resurrect an older valid one."""
    _seed_eval_world(test_db)
    # Insert revision 1: ok, value=0.10, computed_at 2026-10-01
    test_db.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            100, 1, '2026-10-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-06-30', 3, 'eligible', 'live', 'ic', 0.10, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_rev1', 1, NULL
        )
    """)

    # Insert revision 2: insufficient, value=NULL, computed_at 2026-11-01
    test_db.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            101, 2, '2026-11-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-06-30', 3, 'eligible', 'live', 'ic', NULL, 2, 0.6, 'insufficient', 'spearman',
            '', '', 'eh_rev2', 2, 100
        )
    """)

    # At known_at = 2026-10-15: sees revision 1 (value 0.10)
    s_early = ic_series(
        test_db, subject_kind="factor", subject_id="mom_12_1@1", subject_version="1",
        horizon_m=3, scope="eligible", track="live", through="2026-09-30",
        known_at="2026-10-15T00:00:00.000000Z"
    )
    assert len(s_early) == 1
    assert abs(s_early["2026-06-30"] - 0.10) < 1e-12

    # At known_at = 2026-11-15: selects latest revision 2 (status insufficient -> NaN), DOES NOT resurrect revision 1
    s_late = ic_series(
        test_db, subject_kind="factor", subject_id="mom_12_1@1", subject_version="1",
        horizon_m=3, scope="eligible", track="live", through="2026-09-30",
        known_at="2026-11-15T00:00:00.000000Z"
    )
    assert len(s_late) == 1
    assert pd.isna(s_late["2026-06-30"])


def test_family_ic_history_boundaries(test_db):
    """family_ic_history excludes backfill, un-matured endpoints, and future revisions."""
    _seed_eval_world(test_db)
    # Insert evaluation for live 2026-06-30 (matured at 2026-09-30 <= 2026-09-30)
    test_db.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            201, 1, '2026-10-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-06-30', 3, 'eligible', 'live', 'ic', 0.12, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_hist1', 1, NULL
        )
    """)

    # Insert backfill evaluation (must be ignored)
    test_db.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            202, 1, '2026-10-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-06-30', 3, 'eligible', 'backfill', 'ic', 0.99, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_bf', 1, NULL
        )
    """)

    # Insert un-matured live evaluation (cohort 2026-09-30 endpoint 2026-12-31 > as_of 2026-09-30)
    test_db.execute("""
        INSERT INTO evaluations (
            eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
            as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method,
            window_start, window_end, evidence_hash, revision, supersedes_eval_id
        ) VALUES (
            203, 2, '2026-10-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1',
            '2026-09-30', 3, 'eligible', 'live', 'ic', 0.25, 10, 3.3, 'ok', 'spearman',
            '', '', 'eh_future', 1, NULL
        )
    """)

    mv = {
        "model_id": "EW_HIER_v1",
        "version": 1,
        "factor_set_json": '[{"factor_id":"mom_12_1@1","family":"momentum"}]',
        "weights_json": '{"family":{"momentum":1.0}}',
    }

    df_hist = family_ic_history(test_db, mv, as_of="2026-09-30", known_at="2026-10-05T00:00:00.000000Z")
    assert "momentum" in df_hist.columns
    # Only 2026-06-30 cohort has completed endpoint <= 2026-09-30
    assert list(df_hist.index) == ["2026-06-30"]
    assert abs(df_hist.loc["2026-06-30", "momentum"] - 0.12) < 1e-12
