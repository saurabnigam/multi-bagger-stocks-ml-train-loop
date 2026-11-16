import json
import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.benchmarks import update as benchmarks_update, series as benchmarks_series
from quant.data.prices import PriceStore, monthly_panel
from quant.types import Draft, FrozenClock


def test_gross_ew_equals_constituent_gross_returns(tmp_path, ctx):
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)
    ctx.store = store

    # 2 securities: stock 1 (+10%), stock 2 (+20%)
    # Expected EW gross return = (10% + 20%) / 2 = 15%
    for sid, (c1, c2) in [(1, (100.0, 110.0)), (2, (100.0, 120.0))]:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, 'Name', '2026-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}")
        )
        df = pd.DataFrame({
            "date": ["2026-08-31", "2026-09-30"],
            "close": [c1, c2],
            "volume": [1000.0, 1000.0],
            "split_ratio": [1.0, 1.0],
            "dividend": [0.0, 0.0],
        })
        meta = {
            "security_id": sid,
            "close_basis": "raw",
            "observed_at": "2026-09-30T18:29:59.999999Z",
            "capture_id": "cap_bm",
            "source_sha256": "sha_bm",
        }
        store.ingest(ctx, df, meta)

    # Update benchmarks through 2026-09-30
    res = benchmarks_update(ctx, through="2026-09-30")
    assert res.status == "ok"

    s_ew = benchmarks_series(ctx.conn, benchmark_id="BM_NIFTY500_EW", start="2026-08-31", end="2026-09-30", known_at="2026-09-30T18:29:59.999999Z")
    assert len(s_ew) == 2
    # Base 100 on 2026-08-31, then +15% -> 115.0 on 2026-09-30
    assert s_ew["2026-08-31"] == 100.0
    assert s_ew["2026-09-30"] == pytest.approx(115.0, abs=1e-3)


def test_missing_optional_index_is_unavailable(ctx):
    # Nonexistent benchmark
    s = benchmarks_series(ctx.conn, benchmark_id="BM_NONEXISTENT", start="2026-08-31", end="2026-09-30", known_at="2026-09-30T18:29:59.999999Z")
    assert s.empty


def test_monthly_panel_build(tmp_path, ctx):
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)
    ctx.store = store

    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (10, 'INE010A01001', 'Panel Stock', '2026-01-01', '2026-09-30', 'listed')"
    )
    df = pd.DataFrame({
        "date": ["2026-09-30"],
        "close": [250.0],
        "volume": [4000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta = {
        "security_id": 10,
        "close_basis": "raw",
        "observed_at": "2026-09-30T18:29:59.999999Z",
        "capture_id": "cap_panel",
        "source_sha256": "sha_panel",
    }
    store.ingest(ctx, df, meta)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 10}]),
        groups=pd.Series({10: "Technology"}),
        source_refs={"price_manifest_sha": "sha123"},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    panel = monthly_panel(ctx, draft)
    assert len(panel) == 1
    row = panel.iloc[0]
    assert row["security_id"] == 10
    assert row["close_raw"] == 250.0
    assert row["adv_63_inr"] == pytest.approx(250.0 * 4000.0)


def test_manifest_write_and_verify(tmp_path, ctx):
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    df = pd.DataFrame({
        "date": ["2026-09-30"],
        "close": [100.0],
        "volume": [100.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta = {
        "security_id": 1,
        "close_basis": "raw",
        "observed_at": "2026-09-30T18:29:59.999999Z",
        "capture_id": "cap_man",
        "source_sha256": "sha_man",
    }
    store.ingest(ctx, df, meta)

    manifest_file = tmp_path / "manifest.json"
    h = store.manifest_write(manifest_file, vintage_at="2026-09-30T18:29:59.999999Z")
    assert len(h) == 64

    # Verify manifest passes
    chk = store.manifest_verify(manifest_file)
    assert chk.status == "PASS"

    # Simulate changed re-download: tamper with database
    with store.conn() as p_conn:
        p_conn.execute("UPDATE prices_daily SET close_raw = 999.0 WHERE security_id = 1")

    # Manifest verification must FAIL on mismatch
    chk_fail = store.manifest_verify(manifest_file)
    assert chk_fail.status == "FAIL"
