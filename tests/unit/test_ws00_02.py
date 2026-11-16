import json
from pathlib import Path
import sqlite3
import pandas as pd
import pytest

from quant.db.core import (
    apply_schema,
    append_rows,
    connect,
    table_hash,
    update_control,
)
from quant.errors import ImmutableConflict, Refused
from quant.types import FrozenClock


class MinimalContext:
    def __init__(self, conn):
        self.conn = conn
        self.run_id = 1
        self.clock = FrozenClock("2026-10-01T00:00:00.000000Z")


def test_foreign_keys_and_wal_enabled(tmp_path):
    state_db = tmp_path / "state.db"
    conn = connect(state_db)
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert jm.lower() == "wal"
    conn.close()

    # Readonly connection uses URI and does not enable WAL
    ro_conn = connect(state_db, readonly=True)
    fk_ro = ro_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk_ro == 1
    ro_conn.close()


def test_schema_application(tmp_path):
    conn = connect(":memory:")
    apply_schema(conn, kind="state")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cohorts" in tables
    assert "fundamentals" in tables
    assert "ledger_events" in tables
    assert len(tables) >= 40
    conn.close()

    p_conn = connect(":memory:")
    apply_schema(p_conn, kind="prices")
    p_tables = {r[0] for r in p_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert len(p_tables) == 4
    p_conn.close()


def test_annual_and_quarterly_coexist_and_immutability(tmp_path):
    conn = connect(":memory:")
    apply_schema(conn, kind="state")
    ctx = MinimalContext(conn)

    # Seed required runs & securities
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-09-30', 'test', 'live', 1, '2026-10-01T00:00:00.000000Z', 'running', 'g', 'c', 'q', 'r')"
    )
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'SYN1', 'Synthetic', '2026-01-01', '2026-09-30', 'listed')"
    )

    cases = json.loads(Path("docs/spec/contracts/golden_cases.json").read_text())["cases"]
    c = cases["annual_quarterly"]
    
    # Insert Annual and Quarterly facts
    df_annual = pd.DataFrame([{
        "security_id": 1,
        "statement": "income",
        "freq": "A",
        "period_end": c["period_end"],
        "field": c["field"],
        "value": float(c["values"]["A"]),
        "unit": "inr",
        "available_from": c["fetched_at"],
        "available_from_basis": "first_fetch",
        "fetched_at": c["fetched_at"],
        "source": "synthetic",
        "run_id": 1,
    }])
    df_quarterly = pd.DataFrame([{
        "security_id": 1,
        "statement": "income",
        "freq": "Q",
        "period_end": c["period_end"],
        "field": c["field"],
        "value": float(c["values"]["Q"]),
        "unit": "inr",
        "available_from": c["fetched_at"],
        "available_from_basis": "first_fetch",
        "fetched_at": c["fetched_at"],
        "source": "synthetic",
        "run_id": 1,
    }])
    keys = ["security_id", "statement", "freq", "period_end", "field", "fetched_at"]

    assert append_rows(ctx, "fundamentals", df_annual, keys) == 1
    assert append_rows(ctx, "fundamentals", df_quarterly, keys) == 1

    # Verify both rows exist
    count = conn.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
    assert count == 2

    # Same-key same-value is no-op
    assert append_rows(ctx, "fundamentals", df_annual, keys) == 0

    # Same-key different-value raises ImmutableConflict
    df_conflict = df_annual.copy()
    df_conflict["value"] = 999.0
    with pytest.raises(ImmutableConflict):
        append_rows(ctx, "fundamentals", df_conflict, keys)

    # Direct UPDATE and DELETE fail via triggers
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fundamentals SET value = 123 WHERE freq = 'A'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM fundamentals WHERE freq = 'A'")


def test_update_control_and_ledger(tmp_path):
    conn = connect(":memory:")
    apply_schema(conn, kind="state")
    ctx = MinimalContext(conn)

    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-09-30', 'test', 'live', 1, '2026-10-01T00:00:00.000000Z', 'running', 'g', 'c', 'q', 'r')"
    )

    # Allowed control update
    res = update_control(ctx, "runs", {"run_id": 1}, {"status": "ok"})
    assert res == 1
    row = conn.execute("SELECT status FROM runs WHERE run_id = 1").fetchone()
    assert row["status"] == "ok"

    # Journaled in ledger_events
    event = conn.execute("SELECT * FROM ledger_events ORDER BY seq DESC LIMIT 1").fetchone()
    assert event["table_name"] == "runs"
    assert event["operation"] == "update"

    # Disallowed table raises Refused
    with pytest.raises(Refused):
        update_control(ctx, "fundamentals", {"security_id": 1}, {"value": 10})


def test_table_hash_deterministic(tmp_path):
    conn = connect(":memory:")
    apply_schema(conn, kind="state")
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-09-30', 'test', 'live', 1, '2026-10-01T00:00:00.000000Z', 'ok', 'g', 'c', 'q', 'r')"
    )
    h1 = table_hash(conn, "runs")
    h2 = table_hash(conn, "runs")
    assert h1 == h2
    assert len(h1) == 64
