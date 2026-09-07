"""Momentum factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class Mom12_1(Factor):
    """mom_12_1: 12-1 month momentum using Total Return Index."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="mom_12_1",
            version=1,
            family="momentum",
            direction=1,
            horizon_m=3,
            hypothesis="H_MOM_12_1: Intermediate-term price momentum carries forward over 3 months.",
            formula="log(TRI at trading offset -21 / TRI at -252); requires both endpoints and >= 253 bars",
            inputs=("tri",),
            lookback_days=252,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Jegadeesh and Titman (1993)",
            hypothesis_id="hyp_mom_12_1",
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
            p_21 = float(s.iloc[-22])
            p_252 = float(s.iloc[-253])
            if p_21 > 0 and p_252 > 0:
                out[sid] = float(np.log(p_21 / p_252))
            else:
                out[sid] = np.nan
        return pd.Series(out, index=inputs.members)


class Trend200(Factor):
    """trend_200: Distance from trailing 200-day simple moving average."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="trend_200",
            version=1,
            family="momentum",
            direction=1,
            horizon_m=3,
            hypothesis="H_TREND_200: Stocks trading above 200 SMA exhibit positive institutional drift.",
            formula="split-consistent close / trailing 200-close mean - 1",
            inputs=("close_split",),
            lookback_days=200,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Faber (2007)",
            hypothesis_id="hyp_trend_200",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        close_df = inputs.close_split(lookback_days=200)
        out = {}
        for sid in inputs.members:
            if sid not in close_df.columns:
                out[sid] = np.nan
                continue
            s = close_df[sid].dropna()
            if len(s) < 200:
                out[sid] = np.nan
                continue
            curr = float(s.iloc[-1])
            sma200 = float(s.iloc[-200:].mean())
            if sma200 > 0:
                out[sid] = float(curr / sma200 - 1.0)
            else:
                out[sid] = np.nan
        return pd.Series(out, index=inputs.members)


class Mom6_1(Factor):
    """mom_6_1: 6-1 month intermediate momentum (shadow)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="mom_6_1",
            version=1,
            family="momentum",
            direction=1,
            horizon_m=3,
            hypothesis="H_MOM_6_1: 6-month momentum alternative specification.",
            formula="log(TRI[-21] / TRI[-126]); requires >= 127 bars",
            inputs=("tri",),
            lookback_days=126,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Novy-Marx (2012)",
            hypothesis_id="hyp_mom_6_1",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        tri_df = inputs.tri(lookback_days=126)
        out = {}
        for sid in inputs.members:
            if sid not in tri_df.columns:
                out[sid] = np.nan
                continue
            s = tri_df[sid].dropna()
            if len(s) < 127:
                out[sid] = np.nan
                continue
            p_21 = float(s.iloc[-22])
            p_126 = float(s.iloc[-127])
            if p_21 > 0 and p_126 > 0:
                out[sid] = float(np.log(p_21 / p_126))
            else:
                out[sid] = np.nan
        return pd.Series(out, index=inputs.members)


class Dist52wHigh(Factor):
    """dist_52w_high: Proximity to 52-week high (shadow)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="dist_52w_high",
            version=1,
            family="momentum",
            direction=1,
            horizon_m=3,
            hypothesis="H_52W_HIGH: Closeness to 52w high captures informational anchoring.",
            formula="split-consistent close / max(last 252 closes) - 1",
            inputs=("close_split",),
            lookback_days=252,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="George and Hwang (2004)",
            hypothesis_id="hyp_dist_52w_high",
        ))

    def compute(self, inputs: FactorInputs) -> pd.Series:
        close_df = inputs.close_split(lookback_days=252)
        out = {}
        for sid in inputs.members:
            if sid not in close_df.columns:
                out[sid] = np.nan
                continue
            s = close_df[sid].dropna()
            if len(s) < 252:
                out[sid] = np.nan
                continue
            curr = float(s.iloc[-1])
            max_c = float(s.iloc[-252:].max())
            if max_c > 0:
                out[sid] = float(curr / max_c - 1.0)
            else:
                out[sid] = np.nan
        return pd.Series(out, index=inputs.members)


class Rev1m(Factor):
    """rev_1m: 1-month short-term reversal (shadow diagnostic)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="rev_1m",
            version=1,
            family="momentum",
            direction=-1,
            horizon_m=1,
            hypothesis="H_REV_1M: 1-month returns exhibit short-term mean-reversion.",
            formula="log(TRI[0] / TRI[-21]); requires >= 22 bars",
            inputs=("tri",),
            lookback_days=21,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Jegadeesh (1990)",
            hypothesis_id="hyp_rev_1m",
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
            p_0 = float(s.iloc[-1])
            p_21 = float(s.iloc[-22])
            if p_0 > 0 and p_21 > 0:
                out[sid] = float(np.log(p_0 / p_21))
            else:
                out[sid] = np.nan
        return pd.Series(out, index=inputs.members)
