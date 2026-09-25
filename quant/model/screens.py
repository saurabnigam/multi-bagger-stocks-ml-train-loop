"""Tradability and data eligibility screens."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.data.universe import is_index_placeholder
from quant.portfolio.costs import bucket as liquidity_bucket_for

if TYPE_CHECKING:
    from quant.types import Draft, RunContext

# MASTER_SPEC section 6.1: "unknown sector" covers these exact spellings.
UNKNOWN_SECTOR_VALUES = {"UNCLASSIFIED", "UNKNOWN", "", "None", "nan"}


def _placeholder_signal(sid: Any, members: pd.DataFrame | None) -> tuple[Any, Any]:
    """Pull the (symbol, isin) pair for a member, tolerating either column's absence
    -- many synthetic/unit-test member frames carry neither."""
    if members is None or sid not in members.index:
        return None, None
    symbol = members.loc[sid, "symbol"] if "symbol" in members.columns else None
    isin = members.loc[sid, "isin"] if "isin" in members.columns else None
    return symbol, isin


def apply(
    ctx: RunContext,
    draft: Draft,
    scores: pd.DataFrame,
    model_id: str,
) -> pd.DataFrame:
    """Apply eligibility screens: EQ series, known sector, ADV >= cfg threshold, and a
    minimum count of positive-volume sessions in the trailing window (MASTER_SPEC 6.1).

    Thresholds come from `cfg.screens` (min_adv_inr, min_traded_sessions, adv_window);
    liquidity buckets come from `quant.portfolio.costs.bucket`, the same A/B/C/D
    thresholds used for cost modelling.

    Missing evidence is never a silent pass. A security whose ADV cannot be determined
    from `draft.members` or the price store is ineligible with reason `adv_missing` and
    bucket D. A security whose positive-volume session count cannot be determined is
    ineligible with reason `thin_trading` -- there is no default-to-passing fallback for
    either quantity.

    Scored but screened-out names remain in the scores table with scored=1, eligible=0.
    Re-computes rank, decile, quintile over eligible names only, using
    `min(q, 1 + floor((rank_ascending - 1) * q / n))`.
    """
    out = scores.copy()
    members = draft.members
    groups = draft.groups
    idx = out.index

    screens_cfg = getattr(ctx.cfg, "screens", None) if (ctx is not None and ctx.cfg is not None) else None
    min_adv = float(getattr(screens_cfg, "min_adv_inr", 20_000_000.0))
    min_sessions = int(getattr(screens_cfg, "min_traded_sessions", 54))
    adv_window = int(getattr(screens_cfg, "adv_window", 63))

    adv_col = f"adv_{adv_window}_inr"
    sessions_col = f"pos_sessions_{adv_window}"

    # 1. Retrieve ADV and positive-volume session counts. Both stay NaN unless a real
    # source (the members frame or the price store) actually reports them -- no
    # default-to-passing value is ever substituted for missing evidence.
    adv_series = pd.Series(np.nan, index=idx, dtype=float)
    pos_sessions = pd.Series(np.nan, index=idx, dtype=float)

    if members is not None:
        if adv_col in members.columns:
            adv_series = members[adv_col].reindex(idx).astype(float)
        elif "adv_63_inr" in members.columns:
            adv_series = members["adv_63_inr"].reindex(idx).astype(float)
        elif "adv" in members.columns:
            adv_series = members["adv"].reindex(idx).astype(float)

        if sessions_col in members.columns:
            pos_sessions = members[sessions_col].reindex(idx).astype(float)
        elif "pos_sessions_63" in members.columns:
            pos_sessions = members["pos_sessions_63"].reindex(idx).astype(float)
        elif "pos_sessions" in members.columns:
            pos_sessions = members["pos_sessions"].reindex(idx).astype(float)

    # If still missing and a price store is attached, query it directly -- the only
    # other legitimate source. Anything still missing after this stays missing.
    missing_adv = adv_series.isna()
    missing_sessions = pos_sessions.isna()
    if (missing_adv.any() or missing_sessions.any()) and ctx is not None and getattr(ctx, "store", None) is not None:
        sids_to_fetch = [int(s) for s in idx[missing_adv | missing_sessions]]
        adv_df = ctx.store.adv_inr(
            sids_to_fetch,
            as_of=draft.as_of,
            vintage_at=draft.knowledge_cutoff,
            window=adv_window,
        )
        store_adv_col = f"adv_{adv_window}_inr"
        store_sessions_col = f"pos_sessions_{adv_window}"
        if not adv_df.empty:
            if store_adv_col in adv_df.columns:
                adv_series = adv_series.combine_first(adv_df[store_adv_col])
            if store_sessions_col in adv_df.columns:
                pos_sessions = pos_sessions.combine_first(adv_df[store_sessions_col])

    # 2. Determine liquidity buckets from the shared cost-model thresholds (A/B/C/D).
    # `costs.bucket` already treats a missing/non-finite ADV as bucket D.
    liq_bucket = pd.Series(
        [liquidity_bucket_for(adv_series.loc[sid], ctx.cfg if ctx is not None else None) for sid in idx],
        index=idx,
        dtype=object,
    )

    # 3. Apply eligibility rules
    series_col = members["series"].reindex(idx) if (members is not None and "series" in members.columns) else pd.Series("EQ", index=idx)
    sector_col = groups.reindex(idx) if groups is not None else pd.Series("UNKNOWN", index=idx)

    eligible = pd.Series(0, index=idx, dtype=int)
    exclusion_reason = out["exclusion_reason"].copy()

    for sid in idx:
        # Task T9 / decision D10 (MASTER_SPEC 6.1 exclusion reasons): NSE placeholder
        # rows for entities mid-demerger (symbol DUMMY*, or a present-but-invalid
        # ISIN) are excluded as index_placeholder. This takes precedence over
        # coverage and every other reason below -- checked first, unconditionally.
        m_symbol, m_isin = _placeholder_signal(sid, members)
        if is_index_placeholder(m_symbol, m_isin):
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "index_placeholder"
            continue

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
        sec_raw = sector_col.loc[sid]
        sec_val = str(sec_raw) if pd.notna(sec_raw) else "UNKNOWN"
        if sec_val in UNKNOWN_SECTOR_VALUES:
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "unknown_sector"
            continue

        # A missing ADV is ineligible -- never a silent pass.
        adv_val = adv_series.loc[sid]
        if pd.isna(adv_val):
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "adv_missing"
            continue

        # Check ADV >= cfg.screens.min_adv_inr
        if float(adv_val) < min_adv:
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "illiquid"
            continue

        # A missing positive-volume session count is ineligible -- never a silent pass.
        n_pos_raw = pos_sessions.loc[sid]
        if pd.isna(n_pos_raw):
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "thin_trading"
            continue

        # Check positive-volume sessions >= cfg.screens.min_traded_sessions
        if int(n_pos_raw) < min_sessions:
            eligible.loc[sid] = 0
            exclusion_reason.loc[sid] = "thin_trading"
            continue

        # Eligible
        eligible.loc[sid] = 1
        exclusion_reason.loc[sid] = None

    out["eligible"] = eligible
    out["exclusion_reason"] = exclusion_reason
    out["liquidity_bucket"] = liq_bucket

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
