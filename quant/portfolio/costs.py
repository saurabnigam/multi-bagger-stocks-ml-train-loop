"""Transaction costs and liquidity buckets (C08)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quant.config import Config


def _costs_cfg(cfg: Config | None) -> dict:
    """Read the [costs] block; fall back to the contract defaults when absent."""
    section = getattr(cfg, "costs", None) if cfg is not None else None
    if section is None:
        return {
            "fixed": 12.0,
            "impact": {"A": 10.0, "B": 25.0, "C": 50.0},
            "adv": {"A": 500_000_000.0, "B": 100_000_000.0, "C": 20_000_000.0},
            "stress": 1.5,
        }
    impact = getattr(section, "impact_bps", None) or {"A": 10.0, "B": 25.0, "C": 50.0}
    adv = getattr(section, "bucket_adv_inr", None) or {"A": 500_000_000.0, "B": 100_000_000.0, "C": 20_000_000.0}
    return {
        "fixed": float(getattr(section, "fixed_bps_one_way", 12.0)),
        "impact": {k: float(v) for k, v in dict(impact).items()},
        "adv": {k: float(v) for k, v in dict(adv).items()},
        "stress": float(getattr(section, "stress_mult", 1.5)),
    }


def bucket(adv_inr: float, cfg: Config) -> str:
    """Classify 63-day average daily turnover (INR) into liquidity bucket A, B, C or D.

    Thresholds come from config [costs].bucket_adv_inr (defaults A>=500M, B>=100M,
    C>=20M INR). Missing or non-finite turnover is bucket D (not tradable).
    """
    c = _costs_cfg(cfg)
    if adv_inr is None or not math.isfinite(float(adv_inr)):
        return "D"
    adv_inr = float(adv_inr)
    if adv_inr >= c["adv"]["A"]:
        return "A"
    if adv_inr >= c["adv"]["B"]:
        return "B"
    if adv_inr >= c["adv"]["C"]:
        return "C"
    return "D"


def cost_bps_one_way(bucket: str, cfg: Config, stress: bool = False) -> float:
    """One-way transaction cost in basis points: fixed + impact(bucket), x stress_mult.

    Values come from config [costs]; bucket D (or unknown) is not tradable (inf).
    """
    c = _costs_cfg(cfg)
    impact = c["impact"].get(bucket, float("inf"))
    if math.isinf(impact):
        return float("inf")
    total = c["fixed"] + impact
    if stress:
        total *= c["stress"]
    return float(total)
