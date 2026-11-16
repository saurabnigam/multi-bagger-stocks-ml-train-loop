"""Sandbox experiment: 'a closing price counts by the date it is FOR, not the time we recorded it'.
Price bars for a cohort are read at a pinned price vintage (the time the cohort is scored) instead of the
cohort cutoff. Everything else (fundamentals, index membership, corporate-action records) keeps the cutoff."""
import os, sys, time, datetime
from quant.config import load as load_config
from quant.types import Actor, FrozenClock
import quant.run as qrun
import quant.data.prices as P
import quant.factors.registry as _registry   # loaded first: its code-identity map keeps the unmodified functions
cfg = load_config(); assert "/nov14/" in str(cfg.paths.db), cfg.paths.db
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
SEPT_CUT, SEPT_SCORED = "2026-09-30T18:29:59.999999Z", "2026-10-17T18:26:37.448458Z"   # cutoff, time it was published
OCT_CUT = qrun.knowledge_cutoff("2026-10-30")
PIN = {}
def _pin(v): return PIN.get(v, v)
_v, _adv, _spl, _man = P.PriceStore._versioned, P.PriceStore.adv_inr, P.PriceStore.split_events, P.PriceStore._manifest_rows
P.PriceStore._versioned = lambda self, sids, start, end, vintage_at, cols: _v(self, sids, start, end, _pin(vintage_at), cols)
P.PriceStore.adv_inr = lambda self, sids, as_of, vintage_at, window=63: _adv(self, sids, as_of, _pin(vintage_at), window)
P.PriceStore.split_events = lambda self, sids, start, end, vintage_at: _spl(self, sids, start, end, _pin(vintage_at))
P.PriceStore._manifest_rows = lambda self, vintage_at: _man(self, _pin(vintage_at))
actor = Actor(kind="system", name="sandbox-experiment"); clock = FrozenClock(NOW)
def replay(label):
    with qrun.RunContext(as_of="2026-10-30", kind="test", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        ctx.store = P.PriceStore(cfg.paths.prices_db, state_conn=ctx.conn); ctx.calendar = qrun._load_calendar(cfg, ctx.store)
        draft = qrun.build_draft(ctx, "2026-10-30", OCT_CUT)
        chk = qrun._g9_replay(ctx, draft)
        print(f"[{label}] replay of the September list: {chk.status} | {str(chk.observed)[:160]} | {str(chk.reason)[:120]}", flush=True)
        ctx.rollback_staging()
mode = sys.argv[1]
if mode == "replay":
    replay("today's rule: prices must be RECORDED before the cutoff")
    PIN[SEPT_CUT] = SEPT_SCORED; PIN[OCT_CUT] = NOW
    replay("bar-date rule: prices count by the date they are FOR")
elif mode == "october":
    PIN[SEPT_CUT] = SEPT_SCORED; PIN[OCT_CUT] = NOW
    t0 = time.time()
    code = qrun.monthly(cfg=cfg, clock=clock, actor=actor, as_of="2026-10-30", skip_capture=True)
    print(f"monthly exit={code} in {time.time()-t0:.0f}s (scored at {NOW})", flush=True)
