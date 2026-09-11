"""CLI command handlers for the knowledge base, proposals and governance (MASTER_SPEC 10.3, C09).

Every mutating command runs inside a RunContext so its writes are journaled and
attributed to a run row; read-only commands open the database read-only.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
from typing import Any

from quant.cli import register
from quant.config import load
from quant.db.core import connect
from quant.knowledge.adr import check as check_adr
from quant.knowledge.proposals import apply as apply_proposals, approve, draft, ratify, reject
from quant.run import RunContext
from quant.types import Actor, SystemClock


def _cfg(args: argparse.Namespace):
    cfg = load(getattr(args, "config", None) or None)
    db_path = getattr(args, "db_path", None)
    if db_path:
        cfg = cfg.with_paths(db=Path(db_path))
    return cfg


def _actor(args: argparse.Namespace) -> Actor:
    return Actor(kind=getattr(args, "actor_kind", "system") or "system", name=getattr(args, "by", "system:cli") or "system:cli")


def _run(args: argparse.Namespace, kind: str, fn) -> int:
    """Execute ``fn(ctx)`` inside a journaled RunContext; exceptions propagate to the CLI mapper."""
    cfg = _cfg(args)
    clock = SystemClock()
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]
    with RunContext(as_of=as_of, kind=kind, track="live", cfg=cfg, clock=clock, actor=_actor(args)) as ctx:
        return fn(ctx)


def spec_fingerprint() -> str:
    """The specification fingerprint printed by docs/spec/check_spec.py (same function, same bytes)."""
    root = Path(__file__).resolve().parents[2]
    script = root / "docs" / "spec" / "check_spec.py"
    spec = importlib.util.spec_from_file_location("quant_check_spec", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # defines fingerprint(); checks run only under __main__
    return str(module.fingerprint())


def cmd_kb_bootstrap(args: argparse.Namespace) -> int:
    """Seed launch factors, models, hypotheses and the Tier-0 bootstrap decision (idempotent)."""
    from quant.knowledge.bootstrap import seed

    def _do(ctx: RunContext) -> int:
        fp = spec_fingerprint()
        res = seed(ctx, spec_sha256=fp)
        n_models = ctx.conn.execute("SELECT count(*) FROM models").fetchone()[0]
        n_factors = ctx.conn.execute("SELECT count(*) FROM factor_registry").fetchone()[0]
        n_hyp = ctx.conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]
        print(f"Bootstrap {res.status}: spec fingerprint {fp}")
        print(f"  hypotheses seeded now: {res.counts.get('hypotheses_seeded', 0)}; "
              f"totals: models={n_models} factors={n_factors} hypotheses={n_hyp}")
        return 0

    return _run(args, "bootstrap", _do)


def cmd_kb_draft(args: argparse.Namespace) -> int:
    """Draft deduplicated proposals from criteria reviews."""
    def _do(ctx: RunContext) -> int:
        drafted = draft(ctx, as_of=ctx.as_of)
        print(f"Drafted {len(drafted)} proposals: {drafted}")
        return 0

    return _run(args, "proposal", _do)


def _target(args: argparse.Namespace, *names: str) -> str | None:
    for n in ("target", *names):
        v = getattr(args, n, None)
        if v:
            return v
    return None


def cmd_kb_approve(args: argparse.Namespace) -> int:
    """Approve a proposal (Tier-1 provisional for an LLM actor; Tier-2 human only)."""
    pid = _target(args, "proposal_id")
    if not pid:
        sys.stderr.write("Error: proposal ID required\n")
        return 1

    def _do(ctx: RunContext) -> int:
        did = approve(ctx, proposal_id=pid, note=getattr(args, "note", "") or "")
        print(f"Approved {pid} -> decision {did}")
        return 0

    return _run(args, "proposal", _do)


def cmd_kb_reject(args: argparse.Namespace) -> int:
    """Reject a proposal with an audit record."""
    pid = _target(args, "proposal_id")
    if not pid:
        sys.stderr.write("Error: proposal ID required\n")
        return 1

    def _do(ctx: RunContext) -> int:
        did = reject(ctx, proposal_id=pid, note=getattr(args, "note", "") or "")
        print(f"Rejected {pid} -> decision {did}")
        return 0

    return _run(args, "proposal", _do)


def cmd_kb_ratify(args: argparse.Namespace) -> int:
    """Ratify a provisional decision with human authority."""
    did = _target(args, "decision_id")
    if not did:
        sys.stderr.write("Error: decision ID required\n")
        return 1

    def _do(ctx: RunContext) -> int:
        res = ratify(ctx, decision_id=did, note=getattr(args, "note", "") or "")
        print(f"Ratified decision {did}: {res.status}")
        return 0

    return _run(args, "ratify", _do)


def cmd_kb_apply(args: argparse.Namespace) -> int:
    """Apply approved/provisional decisions prospectively and expire stale provisionals."""
    def _do(ctx: RunContext) -> int:
        res = apply_proposals(ctx, as_of=ctx.as_of)
        print(f"Applied decisions: {res.counts.get('applied', 0)} applied")
        return 0

    return _run(args, "apply", _do)


def cmd_kb_check(args: argparse.Namespace) -> int:
    """Check ADR consistency across decisions (read-only)."""
    cfg = _cfg(args)
    knowledge_dir = Path(getattr(args, "knowledge_dir", None) or cfg.paths.knowledge_dir)
    conn = connect(cfg.paths.db, readonly=True)
    try:
        report = check_adr(conn, knowledge_dir)
    finally:
        conn.close()
    for c in report.checks:
        print(f"[{c.status}] {c.id}: {c.reason} (observed={c.observed}, expected={c.expected})")
    return 0 if report.passed else 1


def cmd_kb_report(args: argparse.Namespace) -> int:
    """Render the monthly evidence report for an as-of date from persisted evidence."""
    from quant.knowledge.report import render, render_backfill

    cfg = _cfg(args)
    as_of = getattr(args, "as_of", None)
    if not as_of:
        sys.stderr.write("Error: kb report requires --as-of DATE\n")
        return 1
    track = getattr(args, "track", "live") or "live"
    conn = connect(cfg.paths.db, readonly=True)
    try:
        p = render_backfill(conn, as_of, cfg) if track == "backfill" else render(conn, as_of, cfg)
    finally:
        conn.close()
    print(f"Report generated: {p}")
    return 0


register("kb", "bootstrap", cmd_kb_bootstrap, "Seed launch factors, models, hypotheses and the Tier-0 decision")
register("kb", "draft", cmd_kb_draft, "Draft proposals from active criteria reviews")
register("kb", "approve", cmd_kb_approve, "Approve a proposal")
register("kb", "reject", cmd_kb_reject, "Reject a proposal")
register("kb", "ratify", cmd_kb_ratify, "Ratify a provisional decision")
register("kb", "apply", cmd_kb_apply, "Apply approved decisions prospectively")
register("kb", "check", cmd_kb_check, "Verify ADR consistency")
register("kb", "report", cmd_kb_report, "Render monthly empirical report")
