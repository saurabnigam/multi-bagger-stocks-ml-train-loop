from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal
import pandas as pd

from quant.errors import ImmutableConflict, Refused

CONTROL_ALLOWLIST = {
    "runs",
    "proposals",
    "decisions",
    "portfolio_orders",
    "portfolios",
    "registry",
    "securities",
    "symbols",
    "sector_taxonomies",
    "models",
    "model_versions",
    "data_quality_events",
}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path_str = str(path)
    if path_str == ":memory:":
        conn = sqlite3.connect(":memory:")
    elif readonly:
        abs_path = Path(path).resolve()
        conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    else:
        abs_path = Path(path).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(abs_path))

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    if not readonly and path_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")

    return conn


def apply_schema(conn: sqlite3.Connection, *, kind: Literal["state", "prices"] = "state") -> None:
    pkg_dir = Path(__file__).resolve().parent
    if kind == "state":
        schema_path = pkg_dir / "schema.sql"
    elif kind == "prices":
        schema_path = pkg_dir / "price_schema.sql"
    else:
        raise ValueError(f"Unknown schema kind: {kind}")

    sql_text = schema_path.read_text(encoding="utf-8")
    conn.executescript(sql_text)


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({_validate_identifier(table_name)})")
    return [r["name"] for r in cur.fetchall()]


def _values_equal(val1: Any, val2: Any) -> bool:
    if pd.isna(val1) and pd.isna(val2):
        return True
    if val1 == val2:
        return True
    try:
        f1, f2 = float(val1), float(val2)
        if abs(f1 - f2) < 1e-12:
            return True
    except (ValueError, TypeError):
        pass
    return False


def append_rows(ctx: Any, table: str, rows: pd.DataFrame, keys: list[str]) -> int:
    _validate_identifier(table)
    for k in keys:
        _validate_identifier(k)

    conn: sqlite3.Connection = ctx.conn
    has_ledger = (table != "ledger_events") and _has_table(conn, "ledger_events")
    table_cols = _get_table_columns(conn, table)

    # Filter row columns to only those defined in the table schema
    cols_to_use = [c for c in rows.columns if c in table_cols]
    for k in keys:
        if k not in cols_to_use:
            raise ValueError(f"Key column '{k}' not present in row data for table '{table}'")

    inserted_count = 0
    recorded_at = (
        ctx.clock.iso()
        if hasattr(ctx, "clock")
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    run_id = getattr(ctx, "run_id", 1)

    with conn:
        for _, row in rows.iterrows():
            row_dict = {c: (None if pd.isna(row[c]) else row[c]) for c in cols_to_use}
            key_dict = {k: row_dict[k] for k in keys}

            # Check if matching row exists
            where_clauses = []
            where_vals = []
            for k, v in key_dict.items():
                if v is None:
                    where_clauses.append(f"{k} IS NULL")
                else:
                    where_clauses.append(f"{k} = ?")
                    where_vals.append(v)

            query = f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}"
            existing = conn.execute(query, tuple(where_vals)).fetchall()

            if existing:
                # Check for equivalence or conflict
                matched = False
                for ex in existing:
                    all_cols_match = True
                    for c in cols_to_use:
                        if not _values_equal(ex[c], row_dict[c]):
                            all_cols_match = False
                            break
                    if all_cols_match:
                        matched = True
                        break
                if matched:
                    # Idempotent no-op
                    continue
                else:
                    raise ImmutableConflict(table=table, key=key_dict)

            # Insert new row
            placeholders = ", ".join(["?"] * len(cols_to_use))
            col_names = ", ".join(cols_to_use)
            insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
            conn.execute(insert_sql, [row_dict[c] for c in cols_to_use])
            inserted_count += 1

            # Journal to ledger_events
            if has_ledger:
                key_json = json.dumps(key_dict, sort_keys=True, default=str, separators=(",", ":"))
                after_json = json.dumps(row_dict, sort_keys=True, default=str, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO ledger_events (run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
                    "VALUES (?, ?, ?, 'insert', ?, '', ?)",
                    (run_id, recorded_at, table, key_json, after_json),
                )

    return inserted_count


def update_control(ctx: Any, table: str, key: dict[str, Any], changes: dict[str, Any]) -> int:
    _validate_identifier(table)
    if table not in CONTROL_ALLOWLIST:
        raise Refused(
            "control_update_not_allowed",
            f"Table '{table}' is not in the control update allowlist",
        )

    conn: sqlite3.Connection = ctx.conn
    has_ledger = _has_table(conn, "ledger_events")
    recorded_at = (
        ctx.clock.iso()
        if hasattr(ctx, "clock")
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    run_id = getattr(ctx, "run_id", 1)

    where_clauses = []
    where_vals = []
    for k, v in key.items():
        _validate_identifier(k)
        if v is None:
            where_clauses.append(f"{k} IS NULL")
        else:
            where_clauses.append(f"{k} = ?")
            where_vals.append(v)

    with conn:
        cur = conn.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}", tuple(where_vals))
        row = cur.fetchone()
        if not row:
            return 0

        before_dict = dict(row)
        before_sha256 = hashlib.sha256(
            json.dumps(before_dict, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        set_clauses = []
        set_vals = []
        for col, val in changes.items():
            _validate_identifier(col)
            set_clauses.append(f"{col} = ?")
            set_vals.append(val)

        update_sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}"
        conn.execute(update_sql, tuple(set_vals + where_vals))

        if has_ledger:
            after_cur = conn.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}", tuple(where_vals))
            after_row = dict(after_cur.fetchone())
            key_json = json.dumps(key, sort_keys=True, default=str, separators=(",", ":"))
            after_json = json.dumps(after_row, sort_keys=True, default=str, separators=(",", ":"))
            conn.execute(
                "INSERT INTO ledger_events (run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
                "VALUES (?, ?, ?, 'update', ?, ?, ?)",
                (run_id, recorded_at, table, key_json, before_sha256, after_json),
            )

    return 1


def table_hash(conn: sqlite3.Connection, table: str) -> str:
    _validate_identifier(table)
    # Determine order columns (primary keys if any, else all columns)
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = cur.fetchall()
    pk_cols = [r["name"] for r in sorted(cols, key=lambda x: x["pk"]) if r["pk"] > 0]
    if pk_cols:
        order_by = ", ".join(pk_cols)
    else:
        order_by = ", ".join([r["name"] for r in cols])

    rows_cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    rows_data = [dict(r) for r in rows_cur.fetchall()]
    encoded = json.dumps(rows_data, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
