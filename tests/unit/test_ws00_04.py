import json
from pathlib import Path
import sqlite3
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.types import Actor, FrozenClock
from quant.db.core import apply_schema, append_rows, connect, table_hash
from quant.db.ledger import export as ledger_export, rebuild as ledger_rebuild, verify as ledger_verify
from quant.run import RunContext
from quant.errors import Blocked, ImmutableConflict


def test_run_context_failure_leaves_no_partial_cohort(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = load_config().with_paths(db=db_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # Attempt that fails mid-way
    with pytest.raises(Blocked):
        with RunContext(
            as_of="2026-09-30", kind="monthly", track="live", cfg=cfg, clock=clock, actor=actor
        ) as ctx:
            # Seed securities
            ctx.conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                "VALUES (1, 'SYN1', 'Synthetic', '2026-01-01', '2026-09-30', 'listed')"
            )
            # Try to insert cohort in staging or transaction
            ctx.conn.execute(
                "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
                "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
                "VALUES ('live:1', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def', 'mem', '{}', "
                "'2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, ?)",
                (ctx.run_id,)
            )
            # Simulate a blocking gate failure
            raise Blocked("gate_failed", "G1 failed")

    # Reconnect and verify:
    # 1. No cohort row was committed (rolled back)
    # 2. Run row survived with status='blocked' and error
    verify_conn = connect(db_path, readonly=True)
    cohort_count = verify_conn.execute("SELECT count(*) FROM cohorts").fetchone()[0]
    assert cohort_count == 0

    run_row = verify_conn.execute("SELECT * FROM runs WHERE as_of = '2026-09-30'").fetchone()
    assert run_row is not None
    assert run_row["status"] == "blocked"
    assert "gate_failed" in run_row["notes_json"]
    assert run_row["finished_at"] is not None
    verify_conn.close()


def test_ledger_export_rebuild_and_verify(tmp_path):
    db_path = tmp_path / "source.db"
    ledger_dir = tmp_path / "ledger"
    rebuilt_db_path = tmp_path / "rebuilt.db"

    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = load_config().with_paths(db=db_path)
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    actor = Actor(kind="system", name="runner")

    # Successful run inserting data
    with RunContext(
        as_of="2026-09-30", kind="monthly", track="live", cfg=cfg, clock=clock, actor=actor
    ) as ctx:
        df_sec = pd.DataFrame([{
            "security_id": 1, "isin": "SYN1", "name": "Synthetic",
            "first_seen": "2026-01-01", "last_seen": "2026-09-30", "status": "listed"
        }])
        append_rows(ctx, "securities", df_sec, ["security_id"])

        df_cohort = pd.DataFrame([{
            "cohort_id": "live:1", "as_of": "2026-09-30", "track": "live",
            "knowledge_cutoff": "2026-09-30T18:29:59.999999Z", "definition_hash": "def",
            "membership_hash": "mem", "source_refs_json": "{}",
            "published_at": "2026-10-01T00:00:00.000000Z", "generated_at": "2026-10-01T00:00:00.000000Z",
            "is_clean": 1, "run_id": ctx.run_id
        }])
        append_rows(ctx, "cohorts", df_cohort, ["cohort_id"])
        ctx.status = "ok"

    # Export ledger
    export_conn = connect(db_path, readonly=True)
    exported_files = ledger_export(export_conn, ledger_dir)
    assert len(exported_files) > 0
    assert all(f.is_file() for f in exported_files)

    # Rebuild database from exported ledger
    ledger_rebuild(ledger_dir, rebuilt_db_path)

    # Verify rebuilt database matches source
    report = ledger_verify(export_conn, ledger_dir)
    assert report.passed is True

    # Compare table hashes between source and rebuilt
    rebuilt_conn = connect(rebuilt_db_path, readonly=True)
    assert table_hash(export_conn, "cohorts") == table_hash(rebuilt_conn, "cohorts")
    assert table_hash(export_conn, "securities") == table_hash(rebuilt_conn, "securities")
    assert table_hash(export_conn, "runs") == table_hash(rebuilt_conn, "runs")
    assert table_hash(export_conn, "ledger_events") == table_hash(rebuilt_conn, "ledger_events")

    export_conn.close()
    rebuilt_conn.close()


def test_corrupted_ledger_before_hash_fails_replay(tmp_path):
    ledger_dir = tmp_path / "ledger"
    output_db = tmp_path / "out.db"

    # Create a corrupted ledger file with mismatched before_sha256 on update
    month_dir = ledger_dir / "2026-10"
    month_dir.mkdir(parents=True)
    event1 = {
        "seq": 1,
        "run_id": 1,
        "recorded_at": "2026-10-01T00:00:00.000000Z",
        "table_name": "runs",
        "operation": "insert",
        "key_json": json.dumps({"run_id": 1}),
        "before_sha256": "",
        "after_json": json.dumps({
            "run_id": 1, "as_of": "2026-09-30", "kind": "test", "track": "live", "attempt": 1,
            "started_at": "2026-10-01T00:00:00.000000Z", "finished_at": None, "status": "running",
            "notes_json": None, "git_sha": "g", "code_sha256": "c", "config_sha256": "q", "registry_sha256": "r"
        }),
    }
    # Corrupted update with wrong before_sha256
    event2 = {
        "seq": 2,
        "run_id": 1,
        "recorded_at": "2026-10-01T00:01:00.000000Z",
        "table_name": "runs",
        "operation": "update",
        "key_json": json.dumps({"run_id": 1}),
        "before_sha256": "bad_hash_1234567890abcdef",
        "after_json": json.dumps({
            "run_id": 1, "as_of": "2026-09-30", "kind": "test", "track": "live", "attempt": 1,
            "started_at": "2026-10-01T00:00:00.000000Z", "finished_at": "2026-10-01T00:01:00.000000Z",
            "status": "ok", "notes_json": None, "git_sha": "g", "code_sha256": "c", "config_sha256": "q", "registry_sha256": "r"
        }),
    }
    with open(month_dir / "events.jsonl", "w") as f:
        f.write(json.dumps(event1) + "\n")
        f.write(json.dumps(event2) + "\n")

    with pytest.raises(ValueError, match="before hash"):
        ledger_rebuild(ledger_dir, output_db)
