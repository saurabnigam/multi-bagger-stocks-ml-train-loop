"""Legacy diagnostic factors."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class DcFlag(Factor):
    """dc_flag: Death-cross diagnostic flag (1.0 if close < SMA50 < SMA200 else 0.0)."""

    def __init__(self):
        super().__init__(FactorSpec(
            name="dc_flag",
            version=1,
            family="legacy",
            direction=1,
            horizon_m=1,
            hypothesis="H_DC_FLAG: Death-cross flag from legacy engine (diagnostic only).",
            formula="1 if split-consistent close < SMA50 < SMA200 else 0",
            inputs=("close_split",),
            lookback_days=200,
            applies_to_financials=True,
            level="stock",
            backfillable=True,
            min_coverage=0.95,
            evidence="Legacy V1 Engine Rule",
            hypothesis_id="hyp_dc_flag",
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
            sma50 = float(s.iloc[-50:].mean())
            sma200 = float(s.iloc[-200:].mean())
            if curr < sma50 and sma50 < sma200:
                out[sid] = 1.0
            else:
                out[sid] = 0.0
        return pd.Series(out, index=inputs.members)
