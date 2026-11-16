from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import pandas as pd
import pytest

from quant.data.universe import fetch_list, parse_list, capture, members_at
from quant.errors import Blocked, Refused


def make_sample_csv(n: int = 500, duplicate_isin: bool = False, duplicate_symbol: bool = False) -> bytes:
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    # Include special symbols
    rows.append("Mahindra & Mahindra Ltd.,Automobile,M&M,EQ,INE123A01001")
    rows.append("Bajaj Auto Ltd.,Automobile,BAJAJ-AUTO,EQ,INE123A01002")

    for i in range(3, n + 1):
        isin = "INE123A01001" if (duplicate_isin and i == 3) else f"INE123A{i:05d}"
        symbol = "M&M" if (duplicate_symbol and i == 3) else f"SYM{i}"
        rows.append(f"Company {i},Financial Services,{symbol},EQ,{isin}")

    return "\n".join(rows).encode("utf-8")


def test_parse_list_valid_and_special_characters():
    csv_bytes = make_sample_csv(500)
    df = parse_list(csv_bytes)
    assert len(df) == 500
    assert "M&M" in df["symbol"].values
    assert "BAJAJ-AUTO" in df["symbol"].values
    assert "Automobile" in df["nse_sector"].values


def test_parse_list_rejects_duplicates_and_missing_headers():
    # Duplicate ISIN
    csv_dup_isin = make_sample_csv(500, duplicate_isin=True)
    with pytest.raises((ValueError, Refused)):
        parse_list(csv_dup_isin)

    # Duplicate Symbol
    csv_dup_sym = make_sample_csv(500, duplicate_symbol=True)
    with pytest.raises((ValueError, Refused)):
        parse_list(csv_dup_sym)

    # Missing required header
    bad_csv = b"Company Name,Symbol,Series,ISIN Code\nFoo,FOO,EQ,INE123\n"
    with pytest.raises(ValueError):
        parse_list(bad_csv)


def test_capture_and_members_at(ctx, monkeypatch):
    csv_bytes = make_sample_csv(500)
    capture_time = "2026-09-30T12:00:00.000000Z"

    # Mock fetch_list to return synthetic CSV
    def mock_fetch(name, cfg, clock):
        return csv_bytes, {
            "url": "https://niftyindices.com/test.csv",
            "captured_at": capture_time,
            "source_version": "nifty_v1",
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        }

    monkeypatch.setattr("quant.data.universe.fetch_list", mock_fetch)

    # Run capture
    res = capture(ctx)
    assert res.status == "ok"
    assert res.counts["members"] == 500

    # Retrieve members_at cutoff
    df_members = members_at(ctx.conn, ctx.as_of)
    assert len(df_members) == 500
    assert "M&M" in df_members["symbol"].values


def test_members_at_stale_rejection(ctx, monkeypatch):
    csv_bytes = make_sample_csv(500)

    # Capture with clock set to 70 days before cutoff
    old_time = "2026-06-01T00:00:00.000000Z"
    ctx.clock._dt = datetime.fromisoformat("2026-06-01T00:00:00+00:00")

    def mock_fetch(name, cfg, clock):
        return csv_bytes, {
            "url": "https://niftyindices.com/test.csv",
            "captured_at": old_time,
            "source_version": "nifty_v1",
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        }

    monkeypatch.setattr("quant.data.universe.fetch_list", mock_fetch)
    capture(ctx)

    # Checking members at 2026-09-30 (> 62 days later) should raise Blocked(stale_universe)
    with pytest.raises(Blocked) as excinfo:
        members_at(ctx.conn, "2026-09-30")
    assert excinfo.value.code == "stale_universe"
