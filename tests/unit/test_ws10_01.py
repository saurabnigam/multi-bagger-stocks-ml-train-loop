"""Tests for WS10.01: Source inventory and fixture extraction (C10)."""

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
def legacy_inventory():
    p = Path("docs/spec/contracts/legacy_inventory.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def test_ctx(tmp_path, cfg):
    db_path = tmp_path / "v2_state.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path)

    clock = FrozenClock("2026-09-01T09:00:00.000000Z")
    actor = Actor(kind="system", name="migration")

    ctx = RunContext(
        as_of="2026-09-01",
        kind="migration",
        track="live",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-01', 'migration', 'live', 1, '2026-09-01T09:00:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
    return ctx


def test_open_source_readonly_and_inventory(legacy_inventory):
    """Source database must open mode=ro, match SHA256 and row counts, and reject writes."""
    db_path = Path("quant_engine.db")
    assert db_path.exists()

    expected_sha = legacy_inventory["sha256"]["quant_engine.db"]
    actual_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert actual_sha == expected_sha

    # Open with URI mode=ro
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # Check row counts
    for tbl, expected_n in legacy_inventory["row_counts"].items():
        cnt = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        assert cnt == expected_n, f"Count mismatch on {tbl}: expected {expected_n}, got {cnt}"

    # Attempting to write must fail
    with pytest.raises(sqlite3.OperationalError, match="(?i)readonly"):
        conn.execute("UPDATE daily_predictions SET price = 999 WHERE id = 1")

    conn.close()


def test_build_sample_extracts_20_tickers(tmp_path, legacy_inventory):
    """Build sample creates an isolated fixture with 20 tickers, preserving source SHA256."""
    source_db = Path("quant_engine.db")
    initial_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()

    output_db = tmp_path / "legacy_sample_20.db"
    res = build_sample(source=source_db, output=output_db, tickers=KNOWN_20_TICKERS)
    assert res.status == "ok"
    assert output_db.exists()

    # Verify source SHA256 remained completely unchanged
    post_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()
    assert post_sha == initial_sha

    # Inspect sample database
    sample_conn = sqlite3.connect(output_db)
    sample_conn.row_factory = sqlite3.Row

    # All 20 tickers present
    tickers_in_sample = {r[0] for r in sample_conn.execute("SELECT DISTINCT ticker FROM daily_predictions").fetchall()}
    assert tickers_in_sample == set(KNOWN_20_TICKERS)

    # Active weights fully preserved (all 12 rows)
    n_aw = sample_conn.execute("SELECT count(*) FROM active_weights").fetchone()[0]
    assert n_aw == 12

    # Performance tracking has entries
    n_pt = sample_conn.execute("SELECT count(*) FROM performance_tracking").fetchone()[0]
    assert n_pt > 0

    sample_conn.close()


def test_dry_run_changes_nothing(test_ctx, legacy_inventory):
    """dry_run=True modifies neither the V2 database nor the legacy database."""
    source_db = Path("quant_engine.db")
    initial_sha = hashlib.sha256(source_db.read_bytes()).hexdigest()

    res = run_migration(test_ctx, legacy_db_path=source_db, dry_run=True)
    assert res.status == "ok"
    assert res.counts.get("dry_run") == 1

    # Check V2 state connection: no cohorts or scores inserted
    n_cohorts = test_ctx.conn.execute("SELECT count(*) FROM cohorts").fetchone()[0]
    assert n_cohorts == 0
    n_scores = test_ctx.conn.execute("SELECT count(*) FROM scores").fetchone()[0]
    assert n_scores == 0

    # Source SHA unchanged
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == initial_sha


def test_mismatched_source_facts_reported_never_repaired(tmp_path, test_ctx):
    """Mismatched source facts are reported as defects and never repaired in the legacy DB."""
    # Create a dummy legacy DB with an invalid date and abnormal value
    bad_legacy_db = tmp_path / "bad_legacy.db"
    conn = sqlite3.connect(bad_legacy_db)
    conn.execute("CREATE TABLE daily_predictions (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, price REAL, final_score REAL)")
    conn.execute("CREATE TABLE active_weights (id INTEGER PRIMARY KEY, last_updated TEXT, quality_weight REAL, trained_through TEXT, note TEXT)")
    conn.execute("CREATE TABLE performance_tracking (prediction_id INTEGER, forward_date TEXT, forward_price REAL, return_pct REAL)")

    conn.execute("INSERT INTO daily_predictions VALUES (1, 'invalid-date', 'BAD.NS', -5.0, 150.0)")
    conn.execute("INSERT INTO active_weights VALUES (1, '2026-06-01', 0.2, '2026-06-01', 'note')")
    conn.commit()
    conn.close()

    bad_initial_sha = hashlib.sha256(bad_legacy_db.read_bytes()).hexdigest()

    res = run_migration(test_ctx, legacy_db_path=bad_legacy_db, dry_run=False)
    # Defects should be captured in legacy_defects table in V2
    defects = test_ctx.conn.execute("SELECT * FROM legacy_defects").fetchall()
    assert len(defects) > 0

    # Verify bad legacy DB was NEVER modified in place
    assert hashlib.sha256(bad_legacy_db.read_bytes()).hexdigest() == bad_initial_sha
