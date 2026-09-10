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
        eps_df = inputs.fundamental("income", "Diluted EPS", "A", 4)
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
                out.loc[sid] = np.log(eps_0 / eps_3) / 3.0

        return out.reindex(inputs.members).astype(float)


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
