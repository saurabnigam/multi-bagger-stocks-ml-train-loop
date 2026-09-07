"""Point-in-time holdings captures and monthly lag series."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from quant.data.yahoo import RawBundle, normalize_info
from quant.run import RunContext
from quant.types import Result


def capture(ctx: RunContext, bundles: Dict[int, RawBundle]) -> Result:
    """Capture institutional and insider holdings from raw bundles."""
    cur = ctx.conn.cursor()
    inserted = 0

    for sid, bundle in bundles.items():
        norm, _ = normalize_info(bundle.info, close=None)
        captured_at = bundle.fetched_at

        inst_pct = norm.get("inst_pct")
        insider_pct = norm.get("insider_pct")
        shares_out = norm.get("shares_out")

        cur.execute(
            """
            INSERT OR IGNORE INTO holdings (
                security_id, captured_at, inst_pct, insider_pct, shares_out, source
            ) VALUES (?, ?, ?, ?, ?, 'yahoo')
            """,
            (sid, captured_at, inst_pct, insider_pct, shares_out),
        )
        inserted += 1

    return Result(
        status="ok",
        counts={"rows": inserted},
        details={"message": f"Captured holdings for {inserted} securities"},
    )


def series(
    conn: sqlite3.Connection,
    cutoff: str,
    lag_runs: int,
    security_ids: List[int],
) -> pd.Series:
    """Return point-in-time institutional holdings series at given monthly lag.
    
    Choose at most one last capture per IST calendar month before cutoff.
    lag_runs=0 returns the latest month's capture before cutoff.
    lag_runs=3 returns the 4th distinct month's capture (requires 4 distinct months).
    Returns NaN if insufficient monthly captures exist.
    """
    if not security_ids:
        return pd.Series(dtype=float)

    cur = conn.cursor()
    out = {}
    ist_tz = ZoneInfo("Asia/Kolkata")

    for sid in security_ids:
        cur.execute(
            """
            SELECT captured_at, inst_pct, insider_pct
            FROM holdings
            WHERE security_id = ? AND captured_at <= ?
            ORDER BY captured_at DESC
            """,
            (sid, cutoff),
        )
        rows = cur.fetchall()

        if not rows:
            out[sid] = np.nan
            continue

        # Group by IST calendar month (YYYY-MM), taking the latest capture per month
        monthly_map: Dict[str, Any] = {}
        for r in rows:
            ts_str = r[0].strip()
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            try:
                dt_utc = datetime.fromisoformat(ts_str)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_ist = dt_utc.astimezone(ist_tz)
                m_key = dt_ist.strftime("%Y-%m")
            except Exception:
                m_key = r[0][:7]

            if m_key not in monthly_map:
                monthly_map[m_key] = r[1]  # inst_pct

        # Sort months descending
        sorted_months = sorted(monthly_map.keys(), reverse=True)

        if len(sorted_months) <= lag_runs:
            out[sid] = np.nan
        else:
            target_month = sorted_months[lag_runs]
            val = monthly_map[target_month]
            out[sid] = val if val is not None and not pd.isna(val) else np.nan

    return pd.Series(out, index=security_ids, name="inst_pct")
