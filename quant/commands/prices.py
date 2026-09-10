"""Price data and corporate action management CLI commands."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from quant.cli import register
from quant.config import load as load_config
from quant.data.actions import add as action_add, clear as action_clear
from quant.data.identity import tracked_securities, yahoo_ticker
from quant.data.prices import PriceStore
from quant.data.yahoo import YahooClient
from quant.db.core import connect as connect_state
from quant.run import RunContext
from quant.types import Actor, SystemClock


def _cfg(args: argparse.Namespace):
    cfg = load_config(getattr(args, "config", None) or None)
    db_path = getattr(args, "db_path", None)
    if db_path:
        cfg = cfg.with_paths(db=Path(db_path))
    return cfg


def _actor(args: argparse.Namespace) -> Actor:
    return Actor(kind=getattr(args, "actor_kind", "human") or "human", name=(getattr(args, "by", None) or "cli").split(":")[-1])


def prices_action_add_cmd(args) -> int:
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "human"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    with RunContext(as_of=as_of, kind="action", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = action_add(
            ctx,
            isin=args.isin,
            ex_date=args.ex_date,
            kind=args.kind,
            factor=float(args.factor),
            decision_id=args.decision_id,
        )
        print(f"Action added: {res.details}")
    return 0


def prices_action_clear_cmd(args) -> int:
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "human"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    with RunContext(as_of=as_of, kind="action", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = action_clear(ctx, event_id=int(args.event_id), decision_id=args.decision_id)
        print(f"Action cleared: {res.details}")
    return 0


def _resolve_ticker_map(conn, security_ids: list[int], cutoff: str) -> dict[int, str]:
    ticker_map: dict[int, str] = {}
    for sid in security_ids:
        try:
            ticker_map[int(sid)] = yahoo_ticker(conn, int(sid), cutoff)
        except ValueError:
            continue
    return ticker_map


def prices_backfill_cmd(args: argparse.Namespace) -> int:
    """`prices backfill --start DATE --end DATE`: full history over the tracked universe."""
    cfg = _cfg(args)
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if not start or not end:
        sys.stderr.write("Error: prices backfill requires --start DATE --end DATE\n")
        return 1

    clock = SystemClock()
    actor = _actor(args)
    with RunContext(as_of=end, kind="prices_backfill", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        horizons = list(getattr(getattr(cfg, "horizons", None), "tracked_m", [1, 3, 6, 12, 24, 36]))
        sids = tracked_securities(ctx.conn, cutoff=end, horizons=horizons)
        ticker_map = _resolve_ticker_map(ctx.conn, sids, end)
        client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep)
        res = ctx.store.backfill(ctx, client, ticker_map, start=start, end=end)
        print(f"Prices backfill [{start}..{end}]: status={res.status} counts={res.counts}")
        ctx.checkpoint()
    return 0


def prices_update_cmd(args: argparse.Namespace) -> int:
    """`prices update --through DATE`: bring the tracked universe up to date."""
    cfg = _cfg(args)
    through = getattr(args, "through", None)
    if not through:
        sys.stderr.write("Error: prices update requires --through DATE\n")
        return 1

    clock = SystemClock()
    actor = _actor(args)
    with RunContext(as_of=through, kind="prices_update", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        horizons = list(getattr(getattr(cfg, "horizons", None), "tracked_m", [1, 3, 6, 12, 24, 36]))
        sids = tracked_securities(ctx.conn, cutoff=through, horizons=horizons)
        client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep)
        res = ctx.store.update(ctx, client, sids, through=through)
        print(f"Prices update [through {through}]: status={res.status} counts={res.counts}")
        ctx.checkpoint()
    return 0


def prices_manifest_cmd(args: argparse.Namespace) -> int:
    """`prices manifest --verify`: check the store reproduces data/manifests/prices_<as-of>.json.

    Read-only check -- MASTER_SPEC 10.3: "No fake run rows for read-only status/help/verify".
    This must not open a RunContext or call ctx.checkpoint(): doing so would insert a real
    row into `runs` (and, via the journaling triggers, `ledger_events`) for a command that
    mutates nothing, and would let a failed verify surface as a blocked pipeline run in
    quant.status.read(). Follows the same pattern as `db verify` (quant.commands.db):
    open everything read-only and never touch RunContext.
    """
    if not getattr(args, "verify", False):
        sys.stderr.write("Error: prices manifest requires --verify\n")
        return 1

    cfg = _cfg(args)
    clock = SystemClock()
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]
    manifest_path = Path(cfg.paths.data_dir) / "manifests" / f"prices_{as_of}.json"
    if not manifest_path.exists():
        sys.stderr.write(f"Error: no price manifest at {manifest_path}\n")
        return 1

    state_conn = connect_state(cfg.paths.db, readonly=True)
    try:
        store = PriceStore(cfg.paths.prices_db, state_conn=state_conn)
        chk = store.manifest_verify(manifest_path)
    finally:
        state_conn.close()
    print(f"Prices manifest verify [{manifest_path}]: status={chk.status} reason={chk.reason}")
    return 0 if chk.status == "PASS" else 1


register("prices", "action-add", prices_action_add_cmd, "Add an authorized corporate action")
register("prices", "action-clear", prices_action_clear_cmd, "Clear a flagged corporate action")
register("prices", "backfill", prices_backfill_cmd, "Backfill full price history for the tracked universe")
register("prices", "update", prices_update_cmd, "Update prices for the tracked universe through a date")
register("prices", "manifest", prices_manifest_cmd, "Verify the price store reproduces its manifest")
