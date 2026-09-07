import sqlite3
import numpy as np
import pandas as pd
import pytest

from quant.evaluation.labels import frame as labels_frame, mature
from quant.run import RunContext


@pytest.fixture
def clean_test_db():
    conn = sqlite3.connect(":memory:")
    with open("docs/spec/contracts/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _seed_basic_data(conn):
    # Run
    conn.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (1, '2026-06-30', 'test', 'live', 1, '2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)
    # 5 securities
    for sid in range(1, 6):
        conn.execute("""
            INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, '2026-01-01', '2026-09-30', 'listed')
        """, (sid, f"INE{sid:09d}", f"Stock {sid}"))

    # Cohort 1: live:2026-06-30
    conn.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T10:00:00.000000Z', 'def1', 'mem1', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)
    """)

    # Model and model_version for EW_HIER_v1
    conn.execute("""
        INSERT INTO models (model_id, kind, role, description, params_json, registered_on)
        VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-06-01')
    """)
    conn.execute("""
        INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from)
        VALUES ('EW_HIER_v1', 1, '[]', '{}', '2026-06-01')
    """)

    # Scores for 5 members in live:2026-06-30
    for sid in range(1, 6):
        conn.execute("""
            INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version,
                                family_scores_json, composite, composite_neutral, sector_tilt, final,
                                rank_all, rank, rank_group, decile, quintile, scored, eligible, n_factors_used,
                                input_hash, generated_at, track, run_id)
            VALUES ('live:2026-06-30', '2026-06-30', ?, 'EW_HIER_v1', 1, 'Industrials', 1,
                    '{}', 0.5, 0.5, 0.0, 0.5, ?, ?, ?, 5, 3, 1, 1, 5,
                    'h1', '2026-07-01', 'live', 1)
        """, (sid, sid, sid, sid))

    # Monthly prices at 2026-06-30
    for sid in range(1, 6):
        conn.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('live:2026-06-30', '2026-06-30', ?, 100.0, 100.0, 'synthetic', 'sha1', 1)
        """, (sid,))


def test_original_cohort_member_count_survives_index_dropout(clean_test_db, cfg):
    """Cohort labels follow all original members, even if dropped from index before endpoint."""
    _seed_basic_data(clean_test_db)

    # Monthly prices at endpoint 2026-09-30 (3M horizon for 2026-06-30):
    # Stock 5 was dropped from index (so not in universe_membership at 2026-09-30),
    # but has a price at 2026-09-30
    # Cohort 2026-09-30:
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T10:00:00.000000Z', 'def2', 'mem2', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    for sid in range(1, 6):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('live:2026-09-30', '2026-09-30', ?, 110.0, 110.0, 'synthetic', 'sha2', 1)
        """, (sid,))

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = clean_test_db

    res = mature(ctx, through="2026-09-30")
    assert res.status == "ok"

    # Verify all 5 original members have labels for 3M horizon
    df = labels_frame(clean_test_db, "live:2026-06-30", horizon_m=3, known_at="2026-10-01T00:00:00.000000Z")
    assert len(df) == 5
    assert set(df["security_id"]) == {1, 2, 3, 4, 5}
    assert (df["status"] == "ok").all()


def test_no_row_before_endpoint(clean_test_db, cfg):
    """No label row is generated before the cohort horizon endpoint session has completed."""
    _seed_basic_data(clean_test_db)
    ctx = RunContext(
        as_of="2026-08-31",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = clean_test_db

    # Call mature through August (endpoint for 3M from 2026-06-30 is 2026-09-30)
    mature(ctx, through="2026-08-31")

    # Should be no labels for 3M horizon before its endpoint
    count = clean_test_db.execute("SELECT count(*) FROM labels WHERE horizon_m = 3").fetchone()[0]
    assert count == 0


def test_missing_quote_is_not_automatically_delisted(clean_test_db, cfg):
    """A missing quote at endpoint date yields status='missing', never assumed delisted or 0 return."""
    _seed_basic_data(clean_test_db)

    # Insert prices at 2026-09-30 for stocks 1-4, but stock 5 is missing
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T10:00:00.000000Z', 'def2', 'mem2', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    for sid in range(1, 5):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('live:2026-09-30', '2026-09-30', ?, 110.0, 110.0, 'synthetic', 'sha2', 1)
        """, (sid,))

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = clean_test_db

    mature(ctx, through="2026-09-30")

    df = labels_frame(clean_test_db, "live:2026-06-30", horizon_m=3, known_at="2026-10-01T00:00:00.000000Z")
    assert len(df) == 5
    stock5_label = df[df["security_id"] == 5].iloc[0]
    assert stock5_label["status"] == "missing"
    assert stock5_label["status"] != "delisted_partial"
    assert pd.isna(stock5_label["r_log"])


def test_action_resolution_appends_group_wide_revisions(clean_test_db, cfg):
    """Action resolution / return changes append a new revision for the affected group without modifying old rows."""
    _seed_basic_data(clean_test_db)
    clean_test_db.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (2, '2026-10-31', 'test', 'backfill', 1, '2026-10-31T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)
    # Seed backfill base cohort
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('backfill:2026-06-30', '2026-06-30', 'backfill', '2026-06-30T10:00:00.000000Z', 'def1_bf', 'mem1_bf', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)
    """)
    for sid in range(1, 6):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('backfill:2026-06-30', '2026-06-30', ?, 100.0, 100.0, 'synthetic', 'sha1_bf', 1)
        """, (sid,))
        clean_test_db.execute("""
            INSERT INTO scores (
                cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version,
                family_scores_json, composite, composite_neutral, sector_tilt, final,
                rank_all, rank, rank_group, decile, quintile,
                scored, eligible, exclusion_reason, liquidity_bucket,
                n_factors_used, dc_flag, input_hash, generated_at,
                track, run_id
            ) VALUES (
                'backfill:2026-06-30', '2026-06-30', ?, 'EW_HIER_v1', 1, 'FINANCIALS', 1,
                '{}', 50.0, 50.0, 0.0, 50.0,
                ?, ?, ?, 1, 1,
                1, 1, NULL, 'liquid',
                5, 0, 'hash', '2026-07-01T00:00:00.000000Z',
                'backfill', 1
            )
        """, (sid, sid, sid, sid))

    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('backfill:2026-09-30', '2026-09-30', 'backfill', '2026-09-30T10:00:00.000000Z', 'def2_bf', 'mem2_bf', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    for sid in range(1, 6):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('backfill:2026-09-30', '2026-09-30', ?, 110.0, 110.0, 'synthetic', 'sha2_bf', 1)
        """, (sid,))

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="backfill",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = clean_test_db

    mature(ctx, through="2026-09-30")

    # Old rows are revision 1 for horizon 3
    rev1_rows = clean_test_db.execute("SELECT security_id, revision, r_log FROM labels WHERE cohort_id = 'backfill:2026-06-30' AND horizon_m = 3 AND revision = 1").fetchall()
    assert len(rev1_rows) == 5

    # Simulate corporate action correction at a later run: revised endpoint cohort published
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('backfill:2026-09-30:rev2', '2026-09-30', 'backfill', '2026-09-30T10:00:00.000000Z', 'def2_rev2', 'mem2_rev2', '{}', '2026-10-15T00:00:00.000000Z', '2026-10-15T00:00:00.000000Z', 1, 2)
    """)
    for sid in range(1, 6):
        tri_val = 120.0 if sid == 1 else 110.0
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('backfill:2026-09-30:rev2', '2026-09-30', ?, ?, ?, 'synthetic', 'sha2_rev2', 2)
        """, (sid, tri_val, tri_val))

    ctx2 = RunContext(
        as_of="2026-10-31",
        kind="test",
        track="backfill",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx2.conn = clean_test_db
    ctx2.run_id = 2

    mature(ctx2, through="2026-09-30")

    # Revision 2 should be appended for the affected sector group
    rev2_rows = clean_test_db.execute("SELECT security_id, revision, r_log FROM labels WHERE cohort_id = 'backfill:2026-06-30' AND horizon_m = 3 AND revision = 2").fetchall()
    assert len(rev2_rows) == 5

    # Old rows must remain completely unchanged
    rev1_after = clean_test_db.execute("SELECT security_id, revision, r_log FROM labels WHERE cohort_id = 'backfill:2026-06-30' AND horizon_m = 3 AND revision = 1").fetchall()
    assert rev1_rows == rev1_after

    # labels_frame at earlier known_at sees revision 1
    df_early = labels_frame(clean_test_db, "backfill:2026-06-30", horizon_m=3, known_at="2026-10-01T00:00:00.000000Z")
    assert (df_early["revision"] == 1).all()

    # labels_frame at later known_at sees revision 2
    df_late = labels_frame(clean_test_db, "backfill:2026-06-30", horizon_m=3, known_at="2026-11-01T00:00:00.000000Z")
    assert (df_late["revision"] == 2).all()


def test_live_and_backfill_labels_remain_separate(clean_test_db, cfg, spec_case):
    """Live and backfill tracks with same as_of date do not collide or overwrite each other."""
    c = spec_case("revision_tracks")
    _seed_basic_data(clean_test_db)

    # Insert backfill cohort on same as_of date
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('backfill:2026-06-30', '2026-06-30', 'backfill', '2026-06-30T10:00:00.000000Z', 'def_bf', 'mem_bf', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)
    """)
    # Insert prices for backfill
    for sid in range(1, 6):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('backfill:2026-06-30', '2026-06-30', ?, 100.0, 100.0, 'synthetic', 'sha_bf', 1)
        """, (sid,))

    # Insert endpoint prices for live and backfill
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T10:00:00.000000Z', 'def2', 'mem2', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    clean_test_db.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('backfill:2026-09-30', '2026-09-30', 'backfill', '2026-09-30T10:00:00.000000Z', 'def2_bf', 'mem2_bf', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    for sid in range(1, 6):
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('live:2026-09-30', '2026-09-30', ?, 110.0, 110.0, 'synthetic', 'sha2', 1)
        """, (sid,))
        clean_test_db.execute("""
            INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
            VALUES ('backfill:2026-09-30', '2026-09-30', ?, 120.0, 120.0, 'synthetic', 'sha2_bf', 1)
        """, (sid,))

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = clean_test_db

    mature(ctx, through="2026-09-30")

    live_labels = clean_test_db.execute("SELECT count(*) FROM labels WHERE track = 'live' AND as_of = '2026-06-30' AND horizon_m = 3").fetchone()[0]
    bf_labels = clean_test_db.execute("SELECT count(*) FROM labels WHERE track = 'backfill' AND as_of = '2026-06-30' AND horizon_m = 3").fetchone()[0]

    assert live_labels == 5
    assert bf_labels == 5
    distinct_cohorts = clean_test_db.execute("SELECT count(DISTINCT cohort_id) FROM labels WHERE as_of = '2026-06-30' AND horizon_m = 3").fetchone()[0]
    assert distinct_cohorts == c["expected_distinct_cohorts"]
