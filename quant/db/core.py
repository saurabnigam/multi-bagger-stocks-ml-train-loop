"""Database core: connections, schema application, write journaling and helpers.

Journaling design (MASTER_SPEC 4.1/4.2, INTERFACES C00)
------------------------------------------------------
Every write to a state table must land in ``ledger_events`` inside the same
transaction so that ``ledger.rebuild`` can reproduce the database byte for byte.
Rather than trusting ~30 modules to call a helper, journaling is enforced at the
database level: ``apply_schema`` installs an AFTER INSERT/UPDATE/DELETE trigger
on every state table except ``ledger_events`` itself. The triggers call four
functions registered on every connection returned by :func:`connect`:

* ``quant_run_id()``   -> run_id of the active RunContext (NULL outside one)
* ``quant_now()``      -> the RunContext clock's ISO timestamp (UTC now otherwise)
* ``quant_row_json``   -> canonical JSON of (name, value, name, value, ...) pairs
* ``quant_sha256``     -> hex SHA256 of a string

Canonical JSON is produced in Python (``json.dumps(sort_keys=True,
separators=(",", ":"), default=str)``) so it is bit-identical to ``table_hash``
and to the hashes the rebuild verifies. SQLite's own ``json_object`` is not used
because it prints REAL values with 15 significant digits and would not round-trip.

A write is journaled when a run is known: either a RunContext is active on the
connection, or the row itself carries a ``run_id`` that exists in ``runs``.
Writes made outside any run (ad-hoc SQL, test fixtures) are not journaled and a
later ``db verify`` reports that database as not reproducible, which is the
correct outcome.

Transactions: connections are opened with ``isolation_level=None`` so the Python
driver never opens implicit transactions. RunContext owns the staging
transaction via SAVEPOINT; library code must never call ``commit()``.
"""
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

# Tables whose control columns may change through update_control (INTERFACES C00).
CONTROL_ALLOWLIST = {
    "runs",
    "proposals",
    "decisions",
    "portfolio_orders",
    "portfolios",
    "factor_registry",
    "securities",
    "symbol_history",
    "sector_map",
    "models",
    "model_versions",
    "data_quality_events",
    "hypotheses",
}

JOURNAL_TABLE = "ledger_events"
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_CANON = dict(sort_keys=True, default=str, separators=(",", ":"))


def canonical_json(obj: Any) -> str:
    """Canonical JSON used for ledger payloads, hashes and table digests."""
    return json.dumps(obj, **_CANON)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


class QuantConnection(sqlite3.Connection):
    """sqlite3 connection carrying the active run context for journal triggers."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.quant_run_id: int | None = None
        self.quant_now_fn: Any = None
        self.create_function("quant_run_id", 0, self._fn_run_id)
        self.create_function("quant_now", 0, self._fn_now)
        self.create_function("quant_row_json", -1, _fn_row_json)
        self.create_function("quant_sha256", 1, _fn_sha256)

    def _fn_run_id(self) -> int | None:
        return self.quant_run_id

    def _fn_now(self) -> str:
        if self.quant_now_fn is not None:
            return str(self.quant_now_fn())
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def set_run_context(self, run_id: int | None, now_fn: Any = None) -> None:
        self.quant_run_id = run_id
        self.quant_now_fn = now_fn


def _fn_row_json(*pairs: Any) -> str:
    if len(pairs) % 2 != 0:
        raise ValueError("quant_row_json expects name/value pairs")
    row = {pairs[i]: pairs[i + 1] for i in range(0, len(pairs), 2)}
    return canonical_json(row)


def _fn_sha256(text: Any) -> str:
    return sha256_text("" if text is None else str(text))


def connect(path: str | Path, *, readonly: bool = False) -> QuantConnection:
    path_str = str(path)
    if path_str == ":memory:":
        conn = sqlite3.connect(":memory:", isolation_level=None, factory=QuantConnection)
    elif readonly:
        abs_path = Path(path).resolve()
        conn = sqlite3.connect(
            f"file:{abs_path}?mode=ro", uri=True, isolation_level=None, factory=QuantConnection
        )
    else:
        abs_path = Path(path).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(abs_path), isolation_level=None, factory=QuantConnection)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    if not readonly and path_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def schema_path(kind: Literal["state", "prices"] = "state") -> Path:
    pkg_dir = Path(__file__).resolve().parent
    if kind == "state":
        return pkg_dir / "schema.sql"
    if kind == "prices":
        return pkg_dir / "price_schema.sql"
    raise ValueError(f"Unknown schema kind: {kind}")


def schema_sha256(kind: Literal["state", "prices"] = "state") -> str:
    return hashlib.sha256(schema_path(kind).read_bytes()).hexdigest()


def _check_schema_parity(kind: str) -> None:
    """The packaged DDL must be byte-identical to the contract copy when both exist."""
    contract_name = "schema.sql" if kind == "state" else "price_schema.sql"
    contract = Path(__file__).resolve().parents[2] / "docs" / "spec" / "contracts" / contract_name
    if contract.exists() and contract.read_bytes() != schema_path(kind).read_bytes():
        raise Refused(
            "schema_contract_mismatch",
            f"quant/db/{contract_name} differs from docs/spec/contracts/{contract_name}",
        )


def apply_schema(
    conn: sqlite3.Connection,
    *,
    kind: Literal["state", "prices"] = "state",
    journal: bool = True,
) -> None:
    """Apply the canonical DDL and (for state DBs) install journal triggers."""
    _check_schema_parity(kind)
    conn.executescript(schema_path(kind).read_text(encoding="utf-8"))
    if kind == "state" and journal:
        install_journal_triggers(conn)


def user_tables(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall() if not r[0].startswith("sqlite_")]


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def _table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({_validate_identifier(table)})").fetchall()


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [r["name"] for r in _table_info(conn, table_name)]


def key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = _table_info(conn, table)
    pks = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"] > 0]
    return pks if pks else [r["name"] for r in info]


def _row_json_expr(cols: list[str], prefix: str) -> str:
    return "quant_row_json(" + ", ".join(f"'{c}', {prefix}.{c}" for c in cols) + ")"


def journal_trigger_sql(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return DROP/CREATE statements for the three journal triggers of one table."""
    cols = _get_table_columns(conn, table)
    keys = key_columns(conn, table)
    has_run = "run_id" in cols
    stmts: list[str] = []
    for op, ref in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
        name = f"quant_journal_{table}_{op}"
        run_expr = f"COALESCE(quant_run_id(), {ref}.run_id)" if has_run else "quant_run_id()"
        when = f"WHEN {run_expr} IN (SELECT run_id FROM runs)"
        key_json = _row_json_expr(keys, ref)
        if op == "insert":
            before, after = "''", _row_json_expr(cols, "NEW")
        elif op == "update":
            before, after = f"quant_sha256({_row_json_expr(cols, 'OLD')})", _row_json_expr(cols, "NEW")
        else:
            before, after = f"quant_sha256({_row_json_expr(cols, 'OLD')})", "'{}'"
        stmts.append(f"DROP TRIGGER IF EXISTS {name};")
        stmts.append(
            f"CREATE TRIGGER {name} AFTER {op.upper()} ON {table} {when} BEGIN "
            f"INSERT INTO {JOURNAL_TABLE} (run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
            f"VALUES ({run_expr}, quant_now(), '{table}', '{op}', {key_json}, {before}, {after}); END;"
        )
    return stmts


def install_journal_triggers(conn: sqlite3.Connection) -> int:
    """(Re)create journal triggers for every state table; returns trigger count."""
    if not _has_table(conn, JOURNAL_TABLE):
        return 0
    count = 0
    for table in user_tables(conn):
        if table == JOURNAL_TABLE:
            continue
        for stmt in journal_trigger_sql(conn, table):
            conn.execute(stmt)
            if stmt.startswith("CREATE"):
                count += 1
    return count


def drop_journal_triggers(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'quant_journal_%'"
    )
    for (name,) in cur.fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


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


def _py(value: Any) -> Any:
    """Coerce numpy/pandas scalars to plain Python for sqlite binding."""
    if value is None:
        return None
    if isinstance(value, (bool,)):
        return int(value)
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except Exception:
            return value
    return value


def append_rows(ctx: Any, table: str, rows: pd.DataFrame, keys: list[str]) -> int:
    """Append rows to an append-only table.

    Byte-equivalent existing keys are a no-op; a differing existing key raises
    ImmutableConflict. Journaling is performed by the database triggers.
    """
    _validate_identifier(table)
    for k in keys:
        _validate_identifier(k)

    conn: sqlite3.Connection = ctx.conn
    table_cols = _get_table_columns(conn, table)
    cols_to_use = [c for c in rows.columns if c in table_cols]
    for k in keys:
        if k not in cols_to_use:
            raise ValueError(f"Key column '{k}' not present in row data for table '{table}'")

    inserted_count = 0
    conn.execute("SAVEPOINT quant_append_rows")
    try:
        for _, row in rows.iterrows():
            row_dict = {c: (None if pd.isna(row[c]) else _py(row[c])) for c in cols_to_use}
            key_dict = {k: row_dict[k] for k in keys}

            where_clauses = []
            where_vals = []
            for k, v in key_dict.items():
                if v is None:
                    where_clauses.append(f"{k} IS NULL")
                else:
                    where_clauses.append(f"{k} = ?")
                    where_vals.append(v)

            existing = conn.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}", tuple(where_vals)
            ).fetchall()

            if existing:
                matched = any(
                    all(_values_equal(ex[c], row_dict[c]) for c in cols_to_use) for ex in existing
                )
                if matched:
                    continue
                raise ImmutableConflict(table=table, key=key_dict)

            placeholders = ", ".join(["?"] * len(cols_to_use))
            col_names = ", ".join(cols_to_use)
            conn.execute(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                [row_dict[c] for c in cols_to_use],
            )
            inserted_count += 1
    except Exception:
        conn.execute("ROLLBACK TO quant_append_rows")
        conn.execute("RELEASE quant_append_rows")
        raise
    conn.execute("RELEASE quant_append_rows")
    return inserted_count


def update_control(ctx: Any, table: str, key: dict[str, Any], changes: dict[str, Any]) -> int:
    """Change mutable control columns on an allowlisted table (journaled by trigger)."""
    _validate_identifier(table)
    if table not in CONTROL_ALLOWLIST:
        raise Refused(
            "control_update_not_allowed",
            f"Table '{table}' is not in the control update allowlist",
        )

    conn: sqlite3.Connection = ctx.conn
    where_clauses = []
    where_vals = []
    for k, v in key.items():
        _validate_identifier(k)
        if v is None:
            where_clauses.append(f"{k} IS NULL")
        else:
            where_clauses.append(f"{k} = ?")
            where_vals.append(_py(v))

    row = conn.execute(
        f"SELECT * FROM {table} WHERE {' AND '.join(where_clauses)}", tuple(where_vals)
    ).fetchone()
    if not row:
        return 0

    set_clauses = []
    set_vals = []
    for col, val in changes.items():
        _validate_identifier(col)
        set_clauses.append(f"{col} = ?")
        set_vals.append(_py(val))

    conn.execute(
        f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {' AND '.join(where_clauses)}",
        tuple(set_vals + where_vals),
    )
    return 1


def row_hash(row: sqlite3.Row | dict[str, Any]) -> str:
    return sha256_text(canonical_json(dict(row)))


def table_hash(conn: sqlite3.Connection, table: str) -> str:
    _validate_identifier(table)
    cols = _table_info(conn, table)
    pk_cols = [r["name"] for r in sorted(cols, key=lambda x: x["pk"]) if r["pk"] > 0]
    order_by = ", ".join(pk_cols) if pk_cols else ", ".join([r["name"] for r in cols])
    rows_cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    rows_data = [dict(r) for r in rows_cur.fetchall()]
    return sha256_text(canonical_json(rows_data))


def code_sha256(root: Path | None = None) -> str:
    """Deterministic hash of the quant package source (paths + bytes)."""
    pkg = Path(__file__).resolve().parents[1] if root is None else Path(root)
    h = hashlib.sha256()
    for p in sorted(pkg.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".sql") and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(pkg)).encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()
