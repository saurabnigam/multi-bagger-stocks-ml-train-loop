"""Tests for WS07.02: Statistical functions and oriented metrics (C07)."""

import math
import numpy as np
import pandas as pd
import pytest

from quant.evaluation.metrics import partial_ic, quintiles, rank_ic
from quant.evaluation.stats import block_bootstrap_ci, hac_mean_test, t_crit, wilson
from quant.factors.standardise import transform


def test_hac_matches_hand_calculation(spec_case):
    """Matches hand-computed HAC gamma0, gamma1, SE, and t-stat from golden case."""
    c = spec_case("hac")
    res = hac_mean_test(c["values"], c["lag"])
    assert abs(res.mean - c["expected_mean"]) < 1e-12
    assert abs(res.se - c["expected_se"]) < 1e-12
    assert abs(res.t - c["expected_t"]) < 1e-12
    assert res.status == "ok"
    assert res.n == len(c["values"])
    assert res.n_eff == len(c["values"]) / (c["lag"] + 1)
    assert res.ci_lo is not None and res.ci_hi is not None
    assert abs(res.ci_lo - (res.mean - 1.645 * res.se)) < 1e-12
    assert abs(res.ci_hi - (res.mean + 1.645 * res.se)) < 1e-12


def test_hac_insufficient(spec_case):
    """Short vector N <= lag + 1 returns unavailable uncertainty."""
    c = spec_case("hac_insufficient")
    res = hac_mean_test(c["values"], c["lag"])
    assert res.n == c["expected_n"]
    assert res.n_eff == c["expected_n_eff"]
    assert res.status == c["expected_status"]
    assert res.se is c["expected_se"]
    assert res.t is None
    assert res.ci_lo is None
    assert res.ci_hi is None


def test_hac_constant():
    """Constant series returns zero variance and 'constant' status."""
    res = hac_mean_test([0.05, 0.05, 0.05, 0.05, 0.05], lag=1)
    assert res.status == "constant"
    assert res.se is None
    assert res.t is None
    assert res.ci_lo is None
    assert res.ci_hi is None


def test_negative_direction_applied_once(cfg, spec_case):
    """Oriented low-is-good signal IC is positive; negative raw sign is not applied twice."""
    c = spec_case("negative_direction")
    z = transform(pd.Series(c["raw"]), pd.Series(["A"] * 5), c["direction"], cfg)["z"]
    ic, n, status = rank_ic(z, pd.Series(c["labels"]))
    assert n == 5 and status == "ok"
    assert abs(ic - c["expected_oriented_ic"]) < 1e-12


def test_planted_rank(spec_case):
    """Deterministic planted rank matches exact Spearman rank correlation."""
    c = spec_case("planted_rank")
    ic, n, status = rank_ic(pd.Series(c["scores"]), pd.Series(c["labels"]))
    assert n == 10 and status == "ok"
    assert abs(ic - c["expected_spearman"]) < 1e-12


def test_constant_spearman_is_none():
    """Constant score or label returns None with status 'constant'."""
    ic, n, status = rank_ic(pd.Series([1.0, 1.0, 1.0, 1.0]), pd.Series([1.0, 2.0, 3.0, 4.0]))
    assert ic is None
    assert n == 4
    assert status == "constant"

    ic2, n2, status2 = rank_ic(pd.Series([1.0, 2.0, 3.0, 4.0]), pd.Series([5.0, 5.0, 5.0, 5.0]))
    assert ic2 is None
    assert n2 == 4
    assert status2 == "constant"

    ic3, n3, status3 = rank_ic(pd.Series([1.0]), pd.Series([2.0]))
    assert ic3 is None
    assert n3 == 1
    assert status3 == "insufficient"


def test_t_crit_and_wilson(spec_case):
    """t_crit matches multiple-testing budget thresholds and Wilson interval matches binomial bounds."""
    b = spec_case("promotion_budget")
    for trials, expected in zip(b["trials"], b["expected_thresholds"]):
        actual = t_crit(trials, looks=b["max_looks"], alpha=b["alpha"], floor=b["t_floor"])
        assert abs(actual - expected) < 1e-12

    # Wilson score interval tests
    lo, hi = wilson(50, 100, z=1.645)
    assert lo is not None and hi is not None
    assert 0.4 < lo < 0.5 < hi < 0.6
    # Boundary: 0 successes
    lo0, hi0 = wilson(0, 100, z=1.645)
    assert lo0 == 0.0
    assert hi0 > 0.0
    # Insufficient: n=0
    assert wilson(0, 0) == (None, None)


def test_block_bootstrap_ci():
    """Circular block bootstrap returns 90% confidence interval when N >= 3*block."""
    data = [0.01 * (i % 5) for i in range(30)]
    lo, hi = block_bootstrap_ci(data, block=3, n=500, q=0.90, seed=42)
    assert lo is not None and hi is not None
    assert lo <= hi

    # Insufficient length returns (None, None)
    lo_short, hi_short = block_bootstrap_ci([0.1, 0.2], block=3)
    assert lo_short is None and hi_short is None

    # Constant series returns (None, None)
    lo_const, hi_const = block_bootstrap_ci([0.5] * 30, block=3)
    assert lo_const is None and hi_const is None


def test_quintiles_pooled_after_within_group_assignment():
    """Quintiles output q, n, mean, median, trimmed_mean pooled after within-group assignment."""
    # 2 groups of 10 stocks
    scores = pd.Series(list(range(10)) + list(range(10)))
    returns = pd.Series([float(x) for x in range(10)] + [float(x * 2) for x in range(10)])
    groups = pd.Series(["G1"] * 10 + ["G2"] * 10)

    df_q = quintiles(scores, returns, groups)
    assert list(df_q.columns) == ["q", "n", "mean", "median", "trimmed_mean"]
    assert len(df_q) == 5
    assert list(df_q["q"]) == [1, 2, 3, 4, 5]
    # Each group has 10 items -> 2 items per quintile, total 4 items per quintile
    assert list(df_q["n"]) == [4, 4, 4, 4, 4]
    # Returns should be monotonically increasing across quintiles
    assert df_q["mean"].iloc[0] < df_q["mean"].iloc[-1]


def test_partial_ic():
    """Partial IC regresses candidate on active factors and evaluates rank IC on residual."""
    rng = np.random.default_rng(123)
    n = 50
    # Active factor X
    x = rng.normal(size=n)
    # Candidate correlated with X plus unique component
    cand = 0.8 * x + 0.2 * rng.normal(size=n)
    # Label depends on both X and cand's unique component
    label = 0.5 * x + 0.5 * cand + 0.1 * rng.normal(size=n)

    cand_s = pd.Series(cand)
    active_df = pd.DataFrame({"act1": x})
    label_s = pd.Series(label)

    ic, count, status = partial_ic(cand_s, active_df, label_s)
    assert status == "ok"
    assert count == n
    assert ic is not None
    assert -1.0 <= ic <= 1.0

    # Insufficient observations
    ic_insuf, n_insuf, stat_insuf = partial_ic(cand_s.iloc[:2], active_df.iloc[:2], label_s.iloc[:2])
    assert stat_insuf == "insufficient"
    assert ic_insuf is None
