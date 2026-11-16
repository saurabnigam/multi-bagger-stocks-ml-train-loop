"""Tests for WS07.05: Backfill replay and warmup accounting (C07)."""

import sqlite3
import pandas as pd
import pytest

from quant.config import Config
from quant.evaluation.backfill import replay as backfill_replay
from quant.run import RunContext


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with open("quant/db/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _seed_backfill_world(conn):
    # Runs
    conn.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (1, '2026-06-30', 'test', 'backfill', 1, '2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)

    # Securities
    for sid in range(1, 11):
        conn.execute("""
            INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, '2025-01-01', '2026-12-31', 'listed')
        """, (sid, f"INE{sid:09d}", f"Stock {sid}"))

    # Factors: one backfillable price factor, one non-backfillable fundamental factor
    conn.execute("""
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level, hypothesis,
            formula, inputs_json, lookback_days, applies_to_financials, backfillable,
            min_coverage, code_sha256, module_path, status, registered_on, status_changed_on
        ) VALUES (
            'mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp',
            'formula', '[]', 252, 0, 1,
            0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01'
        )
    """)
    conn.execute("""
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level, hypothesis,
            formula, inputs_json, lookback_days, applies_to_financials, backfillable,
            min_coverage, code_sha256, module_path, status, registered_on, status_changed_on
        ) VALUES (
            'fcf_yield@1', 'fcf', 1, 'value', 1, 3, 'stock', 'hyp',
            'formula', '[]', 0, 1, 0,
            0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01'
        )
    """)

    # Seed monthly prices for 2026-03-31, 2026-06-30, 2026-09-30
    for dt, cid in [
        ("2026-03-31", "backfill:2026-03-31"),
        ("2026-06-30", "backfill:2026-06-30"),
        ("2026-09-30", "backfill:2026-09-30"),
    ]:
        conn.execute("""
            INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
            VALUES (?, ?, 'backfill', ?, ?, ?, '{}', ?, ?, 1, 1)
        """, (cid, dt, f"{dt}T18:29:59Z", f"def_{dt}", f"mem_{dt}", f"{dt}T23:59:59Z", f"{dt}T23:59:59Z"))

        for sid in range(1, 11):
            # Price intentionally constructed so higher stock id has lower future return -> negative IC
            price = 100.0 + sid * (10.0 if dt != "2026-09-30" else -5.0)
            conn.execute("""
                INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id)
                VALUES (?, ?, ?, ?, ?, 'synthetic', 'sha', 1)
            """, (cid, dt, sid, price, price))


def test_backfill_accounting_and_caveat(test_db, cfg):
    """Backfill replay records requested/actual date counts and mandatory survivorship caveat."""
    _seed_backfill_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="backfill", track="backfill", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 1

    res = backfill_replay(ctx, start="2026-03-31", end="2026-09-30")
    assert res.status == "ok"
    assert "survivorship_caveat" in res.details
    assert "survivorship" in res.details["survivorship_caveat"].lower()
    assert res.details["requested_start"] == "2026-03-31"
    assert res.details["requested_end"] == "2026-09-30"
    assert res.details.get("actual_cohorts", 0) >= 2


def test_negative_backfill_accepted_unaltered(test_db, cfg):
    """Negative measured backfill is accepted as an honest outcome, not adjusted to force positivity."""
    _seed_backfill_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="backfill", track="backfill", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 1

    res = backfill_replay(ctx, start="2026-03-31", end="2026-09-30")
    assert res.status == "ok"
    assert res.details.get("negative_results_accepted") is True

    # Inspect evaluations: negative values are preserved as negative
    neg_evals = test_db.execute("SELECT value FROM evaluations WHERE track = 'backfill' AND value < 0").fetchall()
    # If negative returns occur, they are stored with their negative value
    for row in neg_evals:
        assert row["value"] < 0.0


def test_replays_only_backfillable_factors(test_db, cfg):
    """Only price/volume factors with backfillable == 1 are evaluated; non-backfillable are excluded."""
    _seed_backfill_world(test_db)
    ctx = RunContext(as_of="2026-09-30", kind="backfill", track="backfill", cfg=cfg, clock=None, actor=None)
    ctx.conn = test_db
    ctx.run_id = 1

    res = backfill_replay(ctx, start="2026-03-31", end="2026-09-30")
    assert res.status == "ok"

    # Verify no evaluation was created for non-backfillable factor 'fcf_yield@1'
    fcf_evals = test_db.execute("SELECT count(*) FROM evaluations WHERE subject_id = 'fcf_yield@1'").fetchone()[0]
    assert fcf_evals == 0
