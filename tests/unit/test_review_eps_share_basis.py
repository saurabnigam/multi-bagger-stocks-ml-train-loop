"""eps_growth_3y puts both EPS endpoints on one share basis before taking the ratio.

Real-data motivation (2026-09-24 verification): Yahoo left one endpoint of the annual EPS
history on the pre-split share count for TATAINVEST (10:1 split), BEML (2:1) and HDFCBANK's
Diluted EPS (1:1 bonus; HDFCBANK Diluted FY2023 = 88.68 vs Basic 44.51). The factor read
these as -59%, -27% and -22% a year. The implied share count (Net Income / EPS) moved by the
split ratio, which identifies the basis error; genuine share-count changes without a split
record (mergers, rights, preferential issues) must not be touched.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.prices import PriceStore
from quant.factors.growth import EpsGrowth3y, share_basis_multiplier
from quant.factors.inputs import build as build_inputs
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock

CUTOFF = "2026-09-30T18:29:59.999999Z"
YEARS = ("2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31")

#       sid: (EPS FY2023, EPS FY2026, NI FY2023, NI FY2026, split (date, ratio) or None, expected growth)
CASES = {
    1: (20.0, 15.0, 100.0, 150.0, ("2025-06-02", 2.0), np.log(15.0 / 10.0) / 3),   # older EPS pre-split
    2: (44.51, 45.89, 495.4, 704.8, ("2025-08-26", 2.0), np.log(45.89 / 44.51) / 3),  # merger dilution, restated
    3: (10.0, 5.0, 100.0, 120.0, None, np.log(5.0 / 10.0) / 3),                      # dilution, no split record
    4: (10.0, 30.0, 100.0, 150.0, ("2026-06-01", 2.0), np.log(1.5) / 3),              # latest EPS pre-split
    5: (20.0, 15.0, None, None, ("2025-06-02", 2.0), np.log(15.0 / 20.0) / 3),        # no NI: no evidence, no change
    6: (10.0, 15.0, 100.0, 150.0, ("2022-06-01", 5.0), np.log(1.5) / 3),              # split before FY2023 end
    7: (20.0, 15.0, 100.0, 150.0, ("2025-06-02", 2.0), np.log(15.0 / 20.0) / 3),      # split observed after cutoff
}


@pytest.fixture
def ctx(tmp_path):
    from quant.db.core import apply_schema, connect

    db_path = tmp_path / "state.sqlite"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load().with_paths(db=db_path, prices_db=tmp_path / "prices.sqlite")
    with RunContext(as_of="2026-09-30", kind="production", track="live", cfg=cfg,
                    clock=FrozenClock("2026-09-30T18:30:00.000000Z"), actor=Actor(kind="system", name="t")) as c:
        c.store = PriceStore(path=tmp_path / "prices.sqlite", state_conn=c.conn)
        yield c


def _fund(ctx, sid, field, period_end, value):
    ctx.conn.execute(
        "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, available_from, "
        "available_from_basis, fetched_at, source, run_id) VALUES (?, 'income', 'A', ?, ?, ?, 'inr', "
        "'2026-09-01T00:00:00.000000Z', 'run_date', '2026-09-01T00:00:00.000000Z', 'yahoo', 1)",
        (sid, period_end, field, float(value)))


def test_eps_growth_uses_one_share_basis(ctx):
    dates = pd.bdate_range("2022-01-03", "2026-09-30").strftime("%Y-%m-%d")
    for sid, (eps_old, eps_new, ni_old, ni_new, split, _) in CASES.items():
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (?, ?, 'S', '2020-01-01', '2026-09-30', 'listed')", (sid, f"INE00000000{sid}"))
        for i, fy in enumerate(YEARS):
            _fund(ctx, sid, "Diluted EPS", fy, eps_old + (eps_new - eps_old) * i / 3)
            if ni_old is not None:
                _fund(ctx, sid, "Net Income", fy, ni_old + (ni_new - ni_old) * i / 3)
        ratios = np.ones(len(dates))
        if split:
            ratios[list(dates).index(split[0])] = split[1]
        observed = "2026-10-05T10:00:00.000000Z" if sid == 7 else "2026-09-30T10:00:00.000000Z"
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": 100.0, "volume": 1e5, "split_ratio": ratios}),
                         {"security_id": sid, "close_basis": "raw", "observed_at": observed})

    draft = Draft(cohort_id="live:2026-09-30", as_of="2026-09-30", track="live", knowledge_cutoff=CUTOFF,
                  definition_hash="d", members=pd.DataFrame({"security_id": list(CASES)}),
                  groups=pd.Series("Tech", index=list(CASES)), source_refs={}, factor_values=pd.DataFrame(),
                  model_weights=pd.DataFrame(), scores=pd.DataFrame())
    got = EpsGrowth3y().compute(build_inputs(ctx, draft))
    for sid, case in CASES.items():
        assert got.loc[sid] == pytest.approx(case[-1], rel=1e-9), (sid, got.loc[sid], case[-1])


def test_share_basis_multiplier_unit_cases():
    # HDFCBANK FY2023 -> FY2026 real values (INR bn net income): Diluted unadjusted, Basic restated
    assert share_basis_multiplier(45.75, 88.68, 704.8, 495.4, [2.0]) == 2.0
    assert share_basis_multiplier(45.89, 44.51, 704.8, 495.4, [2.0]) == 1.0
    assert share_basis_multiplier(15.0, 20.0, 150.0, 100.0, []) == 1.0
    assert share_basis_multiplier(15.0, 20.0, None, 100.0, [2.0]) == 1.0
    assert share_basis_multiplier(15.0, 20.0, -5.0, 100.0, [2.0]) == 1.0
    assert share_basis_multiplier(0.37, 3.7, 2.0e9, 2.0e9, [10.0]) == 10.0      # TATAINVEST-like
    # NEWGEN real values: Yahoo restated FY2023 EPS for the 2024 1:1 bonus twice (implied 278M
    # shares vs 141M), so the older figure is multiplied back by 2
    assert share_basis_multiplier(21.24, 6.275, 3005.764e6, 1770.115e6, [2.0]) == 0.5
    # AIIL real values: FY2026 EPS 61.61 disagrees with FY2026 net income 19.29bn (849M shares);
    # the implied ratio 0.37 is nearer 1/5 than 1 but far from both -> no adjustment
    assert share_basis_multiplier(61.61, 50.682, 19.2935e9, 43.0403e9, [5.0]) == 1.0
