"""Sandbox: run the monthly pipeline with selected owner decisions SIMULATED.
SIM_D2=1 -> prior-cohort selection (G4, G9) skips cohorts that carry a recorded invalidation event.
Decision 3 is simulated by pointing QUANT_CONFIG_PATH at a copy of the config with price_min_coverage 0.90."""
import os, sqlite3, sys, time
from quant.config import load as load_config
from quant.types import Actor, FrozenClock, Check
import quant.run as qrun
import quant.data.gates as gates
AS_OF = os.environ["AS_OF"]; RUNCLOCK = os.environ["RUNCLOCK"]; SIM_LEVEL = int(os.environ.get("SIM_D2", "0")); SIM_D2 = SIM_LEVEL >= 1
cfg = load_config(); assert "/nov11/" in str(cfg.paths.db), cfg.paths.db
print(f"config price_min_coverage={getattr(getattr(cfg,'factors',None),'price_min_coverage',None)} | SIM_D2={SIM_D2}", flush=True)
if SIM_D2:
    c = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
    INVALID = {r[0] for r in c.execute("select json_extract(detail_json,'$.cohort_id') from data_quality_events "
               "where severity='BLOCK' and code in ('DQ_PRICE_PROVENANCE_FAIL','COHORT_INVALIDATED')") if r[0]}
    c.close(); print("simulated decision 2: skipping invalidated cohorts", sorted(INVALID), flush=True)
    def _prior(ctx, draft):
        if ctx.conn is None: return None
        for row in ctx.conn.execute("SELECT cohort_id, as_of, knowledge_cutoff FROM cohorts WHERE track = ? AND as_of < ? "
                                    "ORDER BY as_of DESC, published_at DESC", (draft.track, draft.as_of)).fetchall():
            if row[0] not in INVALID: return dict(row)
        return None
    gates._prior_cohort = _prior
    _orig = qrun._g9_replay
    def _g9(ctx, draft):
        rows = ctx.conn.execute("SELECT cohort_id FROM cohorts WHERE track='live' AND as_of < ? ORDER BY as_of DESC, published_at DESC", (draft.as_of,)).fetchall()
        if rows and all(r[0] in INVALID for r in rows):
            return Check(id="G9", status="DEFERRED", observed=None, expected="exact_match", blocking=False,
                         reason=f"SIMULATED DECISION 2: prior cohort {rows[0][0]} is invalidated; no earlier clean live cohort to replay")
        return _orig(ctx, draft)
    qrun._g9_replay = _g9
    if SIM_LEVEL >= 2:   # the leakage audit's replay (G10 / T3_REPLAY) picks the latest live cohort the same way
        import quant.evaluation.leakage as leakage
        _orig_t3 = leakage._t3_replay
        def _t3(ctx):
            conn = leakage._conn(ctx)
            row = conn.execute("SELECT cohort_id FROM cohorts WHERE track = 'live' ORDER BY published_at DESC, as_of DESC LIMIT 1").fetchone() if conn is not None else None
            if row is not None and row[0] in INVALID:
                return Check(id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched", blocking=True,
                             reason=f"SIMULATED DECISION 2: latest live cohort {row[0]} is invalidated; replay deferred")
            return _orig_t3(ctx)
        leakage._t3_replay = _t3
t0 = time.time()
code = qrun.monthly(cfg=cfg, clock=FrozenClock(RUNCLOCK), actor=Actor(kind="system", name="sandbox-preview"), as_of=AS_OF, skip_capture=True, allow_mid_month=True)
print(f"monthly exit={code} in {time.time()-t0:.0f}s", flush=True); sys.exit(code)
