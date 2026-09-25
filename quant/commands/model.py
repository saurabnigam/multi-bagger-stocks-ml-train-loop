"""CLI command handlers for models."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.model.models import check as run_check
from quant.model.models import register_challenger
from quant.run import RunContext
from quant.types import Actor, SystemClock


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


def cmd_model_register_challenger(args: argparse.Namespace) -> int:
    """Register a known challenger model definition under an approved decision.

    Usage: quant model register-challenger <model_id> --decision-id <decision_id>
    (model_id is the positional 'target' argument shared by every command group).
    """
    model_id = getattr(args, "target", None)
    decision_id = getattr(args, "decision_id", None)
    if not model_id or not decision_id:
        print("Error: model register-challenger requires <model_id> and --decision-id <decision_id>")
        return 1

    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    if hasattr(args, "db_path") and args.db_path:
        cfg = cfg.with_paths(db=Path(args.db_path))
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system") or "system", name=(getattr(args, "by", None) or "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    with RunContext(as_of=as_of, kind="model_register", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = register_challenger(ctx, model_id, decision_id)
        print(f"Registered challenger {model_id} under decision {decision_id}: {res.counts}")
    return 0


register("model", "check", cmd_model_check, "Check model invariants")
register("model", "list", cmd_model_list, "List registered models")
register("model", "register-challenger", cmd_model_register_challenger,
          "Register a challenger model (target=model_id) under --decision-id <decision_id>")
