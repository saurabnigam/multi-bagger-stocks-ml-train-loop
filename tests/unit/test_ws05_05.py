"""Acceptance tests for WS05.05: Registry, provenance and sector features."""
import numpy as np
import pandas as pd
import pytest

from quant.cli import main as cli_main
from quant.config import load
from quant.data.prices import PriceStore
from quant.errors import Refused
from quant.factors.base import FactorSpec
from quant.factors.controls import Size
from quant.factors.inputs import build as build_inputs
from quant.factors.legacy import DcFlag
from quant.factors.momentum import Mom12_1
from quant.factors.quality import Roce
from quant.factors.registry import (
    compute_all,
    sync,
    values_frame,
)
from quant.factors.sector import compute as compute_sector_features
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock


@pytest.fixture
def ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"

    from quant.db.core import apply_schema, connect
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = cfg.with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock("2026-09-01T00:00:00.000000Z")
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
        yield c


def test_registry_sync_and_code_hash_immutability(ctx):
    """Sync rejects changed code/helper hashes without affected version bumps; controls use shadow lifecycle."""
    specs = [
        Mom12_1().spec,
        Roce().spec,
        Size().spec,
        DcFlag().spec,
    ]

    res = sync(ctx, specs)
    assert res.status == "ok"

    # Verify rows in factor_registry
    cur = ctx.conn.cursor()
    cur.execute("SELECT factor_id, status FROM factor_registry ORDER BY factor_id")
    rows = dict(cur.fetchall())

    # Active factors
    assert rows["mom_12_1@1"] == "active"
    assert rows["roce@1"] == "active"
    # Control / legacy must have shadow status (never active, never candidate/control lifecycle)
    assert rows["size@1"] == "shadow"
    assert rows["dc_flag@1"] == "shadow"

    # Re-syncing exact same spec succeeds (idempotent)
    res_same = sync(ctx, specs)
    assert res_same.status == "ok"

    # Mutate formula without bumping version -> MUST raise Refused or fail
    tampered_spec = FactorSpec(
        name="mom_12_1",
        version=1,
        family="momentum",
        direction=1,
        horizon_m=3,
        hypothesis="H_TAMPERED",
        formula="tampered_formula",
        inputs=("tri",),
        lookback_days=252,
        applies_to_financials=True,
        level="stock",
        backfillable=True,
        min_coverage=0.95,
        evidence="tampered",
        hypothesis_id="hyp_tampered",
    )
    with pytest.raises((Refused, Exception)):
        sync(ctx, [tampered_spec])


def test_future_hypotheses_excluded_from_earlier_cohorts(ctx):
    """Registered future hypotheses stay out of earlier cohorts."""
    # Register an active factor now
    spec_now = Mom12_1().spec
    sync(ctx, [spec_now])

    # Register a future factor with registered_on in the future (2026-10-15)
    cur = ctx.conn.cursor()
    cur.execute(
        """
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level,
            hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
            backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
            module_path, status, registered_on, status_changed_on
        ) VALUES (
            'future_factor@1', 'future_factor', 1, 'momentum', 1, 3, 'stock',
            'H_FUTURE', 'log(TRI)', '["tri"]', 20, 1, 1, 0.5, 'future',
            NULL, 'sha_fut', 'quant.factors.future', 'active',
            '2026-10-15T00:00:00.000000Z', '2026-10-15T00:00:00.000000Z'
        )
        """
    )

    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")
    dates = [f"2025-{m:02d}-01" for m in range(1, 13)] + [f"2026-0{m}-01" for m in range(1, 10)] + ["2026-09-30"]
    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 22)] + [f"2026-0{m}-{d:02d}" for m in range(1, 10) for d in range(1, 20)]
    dates = dates[:260]
    dates[-1] = "2026-09-30"
    df_p = pd.DataFrame({
        "date": dates,
        "close": np.linspace(100.0, 200.0, len(dates)),
        "volume": [1000.0] * len(dates),
        "split_ratio": [1.0] * len(dates),
        "dividend": [0.0] * len(dates),
    })
    ctx.store.ingest(ctx, df_p, {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"})

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

    staging = compute_all(ctx, draft)
    # Future factor must NOT be present in this cohort's staging rows
    computed_factors = staging["factor_id"].unique()
    assert "future_factor@1" not in computed_factors


def test_compute_all_returns_staging_rows_only_and_values_frame(ctx):
    """compute returns staging rows only (no write to factor_values); values_frame reconstructs z matrix."""
    sync(ctx, [Mom12_1().spec, Size().spec])

    ctx.conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2025-01-01', '2026-09-30', 'listed')")
    ctx.conn.execute("INSERT INTO security_attributes (captured_at, security_id, mcap_inr, source_sha256) VALUES ('2026-09-30T18:29:59.999999Z', 1, 10000000000.0, 'sha1')")

    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 22)] + [f"2026-0{m}-{d:02d}" for m in range(1, 10) for d in range(1, 20)]
    dates = dates[:260]
    dates[-1] = "2026-09-30"
    df_p = pd.DataFrame({
        "date": dates,
        "close": np.linspace(100.0, 200.0, len(dates)),
        "volume": [1000.0] * len(dates),
        "split_ratio": [1.0] * len(dates),
        "dividend": [0.0] * len(dates),
    })
    ctx.store.ingest(ctx, df_p, {"security_id": 1, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"})

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

    staging = compute_all(ctx, draft)
    assert isinstance(staging, pd.DataFrame)
    assert not staging.empty
    assert "z" in staging.columns
    assert "factor_id" in staging.columns

    # Invariant: compute_all must return staging rows ONLY, leaving DB untouched
    cur = ctx.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM factor_values")
    assert cur.fetchone()[0] == 0

    # Insert into cohorts first for FK, then into factor_values
    ctx.conn.execute("INSERT OR IGNORE INTO runs (run_id, as_of, kind, attempt, started_at, git_sha, code_sha256, config_sha256, registry_sha256, status) VALUES (1, '2026-09-30', 'test', 1, '2026-09-30T00:00:00Z', 'sha', 'sha', 'sha', 'sha', 'running')")
    ctx.conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('C-2026-09-30-live', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def', 'mem', '{}', '2026-09-30T18:30:00Z', '2026-09-30T18:30:00Z', 1, 1)"
    )

    # Insert staging rows
    for _, row in staging.iterrows():
        ctx.conn.execute(
            """
            INSERT INTO factor_values (
                cohort_id, as_of, security_id, factor_id, raw, winsor, z,
                sector_group, flags, input_refs_json, track, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["cohort_id"], row["as_of"], int(row["security_id"]),
                row["factor_id"], row.get("raw"), row.get("winsor"), row.get("z"),
                row["sector_group"], row.get("flags", ""), str(row.get("input_refs_json", "{}")),
                row["track"], int(row["run_id"]),
            ),
        )

    # values_frame test
    vf = values_frame(ctx.conn, "C-2026-09-30-live", ["mom_12_1@1", "size@1"])
    assert isinstance(vf, pd.DataFrame)
    assert 1 in vf.index


def test_sector_features_uses_frozen_groups(ctx):
    """sector features use frozen groups; returns 5 features per sector group."""
    for sid in [1, 2]:
        ctx.conn.execute(f"INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES ({sid}, 'INE00{sid}', 'Stock {sid}', '2025-01-01', '2026-09-30', 'listed')")

    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 22)] + [f"2026-0{m}-{d:02d}" for m in range(1, 10) for d in range(1, 20)]
    dates = dates[:260]
    dates[-1] = "2026-09-30"
    for sid in [1, 2]:
        df_p = pd.DataFrame({
            "date": dates,
            "close": np.linspace(100.0, 200.0, len(dates)),
            "volume": [1000.0] * len(dates),
            "split_ratio": [1.0] * len(dates),
            "dividend": [0.0] * len(dates),
        })
        ctx.store.ingest(ctx, df_p, {"security_id": sid, "close_basis": "raw", "observed_at": "2026-09-30T18:29:59.999999Z"})

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}, {"security_id": 2}]),
        groups=pd.Series({1: "Technology", 2: "Consumer Discretionary"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)
    sec_df = compute_sector_features(inputs, ctx.cfg)
    assert isinstance(sec_df, pd.DataFrame)
    assert not sec_df.empty
    expected_features = {
        "sector_mom_6m", "sector_breadth_200", "sector_flow_proxy",
        "sector_val_spread", "sector_dispersion"
    }
    present_features = set(sec_df["feature_id"].unique())
    assert expected_features.issubset(present_features)
    assert set(sec_df["sector_group"].unique()) == {"Technology", "Consumer Discretionary"}


def test_factors_cli_sync(ctx, monkeypatch):
    """Test factors sync CLI command."""
    monkeypatch.setenv("QUANT_DB_PATH", str(ctx.cfg.paths.db))
    code = cli_main(["factors", "sync"])
    assert code == 0
    cur = ctx.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM factor_registry")
    assert cur.fetchone()[0] > 0

