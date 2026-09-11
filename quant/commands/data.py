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


def data_ingest_archive_cmd(args) -> int:
    """Replay a retained bundle archive; prices are updated unless --skip-capture is given."""
    from quant.data.capture import ingest_archive

    path = getattr(args, "target", None)
    if not path:
        print("Error: data ingest-archive requires the archive path as its target argument")
        return 1
    cfg = load_config(getattr(args, "config", None) or None)
    if getattr(args, "db_path", None):
        from pathlib import Path
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


register("data", "capture", data_capture_cmd, "Capture fresh Yahoo fundamental and price data")
register("data", "ingest-archive", data_ingest_archive_cmd, "Replay a retained bundle archive (target = path); --skip-capture skips prices")
