"""Z-score bounding at the +/-3 rank-normal cap (MASTER_SPEC 5.2 step 5).

Real-world motivation: quant.factors.standardise.transform is the single place
that turns a raw factor into the bounded z-score the composite score sums across
families. MASTER_SPEC 5.2 requires the bound to be an actual, enforced cap: after
centering, values are divided by max(1.0, max(abs(centered)) / 3.0) so that no
security's z can ever exceed 3 in magnitude, however extreme its rank position.
This test builds a sector group large enough (500 distinct raw values, one
security per rank) that the raw rank-normal transform alone would put the most
extreme member's centered value at about 3.09 -- past the cap -- and checks that
transform() (a) never emits |z| > 3 (plus float tolerance), (b) actually reaches
the cap (its scaled max is 3.0, not merely "small"), and (c) flips sign under
direction=-1 without changing magnitude.

Winsorization (a separate 1st/99th percentile step) is configured off here
(winsor_lo=0.0, winsor_hi=1.0, i.e. clip to the group's own min/max, a no-op)
so it cannot re-tie the extreme ranks and mask the divisor this test targets.
MASTER_SPEC 5.2 documents winsorization and the +/-3 bounding division as two
distinct steps of the formula, and M17 touches only the bounding step's own
divisor (3.0 -> 5.0), not winsorization.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from quant.config import load
from quant.factors.standardise import transform


@pytest.fixture
def cfg():
    c = load()
    # Percentile 0 == the group's own min, percentile 100 == its own max, so
    # clip(lower=min, upper=max) is a no-op: winsorization cannot introduce
    # ties among the extreme ranks this test relies on.
    c.standardise.winsor_lo = 0.0
    c.standardise.winsor_hi = 1.0
    return c


def _independent_expected(n: int, direction: int) -> tuple[np.ndarray, float]:
    """Expected z, derived straight from MASTER_SPEC 5.2's own formula text
    with scipy's normal PPF -- never by calling quant.factors.standardise.

    v = direction * NormalPPF((rank - 0.5) / n); centered = v - mean(v);
    scale = max(1, max(abs(centered)) / 3); z = centered / scale.
    """
    ranks = np.arange(1, n + 1)
    prob = (ranks - 0.5) / n
    v = direction * norm.ppf(prob)
    centered = v - v.mean()
    max_abs = float(np.max(np.abs(centered)))
    scale = max(1.0, max_abs / 3.0)
    z = centered / scale
    return z, max_abs


def test_bound_caps_at_three_and_is_reached_for_a_large_extreme_group(cfg):
    """500 distinct ranks in one group: the raw rank-normal spread (~3.09 at the
    extremes) exceeds 3, so the bounding division must engage. Every output
    stays within [-3, 3] and the cap is genuinely reached (max |z| == 3.0),
    not merely small."""
    n = 500
    sids = list(range(1, n + 1))
    raw = pd.Series(np.arange(1, n + 1, dtype=float), index=sids)
    groups = pd.Series(["G"] * n, index=sids)

    expected_z, expected_max_abs = _independent_expected(n, direction=1)
    assert expected_max_abs > 3.0  # sanity: the unbounded rank-normal spread really exceeds the cap

    res = transform(raw, groups, direction=1, cfg=cfg)

    assert (res["z"].abs() <= 3.0 + 1e-12).all()
    assert abs(res["z"].abs().max() - 3.0) < 1e-9  # the cap is actually reached, not just approached
    for sid, exp in zip(sids, expected_z):
        assert abs(res.loc[sid, "z"] - exp) < 1e-9


def test_direction_minus_one_flips_sign_without_changing_magnitude(cfg):
    """direction=-1 must negate every bounded z exactly, leaving |z| (and the
    fact that the cap is reached) unchanged."""
    n = 500
    sids = list(range(1, n + 1))
    raw = pd.Series(np.arange(1, n + 1, dtype=float), index=sids)
    groups = pd.Series(["G"] * n, index=sids)

    res_pos = transform(raw, groups, direction=1, cfg=cfg)
    res_neg = transform(raw, groups, direction=-1, cfg=cfg)

    assert np.allclose(res_neg["z"].values, -res_pos["z"].values, atol=1e-9)
    assert np.allclose(res_neg["z"].abs().values, res_pos["z"].abs().values, atol=1e-9)
    assert (res_neg["z"].abs() <= 3.0 + 1e-12).all()
