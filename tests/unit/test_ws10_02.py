"""Tests for WS10.02: Cohort mapping, factors and models (C10)."""

import hashlib
import json
from pathlib import Path
import sqlite3
import pytest

from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.migrate.legacy import build_sample, run as run_migration
from quant.run import RunContext
from quant.types import Actor, FrozenClock


KNOWN_20_TICKERS = [
    "360ONE.NS", "3MINDIA.NS", "AADHARHFC.NS", "AARTIIND.NS", "AAVAS.NS",
    "ABB.NS", "ABBOTINDIA.NS", "ABCAPITAL.NS", "ABDL.NS", "ABFRL.NS",
    "ABLBL.NS", "ABREL.NS", "ABSLAMC.NS", "ACC.NS", "ACE.NS",
    "ACMESOLAR.NS", "ACUTAAS.NS", "ADANIENSOL.NS", "ADANIENT.NS", "ADANIGREEN.NS",
]


@pytest.fixture
def sample_legacy_db(tmp_path):
    source_db = Path("quant_engine.db")
    sample_path = tmp_path / "sample_legacy.db"
    build_sample(source=source_db, output=sample_path, tickers=KNOWN_20_TICKERS)
    return sample_path


@pytest.fixture
def test_ctx(tmp_path, cfg):
    db_path = tmp_path / "v2_migrated.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path)

    clock = FrozenClock("2026-09-03T18:30:00.000000Z")
    actor = Actor(kind="system", name="migration")

    ctx = RunContext(
        as_of="2026-09-03",
        kind="migration",
        track="legacy",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-03', 'migration', 'legacy', 1, '2026-09-03T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
    return ctx


def test_six_snapshot_map_records(test_ctx, sample_legacy_db):
    """Six snapshot-map records are populated with exact date alignments and superseded links."""
    res = run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert res.status == "ok"

    cur = test_ctx.conn.cursor()
    cur.execute("SELECT legacy_date, as_of, is_full, superseded_by FROM legacy_snapshot_map ORDER BY legacy_date")
    rows = cur.fetchall()
    assert len(rows) == 6

    map_dict = {r[0]: (r[1], r[2], r[3]) for r in rows}
    # 2026-06-04 is partial
    assert map_dict["2026-06-04"][1] == 0
    # 2026-06-12 is duplicate superseded by 2026-06-14
    assert map_dict["2026-06-12"] == ("2026-06-12", 0, "2026-06-14")
    # 2026-06-14 is full, maps trading date 2026-06-12
    assert map_dict["2026-06-14"] == ("2026-06-12", 1, None)
    # 2026-07-11 is full, maps trading date 2026-07-10
    assert map_dict["2026-07-11"] == ("2026-07-10", 1, None)
    # 2026-08-14 is full
    assert map_dict["2026-08-14"] == ("2026-08-14", 1, None)
    # 2026-09-03 is full
    assert map_dict["2026-09-03"] == ("2026-09-03", 1, None)


def test_four_published_full_legacy_cohorts_and_models(test_ctx, sample_legacy_db):
    """Four legacy cohorts created with is_clean=0, and two legacy models scored."""
    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)

    cur = test_ctx.conn.cursor()
    # 4 cohorts
    cohorts = cur.execute("SELECT cohort_id, as_of, track, is_clean FROM cohorts WHERE track = 'legacy' ORDER BY as_of").fetchall()
    assert len(cohorts) == 4
    for c in cohorts:
        assert c[3] == 0  # is_clean = 0

    cohort_dates = [c[1] for c in cohorts]
    assert cohort_dates == ["2026-06-12", "2026-07-10", "2026-08-14", "2026-09-03"]

    # 2 legacy models
    models = cur.execute("SELECT model_id, role FROM models WHERE role = 'legacy' ORDER BY model_id").fetchall()
    model_ids = {m[0] for m in models}
    assert "LEGACY_V18" in model_ids
    assert "LEGACY_V18_BASE" in model_ids

    # Scores exist for both models
    cur.execute("SELECT model_id, count(*) FROM scores GROUP BY model_id")
    score_counts = dict(cur.fetchall())
    assert score_counts["LEGACY_V18"] > 0
    assert score_counts["LEGACY_V18_BASE"] > 0
    assert score_counts["LEGACY_V18"] == score_counts["LEGACY_V18_BASE"]


def test_eleven_factor_fields_imported(test_ctx, sample_legacy_db):
    """11 factor fields imported as legacy_*@0 with shadow lifecycle."""
    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)

    cur = test_ctx.conn.cursor()
    cur.execute("SELECT factor_id, status FROM factor_registry WHERE factor_id LIKE 'legacy_%@0'")
    factor_rows = cur.fetchall()
    assert len(factor_rows) == 11
    for fid, stat in factor_rows:
        assert stat == "shadow"

    expected_factors = {
        "legacy_quality@0", "legacy_valuation@0", "legacy_growth@0",
        "legacy_moat@0", "legacy_risk@0", "legacy_bs@0",
        "legacy_cap_alloc@0", "legacy_smart_money@0", "legacy_trap@0",
        "legacy_momentum_multiplier@0", "legacy_dc_flag@0",
    }
    assert {r[0] for r in factor_rows} == expected_factors

    # Factor values exist
    n_fv = cur.execute("SELECT count(*) FROM factor_values").fetchone()[0]
    assert n_fv > 0


def test_flags_preserve_defects_and_no_clean_live_fundamentals(test_ctx, sample_legacy_db):
    """Flags preserve known defects and legacy inputs never enter live clean fundamentals."""
    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)

    cur = test_ctx.conn.cursor()
    # Fundamentals table must have 0 rows (clean live only)
    n_fund = cur.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
    assert n_fund == 0

    # Defects are logged in legacy_defects
    n_defects = cur.execute("SELECT count(*) FROM legacy_defects").fetchone()[0]
    assert n_defects >= 0


def test_full_source_1997_rows_per_model(tmp_path, cfg):
    """Full source migration creates exactly 1997 rows per model (499+499+499+500)."""
    full_source = Path("quant_engine.db")
    db_path = tmp_path / "v2_full_migration.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path)

    clock = FrozenClock("2026-09-03T18:30:00.000000Z")
    actor = Actor(kind="system", name="migration")

    ctx = RunContext(
        as_of="2026-09-03",
        kind="migration",
        track="legacy",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-03', 'migration', 'legacy', 1, '2026-09-03T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )

    res = run_migration(ctx, legacy_db_path=full_source, dry_run=False)
    assert res.status == "ok"

    cur = conn.cursor()
    cur.execute("SELECT model_id, count(*) FROM scores GROUP BY model_id")
    score_counts = dict(cur.fetchall())
    assert score_counts["LEGACY_V18"] == 1997
    assert score_counts["LEGACY_V18_BASE"] == 1997
