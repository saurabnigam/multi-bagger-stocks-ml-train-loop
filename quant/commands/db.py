"""`quant db` commands: init | export | rebuild | verify | size (MASTER_SPEC 10.4)."""
from __future__ import annotations

import argparse
from pathlib import Path

from quant.cli import register
from quant.config import load as load_config
from quant.db import ledger
from quant.db.core import apply_schema, connect, install_journal_triggers


def _cfg(args: argparse.Namespace):
    cfg = load_config(getattr(args, "config", None) or None)
    db_path = getattr(args, "db_path", None)
    if db_path:
        cfg = cfg.with_paths(db=Path(db_path))
    return cfg


def _ledger_dir(cfg) -> Path:
    return Path(cfg.paths.data_dir) / "ledger"


def cmd_db_init(args: argparse.Namespace) -> int:
    """Create the state schema (idempotent) and install journal triggers."""
    cfg = _cfg(args)
    conn = connect(cfg.paths.db)
    try:
        apply_schema(conn, kind="state")
        n = install_journal_triggers(conn)
        print(f"Schema applied at {cfg.paths.db}; {n} journal triggers installed.")
    finally:
        conn.close()
    return 0


def cmd_db_export(args: argparse.Namespace) -> int:
    """Export ledger_events to data/ledger/YYYY-MM/events.jsonl."""
    cfg = _cfg(args)
    conn = connect(cfg.paths.db, readonly=True)
    try:
        paths = ledger.export(conn, _ledger_dir(cfg))
    finally:
        conn.close()
    print(f"Exported {len(paths)} ledger partition(s) to {_ledger_dir(cfg)}")
    return 0


def cmd_db_rebuild(args: argparse.Namespace) -> int:
    """Rebuild a database from the exported ledger into --output PATH (or `target`)."""
    cfg = _cfg(args)
    output = getattr(args, "output", None) or getattr(args, "target", None)
    if not output:
        print("db rebuild requires --output PATH")
        return 1
    ledger.rebuild(_ledger_dir(cfg), Path(output))
    print(f"Rebuilt {output} from {_ledger_dir(cfg)}")
    return 0


def cmd_db_verify(args: argparse.Namespace) -> int:
    """Export the ledger, rebuild a temporary database and compare every state table."""
    cfg = _cfg(args)
    conn = connect(cfg.paths.db, readonly=True)
    try:
        ledger.export(conn, _ledger_dir(cfg))
        report = ledger.verify(conn, _ledger_dir(cfg))
    finally:
        conn.close()
    failed = [c for c in report.checks if c.status == "FAIL"]
    for c in report.checks:
        if c.status == "FAIL":
            print(f"FAIL {c.id}: expected {c.expected} got {c.observed}")
    print(f"db verify: {len(report.checks) - len(failed)} tables match, {len(failed)} differ")
    return 0 if report.passed else 1


def cmd_db_size(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    sizes = ledger.size(cfg)
    warn = int(getattr(getattr(cfg, "budgets", None), "state_warn_bytes", 50_000_000))
    flag = " (above state_warn_bytes)" if sizes["db_bytes"] > warn else ""
    print(f"state db {sizes['db_bytes']} bytes{flag}; ledger {sizes['ledger_bytes']} bytes")
    return 0


register("db", "init", cmd_db_init, "Create the state schema and journal triggers")
register("db", "export", cmd_db_export, "Export ledger_events to JSONL partitions")
register("db", "rebuild", cmd_db_rebuild, "Rebuild a database from the ledger (--output PATH)")
register("db", "verify", cmd_db_verify, "Verify the state database against a ledger rebuild")
register("db", "size", cmd_db_size, "Report state database and ledger sizes")
