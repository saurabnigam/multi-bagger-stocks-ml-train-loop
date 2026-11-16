"""Sector-level diagnostic features per MASTER_SPEC §3.4."""
from __future__ import annotations

from typing import List
import numpy as np
import pandas as pd

from quant.config import Config
from quant.factors.inputs import FactorInputs
from quant.factors.value import EarningsYield


def compute(inputs: FactorInputs, cfg: Config) -> pd.DataFrame:
    """Compute sector-level features for all frozen sector groups.
    
    Features computed per MASTER_SPEC §3.4:
    1. sector_mom_6m: EW member log TR over t-6m to t-1m
    2. sector_breadth_200: share of members above trailing 200 SMA
    3. sector_flow_proxy: median three-capture holdings change
    4. sector_val_spread: median earnings yield minus universe median
    5. sector_dispersion: SD of member 3M returns
    """
    prov = inputs.provenance()
    cohort_id = prov.get("cohort_id", f"C-{inputs.as_of}-live")
    as_of = inputs.as_of
    track = prov.get("track", "live")
    sector_groups = inputs.sector_group

    unique_groups = [g for g in sector_groups.dropna().unique() if str(g).strip()]

    # 1. sector_mom_6m
    tris = inputs.tri(lookback_days=180)
    mom_6m_sids = pd.Series(np.nan, index=inputs.members, dtype=float)
    if not tris.empty and len(tris) >= 126:
        # log(TRI[-21] / TRI[-126])
        idx_21 = -21 if len(tris) >= 21 else -1
        idx_126 = -126
        for sid in inputs.members:
            if sid in tris.columns:
                p_end = tris[sid].iloc[idx_21]
                p_start = tris[sid].iloc[idx_126]
                if p_end > 0 and p_start > 0:
                    mom_6m_sids.loc[sid] = float(np.log(p_end / p_start))

    # 2. sector_breadth_200
    closes = inputs.close_split(lookback_days=260)
    above_sma200 = pd.Series(np.nan, index=inputs.members, dtype=float)
    if not closes.empty and len(closes) >= 200:
        for sid in inputs.members:
            if sid in closes.columns:
                series = closes[sid].dropna()
                if len(series) >= 200:
                    sma200 = float(series.iloc[-200:].mean())
                    curr = float(series.iloc[-1])
                    above_sma200.loc[sid] = 1.0 if curr > sma200 else 0.0

    # 3. sector_flow_proxy
    h0 = inputs.holdings(lag_runs=0)
    h3 = inputs.holdings(lag_runs=3)
    flow_diff = h0 - h3

    # 4. sector_val_spread
    ey_factor = EarningsYield()
    ey_vals = ey_factor.compute(inputs)
    univ_med_ey = float(ey_vals.dropna().median()) if not ey_vals.dropna().empty else np.nan

    # 5. sector_dispersion
    ret_3m = pd.Series(np.nan, index=inputs.members, dtype=float)
    if not tris.empty and len(tris) >= 63:
        for sid in inputs.members:
            if sid in tris.columns:
                p_now = tris[sid].iloc[-1]
                p_3m = tris[sid].iloc[-63]
                if p_3m > 0:
                    ret_3m.loc[sid] = float((p_now / p_3m) - 1.0)

    rows = []
    for g in unique_groups:
        g_mask = sector_groups == g
        g_sids = inputs.members[g_mask]
        n_members = len(g_sids)

        # Feature 1: sector_mom_6m
        m_vals = mom_6m_sids.reindex(g_sids).dropna()
        val_mom = float(m_vals.mean()) if not m_vals.empty else np.nan
        rows.append({
            "cohort_id": cohort_id, "as_of": as_of, "track": track,
            "sector_group": g, "feature_id": "sector_mom_6m",
            "value": val_mom, "n_members": n_members,
        })

        # Feature 2: sector_breadth_200
        b_vals = above_sma200.reindex(g_sids).dropna()
        val_breadth = float(b_vals.mean()) if not b_vals.empty else np.nan
        rows.append({
            "cohort_id": cohort_id, "as_of": as_of, "track": track,
            "sector_group": g, "feature_id": "sector_breadth_200",
            "value": val_breadth, "n_members": n_members,
        })

        # Feature 3: sector_flow_proxy
        f_vals = flow_diff.reindex(g_sids).dropna()
        val_flow = float(f_vals.median()) if not f_vals.empty else np.nan
        rows.append({
            "cohort_id": cohort_id, "as_of": as_of, "track": track,
            "sector_group": g, "feature_id": "sector_flow_proxy",
            "value": val_flow, "n_members": n_members,
        })

        # Feature 4: sector_val_spread
        e_vals = ey_vals.reindex(g_sids).dropna()
        if not e_vals.empty and not np.isnan(univ_med_ey):
            val_ey_spread = float(e_vals.median() - univ_med_ey)
        else:
            val_ey_spread = np.nan
        rows.append({
            "cohort_id": cohort_id, "as_of": as_of, "track": track,
            "sector_group": g, "feature_id": "sector_val_spread",
            "value": val_ey_spread, "n_members": n_members,
        })

        # Feature 5: sector_dispersion
        r_vals = ret_3m.reindex(g_sids).dropna()
        val_disp = float(r_vals.std(ddof=0)) if len(r_vals) >= 2 else np.nan
        rows.append({
            "cohort_id": cohort_id, "as_of": as_of, "track": track,
            "sector_group": g, "feature_id": "sector_dispersion",
            "value": val_disp, "n_members": n_members,
        })

    return pd.DataFrame(rows)
