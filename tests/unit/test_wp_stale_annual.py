"""T8: a latest-annual value older than max_annual_age_days before as_of is stale.

Real-data motivation (docs/analysis/verification_2026-09-24.md, task T8): when Yahoo's
newest annual column is null for a field, no row is stored for that fiscal year, so
"latest annual" silently falls back to the previous fiscal year. NESTLEIND on 2026-09-11
used FY2025 (17 months old) for Net Income, Total Assets, Current Liabilities, Stockholders
Equity and Total Debt. MASTER_SPEC 4.4 and 5.3 govern "latest annual"; a value this old is
not a legitimate "latest annual" reading, so it must come back NaN rather than silently
stand in for the missing current year.

Rule (default max_annual_age_days = 487, ~16 months): a security's newest admissible
annual period_end older than ``as_of - max_annual_age_days`` is stale. ``FactorInputs.
fundamental`` / ``fundamental_dated`` return NaN for every period of that security at
annual frequency; the ``ttm()`` latest-annual fallback returns NaN with flag
"stale_annual" instead of the stale value. Quarterly TTM is unaffected.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.data.fundamentals import ttm
from quant.db.core import apply_schema, connect
from quant.factors.inputs import build as build_inputs
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock

AS_OF = "2026-09-11"
CUTOFF = "2026-09-11T18:29:59.999999Z"
AVAILABLE = "2026-06-01T00:00:00.000000Z"
FETCHED = "2026-06-01T00:00:00.000000Z"


@pytest.fixture
def ctx(tmp_path):
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = load().with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock(CUTOFF)
    actor = Actor(kind="system", name="test")
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg, clock=clock, actor=actor) as c:
        yield c


def _insert_fund(ctx, sid, stmt, period_end, field, val, freq="A", fetched=FETCHED, available=AVAILABLE):
    ctx.conn.execute(
        """
        INSERT INTO fundamentals (
            security_id, statement, freq, period_end, field, value, unit,
            available_from, available_from_basis, fetched_at, source, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'inr', ?, 'lodr_60d', ?, 'yahoo', 1)
        """,
        (sid, stmt, freq, period_end, field, float(val), available, fetched),
    )


def _security(ctx, sid):
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (?, ?, 'X', '2020-01-01', '2026-09-11', 'listed')",
        (sid, f"INE00000000{sid}"),
    )


def _draft(sid, as_of=AS_OF, cutoff=CUTOFF):
    return Draft(
        cohort_id=f"C-{as_of}-live", as_of=as_of, track="live", knowledge_cutoff=cutoff,
        definition_hash="def", members=pd.DataFrame([{"security_id": sid}]),
        groups=pd.Series({sid: "Consumer"}), source_refs={},
        factor_values=pd.DataFrame(), model_weights=pd.DataFrame(), scores=pd.DataFrame(),
    )


def test_stale_latest_annual_masks_fundamental_and_fundamental_dated(ctx):
    """NESTLEIND-like: FY2026 missing, FY2025 present, as_of 2026-09-11 -> NaN, not FY2025."""
    sid = 1
    _security(ctx, sid)
    # FY2025 (2025-03-31) is the newest row on file; FY2026 was never ingested because
    # Yahoo's newest annual column was null for this field.
    _insert_fund(ctx, sid, "income", "2025-03-31", "Net Income", 100.0)
    _insert_fund(ctx, sid, "income", "2024-03-31", "Net Income", 90.0)

    inputs = build_inputs(ctx, _draft(sid))
    single = inputs.fundamental("income", "Net Income", "A", 1)
    assert np.isnan(single.loc[sid, 0]), single

    vals, dates = inputs.fundamental_dated("income", "Net Income", "A", 2)
    assert vals.loc[sid].isna().all(), vals
    # Dates are still reported for provenance even though the values are masked.
    assert dates.loc[sid, 0] == "2025-03-31"


def test_current_march_fy_annual_is_not_stale(ctx):
    """A March-FY company with FY2026 on file (164 days old at as_of) is unaffected."""
    sid = 2
    _security(ctx, sid)
    _insert_fund(ctx, sid, "balance", "2026-03-31", "Total Assets", 1000.0)

    inputs = build_inputs(ctx, _draft(sid))
    out = inputs.fundamental("balance", "Total Assets", "A", 1)
    assert out.loc[sid, 0] == pytest.approx(1000.0)


def test_dec_fy_company_at_450_days_is_not_stale(ctx):
    """450 days before as_of is inside the 487-day (~16 month) allowance."""
    sid = 3
    period_end = "2025-12-31"
    as_of = (pd.Timestamp(period_end) + pd.Timedelta(days=450)).strftime("%Y-%m-%d")
    cutoff = f"{as_of}T18:29:59.999999Z"
    _security(ctx, sid)
    _insert_fund(ctx, sid, "balance", period_end, "Stockholders Equity", 500.0,
                 available="2026-01-01T00:00:00.000000Z", fetched="2026-01-01T00:00:00.000000Z")

    inputs = build_inputs(ctx, _draft(sid, as_of=as_of, cutoff=cutoff))
    out = inputs.fundamental("balance", "Stockholders Equity", "A", 1)
    assert out.loc[sid, 0] == pytest.approx(500.0)


def test_dec_fy_company_at_488_days_is_stale(ctx):
    """One day past the 487-day default allowance is stale."""
    sid = 4
    period_end = "2025-12-31"
    as_of = (pd.Timestamp(period_end) + pd.Timedelta(days=488)).strftime("%Y-%m-%d")
    cutoff = f"{as_of}T18:29:59.999999Z"
    _security(ctx, sid)
    _insert_fund(ctx, sid, "balance", period_end, "Stockholders Equity", 500.0,
                 available="2026-01-01T00:00:00.000000Z", fetched="2026-01-01T00:00:00.000000Z")

    inputs = build_inputs(ctx, _draft(sid, as_of=as_of, cutoff=cutoff))
    out = inputs.fundamental("balance", "Stockholders Equity", "A", 1)
    assert np.isnan(out.loc[sid, 0])


def test_fund_fn_without_dates_is_unaffected(ctx):
    """A custom fund_fn with no dates function cannot know staleness -- behaves as today."""
    from quant.factors.inputs import FactorInputs

    members = pd.Index([5], name="security_id")

    def fund_fn(statement, field, freq, n_periods, sids):
        return pd.DataFrame({0: [100.0]}, index=sids)

    inputs = FactorInputs(
        as_of=AS_OF, cutoff=CUTOFF, members=members, sector_group=pd.Series(["A"], index=members),
        price_store=None, state_query_fn=lambda q, p: [], provenance_dict={}, fund_fn=fund_fn,
    )
    out = inputs.fundamental("income", "Net Income", "A", 1)
    assert out.loc[5, 0] == pytest.approx(100.0)


def test_config_default_is_487_days(ctx):
    cfg = load()
    assert getattr(cfg.factors, "max_annual_age_days", None) == 487


def test_config_override_is_honoured(ctx):
    """A narrower configured allowance stales a value the 487-day default would keep."""
    sid = 6
    period_end = "2025-12-31"
    as_of = (pd.Timestamp(period_end) + pd.Timedelta(days=450)).strftime("%Y-%m-%d")
    cutoff = f"{as_of}T18:29:59.999999Z"
    _security(ctx, sid)
    _insert_fund(ctx, sid, "balance", period_end, "Stockholders Equity", 500.0,
                 available="2026-01-01T00:00:00.000000Z", fetched="2026-01-01T00:00:00.000000Z")

    narrow_cfg = ctx.cfg.with_paths()
    narrow_cfg.factors.max_annual_age_days = 200
    ctx.cfg = narrow_cfg

    inputs = build_inputs(ctx, _draft(sid, as_of=as_of, cutoff=cutoff))
    out = inputs.fundamental("balance", "Stockholders Equity", "A", 1)
    assert np.isnan(out.loc[sid, 0])


def test_ttm_annual_fallback_stale_flag(ctx):
    """Quarters missing -> annual fallback; stale annual returns NaN, flag stale_annual."""
    sid = 7
    _security(ctx, sid)
    # Only 2 quarters on file (need 4): forces the annual fallback path.
    _insert_fund(ctx, sid, "income", "2026-06-30", "EBIT", 10.0, freq="Q")
    _insert_fund(ctx, sid, "income", "2026-03-31", "EBIT", 10.0, freq="Q")
    _insert_fund(ctx, sid, "income", "2025-03-31", "EBIT", 40.0, freq="A")

    vals, flags = ttm(ctx.conn, CUTOFF, "EBIT", [sid], as_of=AS_OF)
    assert np.isnan(vals[sid])
    assert flags[sid] == "stale_annual"


def test_ttm_annual_fallback_not_stale_uses_value(ctx):
    sid = 8
    _security(ctx, sid)
    _insert_fund(ctx, sid, "income", "2026-06-30", "EBIT", 10.0, freq="Q")
    _insert_fund(ctx, sid, "income", "2026-03-31", "EBIT", 10.0, freq="Q")
    _insert_fund(ctx, sid, "income", "2026-03-31", "EBIT", 40.0, freq="A")

    vals, flags = ttm(ctx.conn, CUTOFF, "EBIT", [sid], as_of=AS_OF)
    assert vals[sid] == pytest.approx(40.0)
    assert flags[sid] == "ttm_from_annual"


def test_ttm_max_annual_age_days_override(ctx):
    """A narrower override stales an annual fallback the 487-day default would accept."""
    sid = 9
    period_end = "2025-12-31"
    as_of = (pd.Timestamp(period_end) + pd.Timedelta(days=450)).strftime("%Y-%m-%d")
    _security(ctx, sid)
    _insert_fund(ctx, sid, "income", "2026-06-30", "EBIT", 10.0, freq="Q")
    _insert_fund(ctx, sid, "income", period_end, "EBIT", 40.0, freq="A")

    vals_default, flags_default = ttm(ctx.conn, f"{as_of}T18:29:59.999999Z", "EBIT", [sid], as_of=as_of)
    assert vals_default[sid] == pytest.approx(40.0)
    assert flags_default[sid] == "ttm_from_annual"

    vals_narrow, flags_narrow = ttm(ctx.conn, f"{as_of}T18:29:59.999999Z", "EBIT", [sid], as_of=as_of,
                                     max_annual_age_days=200)
    assert np.isnan(vals_narrow[sid])
    assert flags_narrow[sid] == "stale_annual"


def test_ttm_quarterly_path_unaffected_by_staleness(ctx):
    """A full 4 consecutive quarters never falls back to annual, regardless of its age."""
    sid = 10
    _security(ctx, sid)
    for q_end in ("2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"):
        _insert_fund(ctx, sid, "income", q_end, "EBIT", 25.0, freq="Q")

    vals, flags = ttm(ctx.conn, "2026-10-15T18:29:59.999999Z", "EBIT", [sid], as_of="2026-10-15",
                       max_annual_age_days=1)
    assert vals[sid] == pytest.approx(100.0)
    assert flags[sid] == ""
