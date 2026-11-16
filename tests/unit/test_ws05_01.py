"""Acceptance tests for WS05.01: Restricted FactorInputs and Factor base class."""
import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.prices import PriceStore
from quant.errors import LookaheadError
from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs, build as build_inputs
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock


@pytest.fixture
def ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    
    from quant.db.core import apply_schema, connect
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    
    prices_path = tmp_path / "prices.sqlite"
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
        yield c


def test_factor_spec_factor_id():
    """FactorSpec formats factor_id as name@version."""
    spec = FactorSpec(
        name="mom_12_1",
        version=1,
        family="momentum",
        direction=1,
        horizon_m=3,
        hypothesis="H_MOM",
        formula="log(TRI[-21]/TRI[-252])",
        inputs=("tri",),
        lookback_days=252,
        applies_to_financials=True,
        level="stock",
        backfillable=True,
        min_coverage=0.95,
        evidence="prior research",
        hypothesis_id="hyp_001",
    )
    assert spec.factor_id == "mom_12_1@1"


def test_factor_inputs_exposes_no_connection_or_labels(ctx):
    """FactorInputs physically exposes no database connection, network client or label frames."""
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 10}]),
        groups=pd.Series({10: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    assert not hasattr(inputs, "conn")
    assert not hasattr(inputs, "connection")
    assert not hasattr(inputs, "client")
    assert not hasattr(inputs, "labels")
    assert not hasattr(inputs, "_conn")


def test_undeclared_or_future_field_raises_lookahead(ctx):
    """Accessing an undeclared field or future parameter raises LookaheadError."""
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 10}]),
        groups=pd.Series({10: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    with pytest.raises(LookaheadError):
        inputs.attribute("non_existent_future_field")

    with pytest.raises(LookaheadError):
        # Negative lag_runs (future) raises LookaheadError
        inputs.holdings(lag_runs=-1)


def test_pit_cutoff_golden_case(tmp_path, ctx):
    """Revisions observed after knowledge_cutoff are ignored; old as_of gets same pre-revision data."""
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)
    ctx.store = store

    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (10, 'INE010A01001', 'PIT Stock', '2026-01-01', '2026-09-30', 'listed')"
    )

    # Ingest baseline observation before cutoff: price = 100.0 observed 2026-09-29
    df_prior = pd.DataFrame({
        "date": ["2026-09-29"],
        "close": [100.0],
        "volume": [1000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta_prior = {
        "security_id": 10,
        "close_basis": "raw",
        "observed_at": "2026-09-29T12:00:00.000000Z",
        "capture_id": "cap_prior",
        "source_sha256": "sha_prior",
    }
    store.ingest(ctx, df_prior, meta_prior)

    # Ingest future revised observation: price = 150.0 observed 2026-10-01 (after cutoff)
    df_later = pd.DataFrame({
        "date": ["2026-09-29"],
        "close": [150.0],
        "volume": [1000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta_later = {
        "security_id": 10,
        "close_basis": "raw",
        "observed_at": "2026-10-01T00:00:00.000000Z",
        "capture_id": "cap_later",
        "source_sha256": "sha_later",
    }
    store.ingest(ctx, df_later, meta_later)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 10}]),
        groups=pd.Series({10: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    closes = inputs.close_raw(lookback_days=5)
    # The value on 2026-09-29 must be 100.0, NOT the post-cutoff revision 150.0
    assert closes.loc["2026-09-29", 10] == 100.0

    prov = inputs.provenance()
    assert isinstance(prov, dict)
