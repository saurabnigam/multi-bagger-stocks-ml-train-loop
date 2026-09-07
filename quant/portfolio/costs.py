"""Transaction costs and liquidity buckets (C08)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quant.config import Config


def bucket(adv_inr: float, cfg: Config) -> str:
    """Classify 63-day average daily turnover (INR) into liquidity bucket A, B, C, or D.

    A: >= 500M INR
    B: >= 100M and < 500M INR
    C: >= 20M and < 100M INR
    D: < 20M INR (excluded from tradable universe)
    """
    if adv_inr >= 500_000_000.0:
        return "A"
    elif adv_inr >= 100_000_000.0:
        return "B"
    elif adv_inr >= 20_000_000.0:
        return "C"
    else:
        return "D"


def cost_bps_one_way(bucket_name: str, cfg: Config, stress: bool = False) -> float:
    """Calculate one-way transaction cost in basis points.

    Fixed: 12 bps
    Impact: A=10 bps, B=25 bps, C=50 bps, D=infinity
    Stress multiplier: 1.5x
    """
    fixed = 12.0
    impact_map = {
        "A": 10.0,
        "B": 25.0,
        "C": 50.0,
        "D": float("inf"),
    }
    impact = impact_map.get(bucket_name, float("inf"))
    if math.isinf(impact):
        return float("inf")

    total = fixed + impact
    if stress:
        total *= 1.5
    return float(total)
