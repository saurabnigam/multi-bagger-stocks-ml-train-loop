"""Factor management and registry CLI commands."""
from __future__ import annotations

from quant.cli import register
from quant.config import load as load_config
from quant.factors.controls import Beta252, Liq, Size
from quant.factors.flows import InstHoldChg3m
from quant.factors.growth import EarnMom, EpsGrowth3y, RevGrowth3y
from quant.factors.legacy import DcFlag
from quant.factors.low_risk import MaxRet21, Vol252
from quant.factors.momentum import Dist52wHigh, Mom12_1, Mom6_1, Rev1m, Trend200
from quant.factors.quality import Accruals, CashConversion3y, Leverage, Roce, RoeStability3y
from quant.factors.registry import sync
from quant.factors.value import BookToPrice, DivYield, EarningsYield, FcfYield
from quant.run import RunContext
from quant.types import Actor, SystemClock


def factors_sync_cmd(args) -> int:
    """Sync all canonical factor specifications into factor_registry."""
    cfg = load_config()
    clock = SystemClock()
    actor = Actor(kind=getattr(args, "actor_kind", "system"), name=getattr(args, "by", "cli").split(":")[-1])
    as_of = getattr(args, "as_of", None) or clock.iso()[:10]

    all_factors = [
        Mom12_1(), Trend200(), Vol252(), Roce(), Accruals(),
        CashConversion3y(), EarningsYield(), BookToPrice(), EpsGrowth3y(),
        EarnMom(), InstHoldChg3m(), Mom6_1(), Dist52wHigh(), Rev1m(),
        MaxRet21(), Leverage(), RoeStability3y(), FcfYield(), DivYield(),
        RevGrowth3y(), Size(), Liq(), Beta252(), DcFlag(),
    ]
    specs = [f.spec for f in all_factors]

    with RunContext(as_of=as_of, kind="maintenance", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = sync(ctx, specs)
        print(f"Factors synced: {res.details}")
    return 0


register("factors", "sync", factors_sync_cmd, "Sync launch factor specifications into registry")
