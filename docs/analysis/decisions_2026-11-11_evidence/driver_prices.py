"""Sandbox: price-only update under a frozen clock stamped just before the as_of cutoff.
This SIMULATES decision 1 (closes of the as_of session admitted although the vendor delivered them later)."""
import os, time
from quant.config import load as load_config
from quant.types import Actor, FrozenClock
from quant.run import RunContext
from quant.data.yahoo import YahooClient
from quant.data.prices import PriceStore
from quant.data.universe import members_at
from quant.data.identity import yahoo_ticker
AS_OF = os.environ["AS_OF"]; STAMP = os.environ["STAMP"]
assert STAMP == "now" or STAMP <= f"{AS_OF}T18:29:59.999999Z"
cfg = load_config(); assert "/nov11/" in str(cfg.paths.db), cfg.paths.db
clock = (__import__("quant.types", fromlist=["SystemClock"]).SystemClock() if STAMP == "now" else FrozenClock(STAMP)); client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep); t0 = time.time()
with RunContext(as_of=AS_OF, kind="capture", track="live", cfg=cfg, clock=clock, actor=Actor(kind="system", name="sandbox-preview")) as ctx:
    m = members_at(ctx.conn, AS_OF)
    sids = [int(s) for s in m["security_id"].tolist() if yahoo_ticker(ctx.conn, int(s), AS_OF)]
    res = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn).update(ctx, client, sids, through=AS_OF)
    print("prices:", res.status, res.counts, flush=True)
print(f"done in {time.time()-t0:.0f}s", flush=True)
