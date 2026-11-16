"""Control and risk diagnostic factors (never weighted)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class Size(Factor):
    """size: Log of point-in-time market capitalization (control diagnostic, not backfillable)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="size",
            version=1,
            family="control",
            direction=1,
            horizon_m=1,
            hypothesis="H_SIZE: Market cap control factor (never weighted).",
            formula="log(admissible mcap); no backfill",
            inputs=("market_cap_inr",),
            lookback_days=0,
            applies_to_financials=True,
            level="stock",
            backfillable=False,
            min_coverage=0.95,
            evidence="Fama and French (1992)",
            hypothesis_id="hyp_size",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        mcap = inputs.attribute("market_cap_inr")
        mcap_pos = mcap.where(mcap > 0, np.nan)
        return np.log(mcap_pos)


class Liq(Factor):
    """liq: Log of 63-day average daily turnover (INR) (control diagnostic)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="liq",
            version=1,
            family="control",
            direction=1,
            horizon_m=1,
            hypothesis="H_LIQ: Liquidity control factor (never weighted).",
            formula="log(ADV63), positive only",
            inputs=("adv_63_inr",),
            lookback_days=63,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Amihud (2002)",
            hypothesis_id="hyp_liq",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        adv = inputs.adv_inr()
        adv_pos = adv.where(adv > 0, np.nan)
        return np.log(adv_pos)


class Beta252(Factor):
    """beta_252: OLS market beta of 252 daily TRI returns against benchmark (control diagnostic)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="beta_252",
            version=1,
            family="control",
            direction=1,
            horizon_m=1,
            hypothesis="H_BETA_252: Market beta control factor (never weighted).",
            formula="OLS beta of 252 daily TRI returns against index, intercept included",
            inputs=("tri", "benchmark_tri"),
            lookback_days=252,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Sharpe (1964)",
            hypothesis_id="hyp_beta_252",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        tri_df = inputs.tri(lookback_days=252)
        bm_series = inputs.benchmark_tri("BM_NIFTY500_EW", lookback_days=252)
        out = {}

        if bm_series.empty or len(bm_series) < 2:
            # Fall back to equal-weighted average of tri_df returns if benchmark series absent
            bm_tri = tri_df.mean(axis=1).dropna()
        else:
            bm_tri = bm_series.dropna()

        bm_rets = bm_tri.pct_change().dropna()

        for sid in inputs.members:
            if sid not in tri_df.columns:
                out[sid] = np.nan
                continue
            s_tri = tri_df[sid].dropna()
            s_rets = s_tri.pct_change().dropna()

            aligned = pd.concat([s_rets, bm_rets], axis=1, join="inner").dropna()
            if len(aligned) < 200:
                out[sid] = np.nan
                continue

            y = aligned.iloc[:, 0].values
            x = aligned.iloc[:, 1].values

            cov = np.cov(x, y)[0, 1]
            var_x = np.var(x, ddof=1)
            if var_x > 0:
                out[sid] = float(cov / var_x)
            else:
                out[sid] = np.nan

        return pd.Series(out, index=inputs.members)
