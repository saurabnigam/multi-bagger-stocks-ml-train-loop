"""CLI command handlers for paper portfolios."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.portfolio.paper import plan, roll_forward, settle
from quant.run import RunContext
from quant.types import Actor


def cmd_portfolio_plan(args: argparse.Namespace) -> int:
    """Create pending orders for cohort."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    as_of = getattr(args, "as_of", None)
    cohort_id = getattr(args, "cohort_id", None) or (f"live:{as_of}" if as_of else None)
    if not cohort_id and not as_of:
        sys.stderr.write("Error: --cohort-id or --as-of required\n")
        return 1

    by_val = getattr(args, "by", "system:cli")
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=by_val.split(":", 1)[1] if ":" in by_val else by_val,
    )

    with connect(db_path) as conn:
        if not cohort_id and as_of:
            c_row = conn.execute(
                "SELECT cohort_id FROM cohorts WHERE as_of = ? AND track = 'live' ORDER BY created_at DESC LIMIT 1",
                (as_of,),
            ).fetchone()
            if c_row:
                cohort_id = c_row["cohort_id"]
            else:
                cohort_id = f"live:{as_of}"

        ctx = RunContext(
            as_of=as_of or "2026-09-07",
            kind="plan",
            track="live",
            cfg=cfg,
            clock=None,
            actor=actor,
        )
        ctx.conn = conn
        res = plan(ctx, cohort_id=cohort_id)
        print(f"Portfolio plan complete: {res.counts.get('orders_planned', 0)} orders planned")
        return 0


def cmd_portfolio_settle(args: argparse.Namespace) -> int:
    """Settle pending paper orders up to through date."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    through = getattr(args, "through", None) or getattr(args, "as_of", None)
    if not through:
        sys.stderr.write("Error: --through or --as-of required\n")
        return 1

    by_val = getattr(args, "by", "system:cli")
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=by_val.split(":", 1)[1] if ":" in by_val else by_val,
    )

    with connect(db_path) as conn:
        ctx = RunContext(
            as_of=through,
            kind="settle",
            track="live",
            cfg=cfg,
            clock=None,
            actor=actor,
        )
        ctx.conn = conn
        res = settle(ctx, through=through)
        print(f"Portfolio settle complete: {res.counts.get('fills', 0)} fills executed")
        return 0


def cmd_portfolio_roll_forward(args: argparse.Namespace) -> int:
    """Roll forward portfolio NAV and returns up to through date."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    through = getattr(args, "through", None) or getattr(args, "as_of", None)
    if not through:
        sys.stderr.write("Error: --through or --as-of required\n")
        return 1

    by_val = getattr(args, "by", "system:cli")
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=by_val.split(":", 1)[1] if ":" in by_val else by_val,
    )

    with connect(db_path) as conn:
        ctx = RunContext(
            as_of=through,
            kind="roll_forward",
            track="live",
            cfg=cfg,
            clock=None,
            actor=actor,
        )
        ctx.conn = conn
        res = roll_forward(ctx, through=through)
        print(f"Portfolio roll_forward complete: {res.counts.get('returns_updated', 0)} returns updated")
        return 0


def cmd_portfolio_scoreboard(args: argparse.Namespace) -> int:
    """Compute and display portfolio alpha scoreboard."""
    from quant.portfolio.scoreboard import compute

    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    through = getattr(args, "through", None) or getattr(args, "as_of", None)
    if not through:
        sys.stderr.write("Error: --through or --as-of required\n")
        return 1

    with connect(db_path, readonly=True) as conn:
        df = compute(conn, through=through, cfg=cfg)
        if df.empty:
            print("No portfolios found.")
        else:
            print(df.to_string(index=False))
        return 0


register("portfolio", "plan", cmd_portfolio_plan, "Plan pending portfolio orders for cohort")
register("portfolio", "settle", cmd_portfolio_settle, "Settle pending portfolio orders up to through date")
register("portfolio", "roll_forward", cmd_portfolio_roll_forward, "Roll forward portfolio NAV and returns")
register("portfolio", "scoreboard", cmd_portfolio_scoreboard, "Compute scoreboard for portfolios")
