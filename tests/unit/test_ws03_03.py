import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.attributes import at as attributes_at, capture as attributes_capture
from quant.data.capture import run as capture_run
from quant.data.holdings import capture as holdings_capture, series as holdings_series
from quant.data.yahoo import RawBundle, YahooClient
from quant.types import FrozenClock


def test_holdings_monthly_dedup_and_lag3(ctx):
    # Golden case: holdings
    # 4 distinct monthly captures:
    # 2026-06-01: 0.1
    # 2026-07-01: 0.2
    # 2026-08-01: 0.3
    # 2026-09-01: 0.4
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (10, 'INE010A01001', 'Holdings Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    timestamps = [
        "2026-06-01T10:00:00.000000Z",
        "2026-07-01T10:00:00.000000Z",
        "2026-08-01T10:00:00.000000Z",
        "2026-09-01T10:00:00.000000Z",
    ]
    values = [0.1, 0.2, 0.3, 0.4]

    for ts, val in zip(timestamps, values):
        b = RawBundle(
            ticker="HOLD.NS",
            fetched_at=ts,
            info={"heldPercentInstitutions": val},
            statements={},
            earnings_dates=pd.DataFrame(),
            errors=[],
        )
        res = holdings_capture(ctx, {10: b})
        assert res.status == "ok"

    cutoff = "2026-09-30T18:29:59.999999Z"

    # lag 0 is latest (September: 0.4)
    s0 = holdings_series(ctx.conn, cutoff=cutoff, lag_runs=0, security_ids=[10])
    assert s0[10] == 0.4

    # lag 3 is 4th month (June: 0.1)
    s3 = holdings_series(ctx.conn, cutoff=cutoff, lag_runs=3, security_ids=[10])
    assert s3[10] == 0.1

    # Expected change: latest (0.4) - lag 3 (0.1) = 0.3
    assert s0[10] - s3[10] == pytest.approx(0.3, abs=1e-5)


def test_holdings_three_captures_yields_nan_at_lag3(ctx):
    # Only 3 captures (July, August, September) -> lag_runs=3 must return NaN
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (11, 'INE011A01001', 'Three Captures Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    timestamps = [
        "2026-07-01T10:00:00.000000Z",
        "2026-08-01T10:00:00.000000Z",
        "2026-09-01T10:00:00.000000Z",
    ]
    values = [0.2, 0.3, 0.4]

    for ts, val in zip(timestamps, values):
        b = RawBundle(
            ticker="THREE.NS",
            fetched_at=ts,
            info={"heldPercentInstitutions": val},
            statements={},
            earnings_dates=pd.DataFrame(),
            errors=[],
        )
        holdings_capture(ctx, {11: b})

    cutoff = "2026-09-30T18:29:59.999999Z"
    s3 = holdings_series(ctx.conn, cutoff=cutoff, lag_runs=3, security_ids=[11])
    assert np.isnan(s3[11])


def test_same_month_latest_capture_wins(ctx):
    # In August 2026, two captures: Aug 05 (0.25) and Aug 25 (0.35)
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (12, 'INE012A01001', 'Multi Aug Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    for ts, val in [("2026-08-05T10:00:00.000000Z", 0.25), ("2026-08-25T10:00:00.000000Z", 0.35)]:
        b = RawBundle(
            ticker="AUG.NS",
            fetched_at=ts,
            info={"heldPercentInstitutions": val},
            statements={},
            earnings_dates=pd.DataFrame(),
            errors=[],
        )
        holdings_capture(ctx, {12: b})

    cutoff = "2026-08-31T18:29:59.999999Z"
    s = holdings_series(ctx.conn, cutoff=cutoff, lag_runs=0, security_ids=[12])
    # Latest in August wins
    assert s[12] == 0.35


def test_attributes_capture_and_sector_text_storage(ctx):
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (13, 'INE013A01001', 'Attr Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    bundle = RawBundle(
        ticker="ATTR.NS",
        fetched_at="2026-09-01T10:00:00.000000Z",
        info={
            "symbol": "ATTR.NS",
            "sector": "Healthcare",
            "industry": "Biotechnology",
            "marketCap": 50000000000.0,
            "trailingPE": 28.5,
        },
        statements={},
        earnings_dates=pd.DataFrame(),
        errors=[],
    )

    res = attributes_capture(ctx, {13: bundle})
    assert res.status == "ok"

    # Query attributes
    cutoff = "2026-09-30T18:29:59.999999Z"
    sec = attributes_at(ctx.conn, cutoff=cutoff, field="yahoo_sector", security_ids=[13])
    assert sec[13] == "Healthcare"

    pe = attributes_at(ctx.conn, cutoff=cutoff, field="trailing_pe", security_ids=[13])
    assert pe[13] == 28.5

    # PIT isolation: query before fetched_at yields NaN/None
    past_sec = attributes_at(ctx.conn, cutoff="2026-08-31T18:29:59.999999Z", field="yahoo_sector", security_ids=[13])
    assert past_sec[13] is None or pd.isna(past_sec[13])


def test_capture_run_orchestrator(ctx):
    cfg = load_config()
    clock = FrozenClock("2026-09-30T18:29:59.999999Z")

    # Insert security and symbol history
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (14, 'INE014A01001', 'Orch Corp', '2026-01-01', '2026-09-30', 'listed')"
    )
    ctx.conn.execute(
        "INSERT OR IGNORE INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, valid_to, source) "
        "VALUES (14, 'ORCH', 'ORCH.NS', '2026-01-01', NULL, 'manual')"
    )
    ctx.conn.execute(
        "INSERT OR IGNORE INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, series, source, source_sha256) "
        "VALUES ('2026-09-30', '2026-09-30T10:00:00.000000Z', 14, 'NIFTY500', 'ORCH', 'Information Technology', 'EQ', 'nse_csv', 'sha')"
    )

    client = YahooClient(cfg=cfg, clock=clock, sleep=lambda s: None)

    fake_bundle = RawBundle(
        ticker="ORCH.NS",
        fetched_at="2026-09-30T10:00:00.000000Z",
        info={"symbol": "ORCH.NS", "sector": "Technology", "industry": "Software"},
        statements={},
        earnings_dates=pd.DataFrame(),
        errors=[],
    )

    client.bundle = lambda t: fake_bundle
    client.download_batch = lambda tickers, start, end: pd.DataFrame()

    res = capture_run(ctx, client)
    assert res.status == "ok"
