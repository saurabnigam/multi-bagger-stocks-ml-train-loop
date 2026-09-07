import json
import sqlite3
import numpy as np
import pandas as pd
import pytest

from quant.model.models import definition_at, seed, score_all, check
from quant.run import RunContext
from quant.types import Draft


@pytest.fixture
def test_db_conn(tmp_path):
    conn = sqlite3.connect(":memory:")
    with open("docs/spec/contracts/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def test_seed_initializes_launch_models(test_db_conn, cfg):
    """Seed registers initial models: EW_HIER_v1 (champion), EW_FLAT_v1, MOM_ONLY_v1, IC_SHRUNK_v1."""
    ctx = RunContext(
        as_of="2026-09-01",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = test_db_conn
    res = seed(ctx, "BOOTSTRAP-001")
    assert res.status == "ok"

    # Verify models
    rows = dict(test_db_conn.execute("SELECT model_id, role FROM models").fetchall())
    assert rows["EW_HIER_v1"] == "champion"
    assert rows["EW_FLAT_v1"] == "reference"
    assert rows["MOM_ONLY_v1"] == "reference"
    assert rows["IC_SHRUNK_v1"] == "challenger"
    assert "SECTOR_OVERLAY_v1" not in rows

    # Verify versions
    v_rows = test_db_conn.execute("SELECT model_id, version FROM model_versions").fetchall()
    assert len(v_rows) == 4
    for mid, v in v_rows:
        assert v == 1


def test_definition_at_uses_frozen_versions(test_db_conn, cfg):
    """definition_at returns the frozen version active at as_of."""
    ctx = RunContext(
        as_of="2026-09-01",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = test_db_conn
    seed(ctx, "BOOTSTRAP-001")

    defn = definition_at(test_db_conn, "EW_HIER_v1", "2026-09-30")
    assert defn["model_id"] == "EW_HIER_v1"
    assert defn["version"] == 1
    assert defn["role"] == "champion"
    assert len(defn["factor_set"]) > 0

    # Add a newer version valid from 2027-01-01
    test_db_conn.execute("""
        INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from, note)
        VALUES ('EW_HIER_v1', 2, '[]', '{}', '2027-01-01', 'Future version')
    """)

    # as_of 2026-09-30 still returns version 1
    defn_old = definition_at(test_db_conn, "EW_HIER_v1", "2026-09-30")
    assert defn_old["version"] == 1

    # as_of 2027-01-15 returns version 2
    defn_new = definition_at(test_db_conn, "EW_HIER_v1", "2027-01-15")
    assert defn_new["version"] == 2


def test_score_all_and_idempotence(test_db_conn, cfg, spec_case):
    """score_all scores all initialized models; repeated calls produce identical outputs."""
    cfg_test = cfg
    cfg_test.standardise.min_families = 2
    cfg_test.standardise.min_group_nonnull = 1

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg_test,
        clock=None,
        actor=None,
    )
    ctx.conn = test_db_conn
    ctx.run_id = 1
    seed(ctx, "BOOTSTRAP-001")

    # Insert run and cohort rows to satisfy foreign keys
    test_db_conn.execute("""
        INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
        VALUES (1, '2026-09-30', 'test', 'live', 1, '2026-09-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')
    """)
    test_db_conn.execute("""
        INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id)
        VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T10:00:00.000000Z', 'def123', 'mem123', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, 1)
    """)
    # Insert securities
    for sid in range(1, 6):
        test_db_conn.execute("""
            INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, '2026-09-01', '2026-09-30', 'listed')
        """, (sid, f"INE{sid:09d}", f"Company {sid}"))

    sids = [1, 2, 3, 4, 5]
    members = pd.DataFrame({
        "security_id": sids,
        "isin": [f"INE{i:09d}" for i in sids],
        "symbol": [f"SYM{i}" for i in sids],
        "company_name": [f"Company {i}" for i in sids],
        "nse_sector": ["Industrials"] * 5,
        "series": ["EQ"] * 5,
        "adv_63_inr": [50_000_000] * 5,
        "pos_sessions_63": [63] * 5,
    }, index=sids)
    groups = pd.Series(["Industrials"] * 5, index=sids)

    # Factors: mom_12_1, trend_200, earn_yield, book_price
    factor_rows = []
    for sid in sids:
        for fid, val in [("mom_12_1", 0.5), ("trend_200", 0.4), ("earn_yield", 0.3), ("book_price", 0.2)]:
            factor_rows.append({
                "security_id": sid,
                "factor_id": fid,
                "z": val,
                "raw": val,
                "winsor": val,
                "sector_group": "Industrials",
                "flags": "",
                "track": "live",
            })
    factor_df = pd.DataFrame(factor_rows)

    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=factor_df,
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    # 12-month IC history matching weight_fit case
    c = spec_case("weight_fit")
    ic_hist = pd.DataFrame([c["mean_ic"]] * c["n_months"], columns=c["families"])

    scores1, weights1 = score_all(ctx, draft, ic_hist)
    scores2, weights2 = score_all(ctx, draft, ic_hist)

    assert len(scores1) > 0
    assert set(scores1["model_id"]) == {"EW_HIER_v1", "EW_FLAT_v1", "MOM_ONLY_v1", "IC_SHRUNK_v1"}
    pd.testing.assert_frame_equal(scores1, scores2)
    pd.testing.assert_frame_equal(weights1, weights2)

    # Check challenger role remains challenger
    defn_challenger = definition_at(test_db_conn, "IC_SHRUNK_v1", "2026-09-30")
    assert defn_challenger["role"] == "challenger"

    # Invariants check
    # Write weights to DB temporarily to run check()
    weights1.to_sql("model_weights", test_db_conn, if_exists="append", index=False)
    report = check(test_db_conn)
    assert report.passed


def test_valid_promoted_challenger_need_not_equal_ew(test_db_conn, cfg, spec_case):
    """When gate opens and IC history is informative, challenger weights deviate from equal weights."""
    c = spec_case("weight_fit")
    ic_hist = pd.DataFrame([c["mean_ic"]] * c["n_months"], columns=c["families"])

    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )
    ctx.conn = test_db_conn
    ctx.run_id = 1
    seed(ctx, "BOOTSTRAP-001")

    # Promotion via simulated Tier-2 decision
    test_db_conn.execute("UPDATE models SET role = 'reference' WHERE model_id = 'EW_HIER_v1'")
    test_db_conn.execute("UPDATE models SET role = 'champion' WHERE model_id = 'IC_SHRUNK_v1'")

    defn_promoted = definition_at(test_db_conn, "IC_SHRUNK_v1", "2026-09-30")
    assert defn_promoted["role"] == "champion"

    # Fit weights directly and check they differ from EW
    from quant.model.learn import fit_family_weights
    units, diag = fit_family_weights(ic_hist, cfg)
    assert diag["gate"] == "open"
    ew_units = spec_case("equal_weights")["expected_units"]
    assert [units[k] for k in c["families"]] != ew_units
    assert [units[k] for k in c["families"]] == c["expected_units"]
