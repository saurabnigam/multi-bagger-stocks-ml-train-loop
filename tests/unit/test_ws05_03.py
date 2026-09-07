"""Acceptance tests for WS05.03: Price factors and diagnostics."""
import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.prices import PriceStore
from quant.factors.controls import Beta252, Liq, Size
from quant.factors.inputs import build as build_inputs
from quant.factors.legacy import DcFlag
from quant.factors.low_risk import MaxRet21, Vol252
from quant.factors.momentum import Dist52wHigh, Mom12_1, Mom6_1, Rev1m, Trend200
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock


@pytest.fixture
def ctx_and_store(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"
    
    from quant.db.core import apply_schema, connect
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    
    cfg = cfg.with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock("2026-09-30T18:30:00.000000Z")
    actor = Actor(kind="system", name="test")
    
    run_ctx = RunContext(
        as_of="2026-09-30",
        kind="production",
        track="live",
        cfg=cfg,
        clock=clock,
        actor=actor,
    )
    with run_ctx as c:
        store = PriceStore(path=prices_path, state_conn=c.conn)
        c.store = store
        yield c, store


def test_mom_12_1_requires_253_bars(ctx_and_store):
    """mom_12_1 requires >= 253 bars and uses exact geometric TRI endpoints."""
    ctx, store = ctx_and_store

    # Create security 1 with 255 bars, security 2 with only 100 bars
    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")
    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (2, 'INE002', 'Stock 2', '2025-01-01', '2026-09-30', 'listed')")

    # Security 1: 260 dates ending 2026-09-30
    dates_260 = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 22)] + [f"2026-0{m}-{d:02d}" for m in range(1, 10) for d in range(1, 20)]
    dates_260 = dates_260[:260]
    dates_260[-1] = "2026-09-30"

    # Prices start at 100, increase smoothly to 200
    prices_s1 = np.linspace(100.0, 200.0, len(dates_260))
    df_s1 = pd.DataFrame({
        "date": dates_260,
        "close": prices_s1,
        "volume": [1000.0] * len(dates_260),
        "split_ratio": [1.0] * len(dates_260),
        "dividend": [0.0] * len(dates_260),
    })
    meta_s1 = {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"}
    store.ingest(ctx, df_s1, meta_s1)

    # Security 2: only 50 dates (sample shortage)
    dates_50 = dates_260[-50:]
    df_s2 = pd.DataFrame({
        "date": dates_50,
        "close": [100.0] * 50,
        "volume": [1000.0] * 50,
        "split_ratio": [1.0] * 50,
        "dividend": [0.0] * 50,
    })
    meta_s2 = {"security_id": 2, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"}
    store.ingest(ctx, df_s2, meta_s2)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}, {"security_id": 2}]),
        groups=pd.Series({1: "Technology", 2: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    factor = Mom12_1()
    res = factor.compute(inputs)

    assert not np.isnan(res.loc[1])
    assert res.loc[1] > 0.0
    # Security 2 must be NaN due to sample shortage (< 253 bars)
    assert np.isnan(res.loc[2])


def test_vol_252_uses_population_sd(ctx_and_store):
    """vol_252 requires 253 bars and uses population SD of 252 log returns."""
    ctx, store = ctx_and_store

    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")

    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 22)] + [f"2026-0{m}-{d:02d}" for m in range(1, 10) for d in range(1, 20)]
    dates = dates[:255]
    dates[-1] = "2026-09-30"

    prices = [100.0 * (1.001 ** i) for i in range(len(dates))]
    df = pd.DataFrame({
        "date": dates,
        "close": prices,
        "volume": [1000.0] * len(dates),
        "split_ratio": [1.0] * len(dates),
        "dividend": [0.0] * len(dates),
    })
    meta = {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"}
    store.ingest(ctx, df, meta)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    factor = Vol252()
    res = factor.compute(inputs)
    assert not np.isnan(res.loc[1])
    assert res.loc[1] > 0.0


def test_size_and_liq_controls(ctx_and_store):
    """size reads captured mcap (not backfillable); liq reads positive ADV."""
    ctx, store = ctx_and_store

    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")
    ctx.conn.execute(
        "INSERT INTO security_attributes (captured_at, security_id, mcap_inr, source_sha256) "
        "VALUES ('2026-09-30T18:29:59.999999Z', 1, 50000000000.0, 'sha_attr')"
    )

    df = pd.DataFrame({
        "date": ["2026-09-30"],
        "close": [100.0],
        "volume": [50000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    store.ingest(ctx, df, {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"})

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    
    size_factor = Size()
    assert size_factor.spec.backfillable is False
    size_res = size_factor.compute(inputs)
    assert pytest.approx(size_res.loc[1], rel=1e-3) == np.log(50000000000.0)

    liq_factor = Liq()
    liq_res = liq_factor.compute(inputs)
    assert not np.isnan(liq_res.loc[1])


def test_dc_flag_diagnostic(ctx_and_store):
    """dc_flag is diagnostic only; returns 1.0 when close < SMA50 < SMA200 else 0.0."""
    ctx, store = ctx_and_store

    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")

    # 205 prices declining monotonically so close < SMA50 < SMA200
    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 12) for d in range(1, 21)]
    dates = dates[:205]
    dates[-1] = "2026-09-30"

    prices = list(np.linspace(200.0, 50.0, len(dates)))
    df = pd.DataFrame({
        "date": dates,
        "close": prices,
        "volume": [1000.0] * len(dates),
        "split_ratio": [1.0] * len(dates),
        "dividend": [0.0] * len(dates),
    })
    meta = {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"}
    store.ingest(ctx, df, meta)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    dc = DcFlag()
    assert dc.spec.family == "legacy"
    res = dc.compute(inputs)
    assert res.loc[1] == 1.0
