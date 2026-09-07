import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from quant.config import load as load_config
from quant.types import FrozenClock
from quant.data.yahoo import YahooClient, RawBundle, normalize_info


class MockTicker:
    """Mock yfinance Ticker for offline testing."""
    def __init__(self, ticker_str, info=None, fail_429_count=0):
        self.ticker = ticker_str
        self._info = info or {
            "symbol": ticker_str,
            "shortName": f"Company {ticker_str}",
            "debtToEquity": 357.0,
            "dividendRate": 75.0,
            "dividendYield": 1.41,  # Should be ignored
            "heldPercentInstitutions": 0.25,
            "heldPercentInsiders": 0.55,
            "returnOnEquity": None,  # Should stay None
        }
        self.fail_429_count = fail_429_count
        self.calls = 0

    @property
    def info(self):
        self.calls += 1
        if self.calls <= self.fail_429_count:
            raise RuntimeError("429 Client Error: Too Many Requests")
        return self._info

    @property
    def income_stmt(self):
        return pd.DataFrame({"2026-03-31": [1000, 200]}, index=["Total Revenue", "Net Income"])

    @property
    def quarterly_income_stmt(self):
        return pd.DataFrame({"2026-06-30": [250, 50]}, index=["Total Revenue", "Net Income"])

    @property
    def balance_sheet(self):
        return pd.DataFrame({"2026-03-31": [5000, 1000]}, index=["Total Assets", "Total Debt"])

    @property
    def quarterly_balance_sheet(self):
        return pd.DataFrame({"2026-06-30": [5200, 1050]}, index=["Total Assets", "Total Debt"])

    @property
    def cashflow(self):
        return pd.DataFrame({"2026-03-31": [300, -100]}, index=["Operating Cash Flow", "Capital Expenditure"])

    @property
    def quarterly_cashflow(self):
        return pd.DataFrame({"2026-06-30": [75, -25]}, index=["Operating Cash Flow", "Capital Expenditure"])

    @property
    def earnings_dates(self):
        return pd.DataFrame({"Reported EPS": [12.5]}, index=pd.to_datetime(["2026-05-15"]))


def test_throttling_and_429_retry():
    cfg = load_config()
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    sleeps = []

    def mock_sleep(s):
        sleeps.append(s)

    client = YahooClient(cfg=cfg, clock=clock, sleep=mock_sleep)

    mock_ticker = MockTicker("TCS.NS", fail_429_count=2)

    with patch("quant.data.yahoo.yf.Ticker", return_value=mock_ticker):
        bundle = client.bundle("TCS.NS")

    # Verify bundle structure
    assert isinstance(bundle, RawBundle)
    assert bundle.ticker == "TCS.NS"
    assert bundle.fetched_at == "2026-10-01T00:00:00.000000Z"
    assert len(bundle.errors) == 0

    # Verify 429 retry triggered 120s sleeps (twice)
    assert sleeps.count(120.0) == 2
    # Verify accessor sleep >= 0.5s was called
    accessor_sleeps = [s for s in sleeps if s >= 0.5 and s != 120.0]
    assert len(accessor_sleeps) >= 1


def test_download_batch_throttling_and_params():
    cfg = load_config()
    clock = FrozenClock("2026-10-01T00:00:00.000000Z")
    sleeps = []

    def mock_sleep(s):
        sleeps.append(s)

    client = YahooClient(cfg=cfg, clock=clock, sleep=mock_sleep)

    # 30 tickers with batch_size 25 -> 2 batches
    tickers = [f"T{i}.NS" for i in range(30)]

    mock_df = pd.DataFrame(
        {("T0.NS", "Close"): [100.0, 105.0]},
        index=pd.to_datetime(["2026-09-01", "2026-09-02"])
    )

    with patch("quant.data.yahoo.yf.download", return_value=mock_df) as mock_download:
        df = client.download_batch(tickers, start="2026-09-01", end="2026-09-30")
        
        # Verify yf.download call arguments: threads=False
        assert mock_download.call_count == 2
        for call_args in mock_download.call_args_list:
            _, kwargs = call_args
            assert kwargs.get("threads") is False

    # Batch sleep >= 1.0s between batches
    batch_sleeps = [s for s in sleeps if s >= 1.0 and s != 120.0]
    assert len(batch_sleeps) >= 1


def test_normalize_info_units_and_missing_values():
    raw_info = {
        "symbol": "HEROMOTOCO.NS",
        "debtToEquity": 357.0,  # 357% -> 3.57
        "dividendRate": 75.0,   # 75 with price 5300 -> 75/5300
        "dividendYield": 1.41,  # Must be ignored!
        "heldPercentInstitutions": 0.25,
        "heldPercentInsiders": 0.55,
        "returnOnEquity": None, # Must stay None
        "trailingPE": 22.4,
    }

    norm, flags = normalize_info(raw_info, close=5300.0)

    # debtToEquity normalized
    assert norm["debt_to_equity"] == pytest.approx(3.57, abs=1e-3)

    # dividendRate / price normalized
    expected_div_yield = 75.0 / 5300.0
    assert norm["dividend_yield"] == pytest.approx(expected_div_yield, abs=1e-5)
    assert norm["dividend_rate_inr"] == 75.0

    # Institutions / Insiders
    assert norm["inst_pct"] == 0.25
    assert norm["insider_pct"] == 0.55

    # None stays missing
    assert norm["return_on_equity"] is None
    assert norm.get("trailing_pe") == 22.4


def test_archive_bundles_and_captures_record(ctx):
    cfg = load_config()
    clock = ctx.clock
    client = YahooClient(cfg=cfg, clock=clock, sleep=lambda s: None)

    mock_bundle = RawBundle(
        ticker="INFY.NS",
        fetched_at=clock.iso(),
        info={"symbol": "INFY.NS", "debtToEquity": 10.0},
        statements={
            "income_stmt": pd.DataFrame({"2026-03-31": [1000]}, index=["Total Revenue"]),
        },
        earnings_dates=pd.DataFrame(),
        errors=[],
    )

    capture_id = client.archive([mock_bundle], ctx)
    assert capture_id.startswith("cap_")

    # Verify capture row in DB
    cur = ctx.conn.cursor()
    cur.execute("SELECT capture_id, kind, run_id, sha256 FROM captures WHERE capture_id = ?", (capture_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[1] == "yahoo_bundle"
    assert row[2] == ctx.run_id
    assert len(row[3]) == 64  # valid sha256
