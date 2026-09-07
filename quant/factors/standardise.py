"""Factor standardisation, cross-sectional winsorization and bounded centered ranks."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

from quant.config import Config


def transform(
    raw: pd.Series,
    groups: pd.Series,
    direction: int,
    cfg: Config,
) -> pd.DataFrame:
    """Standardise raw factor values into centered, bounded [-3, 3] z-scores per sector group.
    
    Formula per MASTER_SPEC §5.2:
    1. Cross-sectional winsorization at 1st/99th percentiles.
    2. Within each sector group, require >= 5 finite observations and > 1 distinct value.
    3. Average-tie rank -> v = direction * NormalPPF((rank - 0.5) / n).
    4. Centering: v - mean(v).
    5. Bounding: divide by max(1.0, max(abs(v - mean(v))) / 3.0) to strictly enforce [-3, 3].
    """
    if direction not in (1, -1):
        raise ValueError(f"Direction must be +1 or -1, got {direction}")

    idx = raw.index
    groups_aligned = groups.reindex(idx)
    finite_mask = raw.notna() & np.isfinite(raw)

    # 1. Winsorize cross-sectionally
    winsor = raw.astype(float).copy()
    winsor_lo = float(getattr(getattr(cfg, "standardise", None), "winsor_lo", 0.01))
    winsor_hi = float(getattr(getattr(cfg, "standardise", None), "winsor_hi", 0.99))

    if finite_mask.sum() >= 5:
        lo = float(np.percentile(raw[finite_mask], winsor_lo * 100.0))
        hi = float(np.percentile(raw[finite_mask], winsor_hi * 100.0))
        winsor[finite_mask] = raw[finite_mask].clip(lower=lo, upper=hi)

    z = pd.Series(np.nan, index=idx, dtype=float)
    flags = pd.Series("", index=idx, dtype=object)
    min_nonnull = int(getattr(getattr(cfg, "standardise", None), "min_group_nonnull", 5))

    # 2. Sector group standardisation
    for group_name, g_sids in raw.groupby(groups_aligned).groups.items():
        g_sids = pd.Index(g_sids)
        g_finite = g_sids[finite_mask.reindex(g_sids).fillna(False)]
        n_g = len(g_finite)

        if n_g < min_nonnull:
            z.loc[g_sids] = np.nan
            flags.loc[g_sids] = "small_group"
            continue

        distinct_vals = winsor.loc[g_finite].nunique()
        if distinct_vals <= 1:
            z.loc[g_sids] = np.nan
            flags.loc[g_sids] = "constant_group"
            continue

        g_vals = winsor.loc[g_finite]
        ranks = g_vals.rank(method="average").values
        prob = (ranks - 0.5) / n_g
        v = float(direction) * norm.ppf(prob)

        centered = v - np.mean(v)
        max_abs = float(np.max(np.abs(centered))) if len(centered) > 0 else 0.0
        scale = max(1.0, max_abs / 3.0)
        g_z = centered / scale

        z.loc[g_finite] = g_z

        g_missing = g_sids[~finite_mask.reindex(g_sids).fillna(False)]
        if len(g_missing) > 0:
            flags.loc[g_missing] = "missing"

    # Flag unassigned groups
    unassigned = idx[groups_aligned.isna()]
    if len(unassigned) > 0:
        flags.loc[unassigned] = "unknown_group"

    return pd.DataFrame({
        "raw": raw,
        "winsor": winsor,
        "z": z,
        "flags": flags,
    }, index=idx)
