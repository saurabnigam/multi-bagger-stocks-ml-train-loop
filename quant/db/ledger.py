from __future__ import annotations

import glob
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import pandas as pd

from quant.db.core import apply_schema, connect, table_hash
from quant.types import Check, CheckReport


def export(conn: sqlite3.Connection, ledger_dir: Path) -> list[Path]:
    """Export ledger_events to partitioned JSONL files by month."""
    cur = conn.execute(
        "SELECT seq, run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json "
        "FROM ledger_events ORDER BY seq ASC"
    )
    rows = cur.fetchall()
    if not rows:
        return []

    # Group by month YYYY-MM based on recorded_at
    events_by_month: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        month = r["recorded_at"][:7]
        event_dict = {
            "seq": r["seq"],
            "run_id": r["run_id"],
            "recorded_at": r["recorded_at"],
            "table_name": r["table_name"],
            "operation": r["operation"],
            "key_json": r["key_json"],
            "before_sha256": r["before_sha256"],
            "after_json": r["after_json"],
        }
        events_by_month.setdefault(month, []).append(event_dict)

    written_paths = []
    for month, events in events_by_month.items():
        month_dir = ledger_dir / month
        month_dir.mkdir(parents=True, exist_ok=True)
        out_file = month_dir / "events.jsonl"
        tmp_file = month_dir / "events.jsonl.tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            for ev in events:
                line = json.dumps(ev, sort_keys=True, allow_nan=False, separators=(",", ":"))
                f.write(line + "\n")

        tmp_file.replace(out_file)
        written_paths.append(out_file)

    return written_paths


def rebuild(ledger_dir: Path, output_db: Path) -> None:
    """Rebuild a database from scratch by replaying ledger events."""
    if output_db.exists():
        output_db.unlink()
    output_db.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(output_db)
    apply_schema(conn, kind="state")

    # Read all events from ledger files
    all_events = []
    for p in ledger_dir.glob("**/events.jsonl"):
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    all_events.append(json.loads(line))

    all_events.sort(key=lambda x: x["seq"])

    with conn:
        for ev in all_events:
            table = ev["table_name"]
            op = ev["operation"]
            key_dict = json.loads(ev["key_json"])
            before_hash = ev["before_sha256"]
            after_dict = json.loads(ev["after_json"])

            if op == "insert":
                cols = list(after_dict.keys())
                vals = [after_dict[c] for c in cols]
                placeholders = ", ".join(["?"] * len(cols))
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                    tuple(vals),
                )
            elif op == "update":
                where_clauses = []
                where_vals = []
                for k, v in key_dict.items():
                    if v is None:
                        where_clauses.append(f"{k} IS NULL")
                    else:
                        where_clauses.append(f"{k} = ?")
                        where_vals.append(v)

                cur = conn.execute(
                    f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}",
                    tuple(where_vals),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Replay failed: row not found for update in {table}: {key_dict}")

                current_dict = dict(row)
                current_hash = hashlib.sha256(
                    json.dumps(current_dict, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if current_hash != before_hash:
                    raise ValueError(
                        f"changed before hash fails replay on {table}: expected {before_hash}, got {current_hash}"
                    )

                set_clauses = [f"{k} = ?" for k in after_dict.keys()]
                set_vals = [after_dict[k] for k in after_dict.keys()]
                conn.execute(
                    f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}",
                    tuple(set_vals + where_vals),
                )
            elif op == "delete":
                pass

            # Mirror the event in ledger_events
            conn.execute(
                "INSERT INTO ledger_events (seq, run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ev["seq"],
                    ev["run_id"],
                    ev["recorded_at"],
                    ev["table_name"],
                    ev["operation"],
                    ev["key_json"],
                    ev["before_sha256"],
                    ev["after_json"],
                ),
            )

    conn.close()


def verify(conn: sqlite3.Connection, ledger_dir: Path) -> CheckReport:
    """Verify source database against reconstructed temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        temp_db_path = Path(tmp.name)

    try:
        rebuild(ledger_dir, temp_db_path)
        rebuilt_conn = connect(temp_db_path, readonly=True)

        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r["name"] for r in cur.fetchall() if not r["name"].startswith("sqlite_")]

        checks = []
        for t in tables:
            orig_h = table_hash(conn, t)
            reb_h = table_hash(rebuilt_conn, t)
            checks.append(
                Check(
                    id=f"ledger_verify_{t}",
                    status="PASS" if orig_h == reb_h else "FAIL",
                    observed=reb_h,
                    expected=orig_h,
                    reason=f"Table {t} integrity check against replayed ledger",
                    blocking=True,
                )
            )

        rebuilt_conn.close()
        return CheckReport(checks=checks)
    finally:
        if temp_db_path.exists():
            temp_db_path.unlink()


def size(cfg: Any) -> dict[str, int]:
    db_path = cfg.paths.db
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    ledger_dir = cfg.paths.data_dir / "ledger"
    ledger_bytes = 0
    if ledger_dir.exists():
        ledger_bytes = sum(f.stat().st_size for f in ledger_dir.glob("**/*") if f.is_file())
    return {"db_bytes": db_bytes, "ledger_bytes": ledger_bytes}
