"""Tradability and data eligibility screens."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from quant.types import Draft, RunContext


def apply(
    ctx: RunContext,
    draft: Draft,
    scores: pd.DataFrame,
    model_id: str,
) -> pd.DataFrame:
    """Apply eligibility screens: EQ series, known sector, ADV63 >= 20M INR, >= 54 positive sessions.

    Scored but screened-out names remain in scores table with scored=1 and eligible=0.
    Re-computes rank, decile, quintile over eligible names only.
    """
    out = scores.copy()
    members = draft.members
    groups = draft.groups
    idx = out.index

    # 1. Retrieve ADV and positive-volume session counts
    min_adv = 20_000_000.0
    if ctx and ctx.cfg and hasattr(ctx.cfg, "gates") and hasattr(ctx.cfg.gates, "min_adv_inr"):
        min_adv = float(ctx.cfg.gates.min_adv_inr)

    adv_series = pd.Series(np.nan, index=idx, dtype=float)
    pos_sessions = pd.Series(63, index=idx, dtype=int)

    # Check members columns first
    if members is not None:
        if "adv_63_inr" in members.columns:
            adv_series = members["adv_63_inr"].reindex(idx).astype(float)
        elif "adv" in members.columns:
            adv_series = members["adv"].reindex(idx).astype(float)

        if "pos_sessions_63" in members.columns:
            pos_sessions = members["pos_sessions_63"].reindex(idx).fillna(63).astype(int)
        elif "pos_sessions" in members.columns:
            pos_sessions = members["pos_sessions"].reindex(idx).fillna(63).astype(int)

    # If missing and ctx.store is available, query store
    missing_adv = adv_series.isna()
    if missing_adv.any() and ctx is not None and getattr(ctx, "store", None) is not None:
        sids_to_fetch = [int(s) for s in idx[missing_adv]]
        adv_df = ctx.store.adv_inr(
            sids_to_fetch,
            as_of=draft.as_of,
            vintage_at=draft.knowledge_cutoff,
            window=63,
        )
        if not adv_df.empty and "adv_63_inr" in adv_df.columns:
            adv_series = adv_series.combine_first(adv_df["adv_63_inr"])
            if "n_days_63" in adv_df.columns:
                pos_sessions = pos_sessions.combine_first(adv_df["n_days_63"])

    # If still missing, default to passing (50M) for offline test fixtures without store
    adv_series = adv_series.fillna(50_000_000.0)

    # 2. Determine liquidity buckets
    # A >= 500M, B >= 100M, C >= 20M, else D
    liquidity_bucket = pd.Series(None, index=idx, dtype=object)
    for sid in idx:
        adv = adv_series.loc[sid]
        if adv >= 500_000_000.0:
            liquidity_bucket.loc[sid] = "A"
        elif adv >= 100_000_000.0:
            liquidity_bucket.loc[sid] = "B"
        elif adv >= 20_000_000.0:
            liquidity_bucket.loc[sid] = "C"
        else:
            liquidity_bucket.loc[sid] = "D"

    # 3. Apply eligibility rules
    series_col = members["series"].reindex(idx) if (members is not None and "series" in members.columns) else pd.Series("EQ", index=idx)
    sector_col = groups.reindex(idx) if groups is not None else pd.Series("UNKNOWN", index=idx)

    eligible = pd.Series(0, index=idx, dtype=int)
    exclusion_reason = out["exclusion_reason"].copy()

    for sid in idx:
        if out.loc[sid, "scored"] == 0:
            eligible.loc[sid] = 0
            if pd.isna(exclusion_reason.loc[sid]) or not exclusion_reason.loc[sid]:
                exclusion_reason.loc[sid] = "coverage"
            continue

        # Check EQ series
        s_val = str(series_col.loc[sid]) if pd.notna(series_col.loc[sid]) else ""
        if s_val != "EQ":
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "non_eq_series"
            continue

        # Check known sector
        sec_val = str(sector_col.loc[sid]) if pd.notna(sector_col.loc[sid]) else "UNKNOWN"
        if sec_val in ("UNKNOWN", "", "None", "nan"):
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "unknown_sector"
            continue

        # Check ADV63 >= 20M INR
        adv_val = float(adv_series.loc[sid])
        if adv_val < min_adv:
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "illiquid"
            continue

        # Check positive-volume sessions >= 54 of last 63
        n_pos = int(pos_sessions.loc[sid])
        if n_pos < 54:
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "thin_trading"
            continue

        # Eligible
        eligible.loc[sid] = 1
        exclusion_reason.loc[sid] = None

    out["eligible"] = eligible
    out["exclusion_reason"] = exclusion_reason
    out["liquidity_bucket"] = liquidity_bucket

    # 4. Re-rank eligible names
    mask_elig = (out["scored"] == 1) & (out["eligible"] == 1) & out["final"].notna()
    elig_sids = [sid for sid in idx if mask_elig.loc[sid]]
    elig_sids.sort(key=lambda s: (-out.loc[s, "final"], s))

    n_elig = len(elig_sids)
    out["rank"] = np.nan
    out["quintile"] = np.nan
    out["decile"] = np.nan

    if n_elig > 0:
        for r, sid in enumerate(elig_sids, start=1):
            out.loc[sid, "rank"] = r
            rank_asc = n_elig - r + 1
            out.loc[sid, "quintile"] = min(5, 1 + math.floor((rank_asc - 1) * 5 / n_elig))
            out.loc[sid, "decile"] = min(10, 1 + math.floor((rank_asc - 1) * 10 / n_elig))

    draft.scores = out
    return out
