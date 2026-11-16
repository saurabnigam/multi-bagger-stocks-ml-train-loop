"""Acceptance tests for WS05.04: Fundamental and flow factors."""
import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.prices import PriceStore
from quant.factors.flows import InstHoldChg3m
from quant.factors.growth import EarnMom, EpsGrowth3y, RevGrowth3y
from quant.factors.inputs import build as build_inputs
from quant.factors.quality import Accruals, CashConversion3y, Leverage, Roce, RoeStability3y
from quant.factors.standardise import standardise
from quant.factors.value import BookToPrice, DivYield, EarningsYield, FcfYield
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock


@pytest.fixture
def ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"

    from quant.db.core import apply_schema, connect
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = cfg.with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock("2026-09-30T18:30:00.000000Z")
    actor = Actor(kind="system", name="test")

    run_ctx = RunContext(
        as_of="2026-09-30",
        kind="production",
        track="live",
        cfg=cfg,
        clock=clock,
        actor=actor,
    )
    with run_ctx as c:
        store = PriceStore(path=prices_path, state_conn=c.conn)
        c.store = store
        yield c


def _insert_fund(ctx, sid, stmt, freq, period_end, field, val, avail="2026-09-01T00:00:00.000000Z"):
    ctx.conn.execute(
        """
        INSERT OR IGNORE INTO fundamentals (
            security_id, statement, freq, period_end, field, value, unit,
            available_from, available_from_basis, fetched_at, source, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'inr', ?, 'run_date', ?, 'yahoo', 1)
        """,
        (sid, stmt, freq, period_end, field, float(val), avail, avail),
    )


def test_quality_factors_roce_and_accruals(ctx):
    """ROCE excludes financials and nonpositive denominators; accruals negative direction gives higher oriented z."""
    for sid, name in [
        (1, "Tech1"), (2, "FinBank"), (3, "NegDenom"),
        (4, "LowAccrual"), (5, "Tech5"), (6, "Tech6"), (7, "Tech7")
    ]:
        ctx.conn.execute(f"INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES ({sid}, 'INE00{sid}', '{name}', '2025-01-01', '2026-09-30', 'listed')")

    # Quarterly EBIT for TTM: 4 quarters for sid 1: 25 each -> TTM EBIT = 100
    for q_end in ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]:
        _insert_fund(ctx, 1, "income", "Q", q_end, "EBIT", 25.0)
        _insert_fund(ctx, 3, "income", "Q", q_end, "EBIT", 25.0)
        _insert_fund(ctx, 4, "income", "Q", q_end, "EBIT", 25.0)

    # Balance sheet: latest annual Assets and Current Liab
    # Sid 1: Assets=1000, Liab=600 -> Denom = 400. ROCE = 100 / 400 = 0.25
    _insert_fund(ctx, 1, "balance", "A", "2026-03-31", "Total Assets", 1000.0)
    _insert_fund(ctx, 1, "balance", "A", "2026-03-31", "Current Liabilities", 600.0)
    _insert_fund(ctx, 1, "income", "A", "2026-03-31", "Net Income", 80.0)
    _insert_fund(ctx, 1, "cashflow", "A", "2026-03-31", "Operating Cash Flow", 50.0)

    # Sid 2: Financials (Bank)
    _insert_fund(ctx, 2, "balance", "A", "2026-03-31", "Total Assets", 10000.0)
    _insert_fund(ctx, 2, "income", "A", "2026-03-31", "Net Income", 500.0)
    _insert_fund(ctx, 2, "cashflow", "A", "2026-03-31", "Operating Cash Flow", 400.0)

    # Sid 3: Assets=500, Liab=600 -> Denom = -100 <= 0 -> ROCE NaN
    _insert_fund(ctx, 3, "balance", "A", "2026-03-31", "Total Assets", 500.0)
    _insert_fund(ctx, 3, "balance", "A", "2026-03-31", "Current Liabilities", 600.0)

    # Sid 4: Assets=1000, NI=50, OCF=100 -> Accruals = (50 - 100)/1000 = -0.05 (lower accruals)
    # Sid 1: Assets=1000, NI=80, OCF=50 -> Accruals = (80 - 50)/1000 = +0.03 (higher accruals)
    _insert_fund(ctx, 4, "balance", "A", "2026-03-31", "Total Assets", 1000.0)
    _insert_fund(ctx, 4, "balance", "A", "2026-03-31", "Current Liabilities", 600.0)
    _insert_fund(ctx, 4, "income", "A", "2026-03-31", "Net Income", 50.0)
    _insert_fund(ctx, 4, "cashflow", "A", "2026-03-31", "Operating Cash Flow", 100.0)

    # Sids 5, 6, 7 to give Technology group >= 5 finite accrual values
    for sid, ni, ocf in [(5, 60.0, 60.0), (6, 40.0, 50.0), (7, 70.0, 50.0)]:
        _insert_fund(ctx, sid, "balance", "A", "2026-03-31", "Total Assets", 1000.0)
        _insert_fund(ctx, sid, "income", "A", "2026-03-31", "Net Income", ni)
        _insert_fund(ctx, sid, "cashflow", "A", "2026-03-31", "Operating Cash Flow", ocf)

    sids_list = [1, 2, 3, 4, 5, 6, 7]
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": s} for s in sids_list]),
        groups=pd.Series({s: ("Financial Services" if s == 2 else "Technology") for s in sids_list}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)

    # ROCE test
    roce = Roce()
    roce_res = roce.compute(inputs)
    assert pytest.approx(roce_res.loc[1], rel=1e-3) == 0.25
    assert np.isnan(roce_res.loc[2])  # Excluded financial
    assert np.isnan(roce_res.loc[3])  # Negative denominator

    # Accruals test & negative direction standardisation
    accruals = Accruals()
    acc_res = accruals.compute(inputs)
    assert pytest.approx(acc_res.loc[1], rel=1e-3) == 0.03
    assert np.isnan(acc_res.loc[2])   # Excluded financial
    assert pytest.approx(acc_res.loc[4], rel=1e-3) == -0.05

    # Standardise accruals: Sid 4 (low accruals) must get HIGHER oriented z than Sid 1
    groups = draft.groups
    res_df = standardise(acc_res, groups, direction=accruals.spec.direction)
    z = res_df["z"]
    assert z.loc[4] > z.loc[1]


def test_quality_factors_cash_conv_and_roe_stability(ctx):
    """cash_conversion_3y requires 3 FY and positive NI; roe_stability_3y uses population SD and positive equity."""
    for sid in [1, 2]:
        ctx.conn.execute(f"INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES ({sid}, 'INE00{sid}', 'Stock{sid}', '2025-01-01', '2026-09-30', 'listed')")

    # 3 FY for sid 1: OCF = 100, 120, 140 -> sum = 360; NI = 80, 90, 100 -> sum = 270. Cash conv = 360/270 = 1.333
    # Equity = 400, 500, 600 -> ROE = 80/400=0.20, 90/500=0.18, 100/600=0.1667
    for yr, ocf, ni, eq in [("2024-03-31", 100.0, 80.0, 400.0), ("2025-03-31", 120.0, 90.0, 500.0), ("2026-03-31", 140.0, 100.0, 600.0)]:
        _insert_fund(ctx, 1, "cashflow", "A", yr, "Operating Cash Flow", ocf)
        _insert_fund(ctx, 1, "income", "A", yr, "Net Income", ni)
        _insert_fund(ctx, 1, "balance", "A", yr, "Stockholders Equity", eq)

    # Sid 2: only 2 FY (insufficient periods)
    for yr, ocf, ni, eq in [("2025-03-31", 10.0, 10.0, 100.0), ("2026-03-31", 10.0, 10.0, 100.0)]:
        _insert_fund(ctx, 2, "cashflow", "A", yr, "Operating Cash Flow", ocf)
        _insert_fund(ctx, 2, "income", "A", yr, "Net Income", ni)
        _insert_fund(ctx, 2, "balance", "A", yr, "Stockholders Equity", eq)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}, {"security_id": 2}]),
        groups=pd.Series({1: "Technology", 2: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)

    cc = CashConversion3y()
    cc_res = cc.compute(inputs)
    assert pytest.approx(cc_res.loc[1], rel=1e-3) == 360.0 / 270.0
    assert np.isnan(cc_res.loc[2])  # missing 3rd year

    roe_stab = RoeStability3y()
    roe_res = roe_stab.compute(inputs)
    assert not np.isnan(roe_res.loc[1])
    assert np.isnan(roe_res.loc[2])


def test_value_factors_earnings_yield_and_book_to_price(ctx):
    """earnings_yield uses EBIT/EV for nonfinancials, NI/mcap for financials; book_to_price preserves negative equity."""
    for sid in [1, 2, 3]:
        ctx.conn.execute(f"INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES ({sid}, 'INE00{sid}', 'Stock{sid}', '2025-01-01', '2026-09-30', 'listed')")

    # Attributes
    ctx.conn.execute("INSERT INTO security_attributes (captured_at, security_id, ev_inr, mcap_inr, source_sha256) VALUES ('2026-09-30T18:29:59.999999Z', 1, 2000.0, 1800.0, 'sha1')")
    ctx.conn.execute("INSERT INTO security_attributes (captured_at, security_id, ev_inr, mcap_inr, source_sha256) VALUES ('2026-09-30T18:29:59.999999Z', 2, 5000.0, 4000.0, 'sha2')")
    ctx.conn.execute("INSERT INTO security_attributes (captured_at, security_id, ev_inr, mcap_inr, source_sha256) VALUES ('2026-09-30T18:29:59.999999Z', 3, 1000.0, 1000.0, 'sha3')")

    # Sid 1: nonfinancial EBIT = 200 -> EY = 200 / 2000 = 0.10
    for q_end in ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]:
        _insert_fund(ctx, 1, "income", "Q", q_end, "EBIT", 50.0)
    _insert_fund(ctx, 1, "balance", "A", "2026-03-31", "Stockholders Equity", 900.0)

    # Sid 2: financial NI = 400 -> EY = 400 / 4000 = 0.10
    for q_end in ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]:
        _insert_fund(ctx, 2, "income", "Q", q_end, "Net Income", 100.0)
    _insert_fund(ctx, 2, "balance", "A", "2026-03-31", "Stockholders Equity", 2000.0)

    # Sid 3: negative equity = -300 -> B/P = -300 / 1000 = -0.30
    _insert_fund(ctx, 3, "balance", "A", "2026-03-31", "Stockholders Equity", -300.0)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}, {"security_id": 2}, {"security_id": 3}]),
        groups=pd.Series({1: "Technology", 2: "Financial Services", 3: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)

    ey = EarningsYield()
    ey_res = ey.compute(inputs)
    assert pytest.approx(ey_res.loc[1], rel=1e-3) == 0.10
    assert pytest.approx(ey_res.loc[2], rel=1e-3) == 0.10

    bp = BookToPrice()
    bp_res = bp.compute(inputs)
    assert pytest.approx(bp_res.loc[1], rel=1e-3) == 900.0 / 1800.0
    assert pytest.approx(bp_res.loc[3], rel=1e-3) == -0.30


def test_growth_and_flows_factors(ctx):
    """eps_growth_3y requires positive endpoints without default growth; earn_mom requires 8 quarters; flows requires 4 captures."""
    for sid in [1, 2]:
        ctx.conn.execute(f"INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES ({sid}, 'INE00{sid}', 'Stock{sid}', '2025-01-01', '2026-09-30', 'listed')")

    # EPS 3y: Sid 1 has positive endpoints: 2023=10.0, 2026=20.0 -> log(20/10)/3
    # Sid 2 has negative base: 2023=-5.0, 2026=10.0 -> must be NaN (NO default growth!)
    for yr, eps1, eps2 in [("2023-03-31", 10.0, -5.0), ("2024-03-31", 12.0, 2.0), ("2025-03-31", 15.0, 6.0), ("2026-03-31", 20.0, 10.0)]:
        _insert_fund(ctx, 1, "income", "A", yr, "Diluted EPS", eps1)
        _insert_fund(ctx, 2, "income", "A", yr, "Diluted EPS", eps2)

    # Earn mom: 8 quarters of Net Income for Sid 1
    # Offset 0 quarters (newest 4): 2026 Q1..Q4 -> 50 each -> sum = 200
    # Offset 4 quarters (older 4): 2025 Q1..Q4 -> 25 each -> sum = 100
    # Earn mom = (200 - 100) / abs(100) = 1.0
    q_dates = [
        "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30",
        "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30",
    ]
    for q in q_dates[:4]:
        _insert_fund(ctx, 1, "income", "Q", q, "Net Income", 25.0)
    for q in q_dates[4:]:
        _insert_fund(ctx, 1, "income", "Q", q, "Net Income", 50.0)

    # Sid 2 only has 4 quarters -> earn mom must be NaN
    for q in q_dates[4:]:
        _insert_fund(ctx, 2, "income", "Q", q, "Net Income", 50.0)

    # Holdings captures for flows: Sid 1 has 4 monthly captures
    for m, pct in [("2026-06-30", 0.20), ("2026-07-31", 0.22), ("2026-08-31", 0.24), ("2026-09-30", 0.28)]:
        ctx.conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, source) VALUES (?, ?, ?, 'yahoo')",
            (1, f"{m}T18:29:59.999999Z", pct),
        )
    # Sid 2 only has 2 monthly captures -> flows must be NaN
    for m, pct in [("2026-08-31", 0.15), ("2026-09-30", 0.16)]:
        ctx.conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, source) VALUES (?, ?, ?, 'yahoo')",
            (2, f"{m}T18:29:59.999999Z", pct),
        )

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}, {"security_id": 2}]),
        groups=pd.Series({1: "Technology", 2: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    inputs = build_inputs(ctx, draft)

    eps_growth = EpsGrowth3y()
    eps_res = eps_growth.compute(inputs)
    assert pytest.approx(eps_res.loc[1], rel=1e-3) == np.log(20.0 / 10.0) / 3.0
    assert np.isnan(eps_res.loc[2])  # negative endpoint -> NaN

    mom = EarnMom()
    mom_res = mom.compute(inputs)
    assert pytest.approx(mom_res.loc[1], rel=1e-3) == 1.0
    assert np.isnan(mom_res.loc[2])  # only 4 quarters -> NaN

    flows = InstHoldChg3m()
    flow_res = flows.compute(inputs)
    assert pytest.approx(flow_res.loc[1], rel=1e-3) == 0.28 - 0.20  # lag0 - lag3
    assert np.isnan(flow_res.loc[2])  # insufficient captures
