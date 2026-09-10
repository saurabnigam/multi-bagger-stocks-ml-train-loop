"""CLI command handler for monthly execution pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant.cli import register
from quant.config import load
from quant.evaluation.backfill import replay
from quant.run import RunContext, monthly
from quant.types import Actor, SystemClock


def cmd_run_monthly(args: argparse.Namespace) -> int:
    """Execute monthly run pipeline."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    if hasattr(args, "db_path") and args.db_path:
        cfg = cfg.with_paths(db=Path(args.db_path))

    clock = SystemClock()
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=getattr(args, "by", "system:cli"),
    )

    as_of = getattr(args, "as_of", None)
    skip_capture = getattr(args, "skip_capture", False)
    stop_after = getattr(args, "stop_after", None)
    dry_run = getattr(args, "dry_run", False)
    commit = getattr(args, "commit", False)
    push = getattr(args, "push", False)

    return monthly(
        cfg,
        clock,
        actor,
        as_of=as_of,
        skip_capture=skip_capture,
        stop_after=stop_after,
        dry_run=dry_run,
        commit=commit,
        push=push,
    )


def cmd_run_backfill_track(args: argparse.Namespace) -> int:
    """Execute `run backfill-track --start DATE --end DATE`: replay the backfill track."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    if hasattr(args, "db_path") and args.db_path:
        cfg = cfg.with_paths(db=Path(args.db_path))

    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if not start or not end:
        sys.stderr.write("Error: run backfill-track requires --start DATE --end DATE\n")
        return 1

    clock = SystemClock()
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=getattr(args, "by", "system:cli"),
    )

    with RunContext(as_of=end, kind="backfill_track", track="backfill", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = replay(ctx, start=start, end=end)
        print(f"Backfill track replay [{start}..{end}]: status={res.status} counts={res.counts}")
        ctx.checkpoint()
    return 0


register("run", "monthly", cmd_run_monthly, "Execute monthly run pipeline")
register("run", "backfill-track", cmd_run_backfill_track, "Replay the backfill track over a date range")
