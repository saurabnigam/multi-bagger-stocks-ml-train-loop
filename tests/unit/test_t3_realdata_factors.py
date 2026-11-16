"""T3: real-data regression tests for the 24 launch factor classes.

Real-world motivation: every unit test elsewhere in this suite feeds a factor
class a handful of numbers picked to exercise one branch of its formula. That
proves the arithmetic is right for numbers a human chose. It has never once
proven the arithmetic is right for numbers a *vendor* chose -- a genuine
Yahoo/yfinance multi-ticker download, with its two-row header, its mix of
split-adjusted and raw fields, its missing quarters, and its NaNs in odd
places. A factor can pass every hand-built unit test and still be wrong on
real data if a single off-by-one in a lookback window, or a max/min swap,
never gets exercised by hand-picked inputs that happen to be symmetric.

This module builds a small (~450 KB) fixture from the raw vendor archives
used for the 2026-09-11 cohort (`data/archive/captures/{prices,yahoo}/...`,
read-only, never modified) for 8 real Nifty 500 names spanning Healthcare
(LUPIN), Auto (MRF), Consumer Services (NYKAA, DMART), Metals (JSWSTEEL),
Capital Goods (KAYNES), Financial Services (PNB) and FMCG (ZYDUSWELL, whose
non-consecutive quarterly filings exercise the "insufficient periods" NaN
paths). It ingests that fixture through the *real* production paths --
`PriceStore.ingest` / `quant.data.capture.ingest_archive` (which calls
`quant.data.fundamentals.ingest`, `quant.data.holdings.capture` and
`quant.data.attributes.capture` on real `RawBundle` objects) -- builds a real
`FactorInputs` via `quant.factors.inputs.build`, and computes every one of
the 24 classes in `quant.factors.registry.LAUNCH_FACTOR_CLASSES`.

Expected values for the price and fundamentals factors come from
`tests/fixtures/realdata_2026_09_11/expected_*.csv`, which are a trimmed copy
of an *independent* verification pass (raw vendor archive -> pandas/numpy,
never importing `quant`) that separately matched the live engine to 1e-9
(price) and 1e-6 (fundamentals) relative tolerance across the full 2026-09-11
sample -- see `docs analysis / scratchpad verify` for that computation. This
test does not re-derive those formulas from scratch; it re-uses that
independent computation as the oracle, which is exactly what "never copy
expected values from the code under test" requires: the oracle was written
and matched against the engine *before* this test existed and without
importing `quant`.

For the four control/legacy diagnostics that the independent pass did not
cover (`size`, `liq`, `beta_252`, `dc_flag` -- MASTER_SPEC 5.3, "control and
legacy families are diagnostics, never weighted or promoted"), this module
hand-derives expected values from the raw fixture files directly, in
`_hand_compute_controls` below, documenting each formula inline.

Structural NaNs are asserted, not worked around: `earn_mom` needs 8
consecutive quarters and every one of these 8 names has at most 5 in this
window; `inst_hold_chg_3m` needs 4 distinct monthly holdings captures and a
single archive replay produces exactly one. Both must come back NaN for
every name -- a factor silently returning a number here would mean the
"insufficient periods" guard broke.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.capture import ingest_archive
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.factors.controls import Beta252, Liq, Size
from quant.factors.flows import InstHoldChg3m
from quant.factors.growth import EarnMom, EpsGrowth3y, RevGrowth3y
from quant.factors.inputs import build as build_inputs
from quant.factors.legacy import DcFlag
from quant.factors.low_risk import MaxRet21, Vol252
from quant.factors.momentum import Dist52wHigh, Mom6_1, Mom12_1, Rev1m, Trend200
from quant.factors.quality import Accruals, CashConversion3y, Leverage, Roce, RoeStability3y
from quant.factors.registry import LAUNCH_FACTOR_CLASSES
from quant.factors.value import BookToPrice, DivYield, EarningsYield, FcfYield
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock

FIXDIR = Path(__file__).resolve().parents[1] / "fixtures" / "realdata_2026_09_11"
AS_OF = "2026-09-11"
CUTOFF = "2026-09-11T18:29:59.999999Z"

# security_id, yahoo_ticker and sector_group are taken from the same cohort sample
# (`sample.csv`) that the independent verifier used to build expected_*.csv, so the
# ids line up by construction, not by re-derivation here.
TICKERS = {
    "LUPIN":     ("LUPIN.NS",     307, "INE326A01037", "Healthcare"),
    "MRF":       ("MRF.NS",       309, "INE883A01011", "Automobile and Auto Components"),
    "NYKAA":     ("NYKAA.NS",     164, "INE388Y01029", "Consumer Services"),
    "JSWSTEEL":  ("JSWSTEEL.NS",  265, "INE019A01038", "Metals & Mining"),
    "KAYNES":    ("KAYNES.NS",    285, "INE918Z01012", "Capital Goods"),
    "DMART":     ("DMART.NS",      56, "INE192R01011", "Consumer Services"),
    "PNB":       ("PNB.NS",       383, "INE160A01022", "Financial Services"),
    "ZYDUSWELL": ("ZYDUSWELL.NS", 498, "INE768C01028", "Fast Moving Consumer Goods"),
}
SYM_BY_SID = {sid: sym for sym, (_, sid, _, _) in TICKERS.items()}
SIDS = sorted(sid for (_, sid, _, _) in TICKERS.values())

PRICE_REL_TOL = 1e-9
FUND_REL_TOL = 1e-6


@pytest.fixture
def ctx(tmp_path):
    """Real ingest paths: PriceStore.ingest + capture.ingest_archive over RawBundle objects.

    Nothing here is a synthetic INSERT of a factor's own denominator -- every fundamental,
    holdings and attribute row a factor reads was put there by the same
    fundamentals.ingest/holdings.capture/attributes.capture code the monthly pipeline calls,
    fed real vendor bytes replayed from `tests/fixtures/realdata_2026_09_11/yahoo_bundles.json`
    (a straight subset, by ticker, of the archived `cap_yah_95652802e4c5.json` capture -- same
    JSON shape `YahooClient.archive` writes, so `load_archive` reads it unmodified).
    """
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"

    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = load_config().with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock(CUTOFF)
    actor = Actor(kind="system", name="t3_realdata")

    run_ctx = RunContext(as_of=AS_OF, kind="production", track="live", cfg=cfg, clock=clock, actor=actor)
    with run_ctx as c:
        store = PriceStore(path=prices_path, state_conn=c.conn)
        c.store = store

        for sym, (yahoo_ticker, sid, isin, _sector) in TICKERS.items():
            c.conn.execute(
                "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                "VALUES (?, ?, ?, '2015-01-01', ?, 'listed')",
                (sid, isin, sym, AS_OF),
            )
            c.conn.execute(
                "INSERT OR IGNORE INTO symbol_history "
                "(security_id, nse_symbol, yahoo_ticker, valid_from, valid_to, source) "
                "VALUES (?, ?, ?, '2015-01-01', NULL, 'fixture')",
                (sid, sym, yahoo_ticker),
            )

        # Real path: quant.data.capture.ingest_archive -> fundamentals.ingest / holdings.capture
        # / attributes.capture, all against real RawBundle objects reconstructed by load_archive.
        ingest_res = ingest_archive(c, FIXDIR / "yahoo_bundles.json", client=None, prices=False)
        assert ingest_res.counts["mapped"] == 8
        assert ingest_res.counts["unmapped"] == 0

        # Real path: PriceStore.ingest -> normalize_source, close_basis "split_adjusted" exactly
        # as the live capture pipeline calls it for a yfinance download (MASTER_SPEC 4.3).
        prices_df = pd.read_csv(FIXDIR / "prices_320.csv")
        for sym, (_yahoo_ticker, sid, _isin, _sector) in TICKERS.items():
            sub = prices_df[prices_df["nse_symbol"] == sym]
            source_df = pd.DataFrame({
                "date": sub["date"].values,
                "close": sub["Close"].values,
                "volume": sub["Volume"].values,
                "dividend": sub["Dividends"].fillna(0.0).values,
                "split_ratio": sub["StockSplits"].fillna(0.0).replace(0.0, 1.0).values,
            })
            meta = {
                "security_id": sid,
                "close_basis": "split_adjusted",
                "volume_basis": "delivered",
                "dividend_basis": "delivered",
                "observed_at": "2026-09-11T10:06:00.000000Z",
                "capture_id": "cap_test_fixture",
                "source_sha256": "sha_test_fixture",
            }
            res = store.ingest(c, source_df, meta)
            assert res.counts["rows"] == len(sub), f"{sym}: expected {len(sub)} rows ingested"

        yield c


def _draft() -> Draft:
    groups = pd.Series({sid: sector for (_, sid, _, sector) in TICKERS.values()})
    return Draft(
        cohort_id="C-2026-09-11-live",
        as_of=AS_OF,
        track="live",
        knowledge_cutoff=CUTOFF,
        definition_hash="t3_realdata",
        members=pd.DataFrame([{"security_id": s} for s in SIDS]),
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )


def _by_symbol(series: pd.Series) -> pd.Series:
    return series.rename(index=SYM_BY_SID)


def _assert_close_or_nan(actual: float, expected: float, tol: float, label: str):
    if pd.isna(expected):
        assert pd.isna(actual), f"{label}: expected NaN, engine gave {actual!r}"
    else:
        assert not pd.isna(actual), f"{label}: expected {expected!r}, engine gave NaN"
        rel = abs(actual - expected) / max(abs(expected), 1e-12)
        assert rel <= tol, f"{label}: expected {expected!r}, engine gave {actual!r} (rel diff {rel:.3e} > {tol:.0e})"


# --------------------------------------------------------------------------------------- price


def test_launch_factors_complete_coverage():
    """Every class in LAUNCH_FACTOR_CLASSES (24 per MASTER_SPEC 5.3) is exercised below."""
    exercised = {
        "mom_12_1", "trend_200", "mom_6_1", "dist_52w_high", "rev_1m", "vol_252", "max_ret_21",
        "roce", "accruals", "cash_conversion_3y", "earnings_yield", "book_to_price",
        "eps_growth_3y", "rev_growth_3y", "leverage", "fcf_yield", "div_yield", "roe_stability_3y",
        "earn_mom", "inst_hold_chg_3m",
        "size", "liq", "beta_252", "dc_flag",
    }
    assert exercised == set(LAUNCH_FACTOR_CLASSES.keys()), (
        "a launch factor class was added or removed without updating this real-data suite: "
        f"missing={set(LAUNCH_FACTOR_CLASSES) - exercised}, extra={exercised - set(LAUNCH_FACTOR_CLASSES)}"
    )


def test_price_factors_against_independent_recomputation(ctx):
    """mom_12_1, mom_6_1, trend_200, vol_252, dist_52w_high, rev_1m, max_ret_21 at 1e-9 relative.

    Expected values are the independent (raw-archive, no `quant` import) recomputation in
    expected_price_factors.csv, which separately matched the live engine to a max relative
    diff of 7.2e-13 across the full 58-name verification sample (see
    scratchpad/verify/price/summary_exact_agreement.csv). This is the primary regression
    surface for M10 (mom_12_1 12-1 window off by one bar) and M12 (max_ret_21 computing min
    instead of max).
    """
    expected = pd.read_csv(FIXDIR / "expected_price_factors.csv").set_index("nse_symbol")

    draft = _draft()
    inputs = build_inputs(ctx, draft)

    checks = [
        (Mom12_1(), "mom_12_1"),
        (Mom6_1(), "mom_6_1"),
        (Trend200(), "trend_200"),
        (Vol252(), "vol_252"),
        (Dist52wHigh(), "dist_52w_high"),
        (Rev1m(), "rev_1m"),
        (MaxRet21(), "max_ret_21"),
    ]
    for factor, col in checks:
        raw = _by_symbol(factor.compute(inputs))
        for sym in TICKERS:
            _assert_close_or_nan(raw[sym], expected.loc[sym, col], PRICE_REL_TOL, f"{col}[{sym}]")
        # None of these 8 names are structurally NaN for these factors (all have >= 253 bars
        # in the 320-session fixture window) -- a real value is expected for every one.
        assert raw.notna().all(), f"{col}: unexpected NaN in {raw[raw.isna()].index.tolist()}"


def test_mom_12_1_rejects_off_by_one_window(ctx):
    """M10: shifting the 12-month endpoint from s.iloc[-253] to s.iloc[-252] must change the value.

    This is a targeted companion to the tolerance check above: it pins down *why* mom_12_1
    must read exactly 252 trading days back by asserting against the closed-form TRI ratio
    at the correct offset, independently reconstructed from the ingested store (not from the
    factor under test), so a one-bar shift cannot accidentally still satisfy the assertion.
    """
    draft = _draft()
    inputs = build_inputs(ctx, draft)
    tri_df = inputs.tri(lookback_days=252)

    mom = Mom12_1().compute(inputs)
    for sid in SIDS:
        s = tri_df[sid].dropna()
        assert len(s) >= 253
        correct = math.log(float(s.iloc[-22]) / float(s.iloc[-253]))
        off_by_one = math.log(float(s.iloc[-22]) / float(s.iloc[-252]))
        assert abs(correct - off_by_one) > 1e-6, "fixture too flat to distinguish -253 from -252 for this name"
        assert mom.loc[sid] == pytest.approx(correct, rel=1e-9), (
            f"{SYM_BY_SID[sid]}: mom_12_1 must use TRI at -252 trading days (s.iloc[-253]), "
            f"got {mom.loc[sid]!r} vs correct {correct!r} (off-by-one value would be {off_by_one!r})"
        )


def test_max_ret_21_rejects_min_swap(ctx):
    """M12: max_ret_21 must be the BEST day in 21, not the worst -- a max/min swap must fail loudly.

    Independently recomputes both the max and the min of the trailing-21 arithmetic TRI
    returns from the ingested store and asserts the factor equals the max and (for every name
    where the 21-day range is non-degenerate) is strictly greater than the min, so a max/min
    swap cannot pass by coincidence on a flat window.
    """
    draft = _draft()
    inputs = build_inputs(ctx, draft)
    tri_df = inputs.tri(lookback_days=21)

    max_ret = MaxRet21().compute(inputs)
    for sid in SIDS:
        s = tri_df[sid].dropna()
        last_22 = s.iloc[-22:].values
        rets = last_22[1:] / last_22[:-1] - 1.0
        correct_max = float(np.max(rets))
        correct_min = float(np.min(rets))
        assert correct_max > correct_min, "fixture window has zero range for this name; cannot distinguish max/min"
        assert max_ret.loc[sid] == pytest.approx(correct_max, rel=1e-9), (
            f"{SYM_BY_SID[sid]}: max_ret_21 must be max(21 daily TRI returns)={correct_max!r}, "
            f"got {max_ret.loc[sid]!r} (min would be {correct_min!r})"
        )


# --------------------------------------------------------------------------------- fundamentals


def test_fundamentals_factors_against_independent_recomputation(ctx):
    """roce, accruals, cash_conversion_3y, earnings_yield, book_to_price, eps_growth_3y,
    rev_growth_3y, leverage, fcf_yield, div_yield at 1e-6 relative (or NaN where the
    independent pass found an insufficient-periods / non-financial / non-positive-denominator
    guard applies -- e.g. roce/accruals/cash_conversion_3y/leverage/fcf_yield are NaN for PNB,
    a financial, since those formulas explicitly exclude financials; div_yield is NaN for
    NYKAA/KAYNES/DMART, which pay no dividend).
    """
    expected = pd.read_csv(FIXDIR / "expected_fundamentals.csv").set_index("nse_symbol")

    draft = _draft()
    inputs = build_inputs(ctx, draft)

    checks = [
        (Roce(), "roce_mine"),
        (Accruals(), "accruals_mine"),
        (CashConversion3y(), "cash_conversion_3y_mine"),
        (EarningsYield(), "earnings_yield_mine"),
        (BookToPrice(), "book_to_price_mine"),
        (EpsGrowth3y(), "eps_growth_3y_mine"),
        (RevGrowth3y(), "rev_growth_3y_mine"),
        (Leverage(), "leverage_mine"),
        (FcfYield(), "fcf_yield_mine"),
        (DivYield(), "div_yield_mine"),
        (RoeStability3y(), "roe_stability_3y_mine"),
    ]
    for factor, col in checks:
        raw = _by_symbol(factor.compute(inputs))
        for sym in TICKERS:
            _assert_close_or_nan(raw[sym], expected.loc[sym, col], FUND_REL_TOL, f"{factor.spec.name}[{sym}]")


def test_roce_and_leverage_exclude_financials(ctx):
    """PNB (Financial Services) must read NaN for the nonfinancial-only quality/value factors."""
    draft = _draft()
    inputs = build_inputs(ctx, draft)
    pnb = 383
    for factor in (Roce(), Accruals(), CashConversion3y(), Leverage(), FcfYield()):
        val = factor.compute(inputs).loc[pnb]
        assert np.isnan(val), f"{factor.spec.name}: PNB is a financial and must be NaN, got {val!r}"


def test_earn_mom_and_inst_hold_chg_3m_are_structurally_nan(ctx):
    """earn_mom needs 8 consecutive quarters; inst_hold_chg_3m needs 4 monthly captures.

    Every one of these 8 names has at most 5 quarterly filings in this window
    (expected_fundamentals.csv: max_consecutive_quarters_from_latest <= 5) and this fixture's
    single archive replay produces exactly one holdings capture per security -- both fall
    strictly short of what each factor's own docstring requires, so both factors must return
    NaN for every name here, not silently compute on a shorter, wrong window.
    """
    expected = pd.read_csv(FIXDIR / "expected_fundamentals.csv").set_index("nse_symbol")
    assert (expected["max_consecutive_quarters_from_latest"] < 8).all(), \
        "fixture assumption violated: a name now has >= 8 consecutive quarters"

    draft = _draft()
    inputs = build_inputs(ctx, draft)

    earn_mom = _by_symbol(EarnMom().compute(inputs))
    flows = _by_symbol(InstHoldChg3m().compute(inputs))
    for sym in TICKERS:
        assert np.isnan(earn_mom[sym]), f"earn_mom[{sym}]: expected structural NaN (< 8 quarters), got {earn_mom[sym]!r}"
        assert np.isnan(flows[sym]), f"inst_hold_chg_3m[{sym}]: expected structural NaN (1 capture), got {flows[sym]!r}"


# ------------------------------------------------------------------------------------ controls


def _hand_compute_controls():
    """Independently derive size, liq and dc_flag straight from the raw fixture files.

    These four (size, liq, beta_252, dc_flag) are the "control"/"legacy" families that
    MASTER_SPEC 5.3 says are diagnostics only, never weighted -- the independent
    verification pass did not recompute them, so this helper does, reading only
    tests/fixtures/realdata_2026_09_11/{yahoo_bundles.json,prices_320.csv} and never the
    `quant` package:

      size     = log(mcap_inr), mcap_inr = the vendor's raw `marketCap` info field.
      liq      = log(ADV63), ADV63 = mean(close * volume) over the trailing min(63, n) bars
                 (MASTER_SPEC 5.3; equivalent to close_raw * volume_raw here because none of
                 these 8 names has a split inside the trailing-63-session window, so the
                 split-adjustment multiplier is 1.0 for every one of those bars).
      dc_flag  = 1.0 if last_close < SMA50 < SMA200 (split-consistent close) else 0.0.
    """
    with open(FIXDIR / "yahoo_bundles.json") as f:
        bundles = json.load(f)
    mcap_by_ticker = {b["ticker"]: b["info"].get("marketCap") for b in bundles}

    prices = pd.read_csv(FIXDIR / "prices_320.csv")
    out = {}
    for sym, (yahoo_ticker, sid, _isin, _sector) in TICKERS.items():
        sub = prices[prices["nse_symbol"] == sym].sort_values("date")
        close = sub["Close"].values.astype(float)
        volume = sub["Volume"].values.astype(float)

        mcap = mcap_by_ticker[yahoo_ticker]
        size = math.log(mcap)

        window = min(63, len(sub))
        adv63 = float(np.mean(close[-window:] * volume[-window:]))
        liq = math.log(adv63)

        sma50 = float(np.mean(close[-50:]))
        sma200 = float(np.mean(close[-200:]))
        curr = float(close[-1])
        dc_flag = 1.0 if (curr < sma50 < sma200) else 0.0

        out[sym] = {"size": size, "liq": liq, "dc_flag": dc_flag}
    return out


def test_size_liq_dc_flag_against_hand_derivation(ctx):
    """size/liq/dc_flag (control + legacy diagnostics) against a from-scratch hand computation."""
    hand = _hand_compute_controls()

    draft = _draft()
    inputs = build_inputs(ctx, draft)

    size = _by_symbol(Size().compute(inputs))
    liq = _by_symbol(Liq().compute(inputs))
    dc_flag = _by_symbol(DcFlag().compute(inputs))

    for sym in TICKERS:
        _assert_close_or_nan(size[sym], hand[sym]["size"], 1e-9, f"size[{sym}]")
        _assert_close_or_nan(liq[sym], hand[sym]["liq"], 1e-9, f"liq[{sym}]")
        assert dc_flag[sym] == hand[sym]["dc_flag"], (
            f"dc_flag[{sym}]: expected {hand[sym]['dc_flag']!r} (close < SMA50 < SMA200), "
            f"got {dc_flag[sym]!r}"
        )


def test_beta_252_against_hand_derived_ols(ctx):
    """beta_252: OLS beta of daily TRI returns against an equal-weighted basket of these 8 names.

    Beta252.compute() falls back to `tri_df.mean(axis=1)` as its benchmark whenever
    `benchmark_tri("BM_NIFTY500_EW", ...)` is empty (quant/factors/controls.py) -- true here,
    since this fixture never ingests a benchmark index series. This helper reconstructs TRI
    for all 8 names directly from prices_320.csv (same split/dividend/TRI formula documented
    in quant/data/prices.py's module docstring: TRI[d] = TRI[d-1] * split_ratio[d] *
    (close_raw[d] + dividend_raw[d]) / close_raw[d-1]) and reproduces the documented OLS-beta
    formula (cov(security, benchmark) / var(benchmark), population-vs-sample details per
    MASTER_SPEC 5.3) independently of PriceStore.

    Tolerance is relative 1.5%, wider than the other checks: PriceStore.tri() additionally
    applies corporate-action adjustments (`_apply_actions_to_factors`) and reindexes onto its
    own per-security trading-session index before the equal-weighted mean is taken, neither of
    which this from-scratch reconstruction replicates (doing so would mean re-implementing
    PriceStore internals rather than deriving an independent oracle). The formula, direction
    and cross-sectional ordering are still verified exactly: KAYNES (a high-volatility, short-
    history capital-goods name) must have the highest beta, and every beta must be positive.
    """
    prices = pd.read_csv(FIXDIR / "prices_320.csv")

    def _build_tri(sub: pd.DataFrame) -> pd.Series:
        sub = sub.sort_values("date").reset_index(drop=True)
        close = sub["Close"].values.astype(float)
        div = sub["Dividends"].fillna(0.0).values.astype(float)
        splits = sub["StockSplits"].fillna(0.0).values.astype(float)
        split_ratio = np.where(splits <= 0, 1.0, splits)
        rev_cum = np.cumprod(split_ratio[::-1])[::-1]
        future_mult = rev_cum / split_ratio
        close_raw = close * future_mult
        div_raw = div * future_mult
        n = len(sub)
        tri = np.empty(n)
        tri[0] = 100.0
        for i in range(1, n):
            tri[i] = tri[i - 1] * split_ratio[i] * (close_raw[i] + div_raw[i]) / close_raw[i - 1]
        return pd.Series(tri, index=pd.to_datetime(sub["date"]))

    tri_by_sym = {sym: _build_tri(prices[prices["nse_symbol"] == sym]) for sym in TICKERS}
    tri_df = pd.DataFrame(tri_by_sym)
    bm_tri = tri_df.mean(axis=1).dropna()
    bm_rets = bm_tri.pct_change().dropna()

    hand_beta = {}
    for sym in TICKERS:
        s_rets = tri_df[sym].dropna().pct_change().dropna()
        aligned = pd.concat([s_rets, bm_rets], axis=1, join="inner").dropna()
        assert len(aligned) >= 200
        y = aligned.iloc[:, 0].values
        x = aligned.iloc[:, 1].values
        cov = np.cov(x, y)[0, 1]
        var_x = np.var(x, ddof=1)
        hand_beta[sym] = float(cov / var_x)

    draft = _draft()
    inputs = build_inputs(ctx, draft)
    beta = _by_symbol(Beta252().compute(inputs))

    for sym in TICKERS:
        _assert_close_or_nan(beta[sym], hand_beta[sym], 1.5e-2, f"beta_252[{sym}]")
        assert beta[sym] > 0, f"beta_252[{sym}]: expected a positive beta in this bull-tilted window, got {beta[sym]!r}"

    assert beta.idxmax() == "KAYNES", f"expected KAYNES to have the highest beta_252, got {beta.idxmax()} highest"
