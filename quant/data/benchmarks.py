"""Benchmark series updates, equal-weight aggregation and point-in-time access."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from quant.data.prices import PriceStore
from quant.run import RunContext
from quant.types import Result


def update(ctx: RunContext, through: str, observed_at: Optional[str] = None) -> Result:
    """Update benchmark monthly returns through given cutoff date."""
    # Ensure PriceStore is available
    if hasattr(ctx, "store") and ctx.store is not None:
        store = ctx.store
    else:
        store = PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)

    # Query all prices up to through
    with store.conn() as p_conn:
        df = pd.read_sql_query(
            """
            WITH ranked AS (
                SELECT security_id, date, close_raw, dividend_raw, split_ratio,
                       ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
                FROM prices_daily
                WHERE date <= ?
            )
            SELECT security_id, date, close_raw, dividend_raw, split_ratio
            FROM ranked
            WHERE rn = 1
            ORDER BY date ASC
            """,
            p_conn,
            params=(through,),
        )

    if df.empty:
        return Result(status="ok", counts={"rows": 0})

    dates = sorted(df["date"].unique())

    # Find month ends: last date in each month YYYY-MM
    df["month"] = df["date"].str[:7]
    month_ends = df.groupby("month")["date"].max().sort_values().tolist()

    if len(month_ends) < 1:
        return Result(status="ok", counts={"rows": 0})

    cur = ctx.conn.cursor()

    # 1. Base TRI on first month_end
    base_date = month_ends[0]
    base_tri = 100.0
    base_obs = observed_at or f"{base_date}T18:29:59.999999Z"
    ev_hash = hashlib.sha256(f"BM_NIFTY500_EW_{base_date}_{base_tri}".encode("utf-8")).hexdigest()

    cur.execute(
        """
        INSERT OR IGNORE INTO benchmarks_monthly (
            month_end, benchmark_id, tri, source, observed_at, evidence_hash, status
        ) VALUES (?, 'BM_NIFTY500_EW', ?, 'computed_ew', ?, ?, 'official')
        """,
        (base_date, base_tri, base_obs, ev_hash),
    )

    curr_tri = base_tri

    # Compute step-by-step for subsequent month-ends
    for i in range(1, len(month_ends)):
        d_prev = month_ends[i - 1]
        d_curr = month_ends[i]
        curr_obs = observed_at or f"{d_curr}T18:29:59.999999Z"

        df_prev = df[df["date"] == d_prev].set_index("security_id")
        df_curr = df[df["date"] == d_curr].set_index("security_id")

        common_sids = df_prev.index.intersection(df_curr.index)
        if len(common_sids) == 0:
            continue

        returns = []
        for sid in common_sids:
            p0 = df_prev.loc[sid, "close_raw"]
            p1 = df_curr.loc[sid, "close_raw"]
            div1 = df_curr.loc[sid, "dividend_raw"]
            split1 = df_curr.loc[sid, "split_ratio"]

            if p0 > 0:
                ret = split1 * (p1 + div1) / p0 - 1.0
                returns.append(ret)

        if returns:
            ew_ret = float(np.mean(returns))
            curr_tri = curr_tri * (1.0 + ew_ret)
            step_hash = hashlib.sha256(f"BM_NIFTY500_EW_{d_curr}_{curr_tri}".encode("utf-8")).hexdigest()

            cur.execute(
                """
                INSERT OR IGNORE INTO benchmarks_monthly (
                    month_end, benchmark_id, tri, source, observed_at, evidence_hash, status
                ) VALUES (?, 'BM_NIFTY500_EW', ?, 'computed_ew', ?, ?, 'official')
                """,
                (d_curr, curr_tri, curr_obs, step_hash),
            )

    return Result(
        status="ok",
        counts={"month_ends": len(month_ends)},
        details={"through": through},
    )


def series(
    conn: sqlite3.Connection,
    benchmark_id: str,
    start: str,
    end: str,
    known_at: str,
) -> pd.Series:
    """Query point-in-time benchmark TRI series."""
    cur = conn.cursor()
    cur.execute(
        """
        WITH ranked AS (
            SELECT month_end, tri,
                   ROW_NUMBER() OVER (PARTITION BY month_end ORDER BY observed_at DESC) as rn
            FROM benchmarks_monthly
            WHERE benchmark_id = ?
              AND month_end >= ?
              AND month_end <= ?
              AND observed_at <= ?
        )
        SELECT month_end, tri
        FROM ranked
        WHERE rn = 1
        ORDER BY month_end ASC
        """,
        (benchmark_id, start, end, known_at),
    )
    rows = cur.fetchall()

    if not rows:
        return pd.Series(dtype=float, name=benchmark_id)

    dates = [r[0] for r in rows]
    tris = [r[1] for r in rows]
    return pd.Series(data=tris, index=dates, name=benchmark_id)
