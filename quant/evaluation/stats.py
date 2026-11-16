"""Statistical functions for evaluation, testing, and confidence bounds (C07)."""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING

import numpy as np

from quant.types import HacResult


def hac_mean_test(x: list[float], lag: int) -> HacResult:
    """Compute Newey-West HAC mean test with Bartlett kernel.

    lag: truncation parameter L (e.g. 1 for 3M horizon when 2 intervals overlap).
    Returns HacResult with mean, se, t, 90% CI bounds, n, n_eff, and status.
    """
    arr = np.asarray(x, dtype=float)
    arr = arr[~np.isnan(arr)]
    N = len(arr)

    n_eff = float(N / (lag + 1)) if (lag + 1) > 0 else float(N)

    if N <= lag + 1:
        mean_val = float(np.mean(arr)) if N > 0 else None
        return HacResult(
            mean=mean_val,
            se=None,
            t=None,
            ci_lo=None,
            ci_hi=None,
            n=N,
            n_eff=n_eff,
            status="insufficient",
        )

    mean_val = float(np.mean(arr))
    residuals = arr - mean_val
    gamma0 = float(np.mean(residuals**2))

    if gamma0 == 0.0 or np.all(arr == arr[0]):
        return HacResult(
            mean=mean_val,
            se=None,
            t=None,
            ci_lo=None,
            ci_hi=None,
            n=N,
            n_eff=n_eff,
            status="constant",
        )

    # Newey-West Bartlett kernel
    S = gamma0
    for l in range(1, lag + 1):
        gamma_l = float(np.sum(residuals[l:] * residuals[:-l]) / N)
        w_l = 1.0 - float(l) / (lag + 1)
        S += 2.0 * w_l * gamma_l

    if S <= 0.0:
        return HacResult(
            mean=mean_val,
            se=None,
            t=None,
            ci_lo=None,
            ci_hi=None,
            n=N,
            n_eff=n_eff,
            status="insufficient",
        )

    se = float(math.sqrt(S / N))
    t_stat = float(mean_val / se)
    ci_lo = float(mean_val - 1.645 * se)
    ci_hi = float(mean_val + 1.645 * se)

    return HacResult(
        mean=mean_val,
        se=se,
        t=t_stat,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        n=N,
        n_eff=n_eff,
        status="ok",
    )


def block_bootstrap_ci(
    x: list[float],
    block: int,
    n: int = 1000,
    q: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None]:
    """Circular block bootstrap confidence interval of the mean.

    Requires N >= 3 * block. Returns (ci_lo, ci_hi) or (None, None) if insufficient/constant.
    """
    arr = np.asarray(x, dtype=float)
    arr = arr[~np.isnan(arr)]
    N = len(arr)

    if block <= 0 or N < 3 * block:
        return (None, None)
    if np.all(arr == arr[0]):
        return (None, None)

    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(N / block)

    start_indices = rng.integers(0, N, size=(n, n_blocks))
    offsets = np.arange(block)
    indices = (start_indices[:, :, None] + offsets[None, None, :]) % N
    flat_indices = indices.reshape(n, -1)[:, :N]

    boot_samples = arr[flat_indices]
    boot_means = np.mean(boot_samples, axis=1)

    alpha = (1.0 - q) / 2.0
    ci_lo = float(np.percentile(boot_means, 100.0 * alpha))
    ci_hi = float(np.percentile(boot_means, 100.0 * (1.0 - alpha)))
    return (ci_lo, ci_hi)


def t_crit(m: int, looks: int = 3, alpha: float = 0.05, floor: float = 2.0) -> float:
    """Compute multiple-testing critical threshold with floor.

    threshold = max(floor, inv_cdf(1 - alpha / (looks * m))).
    """
    if m <= 0 or looks <= 0 or alpha <= 0.0:
        return float(floor)
    prob = 1.0 - alpha / (looks * m)
    if prob <= 0.0 or prob >= 1.0:
        return float(floor)
    val = statistics.NormalDist().inv_cdf(prob)
    return float(max(floor, val))


def wilson(k: int, n: int, z: float = 1.645) -> tuple[float | None, float | None]:
    """Wilson score confidence interval for binomial proportion."""
    if n <= 0 or k < 0 or k > n:
        return (None, None)
    p = k / n
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    margin = (z * math.sqrt((p * (1.0 - p)) / n + (z * z) / (4.0 * n * n))) / denom
    ci_lo = max(0.0, center - margin)
    ci_hi = min(1.0, center + margin)
    return (float(ci_lo), float(ci_hi))
