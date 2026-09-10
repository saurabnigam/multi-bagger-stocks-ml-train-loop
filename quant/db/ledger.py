"""Ledger export, replay (rebuild) and verification (MASTER_SPEC 4.2)."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from quant.db.core import (
    apply_schema,
    connect,
    install_journal_triggers,
    row_hash,
    table_hash,
    user_tables,
)
from quant.types import Check, CheckReport

EVENT_FIELDS = (
    "seq",
    "run_id",
    "recorded_at",
    "table_name",
    "operation",
    "key_json",
    "before_sha256",
    "after_json",
)


def export(conn: sqlite3.Connection, ledger_dir: Path) -> list[Path]:
    """Export ledger_events to ``<ledger_dir>/YYYY-MM/events.jsonl`` partitions.

    Each partition is rewritten in full (atomic replace) so a partition always
    reflects every event of its month in seq order.
    """
    cur = conn.execute(
        "SELECT seq, run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json "
        "FROM ledger_events ORDER BY seq ASC"
    )
    rows = cur.fetchall()
    if not rows:
        return []

    events_by_month: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        month = r["recorded_at"][:7]
        events_by_month.setdefault(month, []).append({f: r[f] for f in EVENT_FIELDS})

    written_paths = []
    for month, events in sorted(events_by_month.items()):
        month_dir = Path(ledger_dir) / month
        month_dir.mkdir(parents=True, exist_ok=True)
        out_file = month_dir / "events.jsonl"
        tmp_file = month_dir / "events.jsonl.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n")
        tmp_file.replace(out_file)
        written_paths.append(out_file)
    return written_paths


def load_events(ledger_dir: Path) -> list[dict[str, Any]]:
    all_events: list[dict[str, Any]] = []
    for p in sorted(Path(ledger_dir).glob("**/events.jsonl")):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_events.append(json.loads(line))
    all_events.sort(key=lambda x: x["seq"])
    return all_events


def _where(key_dict: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses, vals = [], []
    for k, v in key_dict.items():
        if v is None:
            clauses.append(f"{k} IS NULL")
        else:
            clauses.append(f"{k} = ?")
            vals.append(v)
    return " AND ".join(clauses), vals


def rebuild(ledger_dir: Path, output_db: Path) -> None:
    """Rebuild a database from scratch by replaying ledger events in seq order.

    The schema is created without journal triggers, events are replayed with
    before-hash verification, the original ledger rows are restored verbatim,
    and only then are the journal triggers installed on the rebuilt database.
    """
    output_db = Path(output_db)
    if output_db.exists():
        output_db.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(output_db) + suffix)
        if side.exists():
            side.unlink()
    output_db.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(output_db)
    try:
        apply_schema(conn, kind="state", journal=False)
        events = load_events(ledger_dir)
        conn.execute("BEGIN")
        try:
            for ev in events:
                table = ev["table_name"]
                op = ev["operation"]
                key_dict = json.loads(ev["key_json"])
                after_dict = json.loads(ev["after_json"])

                if op == "insert":
                    cols = list(after_dict.keys())
                    placeholders = ", ".join(["?"] * len(cols))
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                        tuple(after_dict[c] for c in cols),
                    )
                elif op in ("update", "delete"):
                    where_sql, where_vals = _where(key_dict)
                    row = conn.execute(
                        f"SELECT * FROM {table} WHERE {where_sql}", tuple(where_vals)
                    ).fetchone()
                    if not row:
                        raise ValueError(
                            f"Replay failed: row not found for {op} in {table}: {key_dict}"
                        )
                    current_hash = row_hash(row)
                    if current_hash != ev["before_sha256"]:
                        raise ValueError(
                            f"changed before hash fails replay on {table} seq {ev['seq']}: "
                            f"expected {ev['before_sha256']}, got {current_hash}"
                        )
                    if op == "update":
                        set_sql = ", ".join(f"{k} = ?" for k in after_dict.keys())
                        conn.execute(
                            f"UPDATE {table} SET {set_sql} WHERE {where_sql}",
                            tuple(list(after_dict.values()) + where_vals),
                        )
                    else:
                        conn.execute(f"DELETE FROM {table} WHERE {where_sql}", tuple(where_vals))
                else:
                    raise ValueError(f"Unknown ledger operation {op!r} at seq {ev['seq']}")

                conn.execute(
                    "INSERT INTO ledger_events (seq, run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(ev[f] for f in EVENT_FIELDS),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        install_journal_triggers(conn)
    finally:
        conn.close()


def verify(conn: sqlite3.Connection, ledger_dir: Path) -> CheckReport:
    """Compare every state table of ``conn`` with a database rebuilt from the ledger."""
    with tempfile.TemporaryDirectory() as tmp:
        temp_db_path = Path(tmp) / "rebuilt.db"
        checks: list[Check] = []
        try:
            rebuild(ledger_dir, temp_db_path)
        except Exception as exc:  # replay itself failed
            return CheckReport(
                checks=[
                    Check(
                        id="ledger_verify_replay",
                        status="FAIL",
                        observed=str(exc),
                        expected="replay completes",
                        reason="Ledger replay failed",
                        blocking=True,
                    )
                ]
            )
        rebuilt_conn = connect(temp_db_path, readonly=True)
        try:
            for t in user_tables(conn):
                orig_h = table_hash(conn, t)
                reb_h = table_hash(rebuilt_conn, t)
                n_orig = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                n_reb = rebuilt_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                checks.append(
                    Check(
                        id=f"ledger_verify_{t}",
                        status="PASS" if orig_h == reb_h else "FAIL",
                        observed={"rows": n_reb, "sha256": reb_h},
                        expected={"rows": n_orig, "sha256": orig_h},
                        reason=f"Table {t} integrity check against replayed ledger",
                        blocking=True,
                    )
                )
        finally:
            rebuilt_conn.close()
        return CheckReport(checks=checks)


def size(cfg: Any) -> dict[str, int]:
    db_path = Path(cfg.paths.db)
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            db_bytes += side.stat().st_size
    ledger_dir = Path(cfg.paths.data_dir) / "ledger"
    ledger_bytes = 0
    if ledger_dir.exists():
        ledger_bytes = sum(f.stat().st_size for f in ledger_dir.glob("**/*") if f.is_file())
    return {"db_bytes": db_bytes, "ledger_bytes": ledger_bytes}
