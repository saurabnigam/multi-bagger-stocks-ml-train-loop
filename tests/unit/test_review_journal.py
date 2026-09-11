"""Regression tests for the review fixes to journaling, staging and control updates.

MASTER_SPEC 4.1/4.2: every write inside a run is journaled in the same
transaction; `db verify` rebuilds the state from the ledger; staging work is
discarded on a blocked publication while gate diagnostics survive.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from quant.config import load as load_config
from quant.db import ledger
from quant.db.core import (
    CONTROL_ALLOWLIST,
    append_rows,
    apply_schema,
    connect,
    table_hash,
    update_control,
    user_tables,
)
from quant.errors import Blocked, Refused
from quant.run import RunContext
from quant.types import Actor, FrozenClock


def _fresh(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db_path, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", ui_dir=tmp_path / "ui",
                                   prices_db=tmp_path / "prices.db")
    return cfg, db_path


def test_control_allowlist_names_real_tables(tmp_path):
    conn = connect(":memory:")
    apply_schema(conn, kind="state")
    tables = set(user_tables(conn))
    missing = sorted(CONTROL_ALLOWLIST - tables)
    assert missing == [], f"allowlist names tables that do not exist: {missing}"


def test_raw_sql_inside_run_is_journaled_and_rebuildable(tmp_path):
    """Writes made with plain SQL inside a RunContext must reach ledger_events and replay exactly."""
    cfg, db_path = _fresh(tmp_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    with RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=clock,
                    actor=Actor(kind="system", name="t")) as ctx:
        ctx.conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000000001', 'Raw Co', '2026-01-01', '2026-09-30', 'listed')"
        )
        ctx.conn.execute(
            "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
            "VALUES (1, 'RAW', 'RAW.NS', '2026-01-01', 'nifty500_csv')"
        )
        # a REAL that SQLite's json_object would print with 15 digits must still round-trip
        ctx.conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
            "VALUES (1, '2026-09-15T10:00:00.000000Z', ?, 0.5, 1e9, 'yahoo')",
            (0.1 + 0.2,),
        )
        update_control(ctx, "securities", {"security_id": 1}, {"last_seen": "2026-10-01"})
        ctx.status = "ok"

    conn = connect(db_path, readonly=True)
    ops = conn.execute("SELECT table_name, operation, count(*) FROM ledger_events GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    ops = {(r[0], r[1]): r[2] for r in ops}
    assert ops[("securities", "insert")] == 1
    assert ops[("securities", "update")] == 1
    assert ops[("symbol_history", "insert")] == 1
    assert ops[("holdings", "insert")] == 1
    assert ops[("runs", "insert")] == 1
    assert ops[("runs", "update")] == 1

    ledger_dir = tmp_path / "ledger"
    ledger.export(conn, ledger_dir)
    report = ledger.verify(conn, ledger_dir)
    failed = [c.id for c in report.checks if c.status == "FAIL"]
    assert failed == [], failed
    conn.close()


def test_writes_outside_a_run_are_not_journaled(tmp_path):
    cfg, db_path = _fresh(tmp_path)
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (7, 'INE000000007', 'Adhoc', '2026-01-01', '2026-09-30', 'listed')"
    )
    n = conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0]
    conn.close()
    assert n == 0


def test_staging_rollback_keeps_gate_rows(tmp_path):
    """A blocked publication discards staged rows but the dq_runs diagnostics survive."""
    from quant.data import gates
    from quant.types import Draft

    cfg, db_path = _fresh(tmp_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    with RunContext(as_of="2026-09-30", kind="monthly", track="live", cfg=cfg, clock=clock,
                    actor=Actor(kind="system", name="t")) as ctx:
        run_id = ctx.run_id
        # staged work that must disappear
        ctx.conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000000001', 'Staged', '2026-01-01', '2026-09-30', 'listed')"
        )
        draft = Draft(cohort_id="live:2026-09-30", as_of="2026-09-30", track="live",
                      knowledge_cutoff="2026-09-30T18:29:59.999999Z", definition_hash="d",
                      members=pd.DataFrame({"security_id": range(10)}),
                      groups=pd.Series({i: "Technology" for i in range(10)}), source_refs={})
        with pytest.raises(Blocked):
            gates.run(ctx, draft, phase="pre", strict=True)
        ctx.rollback_staging()
        ctx.status = "blocked"

    conn = connect(db_path, readonly=True)
    assert conn.execute("SELECT count(*) FROM securities").fetchone()[0] == 0
    gates_recorded = [r[0] for r in conn.execute("SELECT gate FROM dq_runs WHERE run_id = ? ORDER BY gate", (run_id,))]
    assert gates_recorded == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    assert conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] == "blocked"
    conn.close()


def test_append_rows_does_not_commit_the_staging_transaction(tmp_path):
    cfg, db_path = _fresh(tmp_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    with pytest.raises(RuntimeError):
        with RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=clock,
                        actor=Actor(kind="system", name="t")) as ctx:
            append_rows(ctx, "securities", pd.DataFrame([{
                "security_id": 1, "isin": "INE000000001", "name": "X", "first_seen": "2026-01-01",
                "last_seen": "2026-09-30", "status": "listed"}]), ["security_id"])
            raise RuntimeError("crash after append")
    conn = connect(db_path, readonly=True)
    assert conn.execute("SELECT count(*) FROM securities").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    conn.close()


def test_update_control_rejects_unlisted_table(tmp_path):
    cfg, db_path = _fresh(tmp_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    with RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=clock,
                    actor=Actor(kind="system", name="t")) as ctx:
        with pytest.raises(Refused):
            update_control(ctx, "factor_values", {"cohort_id": "x"}, {"z": 1.0})


def test_cold_start_monthly_reports_bootstrap_required_with_gate_rows(tmp_path):
    """spec 3.2 / TEST plan: run monthly without earlier captures -> exit 2 BOOTSTRAP_REQUIRED, gates exist, no cohort."""
    from quant.run import monthly

    cfg, db_path = _fresh(tmp_path)
    clock = FrozenClock("2026-10-01T18:30:00.000000Z")
    code = monthly(cfg, clock, Actor(kind="system", name="t"), as_of="2026-09-30", skip_capture=True)
    assert code == 2
    conn = connect(db_path, readonly=True)
    run = conn.execute("SELECT run_id, status, notes_json FROM runs WHERE kind = 'monthly' ORDER BY run_id DESC").fetchone()
    assert run["status"] == "blocked" and "BOOTSTRAP_REQUIRED" in (run["notes_json"] or "")
    gates = [r[0] for r in conn.execute("SELECT gate FROM dq_runs WHERE run_id = ? ORDER BY gate", (run["run_id"],))]
    assert gates == ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    assert conn.execute("SELECT count(*) FROM data_quality_events WHERE code = 'BOOTSTRAP_REQUIRED'").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM cohorts WHERE track = 'live'").fetchone()[0] == 0
    conn.close()
