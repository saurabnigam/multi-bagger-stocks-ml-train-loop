"""Bounded capped-simplex weight optimizer and shrink target."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from quant.config import Config


def _project_capped_simplex(
    target: dict[str, float], floor: float, cap: float
) -> dict[str, float]:
    """Euclidean projection of target vector onto capped simplex sum(w) = 1, floor <= w <= cap.

    Solves w_k = clip(target_k - lambda, floor, cap) using bisection on lambda.
    """
    keys = list(target.keys())
    F = len(keys)
    if F == 0:
        return {}
    if F == 1:
        return {keys[0]: 1.0}

    y = np.array([target[k] for k in keys], dtype=float)

    lam_lo = float(np.min(y) - cap - 1.0)
    lam_hi = float(np.max(y) - floor + 1.0)

    for _ in range(100):
        lam_mid = (lam_lo + lam_hi) / 2.0
        w = np.clip(y - lam_mid, floor, cap)
        s = float(np.sum(w))
        if abs(s - 1.0) < 1e-13:
            break
        if s > 1.0:
            lam_lo = lam_mid
        else:
            lam_hi = lam_mid

    lam_star = (lam_lo + lam_hi) / 2.0
    w = np.clip(y - lam_star, floor, cap)
    s = float(np.sum(w))
    if s > 0:
        w = w / s
    return {k: float(v) for k, v in zip(keys, w)}


def allocate_units(
    target: dict[str, float],
    floor_mult: float = 0.5,
    cap_mult: float = 2.0,
    total: int = 10000,
) -> dict[str, int]:
    """Project target family weights onto capped simplex and allocate exact integer units summing to total.

    Bounds for integer units:
      lower_unit = ceil(total * floor_mult / F)
      upper_unit = floor(total * cap_mult / F)

    Residual integer units are distributed/removed by largest fractional remainder,
    breaking ties deterministically by family name in alphabetical order.
    """
    keys = sorted(target.keys())
    F = len(keys)
    if F == 0:
        return {}
    if F == 1:
        return {keys[0]: total}

    floor = floor_mult / F
    cap = cap_mult / F
    lower_unit = math.ceil(total * floor_mult / F)
    upper_unit = math.floor(total * cap_mult / F)

    # 1. Continuous projection
    w_proj = _project_capped_simplex({k: target[k] for k in keys}, floor, cap)

    # 2. Integer units via largest fractional remainder with box constraints
    y = {k: w_proj[k] * total for k in keys}
    units = {k: max(lower_unit, min(upper_unit, int(y[k]))) for k in keys}

    current_sum = sum(units.values())

    if current_sum < total:
        rem = total - current_sum
        # Candidates that have not reached upper_unit
        ranked = sorted(
            [k for k in keys if units[k] < upper_unit],
            key=lambda k: (-(y[k] - units[k]), k),
        )
        for k in ranked[:rem]:
            units[k] += 1
    elif current_sum > total:
        deficit = current_sum - total
        # Candidates that have not reached lower_unit
        ranked = sorted(
            [k for k in keys if units[k] > lower_unit],
            key=lambda k: (-(units[k] - y[k]), k),
        )
        for k in ranked[:deficit]:
            units[k] -= 1

    return units


def fit_family_weights(
    ic_hist: pd.DataFrame, cfg: Config
) -> tuple[dict[str, int], dict[str, Any]]:
    """Fit shrunk family weights from clean live family IC evaluations.

    Drops incomplete common dates across included families.
    Returns (units, diagnostics).
    """
    k_shrink = float(getattr(cfg.learning, "k_shrink", 24.0))
    min_n_eff = float(getattr(cfg.learning, "min_n_eff", 4.0))
    floor_mult = float(getattr(cfg.learning, "floor_mult", 0.5))
    cap_mult = float(getattr(cfg.learning, "cap_mult", 2.0))
    total_units = int(getattr(cfg.learning, "weight_units", 10000))
    horizon_m = int(getattr(cfg.horizons, "learning_m", 3))

    families = list(ic_hist.columns)
    F = len(families)
    if F == 0:
        return (
            {},
            {
                "n_months": 0,
                "n_eff": 0.0,
                "alpha": 0.0,
                "gate": "closed",
                "means": {},
                "omitted_dates": {},
            },
        )

    if ic_hist.empty:
        target = {fam: 1.0 / F for fam in families}
        units = allocate_units(
            target, floor_mult=floor_mult, cap_mult=cap_mult, total=total_units
        )
        return units, {
            "n_months": 0,
            "n_eff": 0.0,
            "alpha": 0.0,
            "gate": "closed",
            "means": {fam: 0.0 for fam in families},
            "omitted_dates": {fam: [] for fam in families},
        }

    # Ensure float dtype across columns
    ic_hist = ic_hist.astype(float)

    # Track omitted dates per family
    omitted_dates: dict[str, list[str]] = {}
    for fam in families:
        s = ic_hist[fam]
        bad_mask = s.isna() | ~np.isfinite(s)
        omitted_dates[fam] = [str(d) for d in ic_hist.index[bad_mask]]

    # Common finite date intersection
    valid_mask = ic_hist.notna().all(axis=1) & np.isfinite(ic_hist).all(axis=1)
    common_df = ic_hist.loc[valid_mask]
    n_months = int(len(common_df))
    n_eff = float(n_months / float(horizon_m))
    alpha = float(n_eff / (n_eff + k_shrink)) if (n_eff + k_shrink) > 0 else 0.0
    gate = "open" if n_eff >= min_n_eff else "closed"

    if n_months == 0:
        means = {fam: 0.0 for fam in families}
        target = {fam: 1.0 / F for fam in families}
    else:
        means = {fam: float(common_df[fam].mean()) for fam in families}
        raw = {fam: max(means[fam], 0.0) for fam in families}
        raw_sum = sum(raw.values())

        if n_eff < min_n_eff or raw_sum == 0.0:
            target = {fam: 1.0 / F for fam in families}
        else:
            norm_raw = {fam: raw[fam] / raw_sum for fam in families}
            target = {
                fam: (1.0 - alpha) / F + alpha * norm_raw[fam] for fam in families
            }

    units = allocate_units(
        target, floor_mult=floor_mult, cap_mult=cap_mult, total=total_units
    )
    diagnostics = {
        "n_months": n_months,
        "n_eff": n_eff,
        "alpha": alpha,
        "gate": gate,
        "means": means,
        "omitted_dates": omitted_dates,
    }
    return units, diagnostics
