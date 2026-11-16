import numpy as np
import pandas as pd
import pytest

from quant.data.prices import PriceStore, normalize_source
from quant.errors import Refused


def test_normalize_source_split_adjusted_basis():
    # Golden case: source_basis
    # delivered_close: [100, 100], delivered_volume: [60, 60], splits: [1, 6]
    # volume_basis: 'split_adjusted'
    # expected_close_raw: [600, 100], expected_volume_raw: [10, 60]
    source_df = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"],
        "close": [100.0, 100.0],
        "volume": [60.0, 60.0],
        "split_ratio": [1.0, 6.0],
        "dividend": [0.0, 0.0],
    })
    meta = {
        "security_id": 1,
        "close_basis": "split_adjusted",
        "volume_basis": "split_adjusted",
        "dividend_basis": "delivered",
        "observed_at": "2026-09-02T18:29:59.999999Z",
        "capture_id": "cap_test",
        "source_sha256": "sha_test",
    }

    norm = normalize_source(source_df, meta)

    assert norm["close_raw"].tolist() == [600.0, 100.0]
    assert norm["volume_raw"].tolist() == [10.0, 60.0]


def test_unknown_basis_refuses():
    source_df = pd.DataFrame({
        "date": ["2026-09-01"],
        "close": [100.0],
        "volume": [60.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta = {
        "security_id": 1,
        "close_basis": "unsupported_magic_basis",
        "volume_basis": "split_adjusted",
        "dividend_basis": "delivered",
        "observed_at": "2026-09-01T18:29:59.999999Z",
        "capture_id": "cap_test",
        "source_sha256": "sha_test",
    }
    with pytest.raises(Refused):
        normalize_source(source_df, meta)


def test_price_store_init_and_ddl(tmp_path, ctx):
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    # Verify tables created from canonical price_schema.sql
    with store.conn() as p_conn:
        cur = p_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        assert "prices_daily" in tables
        assert "bm_symbols" in tables
        assert "prices_daily_quarantine" in tables
        assert "accepted_price_revisions" in tables


def test_tri_split_oracle(tmp_path, ctx):
    # Golden case: split (close_raw [600, 100], dividend_raw [0, 0], split_ratio [1, 6] -> TRI [100, 100])
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    source_df = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"],
        "close": [600.0, 100.0],
        "volume": [100.0, 600.0],
        "split_ratio": [1.0, 6.0],
        "dividend": [0.0, 0.0],
    })
    meta = {
        "security_id": 1,
        "close_basis": "raw",
        "volume_basis": "delivered",
        "dividend_basis": "delivered",
        "observed_at": "2026-09-02T18:29:59.999999Z",
        "capture_id": "cap_split",
        "source_sha256": "sha_split",
    }
    res = store.ingest(ctx, source_df, meta)
    assert res.status == "ok"

    tri_df = store.tri([1], start="2026-09-01", end="2026-09-02", vintage_at="2026-09-02T18:29:59.999999Z")
    assert tri_df[1].tolist() == [100.0, 100.0]


def test_tri_dividend_oracle(tmp_path, ctx):
    # Golden case: dividend (close_raw [100, 90], dividend_raw [0, 10], split_ratio [1, 1] -> TRI [100, 100])
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    source_df = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"],
        "close": [100.0, 90.0],
        "volume": [100.0, 100.0],
        "split_ratio": [1.0, 1.0],
        "dividend": [0.0, 10.0],
    })
    meta = {
        "security_id": 2,
        "close_basis": "raw",
        "volume_basis": "delivered",
        "dividend_basis": "delivered",
        "observed_at": "2026-09-02T18:29:59.999999Z",
        "capture_id": "cap_div",
        "source_sha256": "sha_div",
    }
    store.ingest(ctx, source_df, meta)

    tri_df = store.tri([2], start="2026-09-01", end="2026-09-02", vintage_at="2026-09-02T18:29:59.999999Z")
    assert tri_df[2].tolist() == [100.0, 100.0]


def test_tri_split_dividend_oracle(tmp_path, ctx):
    # Golden case: split_dividend (close_raw [600, 95], dividend_raw [0, 5], split_ratio [1, 6] -> TRI [100, 100])
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    source_df = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"],
        "close": [600.0, 95.0],
        "volume": [100.0, 600.0],
        "split_ratio": [1.0, 6.0],
        "dividend": [0.0, 5.0],
    })
    meta = {
        "security_id": 3,
        "close_basis": "raw",
        "volume_basis": "delivered",
        "dividend_basis": "delivered",
        "observed_at": "2026-09-02T18:29:59.999999Z",
        "capture_id": "cap_sdiv",
        "source_sha256": "sha_sdiv",
    }
    store.ingest(ctx, source_df, meta)

    tri_df = store.tri([3], start="2026-09-01", end="2026-09-02", vintage_at="2026-09-02T18:29:59.999999Z")
    assert tri_df[3].tolist() == [100.0, 100.0]
