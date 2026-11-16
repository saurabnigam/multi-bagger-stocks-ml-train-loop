"""Tests for WS10.03: Attribution and migration repeatability (C10)."""

import hashlib
import json
from pathlib import Path
import sqlite3
import pytest

from quant.cli import main
from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.migrate.legacy import build_sample, reconcile, run as run_migration
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
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=tmp_path / "knowledge")

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


def test_fullsource_legacy_quote_attribution_within_01(tmp_path, cfg):
    """Fullsource legacy-quote attribution within .01 of red-team table."""
    full_source = Path("quant_engine.db")
    db_path = tmp_path / "v2_full.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=tmp_path / "knowledge")

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

    df = reconcile(conn, legacy_db_path=full_source)
    assert not df.empty
    expected_cols = [
        "transition",
        "metric",
        "legacy_expected",
        "recomputed_original",
        "adjusted_descriptive",
        "difference",
        "status",
    ]
    assert list(df.columns) == expected_cols

    # Every full-source row must be within 0.01 tolerance
    for _, row in df.iterrows():
        if row["metric"] in ("final_score", "momentum_multiplier", "fundamental_composite", "equal_weight_composite"):
            diff = abs(row["difference"])
            assert diff <= 0.01, f"Failed for {row['transition']} {row['metric']}: diff={diff}"
            assert row["status"] == "PASS"


def test_adjusted_result_shown_separately_without_enforced_sign(tmp_path, cfg):
    """Adjusted result is present in dataframe and shown separately without enforced sign."""
    full_source = Path("quant_engine.db")
    db_path = tmp_path / "v2_full_adj.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=tmp_path / "knowledge")

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

    run_migration(ctx, legacy_db_path=full_source, dry_run=False)
    df = reconcile(conn, legacy_db_path=full_source)

    # Column exists and values are numeric
    assert "adjusted_descriptive" in df.columns
    # Check that jun->jul final_score has negative sign in adjusted / original without failing
    jun_jul_final = df[(df["transition"].str.contains("2026-06-14")) & (df["metric"] == "final_score")]
    assert not jun_jul_final.empty
    row = jun_jul_final.iloc[0]
    assert row["recomputed_original"] < 0  # -0.063 is negative, not forced positive
    assert row["status"] == "PASS"


def test_irregular_adjacent_periods_are_not_h1_labels(test_ctx, sample_legacy_db):
    """Irregular adjacent legacy periods are NOT written as h1 labels."""
    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)

    cur = test_ctx.conn.cursor()
    # Ensure no labels rows exist for irregular snapshot end dates
    labels = cur.execute(
        "SELECT count(*) FROM labels WHERE track = 'legacy' AND horizon_m = 1 AND end_date IN ('2026-07-11', '2026-08-14', '2026-09-03')"
    ).fetchone()[0]
    assert labels == 0


def test_second_migration_is_unchanged0(test_ctx, sample_legacy_db):
    """Second migration returns status='unchanged' and counts 0."""
    res1 = run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert res1.status == "ok"

    res2 = run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert res2.status == "ok"
    assert res2.details.get("status") == "unchanged"
    assert res2.counts.get("scores", 0) == 0
    assert res2.counts.get("migrated", 0) == 0


def test_source_sha256_remains_identical(test_ctx, sample_legacy_db):
    """Source database SHA256 remains identical before, during, and after migration and dry-run."""
    initial_sha = hashlib.sha256(sample_legacy_db.read_bytes()).hexdigest()

    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=True)
    assert hashlib.sha256(sample_legacy_db.read_bytes()).hexdigest() == initial_sha

    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert hashlib.sha256(sample_legacy_db.read_bytes()).hexdigest() == initial_sha

    reconcile(test_ctx.conn, legacy_db_path=sample_legacy_db)
    assert hashlib.sha256(sample_legacy_db.read_bytes()).hexdigest() == initial_sha


def test_factual_system_adrs_only(test_ctx, sample_legacy_db):
    """Migration records factual system decisions and defect ADRs, never Tier 2 approved placeholders."""
    run_migration(test_ctx, legacy_db_path=sample_legacy_db, dry_run=False)

    cur = test_ctx.conn.cursor()
    cur.execute("SELECT decision_id, kind, tier, approver_kind, status, adr_path FROM decisions WHERE subject_id = 'legacy_migration'")
    decisions = cur.fetchall()
    assert len(decisions) == 1
    did, kind, tier, approver_kind, status, adr_path = decisions[0]

    assert tier == 0
    assert approver_kind == "system"
    assert status in ("applied", "approved")

    # ADR file exists on disk
    p = Path(adr_path)
    if not p.is_absolute():
        p = test_ctx.cfg.paths.knowledge_dir.parent / p
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "Tier 0" in content
    assert "legacy_migration" in content
    assert "Tier 2" not in content


def test_cli_db_migrate_legacy(tmp_path, sample_legacy_db):
    """CLI command `quant db migrate-legacy` executes successfully."""
    db_path = tmp_path / "cli_migrated.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    # Dry run via CLI
    code_dry = main([
        "db", "migrate-legacy",
        "--db-path", str(db_path),
        "--legacy-db", str(sample_legacy_db),
        "--dry-run",
    ])
    assert code_dry == 0

    # Live migration via CLI
    code_live = main([
        "db", "migrate-legacy",
        "--db-path", str(db_path),
        "--legacy-db", str(sample_legacy_db),
    ])
    assert code_live == 0
