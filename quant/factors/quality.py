"""Quality family factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


def _is_financial(inputs: FactorInputs) -> pd.Series:
    """Check whether securities belong to Financial Services sector."""
    group_str = inputs.sector_group.astype(str).str.lower()
    return group_str.isin(["financial services", "financials", "financial"])


class Roce(Factor):
    """ROCE: TTM EBIT / (latest annual Assets - Current Liabilities)."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="roce",
                version=1,
                family="quality",
                direction=1,
                horizon_m=12,
                hypothesis="H_ROCE",
                formula="TTM EBIT / (latest annual Assets - Current Liabilities); denominator > 0",
                inputs=("ttm_ebit", "total_assets", "current_liabilities"),
                lookback_days=365,
                applies_to_financials=False,
                level="stock",
                backfillable=True,
                min_coverage=0.70,
                evidence="Greenblatt ROCE / capital employed",
                hypothesis_id="hyp_roce_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ebit = inputs.ttm("EBIT")
        assets_df = inputs.fundamental("balance", "Total Assets", "A", 1)
        liab_df = inputs.fundamental("balance", "Current Liabilities", "A", 1)

        assets = assets_df[0] if 0 in assets_df.columns else pd.Series(np.nan, index=inputs.members)
        liab = liab_df[0] if 0 in liab_df.columns else pd.Series(np.nan, index=inputs.members)

        denom = assets - liab
        out = ebit / denom

        # Denominator must be strictly > 0
        out[denom <= 0] = np.nan

        # Nonfinancial exclusion
        out[_is_financial(inputs)] = np.nan

        return out.reindex(inputs.members).astype(float)


class Accruals(Factor):
    """Accruals: (annual Net Income - annual OCF) / same-period Assets. Direction = -1."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="accruals",
                version=1,
                family="quality",
                direction=-1,
                horizon_m=12,
                hypothesis="H_ACCRUALS",
                formula="(annual Net Income - annual OCF) / same-period Assets; denominator > 0",
                inputs=("net_income", "ocf", "total_assets"),
                lookback_days=365,
                applies_to_financials=False,
                level="stock",
                backfillable=True,
                min_coverage=0.70,
                evidence="Sloan (1996) accrual anomaly",
                hypothesis_id="hyp_accruals_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ni_df = inputs.fundamental("income", "Net Income", "A", 1)
        ocf_df = inputs.fundamental("cashflow", "Operating Cash Flow", "A", 1)
        assets_df = inputs.fundamental("balance", "Total Assets", "A", 1)

        ni = ni_df[0] if 0 in ni_df.columns else pd.Series(np.nan, index=inputs.members)
        ocf = ocf_df[0] if 0 in ocf_df.columns else pd.Series(np.nan, index=inputs.members)
        assets = assets_df[0] if 0 in assets_df.columns else pd.Series(np.nan, index=inputs.members)

        out = (ni - ocf) / assets
        out[assets <= 0] = np.nan
        out[_is_financial(inputs)] = np.nan

        return out.reindex(inputs.members).astype(float)


class CashConversion3y(Factor):
    """Cash Conversion: sum same three FY OCF / sum Net Income; denominator > 0."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="cash_conversion_3y",
                version=1,
                family="quality",
                direction=1,
                horizon_m=12,
                hypothesis="H_CASH_CONV",
                formula="sum same three FY OCF / sum Net Income; denominator > 0",
                inputs=("ocf", "net_income"),
                lookback_days=1095,
                applies_to_financials=False,
                level="stock",
                backfillable=True,
                min_coverage=0.70,
                evidence="multi-year cash conversion quality",
                hypothesis_id="hyp_cash_conv_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ocf_df = inputs.fundamental("cashflow", "Operating Cash Flow", "A", 3)
        ni_df = inputs.fundamental("income", "Net Income", "A", 3)

        out = pd.Series(np.nan, index=inputs.members, dtype=float)
        fin_mask = _is_financial(inputs)

        for sid in inputs.members:
            if fin_mask.get(sid, False):
                continue
            if sid not in ocf_df.index or sid not in ni_df.index:
                continue

            o_row = ocf_df.loc[sid]
            n_row = ni_df.loc[sid]

            # Must have 3 valid annual observations
            if len(o_row.dropna()) < 3 or len(n_row.dropna()) < 3:
                continue

            sum_ocf = float(o_row.iloc[:3].sum())
            sum_ni = float(n_row.iloc[:3].sum())

            if sum_ni > 0:
                out.loc[sid] = sum_ocf / sum_ni

        return out.reindex(inputs.members).astype(float)


class Leverage(Factor):
    """Solvency leverage: (annual Debt - Cash) / TTM EBITDA. Direction = -1."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="leverage",
                version=1,
                family="quality",
                direction=-1,
                horizon_m=12,
                hypothesis="H_LEVERAGE",
                formula="(annual Debt - Cash) / TTM EBITDA; EBITDA positive",
                inputs=("total_debt", "cash", "ttm_ebitda"),
                lookback_days=365,
                applies_to_financials=False,
                level="stock",
                backfillable=True,
                min_coverage=0.70,
                evidence="net debt to EBITDA solvency risk",
                hypothesis_id="hyp_leverage_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        debt_df = inputs.fundamental("balance", "Total Debt", "A", 1)
        cash_df = inputs.fundamental("balance", "Cash And Cash Equivalents", "A", 1)
        ebitda = inputs.ttm("EBITDA")

        debt = debt_df[0] if 0 in debt_df.columns else pd.Series(np.nan, index=inputs.members)
        cash = cash_df[0] if 0 in cash_df.columns else pd.Series(0.0, index=inputs.members)
        cash = cash.fillna(0.0)

        out = (debt - cash) / ebitda
        out[ebitda <= 0] = np.nan
        out[_is_financial(inputs)] = np.nan

        return out.reindex(inputs.members).astype(float)


class RoeStability3y(Factor):
    """ROE stability: mean(three annual NI/Equity) / population SD; Equity positive."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="roe_stability_3y",
                version=1,
                family="quality",
                direction=1,
                horizon_m=12,
                hypothesis="H_ROE_STABILITY",
                formula="mean(three annual NI/Equity) / population SD; Equity positive; zero SD -> NaN",
                inputs=("net_income", "total_equity"),
                lookback_days=1095,
                applies_to_financials=True,
                level="stock",
                backfillable=True,
                min_coverage=0.70,
                evidence="consistency of ROE compounders",
                hypothesis_id="hyp_roe_stab_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ni_df = inputs.fundamental("income", "Net Income", "A", 3)
        eq_df = inputs.fundamental("balance", "Stockholders Equity", "A", 3)

        out = pd.Series(np.nan, index=inputs.members, dtype=float)

        for sid in inputs.members:
            if sid not in ni_df.index or sid not in eq_df.index:
                continue

            n_row = ni_df.loc[sid]
            e_row = eq_df.loc[sid]

            if len(n_row.dropna()) < 3 or len(e_row.dropna()) < 3:
                continue

            roes = []
            valid = True
            for i in range(3):
                eq_val = float(e_row.iloc[i])
                ni_val = float(n_row.iloc[i])
                if eq_val <= 0 or np.isnan(eq_val) or np.isnan(ni_val):
                    valid = False
                    break
                roes.append(ni_val / eq_val)

            if not valid or len(roes) < 3:
                continue

            sd = float(np.std(roes, ddof=0))
            if sd <= 1e-12 or np.isnan(sd):
                continue

            mean_val = float(np.mean(roes))
            out.loc[sid] = mean_val / sd

        return out.reindex(inputs.members).astype(float)
