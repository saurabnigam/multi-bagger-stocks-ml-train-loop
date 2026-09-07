from __future__ import annotations

from quant.cli import register
from quant.config import load as load_config
from quant.data.universe import capture
from quant.run import RunContext
from quant.types import Actor, SystemClock


def universe_capture_cmd(args) -> int:
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    with RunContext(as_of=as_of, kind="capture", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = capture(ctx)
        print(f"Captured universe: {res.counts.get('members', 0)} members")
    return 0


register("universe", "capture", universe_capture_cmd, "Capture fresh universe constituent list")
