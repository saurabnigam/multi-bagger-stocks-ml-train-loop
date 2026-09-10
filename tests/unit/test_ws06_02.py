import json
import numpy as np
import pandas as pd
import pytest

from quant.model.composite import compose
from quant.model.screens import apply as apply_screens
from quant.run import RunContext
from quant.types import Draft


def test_flat_equals_finite_factor_mean_and_differs_from_hierarchical(cfg):
    """Flat mode averages all finite active factors directly and differs from hierarchical."""
    cfg.standardise.min_group_nonnull = 1
    # 2 families: Quality (3 factors), Value (1 factor)
    definitions = pd.DataFrame([
        {"factor_id": "q1", "family": "quality", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "q2", "family": "quality", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "q3", "family": "quality", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "v1", "family": "value", "status_weight": 1.0, "nonfinancial": False},
    ])
    # Units: 50% Quality, 50% Value
    units = {"quality": 5000, "value": 5000}
    # 5 stocks in group A to satisfy standardisation
    sids = [1, 2, 3, 4, 5]
    groups = pd.Series(["A"] * 5, index=sids)

    # Stock 1 has q1=1.0, q2=1.0, q3=1.0, v1=0.0
    z_data = {
        "q1": [1.0, 0.5, -0.5, 0.0, 0.2],
        "q2": [1.0, 0.5, -0.5, 0.0, 0.2],
        "q3": [1.0, 0.5, -0.5, 0.0, 0.2],
        "v1": [0.0, -0.5, 0.5, 0.0, -0.2],
    }
    z_df = pd.DataFrame(z_data, index=sids)

    # In flat mode, raw composite for stock 1 = (1 + 1 + 1 + 0) / 4 = 0.75
    # For testing raw composite before neutralization, we check the composite column
    import copy
    cfg_flat = copy.deepcopy(cfg)
    cfg_flat.standardise.min_families = 2
    res_flat = compose(z_df, definitions, units, groups, cfg_flat, mode="flat")
    assert abs(res_flat.loc[1, "composite"] - 0.75) < 1e-6

    # In hierarchical mode:
    # Quality family = (1 + 1 + 1) / 3 = 1.0
    # Value family = 0.0
    # Weighted by units (5000/5000) = 0.5 * 1.0 + 0.5 * 0.0 = 0.50
    res_hier = compose(z_df, definitions, units, groups, cfg_flat, mode="hierarchical")
    assert abs(res_hier.loc[1, "composite"] - 0.50) < 1e-6
    assert abs(res_flat.loc[1, "composite"] - res_hier.loc[1, "composite"]) > 0.20


def test_probation_has_half_status_weight(cfg):
    """Probation factors receive 0.5 status weight vs 1.0 for active."""
    import copy
    cfg_test = copy.deepcopy(cfg)
    cfg_test.standardise.min_families = 1
    cfg_test.standardise.min_group_nonnull = 1

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "m2", "family": "momentum", "status_weight": 0.5, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    sids = [1, 2, 3, 4, 5]
    groups = pd.Series(["A"] * 5, index=sids)

    # Stock 1: m1 = 1.0 (active, wt 1.0), m2 = 0.0 (probation, wt 0.5)
    # Expected weighted mean = (1.0*1.0 + 0.5*0.0) / (1.0 + 0.5) = 1.0 / 1.5 = 2/3
    z_df = pd.DataFrame({
        "m1": [1.0, 0.0, -1.0, 0.5, -0.5],
        "m2": [0.0, 0.0, 0.0, 0.0, 0.0],
    }, index=sids)

    res = compose(z_df, definitions, units, groups, cfg_test, mode="hierarchical")
    assert abs(res.loc[1, "composite"] - (2.0 / 3.0)) < 1e-6
    f_scores = json.loads(res.loc[1, "family_scores_json"])
    assert abs(f_scores["momentum"] - (2.0 / 3.0)) < 1e-6


def test_financial_non_applicability_excluded_from_denominator(cfg):
    """Structural non-applicability (nonfinancial factors for Financial Services) is excluded from coverage denominator."""
    import copy
    cfg_test = copy.deepcopy(cfg)
    cfg_test.standardise.min_families = 3
    cfg_test.standardise.min_factor_share = 0.60
    cfg_test.standardise.min_group_nonnull = 1

    # 10 factors across 4 families; 3 factors are nonfinancial
    definitions = pd.DataFrame([
        {"factor_id": "roce", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "accruals", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "cash_conv", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "earn_yield", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "book_price", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "eps_grow", "family": "growth", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "earn_mom", "family": "growth", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "mom12", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "trend200", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "vol252", "family": "low_risk", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"quality": 2000, "value": 2000, "growth": 2000, "momentum": 2000, "low_risk": 2000}

    # Stock 1: Financial Services; has 5 finite factors out of 7 applicable (71.4% >= 60%) across 3 families (value, growth, momentum)
    # Stock 2: Industrials (nonfinancial); has same 5 finite factors out of 10 applicable (50% < 60%)
    # Stock 6: a second Financial Services name with a different composite value. Its
    # sector group must have >1 distinct finite composite to be neutralized at all
    # (MASTER_SPEC 5.2: a single-member group is trivially "constant" and returns NaN;
    # composite.py no longer falls back to the un-neutralized value for such a group).
    sids = [1, 2, 3, 4, 5, 6]
    groups = pd.Series(
        ["Financial Services", "Industrials", "Industrials", "Industrials", "Industrials", "Financial Services"],
        index=sids,
    )

    # 5 finite factors: earn_yield, book_price, eps_grow, mom12, trend200
    z_data = {
        "roce": [np.nan, np.nan, 0.1, 0.2, 0.3, np.nan],
        "accruals": [np.nan, np.nan, 0.1, 0.2, 0.3, np.nan],
        "cash_conv": [np.nan, np.nan, 0.1, 0.2, 0.3, np.nan],
        "earn_yield": [0.5, 0.5, 0.1, 0.2, 0.3, 0.3],
        "book_price": [0.5, 0.5, 0.1, 0.2, 0.3, 0.3],
        "eps_grow": [0.5, 0.5, 0.1, 0.2, 0.3, 0.3],
        "earn_mom": [np.nan, np.nan, 0.1, 0.2, 0.3, np.nan],
        "mom12": [0.5, 0.5, 0.1, 0.2, 0.3, 0.3],
        "trend200": [0.5, 0.5, 0.1, 0.2, 0.3, 0.3],
        "vol252": [np.nan, np.nan, 0.1, 0.2, 0.3, np.nan],
    }
    z_df = pd.DataFrame(z_data, index=sids)

    res = compose(z_df, definitions, units, groups, cfg_test, mode="hierarchical")

    # Stock 1 (Financial Services): 5 finite / 7 applicable = 71.4% >= 60%, 3 families present -> scored
    assert res.loc[1, "scored"] == 1
    assert pd.isna(res.loc[1, "exclusion_reason"]) or res.loc[1, "exclusion_reason"] is None

    # Stock 2 (Industrials): 5 finite / 10 applicable = 50% < 60% -> unscored due to coverage
    assert res.loc[2, "scored"] == 0
    assert res.loc[2, "exclusion_reason"] == "coverage"


def test_mom_only_may_score_from_one_family(cfg):
    """MOM_ONLY model requires only 1 family (momentum) and >= 1 factor present."""
    definitions = pd.DataFrame([
        {"factor_id": "mom_12_1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "trend_200", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "roce", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "earn_yield", "family": "value", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    sids = [1, 2, 3, 4, 5]
    groups = pd.Series(["A"] * 5, index=sids)

    # Stock 1 has ONLY mom_12_1 finite, all other factors are NaN
    z_df = pd.DataFrame({
        "mom_12_1": [0.8, 0.4, -0.2, 0.1, -0.5],
        "trend_200": [np.nan, 0.3, -0.1, 0.0, -0.4],
        "roce": [np.nan, 0.2, 0.1, 0.0, -0.2],
        "earn_yield": [np.nan, 0.1, 0.0, -0.1, -0.3],
    }, index=sids)

    # Under standard hierarchical mode, stock 1 fails (1 family < 3)
    res_hier = compose(z_df, definitions, units, groups, cfg, mode="hierarchical")
    assert res_hier.loc[1, "scored"] == 0
    assert res_hier.loc[1, "exclusion_reason"] == "coverage"

    # Under mom_only mode, stock 1 scores
    res_mom = compose(z_df, definitions, units, groups, cfg, mode="mom_only")
    assert res_mom.loc[1, "scored"] == 1
    assert abs(res_mom.loc[1, "composite"] - 0.8) < 1e-6


def test_illiquid_names_remain_stored_but_ineligible(cfg):
    """Illiquid securities remain in scores table with scored=1 but eligible=0."""
    sids = [1, 2, 3, 4, 5]
    groups = pd.Series(["A"] * 5, index=sids)
    members = pd.DataFrame({
        "security_id": sids,
        "isin": [f"INE{i:09d}" for i in sids],
        "symbol": [f"SYM{i}" for i in sids],
        "company_name": [f"Co {i}" for i in sids],
        "nse_sector": ["Sector A"] * 5,
        "series": ["EQ"] * 5,
        "adv_63_inr": [50_000_000, 5_000_000, 30_000_000, 40_000_000, 60_000_000],  # sid 2 is illiquid (< 20M)
        "pos_sessions_63": [63, 60, 63, 63, 63],
    }, index=sids)

    scores_initial = pd.DataFrame({
        "security_id": sids,
        "family_scores_json": ["{}"] * 5,
        "composite": [1.0, 0.9, 0.8, 0.7, 0.6],
        "composite_neutral": [1.0, 0.9, 0.8, 0.7, 0.6],
        "sector_tilt": [0.0] * 5,
        "final": [1.0, 0.9, 0.8, 0.7, 0.6],
        "scored": [1, 1, 1, 1, 1],
        "n_factors_used": [5, 5, 5, 5, 5],
        "rank_all": [1, 2, 3, 4, 5],
        "rank": [1, 2, 3, 4, 5],
        "rank_group": [1, 2, 3, 4, 5],
        "decile": [10, 8, 6, 4, 2],
        "quintile": [5, 4, 3, 2, 1],
        "eligible": [1, 1, 1, 1, 1],
        "exclusion_reason": [None] * 5,
    }, index=sids)

    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=scores_initial,
    )
    ctx = RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=None,
        actor=None,
    )

    scores_screened = apply_screens(ctx, draft, scores_initial, "EW_HIER_v1")

    # sid 2 is still present in scores table with scored=1
    assert 2 in scores_screened.index
    assert scores_screened.loc[2, "scored"] == 1
    assert scores_screened.loc[2, "final"] == 0.9

    # But sid 2 is ineligible due to liquidity
    assert scores_screened.loc[2, "eligible"] == 0
    assert scores_screened.loc[2, "exclusion_reason"] == "illiquid"
    assert pd.isna(scores_screened.loc[2, "rank"])

    # Liquid stocks are eligible and ranked
    assert scores_screened.loc[1, "eligible"] == 1
    assert scores_screened.loc[1, "rank"] == 1
    assert scores_screened.loc[3, "eligible"] == 1
    assert scores_screened.loc[3, "rank"] == 2  # Takes rank 2 since sid 2 was screened out
