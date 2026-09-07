"""Tests for WS08.01: Construction and cost arithmetic (C08)."""

import pandas as pd
import pytest

from quant.config import Config
from quant.portfolio.construct import rebalance
from quant.portfolio.costs import bucket, cost_bps_one_way


def test_cost_arithmetic_matches_golden_case(cfg, spec_case):
    """Cost arithmetic matches golden case: 0.10 weight B trade costs 0.00037, stress 0.000555."""
    c = spec_case("cost")
    b = c["bucket"]
    bps = cost_bps_one_way(b, cfg, stress=False)
    stress_bps = cost_bps_one_way(b, cfg, stress=True)

    assert abs(bps - c["one_way_bps"]) < 1e-12
    cost_fraction = c["weight_delta"] * bps / 10000.0
    stress_cost_fraction = c["weight_delta"] * stress_bps / 10000.0

    assert abs(cost_fraction - c["expected_cost_fraction"]) < 1e-15
    assert abs(stress_cost_fraction - c["expected_stress_cost_fraction"]) < 1e-15


def test_bucket_boundaries_inclusive(cfg):
    """Bucket boundaries are inclusive: 500m->A, 100m->B, 20m->C, below 20m->D."""
    assert bucket(500_000_000.0, cfg) == "A"
    assert bucket(600_000_000.0, cfg) == "A"
    assert bucket(499_999_999.0, cfg) == "B"
    assert bucket(100_000_000.0, cfg) == "B"
    assert bucket(99_999_999.0, cfg) == "C"
    assert bucket(20_000_000.0, cfg) == "C"
    assert bucket(19_999_999.0, cfg) == "D"
    assert bucket(0.0, cfg) == "D"


def test_top30_buffer_retains_rank45_removes_rank61(cfg):
    """TOP30 buffer retains existing holdings with rank <= 60 and removes rank > 60."""
    # Previous holdings: stock 10 (rank 45), stock 20 (rank 61)
    previous = pd.DataFrame([
        {"security_id": 10, "weight": 0.033, "entry_as_of": "2026-06-30", "group": "IND"},
        {"security_id": 20, "weight": 0.033, "entry_as_of": "2026-06-30", "group": "IND"},
    ])

    # 100 stocks universe
    sids = list(range(1, 101))
    ranks = pd.Series({s: s for s in sids})
    ranks[10] = 45
    ranks[20] = 61

    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({s: f"G{s % 10}" for s in sids})
    groups[10] = "IND"
    groups[20] = "IND"
    buckets = pd.Series({s: "A" for s in sids})

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    pos_sids = set(positions["security_id"])
    assert 10 in pos_sids  # rank 45 retained
    assert 20 not in pos_sids  # rank 61 removed

    # Entry date preserved for stock 10
    entry_10 = positions.loc[positions["security_id"] == 10, "entry_as_of"].iloc[0]
    assert entry_10 == "2026-06-30"

    # Exit delta generated for stock 20
    sell_20 = deltas[deltas["security_id"] == 20]
    assert len(sell_20) == 1
    assert sell_20["side"].iloc[0] == "SELL"


def test_sector_cap_skips_entrants(cfg):
    """Sector cap enforces at most 6 names per sector and skips entrants exceeding limit."""
    # 50 stocks, all in sector 'BANK' except stocks 40..50 in 'TECH'
    sids = list(range(1, 51))
    ranks = pd.Series({s: s for s in sids})
    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({s: "BANK" if s < 40 else "TECH" for s in sids})
    buckets = pd.Series({s: "A" for s in sids})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    # Sector BANK should have at most 6 names, even though ranks 1..39 are in BANK
    pos_df = positions.merge(groups.rename("group"), left_on="security_id", right_index=True)
    bank_count = len(pos_df[pos_df["group"] == "BANK"])
    assert bank_count == 6

    # Entrants from TECH should be selected even though ranked 40+
    tech_count = len(pos_df[pos_df["group"] == "TECH"])
    assert tech_count > 0


def test_bucket_c_cap_and_cash_residual(cfg):
    """Bucket C names capped at 2% (0.02) target weight; infeasible residual goes to cash."""
    sids = list(range(1, 31))
    ranks = pd.Series({s: s for s in sids})
    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({s: f"G{s}" for s in sids})
    # All 30 names are in bucket C
    buckets = pd.Series({s: "C" for s in sids})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    # All 30 stocks must be capped at <= 0.02
    for w in positions["target_weight"]:
        assert w <= 0.02 + 1e-12

    total_stock_weight = positions["target_weight"].sum()
    # 30 * 0.02 = 0.60, remaining 0.40 is cash residual
    assert abs(total_stock_weight - 0.60) < 1e-12


def test_ties_broken_by_security_id(cfg):
    """Ties in rank are broken deterministically by security_id ascending."""
    # Stocks 105 and 102 have identical rank 1
    sids = [105, 102]
    ranks = pd.Series({105: 1, 102: 1})
    eligible = pd.Series({105: True, 102: True})
    groups = pd.Series({105: "G1", 102: "G1"})
    buckets = pd.Series({105: "A", 102: "A"})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, _ = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    # 102 should precede 105
    assert list(positions["security_id"]) == [102, 105]
