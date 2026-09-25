"""Factor standardisation, cross-sectional winsorization and bounded centered ranks."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import norm

from quant.config import Config


def transform(
    raw: pd.Series,
    groups: pd.Series,
    direction: int,
    cfg: Optional[Config] = None,
    applicable: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Standardise raw factor values into centered, bounded [-3, 3] z-scores per sector group.

    Formula per MASTER_SPEC §5.2:
    1. Cross-sectional winsorization at 1st/99th percentiles.
    2. Within each sector group, require >= 5 finite observations and > 1 distinct value.
    3. Average-tie rank -> v = direction * NormalPPF((rank - 0.5) / n).
    4. Centering: v - mean(v).
    5. Bounding: divide by max(1.0, max(abs(v - mean(v))) / 3.0) to strictly enforce [-3, 3].

    `applicable` (task T9 / decision D10) is an optional per-security boolean series:
    False marks a security for which this factor is structurally not applicable (e.g.
    a nonfinancial-only factor on a Financial Services name), as opposed to one that
    is merely missing data. Per MASTER_SPEC §5.2, "coverage denominator excludes
    structural non-applicability" -- such securities are flagged not_applicable and
    excluded from the group's finite-count/distinct-value denominator entirely,
    rather than counting against it as small_group would. Omitted (the default),
    every security is treated as applicable, which is the prior behaviour exactly.
    """
    if cfg is None:
        from quant.config import load
        cfg = load()
    if direction not in (1, -1):
        raise ValueError(f"Direction must be +1 or -1, got {direction}")

    idx = raw.index
    groups_aligned = groups.reindex(idx)
    finite_mask = raw.notna() & np.isfinite(raw)
    if applicable is None:
        applicable_mask = pd.Series(True, index=idx)
    else:
        applicable_mask = applicable.reindex(idx).fillna(True).astype(bool)

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

        # Structurally not-applicable securities never count toward the group's
        # denominator (MASTER_SPEC §5.2) and are flagged distinctly from small_group.
        g_not_applicable = g_sids[~applicable_mask.reindex(g_sids).fillna(True)]
        if len(g_not_applicable) > 0:
            z.loc[g_not_applicable] = np.nan
            flags.loc[g_not_applicable] = "not_applicable"

        g_applicable = g_sids[applicable_mask.reindex(g_sids).fillna(True)]
        if len(g_applicable) == 0:
            continue

        g_finite = g_applicable[finite_mask.reindex(g_applicable).fillna(False)]
        n_g = len(g_finite)

        if n_g < min_nonnull:
            z.loc[g_applicable] = np.nan
            flags.loc[g_applicable] = "small_group"
            continue

        distinct_vals = winsor.loc[g_finite].nunique()
        if distinct_vals <= 1:
            z.loc[g_applicable] = np.nan
            flags.loc[g_applicable] = "constant_group"
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

        g_missing = g_applicable[~finite_mask.reindex(g_applicable).fillna(False)]
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


standardise = transform

