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


def _naive_day(ts: pd.Timestamp) -> pd.Timestamp:
    """Vendor timestamps arrive tz-aware; compare calendar days in UTC without a tz."""
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


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
        # Only events with a reported EPS are publication evidence; scheduled (future)
        # dates in the vendor table are estimates and must not set availability.
        ed = earnings_dates
        if "Reported EPS" in ed.columns:
            ed = ed[ed["Reported EPS"].notna()]
        dt_end = _naive_day(pd.Timestamp(period_end))
        idx_dates = [_naive_day(d) for d in pd.to_datetime(ed.index, errors="coerce") if pd.notna(d)]
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
    skipped = 0
    unchanged = 0

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
        # MASTER_SPEC 4.4: "An unchanged subsequent fetch need not duplicate the fact; a
        # changed value inserts a version with its new timestamp." Latest version per fact
        # identity observed no later than this fetch:
        latest: Dict[tuple, float] = {}
        for row in cur.execute(
            """
            SELECT statement, freq, period_end, field, value FROM (
                SELECT statement, freq, period_end, field, value,
                       ROW_NUMBER() OVER (PARTITION BY statement, freq, period_end, field
                                          ORDER BY fetched_at DESC) AS rn
                FROM fundamentals WHERE security_id = ? AND fetched_at <= ?
            ) WHERE rn = 1
            """,
            (sid, fetched_at),
        ).fetchall():
            latest[(row[0], row[1], row[2], row[3])] = row[4]

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
                    if str(field_name) not in ingestible_fields():
                        skipped += 1
                        continue
                    if pd.isna(val):
                        continue
                    try:
                        f_val = float(val)
                    except (ValueError, TypeError):
                        continue

                    unit = "shares" if "shares" in str(field_name).lower() else "inr"
                    prev = latest.get((stmt_name, freq, period_end, str(field_name)))
                    if prev is not None and abs(float(prev) - f_val) <= 1e-9 * max(1.0, abs(f_val)):
                        unchanged += 1
                        continue

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
                    inserted += max(cur.rowcount, 0)
                    latest[(stmt_name, freq, period_end, str(field_name))] = f_val

    return Result(
        status="ok",
        counts={"rows": inserted, "skipped_uncontracted": skipped, "unchanged": unchanged},
        details={"message": f"Ingested {inserted} fundamental observations; "
                            f"{skipped} vendor line items outside the field contract left in the archive"},
    )


FIELD_ALIASES: Dict[str, List[str]] = {
    "ebit": ["EBIT", "Operating Income", "ebit_inr", "ebit"],
    "ebit_inr": ["EBIT", "Operating Income", "ebit_inr", "ebit"],
    "net_income": ["Net Income", "Net Income Common Stockholders", "net_income_inr", "net_income"],
    "net_income_inr": ["Net Income", "Net Income Common Stockholders", "net_income_inr", "net_income"],
    "revenue": ["Total Revenue", "Operating Revenue", "revenue_inr", "revenue"],
    "revenue_inr": ["Total Revenue", "Operating Revenue", "revenue_inr", "revenue"],
    "total_revenue": ["Total Revenue", "Operating Revenue", "revenue_inr", "revenue"],
    "ocf": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "ocf_inr", "ocf"],
    "ocf_inr": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "ocf_inr", "ocf"],
    "operating_cash_flow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "ocf_inr", "ocf"],
    "capex": ["Capital Expenditure", "Capital Expenditures", "capex_inr", "capex"],
    "capex_inr": ["Capital Expenditure", "Capital Expenditures", "capex_inr", "capex"],
    "capital_expenditure": ["Capital Expenditure", "Capital Expenditures", "capex_inr", "capex"],
    "total_assets": ["Total Assets", "total_assets_inr", "total_assets", "assets"],
    "total_assets_inr": ["Total Assets", "total_assets_inr", "total_assets", "assets"],
    "current_liabilities": ["Current Liabilities", "Total Current Liabilities", "current_liab_inr", "current_liab"],
    "current_liab_inr": ["Current Liabilities", "Total Current Liabilities", "current_liab_inr", "current_liab"],
    "total_debt": ["Total Debt", "total_debt_inr", "total_debt"],
    "total_debt_inr": ["Total Debt", "total_debt_inr", "total_debt"],
    "stockholders_equity": ["Stockholders Equity", "Total Stockholders Equity", "Total Equity", "Common Stock Equity", "total_equity_inr", "total_equity", "equity"],
    "total_equity_inr": ["Stockholders Equity", "Total Stockholders Equity", "Total Equity", "Common Stock Equity", "total_equity_inr", "total_equity", "equity"],
    "cash": ["Cash And Cash Equivalents", "Cash Financial", "cash_inr", "cash"],
    "cash_and_cash_equivalents": ["Cash And Cash Equivalents", "Cash Financial", "cash_inr", "cash"],
    "ebitda": ["EBITDA", "Normalized EBITDA", "ebitda"],
    "diluted_eps": ["Diluted EPS", "Basic EPS", "EPS", "eps"],
    "eps": ["Diluted EPS", "Basic EPS", "EPS", "eps"],
}


def ingestible_fields() -> set:
    """Vendor line items the state database stores (config/field_contracts_v1.json via FIELD_ALIASES).

    Only fields a registered factor can read through ``_expand_fields`` are persisted; the
    raw archive keeps every vendor line item, so widening the contract is a re-ingest, not a
    re-download. This keeps the state file within the spec's storage budget.
    """
    return {alias for aliases in FIELD_ALIASES.values() for alias in aliases}


def _expand_fields(field: str) -> List[str]:
    norm_key = field.lower().replace(" ", "_")
    aliases = FIELD_ALIASES.get(norm_key, [field])
    res = list(aliases)
    if field not in res:
        res.insert(0, field)
    return res


def _alias_rank(fields: List[str]) -> Tuple[str, tuple]:
    """SQL ranking of vendor aliases by contract priority, and its parameters.

    One vendor fetch usually carries several aliases for the same period (Diluted and Basic
    EPS, EBIT and Operating Income, Cash And Cash Equivalents and Cash Financial) with the
    same ``fetched_at`` and different values. Ordering by ``fetched_at`` alone left the choice
    to SQLite's sort order, so a factor could read Basic EPS for one security and Diluted for
    the next. The first alias in ``_expand_fields`` order wins a same-fetch tie.
    """
    case = "CASE field " + " ".join(f"WHEN ? THEN {i}" for i in range(len(fields))) + f" ELSE {len(fields)} END"
    return case, tuple(fields)


def pit_frame(
    conn: sqlite3.Connection,
    cutoff: str,
    statement: str,
    field: str,
    freq: str,
    n_periods: int,
    security_ids: List[int],
    with_dates: bool = False,
):
    """Return point-in-time fiscal frame for securities.

    Rows are security_ids, columns are period_rank 0..n-1 with newest admissible first.
    With ``with_dates`` the result is ``(values, period_ends)``, two frames of the same shape.
    """
    if not security_ids or n_periods <= 0:
        empty = pd.DataFrame(index=security_ids, columns=list(range(n_periods)))
        return (empty, empty.copy()) if with_dates else empty

    cur = conn.cursor()
    data = {sid: [np.nan] * n_periods for sid in security_ids}
    dates = {sid: [None] * n_periods for sid in security_ids}
    fields = _expand_fields(field)
    placeholders = ",".join("?" for _ in fields)
    rank_sql, rank_params = _alias_rank(fields)

    for sid in security_ids:
        # Choose the latest fetched version for each period_end admissible at cutoff;
        # within one fetch, the highest-priority alias
        query = f"""
        WITH ranked AS (
            SELECT period_end, value, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY period_end ORDER BY fetched_at DESC, {rank_sql}) as rn
            FROM fundamentals
            WHERE security_id = ?
              AND statement = ?
              AND field IN ({placeholders})
              AND freq = ?
              AND available_from <= ?
              AND fetched_at <= ?
        )
        SELECT period_end, value
        FROM ranked
        WHERE rn = 1
        ORDER BY period_end DESC
        LIMIT ?
        """
        cur.execute(query, (*rank_params, sid, statement, *fields, freq, cutoff, cutoff, n_periods))
        rows = cur.fetchall()
        for rank, r in enumerate(rows):
            data[sid][rank] = r[1]
            dates[sid][rank] = r[0]

    df = pd.DataFrame.from_dict(data, orient="index", columns=list(range(n_periods)))
    df.index.name = "security_id"
    if with_dates:
        dts = pd.DataFrame.from_dict(dates, orient="index", columns=list(range(n_periods)))
        dts.index.name = "security_id"
        return df, dts
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
    fields = _expand_fields(field)
    placeholders = ",".join("?" for _ in fields)
    rank_sql, rank_params = _alias_rank(fields)

    for sid in security_ids:
        query_q = f"""
        WITH ranked AS (
            SELECT period_end, value, field, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY period_end ORDER BY fetched_at DESC, {rank_sql}) as rn
            FROM fundamentals
            WHERE security_id = ?
              AND field IN ({placeholders})
              AND freq = 'Q'
              AND available_from <= ?
              AND fetched_at <= ?
        )
        SELECT period_end, value, field
        FROM ranked
        WHERE rn = 1
        ORDER BY period_end DESC
        LIMIT ?
        """
        cur.execute(query_q, (*rank_params, sid, *fields, cutoff, cutoff, req_periods))
        rows = cur.fetchall()

        if len(rows) < req_periods:
            # Not enough quarters
            if offset_quarters == 0:
                # Fallback to latest annual
                query_ann = f"""
                SELECT value FROM fundamentals
                WHERE security_id = ?
                  AND field IN ({placeholders})
                  AND freq = 'A'
                  AND available_from <= ?
                  AND fetched_at <= ?
                ORDER BY period_end DESC, fetched_at DESC, {rank_sql}
                LIMIT 1
                """
                cur.execute(query_ann, (sid, *fields, cutoff, cutoff, *rank_params))
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

        # Verify quarters are consecutive and one line item: a sum of three quarters of EBIT and
        # one of Operating Income is neither (2026-09 review: NETWEB, URBANCO) -> annual instead
        periods = [pd.Period(pd.to_datetime(d), freq="Q") for d in sub_dates]
        is_consecutive = all(getattr(periods[i] - periods[i + 1], "n", None) == 1 for i in range(3))
        is_consecutive = is_consecutive and len({r[2] for r in sub_rows}) == 1

        if is_consecutive:
            vals[sid] = float(sum(sub_values))
            flags[sid] = ""
        else:
            if offset_quarters == 0:
                # Fallback to latest annual
                query_ann = f"""
                SELECT value FROM fundamentals
                WHERE security_id = ?
                  AND field IN ({placeholders})
                  AND freq = 'A'
                  AND available_from <= ?
                  AND fetched_at <= ?
                ORDER BY period_end DESC, fetched_at DESC, {rank_sql}
                LIMIT 1
                """
                cur.execute(query_ann, (sid, *fields, cutoff, cutoff, *rank_params))
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
