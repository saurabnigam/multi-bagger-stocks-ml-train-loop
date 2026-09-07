import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.calendar import Calendar
from quant.data.fundamentals import (
    available_from,
    ingest,
    pit_frame,
    ttm,
)
from quant.data.yahoo import RawBundle
from quant.types import FrozenClock


def make_test_calendar():
    dates = pd.bdate_range("2024-01-01", "2027-12-31")
    sessions = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "close_at": [f"{d.strftime('%Y-%m-%d')}T10:00:00.000000Z" for d in dates],
    })
    return Calendar(sessions)


def test_available_from_lags_and_calendar():
    cal = make_test_calendar()

    # Quarterly: period_end 2026-06-30 + 45 days -> 2026-08-14, next session 2026-08-17
    # Case: fetched_at before estimated publication
    avail, basis = available_from(
        period_end="2026-06-30",
        freq="Q",
        fetched_at="2026-07-01T00:00:00.000000Z",
        earnings_dates=pd.DataFrame(),
        calendar=cal,
    )
    assert avail.startswith("2026-08-17")
    assert basis == "lodr_45d"

    # Annual: period_end 2026-03-31 + 60 days -> 2026-05-30, next session 2026-06-01
    avail_ann, basis_ann = available_from(
        period_end="2026-03-31",
        freq="A",
        fetched_at="2026-04-01T00:00:00.000000Z",
        earnings_dates=pd.DataFrame(),
        calendar=cal,
    )
    assert avail_ann.startswith("2026-06-01")
    assert basis_ann == "lodr_60d"

    # Invariant: fetched_at after estimated publication -> availability can NEVER precede fetched_at!
    avail_late, basis_late = available_from(
        period_end="2026-06-30",
        freq="Q",
        fetched_at="2026-09-01T12:00:00.000000Z",
        earnings_dates=pd.DataFrame(),
        calendar=cal,
    )
    assert avail_late == "2026-09-01T12:00:00.000000Z"
    assert basis_late == "first_fetch"


def test_annual_and_quarterly_coexist_at_one_fiscal_end(ctx):
    # Golden case: annual_quarterly
    # At period_end 2026-03-31: Annual Net Income is 100, Q4 Net Income is 25
    bundle = RawBundle(
        ticker="COEXIST.NS",
        fetched_at="2026-08-01T12:00:00.000000Z",
        info={"symbol": "COEXIST.NS"},
        statements={
            "income_stmt": pd.DataFrame({"2026-03-31": [100.0]}, index=["Net Income"]),
            "quarterly_income_stmt": pd.DataFrame({"2026-03-31": [25.0]}, index=["Net Income"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )

    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'INE001A01001', 'Coexist Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    res = ingest(ctx, {1: bundle})
    assert res.status == "ok"

    # Query pit_frame for Annual
    df_ann = pit_frame(
        conn=ctx.conn,
        cutoff="2026-09-30T18:29:59.999999Z",
        statement="income",
        field="Net Income",
        freq="A",
        n_periods=1,
        security_ids=[1],
    )
    assert df_ann.loc[1, 0] == 100.0

    # Query pit_frame for Quarterly
    df_qtr = pit_frame(
        conn=ctx.conn,
        cutoff="2026-09-30T18:29:59.999999Z",
        statement="income",
        field="Net Income",
        freq="Q",
        n_periods=1,
        security_ids=[1],
    )
    assert df_qtr.loc[1, 0] == 25.0


def test_pit_cutoff_and_revision_isolation(ctx):
    # Golden case: pit_cutoff
    # Prior fetch on 2026-09-29 has 100
    # Later fetch on 2026-10-01 has revision 150
    # Cutoff at 2026-09-30 must see 100, revision 150 must be invisible!
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (2, 'INE002A01001', 'Revision Corp', '2026-01-01', '2026-10-31', 'listed')"
    )

    b1 = RawBundle(
        ticker="REV.NS",
        fetched_at="2026-09-29T12:00:00.000000Z",
        info={},
        statements={
            "income_stmt": pd.DataFrame({"2026-03-31": [100.0]}, index=["Total Revenue"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )
    ingest(ctx, {2: b1})

    b2 = RawBundle(
        ticker="REV.NS",
        fetched_at="2026-10-01T00:00:00.000000Z",
        info={},
        statements={
            "income_stmt": pd.DataFrame({"2026-03-31": [150.0]}, index=["Total Revenue"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )
    ingest(ctx, {2: b2})

    # Query at cutoff 2026-09-30T18:29:59.999999Z
    df_pit = pit_frame(
        conn=ctx.conn,
        cutoff="2026-09-30T18:29:59.999999Z",
        statement="income",
        field="Total Revenue",
        freq="A",
        n_periods=1,
        security_ids=[2],
    )
    assert df_pit.loc[2, 0] == 100.0

    # Query after later fetch at cutoff 2026-10-02
    df_rev = pit_frame(
        conn=ctx.conn,
        cutoff="2026-10-02T00:00:00.000000Z",
        statement="income",
        field="Total Revenue",
        freq="A",
        n_periods=1,
        security_ids=[2],
    )
    assert df_rev.loc[2, 0] == 150.0


def test_ttm_eight_quarters_and_offset4(ctx):
    # 8 consecutive quarters for security 3: Q1..Q8 (values 10, 20, 30, 40, 50, 60, 70, 80)
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (3, 'INE003A01001', 'TTM Corp', '2024-01-01', '2026-09-30', 'listed')"
    )

    q_dates = [
        "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
        "2025-06-30", "2025-03-31", "2024-12-31", "2024-09-30"
    ]
    q_values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]

    b = RawBundle(
        ticker="TTM.NS",
        fetched_at="2026-07-01T00:00:00.000000Z",
        info={},
        statements={
            "quarterly_income_stmt": pd.DataFrame({d: [v] for d, v in zip(q_dates, q_values)}, index=["Net Income"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )
    ingest(ctx, {3: b})

    # TTM at offset 0 (sum of first 4 quarters: 10 + 20 + 30 + 40 = 100)
    vals_0, flags_0 = ttm(ctx.conn, cutoff="2026-09-30T18:29:59.999999Z", field="Net Income", security_ids=[3], offset_quarters=0)
    assert vals_0[3] == 100.0
    assert flags_0[3] == ""

    # TTM at offset 4 (sum of quarters 4..7: 50 + 60 + 70 + 80 = 260)
    vals_4, flags_4 = ttm(ctx.conn, cutoff="2026-09-30T18:29:59.999999Z", field="Net Income", security_ids=[3], offset_quarters=4)
    assert vals_4[3] == 260.0
    assert flags_4[3] == ""


def test_ttm_missing_earlier_quarters_yields_nan(ctx):
    # Security 4 has only 6 quarters -> offset 4 must yield NaN!
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (4, 'INE004A01001', 'Partial Corp', '2024-01-01', '2026-09-30', 'listed')"
    )

    q_dates = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
    q_values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]

    b = RawBundle(
        ticker="PART.NS",
        fetched_at="2026-07-01T00:00:00.000000Z",
        info={},
        statements={
            "quarterly_income_stmt": pd.DataFrame({d: [v] for d, v in zip(q_dates, q_values)}, index=["Net Income"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )
    ingest(ctx, {4: b})

    # Offset 0 succeeds (10 + 20 + 30 + 40 = 100)
    vals_0, _ = ttm(ctx.conn, cutoff="2026-09-30T18:29:59.999999Z", field="Net Income", security_ids=[4], offset_quarters=0)
    assert vals_0[4] == 100.0

    # Offset 4 fails with NaN because quarters 6 and 7 are missing!
    vals_4, flags_4 = ttm(ctx.conn, cutoff="2026-09-30T18:29:59.999999Z", field="Net Income", security_ids=[4], offset_quarters=4)
    assert np.isnan(vals_4[4])
    assert "missing_quarters" in flags_4[4]
