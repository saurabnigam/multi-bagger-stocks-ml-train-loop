"""Factor management and registry CLI commands."""
from __future__ import annotations

from quant.cli import register
from quant.config import load as load_config
from quant.factors.registry import launch_specs, sync
from quant.run import RunContext
from quant.types import Actor, SystemClock


def factors_sync_cmd(args) -> int:
    """Sync all canonical factor specifications into factor_registry."""
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    specs = launch_specs()

    with RunContext(as_of=as_of, kind="maintenance", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = sync(ctx, specs)
        print(f"Factors synced: {res.details}")
    return 0


register("factors", "sync", factors_sync_cmd, "Sync launch factor specifications into registry")
