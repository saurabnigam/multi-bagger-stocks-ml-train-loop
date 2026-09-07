"""Bitemporal fundamentals, point-in-time fiscal frames and TTM logic."""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from quant.data.calendar import Calendar
from quant.data.yahoo import RawBundle
from quant.run import RunContext
from quant.types import Result


def available_from(
    period_end: str,
    freq: str,
    fetched_at: str,
    earnings_dates: pd.DataFrame,
    calendar: Calendar,
) -> Tuple[str, str]:
    """Calculate point-in-time availability and its basis.
    
    Estimated publication date is earnings report date + one trading day when a matching
    reported-EPS event exists; otherwise quarterly period end +45 calendar days followed
    by next session, annual/Q4 +60 followed by next session.
    Live availability is always max(estimated_publication, actual fetched_at).
    """
    days = 45 if freq == "Q" else 60
    basis = "lodr_45d" if freq == "Q" else "lodr_60d"

    est_date = None
    if earnings_dates is not None and not earnings_dates.empty:
        # Check if any earnings date falls after period_end
        dt_end = pd.to_datetime(period_end)
        idx_dates = pd.to_datetime(earnings_dates.index)
        after_dates = [d for d in idx_dates if d > dt_end and (d - dt_end).days <= 90]
        if after_dates:
            rep_date = min(after_dates).strftime("%Y-%m-%d")
            try:
                est_date = calendar.next_session_after(rep_date)
                basis = "earnings_date"
            except Exception:
                est_date = None

    if est_date is None:
        dt_end = pd.to_datetime(period_end)
        target_day = (dt_end + pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        est_date = calendar.next_session_after(target_day)

    est_ts = calendar._close_ats.get(est_date, f"{est_date}T18:29:59.999999Z")

    # Invariant: engine can NEVER see data before fetched_at
    if fetched_at > est_ts:
        return fetched_at, "first_fetch"
    return est_ts, basis


def ingest(ctx: RunContext, bundles: Dict[int, RawBundle]) -> Result:
    """Ingest raw bundles into bitemporal fundamentals table."""
    # Build or get calendar
    if hasattr(ctx, "calendar") and ctx.calendar is not None:
        cal = ctx.calendar
    else:
        dates = pd.bdate_range("2020-01-01", "2030-12-31")
        sessions = pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "close_at": [f"{d.strftime('%Y-%m-%d')}T18:29:59.999999Z" for d in dates],
        })
        cal = Calendar(sessions)

    cur = ctx.conn.cursor()
    inserted = 0

    stmt_map = [
        ("income_stmt", "income", "A"),
        ("quarterly_income_stmt", "income", "Q"),
        ("balance_sheet", "balance", "A"),
        ("quarterly_balance_sheet", "balance", "Q"),
        ("cashflow", "cashflow", "A"),
        ("quarterly_cashflow", "cashflow", "Q"),
    ]

    for sid, bundle in bundles.items():
        fetched_at = bundle.fetched_at

        for attr, stmt_name, freq in stmt_map:
            df = bundle.statements.get(attr)
            if df is None or df.empty:
                continue

            for col in df.columns:
                # Column is period_end date
                try:
                    period_end = pd.to_datetime(col).strftime("%Y-%m-%d")
                except Exception:
                    period_end = str(col)[:10]

                avail, basis = available_from(
                    period_end=period_end,
                    freq=freq,
                    fetched_at=fetched_at,
                    earnings_dates=bundle.earnings_dates,
                    calendar=cal,
                )

                for field_name, val in df[col].items():
                    if pd.isna(val):
                        continue
                    try:
                        f_val = float(val)
                    except (ValueError, TypeError):
                        continue

                    unit = "shares" if "shares" in str(field_name).lower() else "inr"

                    cur.execute(
                        """
                        INSERT OR IGNORE INTO fundamentals (
                            security_id, statement, freq, period_end, field,
                            value, unit, available_from, available_from_basis,
                            fetched_at, source, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo', ?)
                        """,
                        (
                            sid,
                            stmt_name,
                            freq,
                            period_end,
                            str(field_name),
                            f_val,
                            unit,
                            avail,
                            basis,
                            fetched_at,
                            ctx.run_id,
                        )
                    )
                    inserted += 1

    return Result(
        status="ok",
        counts={"rows": inserted},
        details={"message": f"Ingested {inserted} fundamental observations"},
    )


def pit_frame(
    conn: sqlite3.Connection,
    cutoff: str,
    statement: str,
    field: str,
    freq: str,
    n_periods: int,
    security_ids: List[int],
) -> pd.DataFrame:
    """Return point-in-time fiscal frame for securities.
    
    Rows are security_ids, columns are period_rank 0..n-1 with newest admissible first.
    """
    if not security_ids or n_periods <= 0:
        return pd.DataFrame(index=security_ids, columns=list(range(n_periods)))

    cur = conn.cursor()
    data = {sid: [np.nan] * n_periods for sid in security_ids}

    for sid in security_ids:
        # Choose the latest fetched version for each period_end admissible at cutoff
        cur.execute(
            """
            WITH ranked AS (
                SELECT period_end, value, fetched_at,
                       ROW_NUMBER() OVER (PARTITION BY period_end ORDER BY fetched_at DESC) as rn
                FROM fundamentals
                WHERE security_id = ?
                  AND statement = ?
                  AND field = ?
                  AND freq = ?
                  AND available_from <= ?
                  AND fetched_at <= ?
            )
            SELECT period_end, value
            FROM ranked
            WHERE rn = 1
            ORDER BY period_end DESC
            LIMIT ?
            """,
            (sid, statement, field, freq, cutoff, cutoff, n_periods),
        )
        rows = cur.fetchall()
        for rank, r in enumerate(rows):
            data[sid][rank] = r[1]

    df = pd.DataFrame.from_dict(data, orient="index", columns=list(range(n_periods)))
    df.index.name = "security_id"
    return df


def ttm(
    conn: sqlite3.Connection,
    cutoff: str,
    field: str,
    security_ids: List[int],
    offset_quarters: int = 0,
) -> Tuple[pd.Series, pd.Series]:
    """Compute Trailing Twelve Months (TTM) sum over consecutive quarters.
    
    offset_quarters=0 sums quarters [0..3] (or falls back to latest annual).
    offset_quarters=4 sums quarters [4..7] (requires 8 consecutive quarters).
    Returns (values_series, flags_series).
    """
    cur = conn.cursor()
    vals = {}
    flags = {}

    req_periods = offset_quarters + 4

    for sid in security_ids:
        cur.execute(
            """
            WITH ranked AS (
                SELECT period_end, value, fetched_at,
                       ROW_NUMBER() OVER (PARTITION BY period_end ORDER BY fetched_at DESC) as rn
                FROM fundamentals
                WHERE security_id = ?
                  AND field = ?
                  AND freq = 'Q'
                  AND available_from <= ?
                  AND fetched_at <= ?
            )
            SELECT period_end, value
            FROM ranked
            WHERE rn = 1
            ORDER BY period_end DESC
            LIMIT ?
            """,
            (sid, field, cutoff, cutoff, req_periods),
        )
        rows = cur.fetchall()

        if len(rows) < req_periods:
            # Not enough quarters
            if offset_quarters == 0:
                # Fallback to latest annual
                cur.execute(
                    """
                    SELECT value FROM fundamentals
                    WHERE security_id = ?
                      AND field = ?
                      AND freq = 'A'
                      AND available_from <= ?
                      AND fetched_at <= ?
                    ORDER BY period_end DESC, fetched_at DESC
                    LIMIT 1
                    """,
                    (sid, field, cutoff, cutoff),
                )
                ann_row = cur.fetchone()
                if ann_row:
                    vals[sid] = ann_row[0]
                    flags[sid] = "ttm_from_annual"
                else:
                    vals[sid] = np.nan
                    flags[sid] = "missing_quarters"
            else:
                vals[sid] = np.nan
                flags[sid] = "missing_quarters"
            continue

        # Extract target 4 quarters
        sub_rows = rows[offset_quarters : offset_quarters + 4]
        sub_dates = [r[0] for r in sub_rows]
        sub_values = [r[1] for r in sub_rows]

        # Verify quarters are consecutive
        periods = [pd.Period(pd.to_datetime(d), freq="Q") for d in sub_dates]
        is_consecutive = all(getattr(periods[i] - periods[i + 1], "n", None) == 1 for i in range(3))

        if is_consecutive:
            vals[sid] = float(sum(sub_values))
            flags[sid] = ""
        else:
            if offset_quarters == 0:
                # Fallback to latest annual
                cur.execute(
                    """
                    SELECT value FROM fundamentals
                    WHERE security_id = ?
                      AND field = ?
                      AND freq = 'A'
                      AND available_from <= ?
                      AND fetched_at <= ?
                    ORDER BY period_end DESC, fetched_at DESC
                    LIMIT 1
                    """,
                    (sid, field, cutoff, cutoff),
                )
                ann_row = cur.fetchone()
                if ann_row:
                    vals[sid] = ann_row[0]
                    flags[sid] = "ttm_from_annual"
                else:
                    vals[sid] = np.nan
                    flags[sid] = "missing_quarters"
            else:
                vals[sid] = np.nan
                flags[sid] = "missing_quarters"

    return pd.Series(vals, index=security_ids), pd.Series(flags, index=security_ids)
