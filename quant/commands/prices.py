"""Price data and corporate action management CLI commands."""
from __future__ import annotations

from quant.cli import register
from quant.config import load as load_config
from quant.data.actions import add as action_add, clear as action_clear
from quant.data.prices import PriceStore
from quant.run import RunContext
from quant.types import Actor, SystemClock


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


register("prices", "action-add", prices_action_add_cmd, "Add an authorized corporate action")
register("prices", "action-clear", prices_action_clear_cmd, "Clear a flagged corporate action")
