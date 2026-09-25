"""Data acquisition CLI commands."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from quant.cli import register
from quant.config import load as load_config
from quant.data.capture import run as capture_run
from quant.data.yahoo import YahooClient
from quant.run import RunContext
from quant.types import Actor, SystemClock


def data_capture_cmd(args) -> int:
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep)

    with RunContext(as_of=as_of, kind="capture", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = capture_run(ctx, client)
        print(f"Data capture completed: {res.counts.get('bundles', 0)} bundles")
    return 0


def data_ingest_archive_cmd(args) -> int:
    """Replay a retained bundle archive; prices are updated unless --skip-capture is given."""
    from quant.data.capture import ingest_archive

    path = getattr(args, "target", None)
    if not path:
        print("Error: data ingest-archive requires the archive path as its target argument")
        return 1
    cfg = load_config(getattr(args, "config", None) or None)
    if getattr(args, "db_path", None):
        cfg = cfg.with_paths(db=Path(args.db_path))
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system") or "system", name=(getattr(args, "by", None) or "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]
    skip_prices = bool(getattr(args, "skip_capture", False))
    client = None if skip_prices else YahooClient(cfg=cfg, clock=clock, sleep=time.sleep)

    with RunContext(as_of=as_of, kind="capture", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = ingest_archive(ctx, path, client, prices=not skip_prices)
        print(f"Archive ingest {res.status}: {res.counts}")
        if res.details.get("unmapped_tickers"):
            print(f"  unmapped tickers (first 20): {res.details['unmapped_tickers']}")
    return 0


def _actions_cfg(args):
    cfg = load_config(getattr(args, "config", None) or None)
    if getattr(args, "db_path", None):
        cfg = cfg.with_paths(db=Path(args.db_path))
    return cfg


def data_actions_list_cmd(args) -> int:
    """List suspected corporate actions and their resolution status (read-only; D3/T2)."""
    from quant.data import actions
    from quant.data.prices import PriceStore
    from quant.db.core import connect

    cfg = _actions_cfg(args)
    conn = connect(cfg.paths.db, readonly=True)
    try:
        store = PriceStore(cfg.paths.prices_db, state_conn=conn)
        rows = actions.list_suspects(conn, price_store=store, include_resolved=bool(getattr(args, "all", False)))
    finally:
        conn.close()

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if not rows:
        print("No suspected corporate actions." if getattr(args, "all", False)
              else "No unresolved suspected corporate actions.")
        return 0

    print(f"{'SYMBOL':<12}{'ISIN':<14}{'EX_DATE':<12}{'GROSS':>8}  {'IN_13M':<7}EVIDENCE_AT / RESOLUTION")
    for r in rows:
        gross = f"{r['gross_factor']:.3f}" if r["gross_factor"] is not None else "?"
        in_13m = {True: "yes", False: "no", None: "?"}[r["in_trailing_13m"]]
        tail = r["evidence_observed_at"] or ""
        if r["resolved"]:
            res = r["resolved"]
            tail += f"  -> resolved {res['kind']}={res['value']} ({res['decision_id']})"
        print(f"{(r['symbol'] or '?'):<12}{(r['isin'] or '?'):<14}{r['ex_date']:<12}{gross:>8}  {in_13m:<7}{tail}")
    return 0


def data_actions_resolve_cmd(args) -> int:
    """Resolve a suspected corporate action under a human-approved Tier-1 decision (D3/T2)."""
    from quant.data import actions

    cfg = _actions_cfg(args)
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system") or "system",
                  name=(getattr(args, "by", None) or "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    symbol = getattr(args, "symbol", None)
    isin = getattr(args, "isin", None)
    ex_date = getattr(args, "ex_date", None)
    kind = getattr(args, "kind", None)
    evidence = getattr(args, "evidence", None)

    if not symbol and not isin:
        sys.stderr.write("Error: data actions-resolve requires --symbol or --isin\n")
        return 1
    if not ex_date:
        sys.stderr.write("Error: data actions-resolve requires --ex-date YYYY-MM-DD\n")
        return 1
    if not evidence:
        sys.stderr.write("Error: data actions-resolve requires --evidence TEXT\n")
        return 1
    if kind not in actions.RESOLVE_KINDS:
        sys.stderr.write(f"Error: --kind must be one of {actions.RESOLVE_KINDS}\n")
        return 1

    factor_raw = getattr(args, "factor", None)
    ratio_raw = getattr(args, "ratio", None)
    if kind == "genuine":
        factor = 1.0
    elif kind in actions.RESOLVE_SPLIT_KINDS:
        if ratio_raw is None:
            sys.stderr.write(f"Error: --ratio is required for kind '{kind}'\n")
            return 1
        factor = float(ratio_raw)
    else:
        if factor_raw is None:
            sys.stderr.write(f"Error: --factor is required for kind '{kind}'\n")
            return 1
        factor = float(factor_raw)

    with RunContext(as_of=as_of, kind="data_fix", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = actions.resolve(
            ctx, isin=isin, symbol=symbol, ex_date=ex_date, kind=kind, factor=factor,
            evidence=evidence, title=getattr(args, "title", None) or None,
            force=bool(getattr(args, "force", False)),
        )
        d = res.details
        print(f"Resolved {d['isin']} {d['ex_date']} as {d['kind']}={d['factor']} under decision {d['decision_id']}")
        print(f"  ADR: {d['adr_path']}")
        if d.get("warning"):
            print(f"  WARNING: {d['warning']}")
    return 0


register("data", "capture", data_capture_cmd, "Capture fresh Yahoo fundamental and price data")
register("data", "ingest-archive", data_ingest_archive_cmd, "Replay a retained bundle archive (target = path); --skip-capture skips prices")
register("data", "actions-list", data_actions_list_cmd,
         "List unresolved suspected corporate actions (--all for resolved too, --json for machine output)")
register("data", "actions-resolve", data_actions_resolve_cmd,
         "Resolve a suspected corporate action under a human-approved Tier-1 decision")
