"""Portfolio construction, buffer retention, sector caps and target weighting (C08)."""

from __future__ import annotations

import collections
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from quant.config import Config


def rebalance(
    previous: pd.DataFrame,
    ranks: pd.Series,
    eligible: pd.Series,
    groups: pd.Series,
    buckets: pd.Series,
    cfg: Config,
    rule: str = "top30_buffer",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rebalance portfolio under stated rule, turnover buffer, sector caps, and liquidity limits.

    Returns:
      positions: DataFrame(security_id, target_weight, entry_as_of)
      deltas: DataFrame(security_id, weight_delta, side)
    """
    target_n = 30
    sector_cap = 6
    buffer_rank = 60
    bucket_c_cap = 0.02

    prev_map: dict[int, dict] = {}
    if previous is not None and not previous.empty:
        for _, row in previous.iterrows():
            sid = int(row["security_id"])
            if sid != 0 and row.get("weight", 0) > 0:
                prev_map[sid] = dict(row)

    # 1. Determine selected names
    selected: list[int] = []
    sector_counts: dict[str, int] = collections.defaultdict(int)
    entry_dates: dict[int, str] = {}

    if rule == "top30_buffer":
        # First: retain eligible existing holdings with rank <= buffer_rank (60) and bucket != 'D'
        retained_candidates = []
        for sid, p_data in prev_map.items():
            if (
                eligible.get(sid, False)
                and buckets.get(sid, "D") != "D"
                and ranks.get(sid, 999999) <= buffer_rank
            ):
                grp = groups.get(sid, p_data.get("group", "UNKNOWN"))
                rk = ranks.get(sid, 999999)
                retained_candidates.append((rk, sid, grp, p_data.get("entry_as_of", "")))

        # Sort retained by (rank, sid)
        retained_candidates.sort(key=lambda x: (x[0], x[1]))

        for rk, sid, grp, entry_dt in retained_candidates:
            if sector_counts[grp] < sector_cap and len(selected) < target_n:
                selected.append(sid)
                sector_counts[grp] += 1
                entry_dates[sid] = entry_dt

        # Second: fill vacancies up to target_n from remaining eligible names
        vacancies = target_n - len(selected)
        if vacancies > 0:
            entrant_candidates = []
            for sid in ranks.index:
                if (
                    sid not in selected
                    and eligible.get(sid, False)
                    and buckets.get(sid, "D") != "D"
                ):
                    rk = ranks.get(sid, 999999)
                    grp = groups.get(sid, "UNKNOWN")
                    entrant_candidates.append((rk, sid, grp))

            entrant_candidates.sort(key=lambda x: (x[0], x[1]))

            for rk, sid, grp in entrant_candidates:
                if sector_counts[grp] < sector_cap:
                    selected.append(sid)
                    sector_counts[grp] += 1
                    entry_dates[sid] = ""  # New entrant
                    if len(selected) == target_n:
                        break

    elif rule == "decile":
        # Unbuffered top 10%
        cand = []
        for sid in ranks.index:
            if eligible.get(sid, False) and buckets.get(sid, "D") != "D":
                cand.append((ranks.get(sid, 999999), sid, groups.get(sid, "UNKNOWN")))
        cand.sort(key=lambda x: (x[0], x[1]))
        target_count = min(len(cand), max(1, len(cand) // 10))
        for rk, sid, grp in cand[:target_count]:
            selected.append(sid)
            entry_dates[sid] = prev_map.get(sid, {}).get("entry_as_of", "")

    elif rule == "ew_universe":
        # All eligible
        cand = []
        for sid in ranks.index:
            if eligible.get(sid, False) and buckets.get(sid, "D") != "D":
                cand.append((ranks.get(sid, 999999), sid))
        cand.sort(key=lambda x: (x[0], x[1]))
        for _, sid in cand:
            selected.append(sid)
            entry_dates[sid] = prev_map.get(sid, {}).get("entry_as_of", "")

    else:
        # Default fallback to top30
        cand = []
        for sid in ranks.index:
            if eligible.get(sid, False) and buckets.get(sid, "D") != "D":
                cand.append((ranks.get(sid, 999999), sid))
        cand.sort(key=lambda x: (x[0], x[1]))
        for _, sid in cand[:target_n]:
            selected.append(sid)
            entry_dates[sid] = prev_map.get(sid, {}).get("entry_as_of", "")

    # 2. Compute target weights with Bucket C cap
    positions_list = []
    K = len(selected)

    if K > 0:
        nom_weight = 1.0 / target_n if rule == "top30_buffer" else 1.0 / K

        c_sids = [s for s in selected if buckets.get(s) == "C"]
        ab_sids = [s for s in selected if buckets.get(s) in ("A", "B")]

        c_weight = min(nom_weight, bucket_c_cap)
        total_c_weight = len(c_sids) * c_weight
        rem_weight = max(0.0, 1.0 - total_c_weight)

        ab_weight = (rem_weight / len(ab_sids)) if ab_sids else 0.0

        for sid in selected:
            if sid in c_sids:
                w = c_weight
            else:
                w = ab_weight
            positions_list.append({
                "security_id": sid,
                "target_weight": float(w),
                "entry_as_of": entry_dates.get(sid, ""),
            })

    positions = pd.DataFrame(
        positions_list, columns=["security_id", "target_weight", "entry_as_of"]
    )

    # 3. Compute weight deltas vs previous holdings
    deltas_list = []
    target_map = {row["security_id"]: row["target_weight"] for row in positions_list}

    # Sells for exited securities
    for sid, p_data in prev_map.items():
        if sid not in target_map:
            prev_w = float(p_data.get("weight", 0.0))
            if prev_w > 0:
                deltas_list.append({
                    "security_id": sid,
                    "weight_delta": -prev_w,
                    "side": "SELL",
                })

    # Deltas for current targets
    for sid, target_w in target_map.items():
        prev_w = float(prev_map.get(sid, {}).get("weight", 0.0))
        delta = target_w - prev_w
        if delta > 1e-6:
            deltas_list.append({
                "security_id": sid,
                "weight_delta": delta,
                "side": "BUY",
            })
        elif delta < -1e-6:
            deltas_list.append({
                "security_id": sid,
                "weight_delta": delta,
                "side": "SELL",
            })

    deltas = pd.DataFrame(deltas_list, columns=["security_id", "weight_delta", "side"])
    return positions, deltas
