"""Sandbox: capture fundamentals (true clock, before tonight's cutoff) only for current members that have none."""
import os, time, sqlite3
from quant.config import load as load_config
from quant.types import Actor, SystemClock
from quant.run import RunContext
from quant.data.yahoo import YahooClient
import quant.data.capture as cap
AS_OF = os.environ["AS_OF"]
cfg = load_config(); assert "/nov11/" in str(cfg.paths.db), cfg.paths.db
clock = SystemClock(); assert clock.iso() <= f"{AS_OF}T18:29:59.999999Z", "past the cutoff"
c = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
missing = [r[0] for r in c.execute("select m.security_id from universe_membership m where m.index_name='NIFTY500' and m.as_of=(select max(as_of) from universe_membership) except select distinct security_id from fundamentals")]
c.close(); print("members without fundamentals:", len(missing), flush=True)
cap.tracked_securities = lambda conn, as_of, horizons: sorted(missing)
client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep); t0 = time.time()
with RunContext(as_of=AS_OF, kind="capture", track="live", cfg=cfg, clock=clock, actor=Actor(kind="system", name="sandbox-preview")) as ctx:
    r = cap.run(ctx, client); print("capture:", r.status, r.counts, "at", clock.iso(), flush=True)
print(f"done in {time.time()-t0:.0f}s", flush=True)
