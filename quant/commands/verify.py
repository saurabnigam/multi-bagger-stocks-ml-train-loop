"""`quant verify` commands: pit | report (MASTER_SPEC 10.3, INTERFACES C11)."""
from __future__ import annotations

import argparse
from pathlib import Path

from quant.cli import register
from quant.config import load as load_config
from quant.db.core import connect
from quant.types import CheckReport
from quant.verify import pit as verify_pit, report as verify_report


def _cfg(args: argparse.Namespace):
    cfg = load_config(getattr(args, "config", None) or None)
    db_path = getattr(args, "db_path", None)
    if db_path:
        cfg = cfg.with_paths(db=Path(db_path))
    return cfg


def _print_report(label: str, rep: CheckReport) -> None:
    print(f"{label}: {'PASS' if rep.passed else 'FAIL'}")
    for c in rep.checks:
        print(f"  [{c.status}] {c.id}: {c.reason}")


def cmd_verify_pit(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    months = int(getattr(args, "months", None) or 12)
    conn = connect(cfg.paths.db, readonly=True)
    try:
        rep = verify_pit(conn, months, cfg)
    finally:
        conn.close()
    _print_report(f"verify pit (months={months})", rep)
    return 0 if rep.passed else 1


def cmd_verify_report(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    as_of = getattr(args, "as_of", None)
    if not as_of:
        print("Error: verify report requires --as-of DATE")
        return 1
    conn = connect(cfg.paths.db, readonly=True)
    try:
        rep = verify_report(conn, as_of, cfg)
    finally:
        conn.close()
    _print_report(f"verify report (as-of={as_of})", rep)
    return 0 if rep.passed else 1


register("verify", "pit", cmd_verify_pit, "Verify point-in-time integrity over the last N months")
register("verify", "report", cmd_verify_report, "Verify a persisted report reproduces its pinned evidence")
