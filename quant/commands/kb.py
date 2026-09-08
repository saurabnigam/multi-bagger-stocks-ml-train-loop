"""CLI command handlers for knowledge base, proposals, and governance."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.knowledge.adr import check as check_adr
from quant.knowledge.proposals import apply as apply_proposals, approve, draft, ratify, reject
from quant.run import RunContext
from quant.types import Actor, SystemClock


def _make_ctx(args: argparse.Namespace, kind: str) -> RunContext:
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    as_of = getattr(args, "as_of", None) or "2026-09-01"
    by_val = getattr(args, "by", "system:cli")
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=by_val,
    )
    conn = connect(db_path)
    ctx = RunContext(
        as_of=as_of,
        kind=kind,
        track="live",
        cfg=cfg,
        clock=SystemClock(),
        actor=actor,
    )
    ctx.conn = conn
    return ctx


def cmd_kb_draft(args: argparse.Namespace) -> int:
    """Draft proposals for eligible reviews."""
    ctx = _make_ctx(args, kind="proposal")
    try:
        drafted = draft(ctx, as_of=ctx.as_of)
        print(f"Drafted {len(drafted)} proposals: {drafted}")
        return 0
    finally:
        if ctx.conn:
            ctx.conn.close()


def cmd_kb_approve(args: argparse.Namespace) -> int:
    """Approve a proposal."""
    pid = getattr(args, "target", None) or getattr(args, "proposal_id", None)
    if not pid:
        sys.stderr.write("Error: proposal ID required\n")
        return 1
    ctx = _make_ctx(args, kind="proposal")
    try:
        did = approve(ctx, proposal_id=pid, note=getattr(args, "note", "") or "")
        print(f"Approved {pid} -> decision {did}")
        return 0
    finally:
        if ctx.conn:
            ctx.conn.close()


def cmd_kb_reject(args: argparse.Namespace) -> int:
    """Reject a proposal."""
    pid = getattr(args, "target", None) or getattr(args, "proposal_id", None)
    if not pid:
        sys.stderr.write("Error: proposal ID required\n")
        return 1
    ctx = _make_ctx(args, kind="proposal")
    try:
        did = reject(ctx, proposal_id=pid, note=getattr(args, "note", "") or "")
        print(f"Rejected {pid} -> decision {did}")
        return 0
    finally:
        if ctx.conn:
            ctx.conn.close()


def cmd_kb_ratify(args: argparse.Namespace) -> int:
    """Ratify a provisional decision with human authority."""
    did = getattr(args, "target", None) or getattr(args, "decision_id", None)
    if not did:
        sys.stderr.write("Error: decision ID required\n")
        return 1
    ctx = _make_ctx(args, kind="ratify")
    try:
        res = ratify(ctx, decision_id=did, note=getattr(args, "note", "") or "")
        print(f"Ratified decision {did}: {res.status}")
        return 0
    finally:
        if ctx.conn:
            ctx.conn.close()


def cmd_kb_apply(args: argparse.Namespace) -> int:
    """Apply approved decisions and expire provisional decisions."""
    ctx = _make_ctx(args, kind="apply")
    try:
        res = apply_proposals(ctx, as_of=ctx.as_of)
        print(f"Applied decisions: {res.counts.get('applied', 0)} applied")
        return 0
    finally:
        if ctx.conn:
            ctx.conn.close()


def cmd_kb_check(args: argparse.Namespace) -> int:
    """Check ADR consistency across decisions."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    knowledge_dir = Path(getattr(args, "knowledge_dir", None) or cfg.paths.knowledge_dir)
    with connect(db_path, readonly=True) as conn:
        report = check_adr(conn, knowledge_dir)
        for c in report.checks:
            print(f"[{c.status}] {c.id}: {c.reason} (observed={c.observed}, expected={c.expected})")
        return 0 if report.passed else 1


def cmd_kb_report(args: argparse.Namespace) -> int:
    """Render monthly evidence report for an as-of date."""
    from quant.knowledge.report import render, render_backfill

    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    as_of = getattr(args, "as_of", None) or "2026-09-01"
    track = getattr(args, "track", "live") or "live"
    with connect(db_path, readonly=True) as conn:
        if track == "backfill":
            p = render_backfill(conn, as_of, cfg)
        else:
            p = render(conn, as_of, cfg)
        print(f"Report generated: {p}")
        return 0


register("kb", "draft", cmd_kb_draft, "Draft proposals from active criteria reviews")
register("kb", "approve", cmd_kb_approve, "Approve a proposal")
register("kb", "reject", cmd_kb_reject, "Reject a proposal")
register("kb", "ratify", cmd_kb_ratify, "Ratify a provisional decision")
register("kb", "apply", cmd_kb_apply, "Apply approved decisions prospectively")
register("kb", "check", cmd_kb_check, "Verify ADR consistency")
register("kb", "report", cmd_kb_report, "Render monthly empirical report")
