"""Data acquisition CLI commands."""
from __future__ import annotations

import time
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


register("data", "capture", data_capture_cmd, "Capture fresh Yahoo fundamental and price data")
