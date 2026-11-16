"""Low risk factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class Vol252(Factor):
    """vol_252: Annualized historical return volatility using population standard deviation."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="vol_252",
            version=1,
            family="low_risk",
            direction=-1,
            horizon_m=12,
            hypothesis="H_VOL_252: Low-volatility stocks deliver superior risk-adjusted returns (Low-Beta Anomaly).",
            formula="population SD of 252 daily log TRI returns * sqrt(252); requires 253 bars",
            inputs=("tri",),
            lookback_days=252,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Baker, Bradley and Wurgler (2011)",
            hypothesis_id="hyp_vol_252",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        tri_df = inputs.tri(lookback_days=252)
        out = {}
        for sid in inputs.members:
            if sid not in tri_df.columns:
                out[sid] = np.nan
                continue
            s = tri_df[sid].dropna()
            if len(s) < 253:
                out[sid] = np.nan
                continue
            last_253 = s.iloc[-253:].values
            if (last_253 <= 0).any():
                out[sid] = np.nan
                continue
            log_rets = np.diff(np.log(last_253))
            sd_pop = float(np.std(log_rets, ddof=0))
            out[sid] = sd_pop * float(np.sqrt(252.0))
        return pd.Series(out, index=inputs.members)


class MaxRet21(Factor):
    """max_ret_21: Maximum daily return over trailing 21 trading days (lottery demand proxy)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="max_ret_21",
            version=1,
            family="low_risk",
            direction=-1,
            horizon_m=1,
            hypothesis="H_MAX_RET_21: Stocks with extreme positive returns suffer lottery-preference underperformance.",
            formula="max(last 21 arithmetic daily TRI returns); requires >= 22 bars",
            inputs=("tri",),
            lookback_days=21,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Bali, Cakici and Whitelaw (2011)",
            hypothesis_id="hyp_max_ret_21",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        tri_df = inputs.tri(lookback_days=21)
        out = {}
        for sid in inputs.members:
            if sid not in tri_df.columns:
                out[sid] = np.nan
                continue
            s = tri_df[sid].dropna()
            if len(s) < 22:
                out[sid] = np.nan
                continue
            last_22 = s.iloc[-22:].values
            prev = last_22[:-1]
            curr = last_22[1:]
            if (prev <= 0).any():
                out[sid] = np.nan
                continue
            rets = (curr - prev) / prev
            out[sid] = float(np.max(rets))
        return pd.Series(out, index=inputs.members)
