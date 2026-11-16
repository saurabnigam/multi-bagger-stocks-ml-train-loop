"""CLI command handlers for evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.evaluation.evaluate import run as evaluate_run
from quant.run import RunContext


def cmd_evaluate_run(args: argparse.Namespace) -> int:
    """Run evaluations for matured cohorts up to through date."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.db_path
    through = args.through if hasattr(args, "through") and args.through else getattr(args, "as_of", None)
    if not through:
        sys.stderr.write("Error: --through or --as-of required\n")
        return 1

    track = args.track if hasattr(args, "track") and args.track else "live"

    with connect(db_path) as conn:
        ctx = RunContext(
            as_of=through,
            kind="evaluate",
            track=track,
            cfg=cfg,
            clock=None,
            actor=None,
        )
        ctx.conn = conn
        res = evaluate_run(ctx, through=through, track=track)
        print(f"Evaluations complete: {res.counts.get('inserted', 0)} inserted")
        return 0


register("evaluate", "run", cmd_evaluate_run, "Run evaluations up to through date")
