"""Value family factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


def _is_financial(inputs: FactorInputs) -> pd.Series:
    """Check whether securities belong to Financial Services sector."""
    group_str = inputs.sector_group.astype(str).str.lower()
    return group_str.isin(["financial services", "financials", "financial"])


class EarningsYield(Factor):
    """Earnings yield: nonfinancial TTM EBIT / EV; financial TTM NI / mcap."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="earnings_yield",
                version=1,
                family="value",
                direction=1,
                horizon_m=12,
                hypothesis="H_EY",
                formula="nonfinancial TTM EBIT / EV; financial TTM NI / mcap; denominator > 0",
                inputs=("ttm_ebit", "ttm_net_income", "ev_inr", "market_cap_inr"),
                lookback_days=365,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="enterprise earnings yield / PE inversion",
                hypothesis_id="hyp_ey_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        fin_mask = _is_financial(inputs)
        ev = inputs.attribute("ev_inr")
        mcap = inputs.attribute("market_cap_inr")
        ebit = inputs.ttm("EBIT")
        ni = inputs.ttm("Net Income")

        out = pd.Series(np.nan, index=inputs.members, dtype=float)

        for sid in inputs.members:
            if fin_mask.get(sid, False):
                # Financial: TTM NI / mcap
                m_val = mcap.get(sid)
                ni_val = ni.get(sid)
                if m_val is not None and m_val > 0 and ni_val is not None and not np.isnan(ni_val):
                    out.loc[sid] = float(ni_val) / float(m_val)
            else:
                # Nonfinancial: TTM EBIT / EV
                ev_val = ev.get(sid)
                ebit_val = ebit.get(sid)
                if ev_val is not None and ev_val > 0 and ebit_val is not None and not np.isnan(ebit_val):
                    out.loc[sid] = float(ebit_val) / float(ev_val)

        return out.reindex(inputs.members).astype(float)


class BookToPrice(Factor):
    """Book to price: annual Equity / mcap; positive mcap; negative equity remains negative."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="book_to_price",
                version=1,
                family="value",
                direction=1,
                horizon_m=12,
                hypothesis="H_BP",
                formula="annual Equity / mcap; positive mcap; negative equity remains negative",
                inputs=("total_equity", "market_cap_inr"),
                lookback_days=365,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="Fama French Book to Market",
                hypothesis_id="hyp_bp_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        eq_df = inputs.fundamental("balance", "Stockholders Equity", "A", 1)
        mcap = inputs.attribute("market_cap_inr")

        eq = eq_df[0] if 0 in eq_df.columns else pd.Series(np.nan, index=inputs.members)

        out = eq / mcap
        out[mcap <= 0] = np.nan

        return out.reindex(inputs.members).astype(float)


class FcfYield(Factor):
    """FCF Yield: mean(three annual OCF + signed CapEx) / EV; EV positive."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="fcf_yield",
                version=1,
                family="value",
                direction=1,
                horizon_m=12,
                hypothesis="H_FCF_YIELD",
                formula="mean(three annual OCF+signed CapEx)/EV; EV positive",
                inputs=("ocf", "capex", "ev_inr"),
                lookback_days=1095,
                applies_to_financials=False,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="free cash flow yield over 3 years",
                hypothesis_id="hyp_fcf_yield_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        ocf_df = inputs.fundamental("cashflow", "Operating Cash Flow", "A", 3)
        capex_df = inputs.fundamental("cashflow", "Capital Expenditure", "A", 3)
        ev = inputs.attribute("ev_inr")

        out = pd.Series(np.nan, index=inputs.members, dtype=float)
        fin_mask = _is_financial(inputs)

        for sid in inputs.members:
            if fin_mask.get(sid, False):
                continue
            ev_val = ev.get(sid)
            if ev_val is None or ev_val <= 0 or np.isnan(ev_val):
                continue
            if sid not in ocf_df.index or sid not in capex_df.index:
                continue

            o_row = ocf_df.loc[sid]
            c_row = capex_df.loc[sid]

            if len(o_row.dropna()) < 3 or len(c_row.dropna()) < 3:
                continue

            fcfs = []
            for i in range(3):
                o_val = float(o_row.iloc[i])
                c_val = float(c_row.iloc[i])
                signed_capex = c_val if c_val < 0 else -c_val
                fcfs.append(o_val + signed_capex)

            mean_fcf = float(np.mean(fcfs))
            out.loc[sid] = mean_fcf / float(ev_val)

        return out.reindex(inputs.members).astype(float)


class DivYield(Factor):
    """Dividend yield: admissible dividend_rate / raw quoted close."""

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="div_yield",
                version=1,
                family="value",
                direction=1,
                horizon_m=12,
                hypothesis="H_DIV_YIELD",
                formula="admissible dividend_rate / raw quoted close",
                inputs=("dividend_rate_inr", "close_raw"),
                lookback_days=5,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.70,
                evidence="dividend yield income factor",
                hypothesis_id="hyp_div_yield_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        div_rate = inputs.attribute("dividend_rate_inr")
        closes = inputs.close_raw(lookback_days=5)

        if closes.empty:
            return pd.Series(np.nan, index=inputs.members, dtype=float)

        last_close = closes.iloc[-1]
        out = div_rate / last_close
        out[last_close <= 0] = np.nan

        return out.reindex(inputs.members).astype(float)
