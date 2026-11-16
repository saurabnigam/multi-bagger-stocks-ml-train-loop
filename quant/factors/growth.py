"""Growth family factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class EpsGrowth3y(Factor):
    """3-year compound EPS growth: log(EPS latest / EPS three FY earlier) / 3; endpoints positive."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="eps_growth_3y",
                version=1,
                family="growth",
                direction=1,
                horizon_m=12,
                hypothesis="H_EPS_GROWTH_3Y",
                formula="log(EPS latest / EPS three FY earlier)/3; both endpoints positive",
                inputs=("eps",),
                lookback_days=1095,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="3-year EPS compound growth",
                hypothesis_id="hyp_eps_growth_3y_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        eps_df, eps_dates = inputs.fundamental_dated("income", "Diluted EPS", "A", 4)
        ni_df, ni_dates = inputs.fundamental_dated("income", "Net Income", "A", 4)
        splits = inputs.splits(self.spec.lookback_days)
        out = pd.Series(np.nan, index=inputs.members, dtype=float)

        for sid in inputs.members:
            if sid not in eps_df.index:
                continue

            row = eps_df.loc[sid]
            # Need period 0 (latest) and period 3 (three FY earlier)
            if 0 not in row.index or 3 not in row.index:
                continue

            eps_0 = float(row[0])
            eps_3 = float(row[3])

            # Both endpoints must be strictly positive (no default growth or imputation)
            if eps_0 > 0 and eps_3 > 0 and not np.isnan(eps_0) and not np.isnan(eps_3):
                d_0, d_3 = eps_dates.loc[sid, 0], eps_dates.loc[sid, 3]
                ni_by_date = dict(zip(ni_dates.loc[sid].tolist(), ni_df.loc[sid].tolist())) if sid in ni_df.index else {}
                eps_3 = eps_3 / share_basis_multiplier(
                    eps_0, eps_3, ni_by_date.get(d_0), ni_by_date.get(d_3),
                    [ratio for date, ratio in splits.get(int(sid), []) if d_3 is not None and date > str(d_3)],
                )
                out.loc[sid] = np.log(eps_0 / eps_3) / 3.0

        return out.reindex(inputs.members).astype(float)


def share_basis_multiplier(eps_0: float, eps_old: float, ni_0, ni_old, splits_after: list,
                           tolerance: float = 1.5) -> float:
    """Factor that puts an older per-share figure on the latest figure's share basis.

    Yahoo does not restate every year of its EPS history after a split or bonus issue
    (2026-09 review: TATAINVEST 10:1, BEML 2:1 and HDFCBANK's 1:1 bonus left one endpoint on
    the old share count, reading as -59%, -27% and -22% a year). The implied share count is
    Net Income / EPS at each endpoint. When a split or bonus of cumulative ratio K occurred
    after the older period end and the implied share ratio sits nearer K (older figure on
    the pre-split basis) or 1/K (older figure restated twice, as for NEWGEN's 2024 bonus, or
    latest figure on the pre-split basis) than 1, divide the older figure by that multiplier.
    The ratio must also lie within a factor ``tolerance`` of the chosen multiplier: when Net
    Income and EPS disagree with each other (AIIL FY2026: 61.61 EPS on 19.3bn net income and
    849M shares) the implied ratio is noise and nothing is adjusted. Genuine share-count
    changes without a split record (mergers, rights, preferential issues) are never adjusted;
    without Net Income at both endpoints nothing is adjusted.
    """
    k = float(np.prod(splits_after)) if splits_after else 1.0
    try:
        ni_0, ni_old = float(ni_0), float(ni_old)
    except (TypeError, ValueError):
        return 1.0
    if k == 1.0 or not (ni_0 > 0 and ni_old > 0 and eps_0 > 0 and eps_old > 0):
        return 1.0
    ratio = (ni_0 / eps_0) / (ni_old / eps_old)
    best = min((1.0, k, 1.0 / k), key=lambda m: abs(np.log(ratio / m)))
    return best if abs(np.log(ratio / best)) <= np.log(tolerance) else 1.0


class RevGrowth3y(Factor):
    """3-year compound Revenue growth: log(Revenue latest / three FY earlier) / 3; endpoints positive."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="rev_growth_3y",
                version=1,
                family="growth",
                direction=1,
                horizon_m=12,
                hypothesis="H_REV_GROWTH_3Y",
                formula="log(Revenue latest/three FY earlier)/3; endpoints positive",
                inputs=("revenue",),
                lookback_days=1095,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="3-year revenue compound growth",
                hypothesis_id="hyp_rev_growth_3y_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        rev_df = inputs.fundamental("income", "Total Revenue", "A", 4)
        out = pd.Series(np.nan, index=inputs.members, dtype=float)

        for sid in inputs.members:
            if sid not in rev_df.index:
                continue

            row = rev_df.loc[sid]
            if 0 not in row.index or 3 not in row.index:
                continue

            rev_0 = float(row[0])
            rev_3 = float(row[3])

            if rev_0 > 0 and rev_3 > 0 and not np.isnan(rev_0) and not np.isnan(rev_3):
                out.loc[sid] = np.log(rev_0 / rev_3) / 3.0

        return out.reindex(inputs.members).astype(float)


class EarnMom(Factor):
    """Quarterly earnings momentum: (TTM NI offset0 - TTM NI offset4) / abs(TTM NI offset4)."""

    # Needs eight consecutive admissible fiscal quarters of net income (TTM now vs a year ago).
    # Yahoo returns about five quarters per capture; the bitemporal store accumulates them.
    prerequisite = {"consecutive_quarters": 8}

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="earn_mom",
                version=1,
                family="growth",
                direction=1,
                horizon_m=3,
                hypothesis="H_EARN_MOM",
                formula="(TTM NI offset0 - TTM NI offset4) / abs(TTM NI offset4); eight quarters, nonzero denominator",
                inputs=("ttm_net_income",),
                lookback_days=730,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="quarterly earnings acceleration momentum",
                hypothesis_id="hyp_earn_mom_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ni_0 = inputs.ttm("Net Income", offset_quarters=0)
        ni_4 = inputs.ttm("Net Income", offset_quarters=4)

        denom = np.abs(ni_4)
        out = (ni_0 - ni_4) / denom
        out[denom <= 0] = np.nan

        return out.reindex(inputs.members).astype(float)
