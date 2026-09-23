"""Flows family factor implementations."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs


class InstHoldChg3m(Factor):
    """Institutional ownership change over 3 months: lag 0 - lag 3 (four monthly captures)."""

    # Needs holdings captures in four distinct IST calendar months before the cutoff.
    prerequisite = {"holdings_months": 4}

    def __init__(self):
        super().__init__(
            FactorSpec(
                name="inst_hold_chg_3m",
                version=1,
                family="flows",
                direction=1,
                horizon_m=3,
                hypothesis="H_INST_FLOWS",
                formula="eligible holdings lag 0 - lag 3; four monthly captures",
                inputs=("holdings",),
                lookback_days=120,
                applies_to_financials=True,
                level="stock",
                backfillable=False,  # needs statements/attributes/holdings: not a backfillable price factor
                min_coverage=0.50,
                evidence="institutional accumulation/distribution",
                hypothesis_id="hyp_inst_flows_1",
            )
        )

    def compute(self, inputs: FactorInputs) -> pd.Series:
        h0 = inputs.holdings(lag_runs=0)
        h3 = inputs.holdings(lag_runs=3)

        out = h0 - h3
        return out.reindex(inputs.members).astype(float)
