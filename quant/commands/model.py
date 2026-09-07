"""CLI command handlers for models."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.model.models import check as run_check


def cmd_model_check(args: argparse.Namespace) -> int:
    """Validate model invariants across stored model versions and weights."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.db_path
    with connect(db_path) as conn:
        report = run_check(conn)
        for c in report.checks:
            print(f"[{c.status}] {c.id}: {c.reason} (observed={c.observed}, expected={c.expected})")
        return 0 if report.passed else 1


def cmd_model_list(args: argparse.Namespace) -> int:
    """List registered models."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.db_path
    with connect(db_path) as conn:
        rows = conn.execute("SELECT model_id, kind, role, description FROM models ORDER BY model_id").fetchall()
        for r in rows:
            print(f"{r[0]:<15} {r[1]:<10} {r[2]:<12} {r[3]}")
    return 0


register("model", "check", cmd_model_check, "Check model invariants")
register("model", "list", cmd_model_list, "List registered models")
