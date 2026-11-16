# V2 Engine Design — "Pragmatic Incremental Builder"

**Status:** draft for review, written 2026-09-05 against branch `red-team-review-sep-2026`; revised the same day after live verification of yfinance fields, NSE constituent files and the working Python environment (Python 3.14.3, pandas 3.0.3, numpy 2.4.6, scipy 1.17.1, yfinance 1.4.1; **no** `pyarrow`, `duckdb`, `statsmodels` or `git-lfs` available). Every "verified" claim below names the date it was checked.
**Lens:** what a coding LLM can build reliably, in small verifiable increments, on SQLite + pandas + vanilla JS, reusing what exists. A working month-1 loop beats a perfect month-12 design. Every component ships with a test that proves it.
**Audience:** an implementer LLM with no access to the conversation that produced this. All paths are repository-relative unless absolute. All numbers are targets or measurements and are labelled as such.

> **The design in three sentences.** Keep the plumbing pattern that already works (snapshot → forward return → evaluate → adjust) but move it onto point-in-time tables, adjusted total returns, continuous sector-neutral factors, and an equal-weight baseline that learned weights must beat before they are trusted. Make the git-committed source of truth a set of append-only monthly CSV files plus a rebuildable SQLite database, so five years of monthly runs do not bloat the repository. Get a learning curve on screen in month 1 by backfilling ten years of *price-derived* factors (clearly labelled as survivorship-biased "tier B" evidence) while the *fundamental* factors accumulate honestly, one clean month at a time.

Where this design disagrees with the seed hypotheses in `docs/spec/00_context_brief.md` §5, it says so in a box like this:

> **Seed challenged.** ...

---

**Contents**

1. Objective & success metrics
2. Prediction target & horizons
3. Universe & sector taxonomy
4. Data layer
5. Factor library
6. Scoring model & weight learning
7. Evaluation protocol
8. Portfolio & cost model
9. Feedback loop & knowledge base
10. Architecture
11. Phased roadmap
12. Risks, failure modes and open questions

Reuse of existing code is §10.8, the acceptance-test index §10.9, and the 4-week cut §11.6.

---

## 1. Objective & success metrics

### 1.1 Objective

Build a monthly loop that ranks the Nifty 500 by a sector-neutral multi-factor score, records every input point-in-time, measures its own out-of-sample predictive power with statistics that respect overlapping returns, and only changes its own weights when the accumulated evidence clears a pre-declared bar. The purpose of the ranking is to find stocks that compound over multiple years; the loop's job is to find out whether it can.

### 1.2 What "success" means, by date

All values below are **targets**, not measurements. Nothing in this repository has yet demonstrated out-of-sample alpha (see `docs/analysis/red_team_review.md`).

```
Milestone   Date        Target (measurable, binary)
---------   ----------  ----------------------------------------------------------------------
M1          2026-10-31  `python -m quant run-month --as-of 2026-09-30` completes end-to-end on
                        real data in < 60 min on a laptop. Legacy DB migrated (2,543 rows).
                        10-year daily price cache built (~1.3 M rows). >= 120 pytest tests pass
                        offline. First learning-curve chart rendered (tier-B price factors only).
M3          2026-12-31  3 clean monthly fundamental snapshots. Tier-B backtest of price factors
                        2016-2026 published in knowledge/reports/ with HAC t-stats. DQ gate pass
                        rate 3/3.
M6          2027-03-31  3-month labels complete for the first four Tier-A snapshots (as_of
                        2026-09-30 .. 2026-12-31). Shuffle and planted-signal leakage tests run in
                        CI every month. Paper portfolios (built at M3, replayed deterministically
                        from the stored scores back to 2026-09-30) show 6 months of net-of-cost
                        record against the benchmark set.
M12         2027-09-30  12 clean snapshots (2026-09-30 .. 2027-08-31); 10 labelled 3M periods
                        (as_of <= 2027-06-30); 12 learning-curve points. Weights are STILL equal
                        (gate closed, see §6.4). At least one challenger has a 12-month paper record.
M15         2027-12-01  Gate opens at the run for as_of 2027-11-30: the 12th snapshot (2027-08-31)
                        now has a complete 3M label, n_eff = 4. First permitted deviation from
                        equal weights, shrunk to alpha = 0.14 (§6.3).
M36         2029-09-30  Statistical targets below become testable.
```

### 1.3 Numeric targets at month 36 (and what falsifies the approach)

Decode: "3M sector-neutral Rank IC" is the Spearman correlation, at one month-end, between each stock's composite score and its subsequent 3-month total return minus the median 3-month return of its sector group. "HAC t" is the t-statistic of the mean of that monthly IC series using a Newey–West standard error that accounts for the 3-month overlap (§7.3).

```
Metric                                            Target at M36           Falsifies the approach if
------------------------------------------------  ----------------------  -----------------------------------------
Rolling-12 mean 3M sector-neutral IC, EW composite >= +0.04, HAC t >= 2.0  90% HAC CI entirely below +0.02 after
                                                                          36 labelled periods
Net-of-cost top-minus-bottom quintile spread, 3M   >= +1.0% per quarter    <= 0 averaged over 24 labelled periods
Learned weights vs EW (walk-forward, 3M IC)        mean diff > 0, t >= 1.5 learned <= EW over 24 periods -> learning
                                                                          rule shelved; EW baseline continues
36M multi-bagger lift (precision / base rate)      >= 1.5x                 < 1.2x on both of the first two cohorts
DQ gate pass rate                                  >= 90% of months        --
```

If the composite is falsified but individual price families (momentum, low volatility) hold, the design says: keep the loop, retire the families that failed, and keep counting. The loop's value is that it can reach a negative conclusion honestly.

### 1.4 The one chart the owner looks at

```
 OOS IC (rolling 12m, 3M horizon)
  +0.10 |                                                     ..
        |                                       . . . . . . .       <- challenger (walk-forward weights)
  +0.05 |            ---- ---- ---- ---- ---- ---- ---- ---- ----   <- EW baseline
        |  - - - - -                                                <- tier-B price factors, 2016-2026 backtest
   0.00 |----------------------------------------------------------- 
        |       shaded band = HAC 90% CI
  -0.05 |
        +---------+---------+---------+---------+---------+--------->
        0         6        12        18        24        30   months of clean data
```

Per-factor and per-family versions of the same chart sit one click away. The x-axis is *months of clean data*, not calendar time, so a month that fails the DQ gate does not advance the curve.

---

## 2. Prediction target & horizons

### 2.1 Decision

> **Seed challenged.** The brief proposes 12-month forward return as the primary target. From an incremental-builder lens that is the wrong *learning* horizon: the first 12-month label for a clean snapshot arrives 12 months after it, and the first honest walk-forward comparison of learned vs equal weights (training window + 12-month embargo) cannot happen before ~month 27. The loop would run for two years without a single decision-relevant number.

The design uses a **horizon ladder** where each horizon has one job:

```
Horizon  Role                                       Labels/yr  Independent/yr  Used for
-------  -----------------------------------------  ---------  --------------  -----------------------------------
1M       noise / data-quality diagnostic             12         12              DQ, leakage tests, NOT decisions
3M       LEARNING and promotion decisions            12 (ovl)    4              weight learning, factor promotion
6M       reporting                                   12 (ovl)    2              dashboard
12M      PRIMARY REPORTING, confirmation gate        12 (ovl)    1              dashboard headline; a weight change
                                                                                 must not reduce 12M IC where labels
                                                                                 exist
24M      reporting                                   12 (ovl)    0.5            dashboard
36M      multi-bagger KPI (slow truth)               12 (ovl)    0.33           recall/precision/lift of 2x names
```

"ovl" = overlapping: evaluated every month, so consecutive observations share most of their return window. §7.3 handles this statistically.

Why 3M rather than 1M or 12M for learning: 1M is dominated by short-term reversal and microstructure noise and is the horizon the red team showed to be mostly a trend filter; 12M yields one independent observation per year. 3M is the shortest horizon at which value/quality factors show measurable IC in published cross-sectional studies, and it gives four independent labels a year, so the gate in §6.4 opens at month 15 instead of month 27+.

### 2.2 The measurable target

For stock *s*, month-end *t*, horizon *h* months:

```
ret_total(s,t,h)       = adj_close(s, t+h) / adj_close(s, t) - 1        (dividends reinvested, splits adjusted)
ret_excess_sector      = ret_total(s,t,h) - median_{s' in sector_group(s,t)} ret_total(s',t,h)
ret_excess_universe    = ret_total(s,t,h) - median_{s' in universe(t)}   ret_total(s',t,h)
```

The **learning target is `ret_excess_sector` at h = 3**. Medians, not means, so one demerger or bonus issue does not move the sector's centre.

### 2.3 How "multi-bagger" becomes a number

```
is_2x_36m(s,t)   = adj_close(s,t+36)/adj_close(s,t) >= 2.0          (a 26% CAGR for three years)
top_decile(s,t)  = rank_overall(composite, t) <= N(t)/10
recall_36m(t)    = |{s: top_decile & is_2x}| / |{s: is_2x}|
precision_36m(t) = |{s: top_decile & is_2x}| / |{s: top_decile}|
base_rate(t)     = |{s: is_2x}| / N(t)
lift_36m(t)      = precision_36m(t) / base_rate(t)
```

Lift is the headline: "top-decile names doubled 1.6x as often as a random pick" is a sentence that survives repetition. Recall alone rewards picking everything. These are computed for every cohort *t* whose 36-month window is complete. The first genuine cohort completes in **October 2029**; the tier-B backfill (§4.5) gives survivorship-biased cohorts from 2016–2023 immediately, plotted with a distinct dotted style and never quoted without the label.

### 2.4 Acceptance tests

```
tests/test_labels.py::test_ret_total_uses_adj_close_ratio
tests/test_labels.py::test_excess_sector_uses_group_median_not_mean
tests/test_labels.py::test_label_incomplete_when_end_price_missing
tests/test_labels.py::test_multibagger_lift_on_synthetic_cohort   (planted 2x names -> lift == expected)
```

---

## 3. Universe & sector taxonomy

### 3.1 Universe

Nifty 500 constituents from `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv`, fetched once per monthly run and **saved verbatim** to `data/universe/nifty500_<as_of>.csv` (about 40 KB; committed). Verified 2026-09-05: columns are `Company Name, Industry, Symbol, Series, ISIN Code`; 500 rows, all with `Series = EQ`, no duplicate Symbol or ISIN. The loader asserts both uniqueness constraints and stores `series`, so a name moved to the trade-to-trade `BE` series is flagged `series_not_eq` (still scored, `eligible = 0`). Seven symbols contain `&` or `-` (`ARE&M`, `BAJAJ-AUTO`, `GVT&D`, `J&KBANK`, `M&MFIN`, `M&M`, `NAM-INDIA`): Yahoo accepts them verbatim with the `.NS` suffix, but per-symbol cache files are named by ISIN, never by symbol. Fallback if the fetch fails: the most recent file in `data/universe/`, and the run is flagged `universe_stale`. If no file exists the run aborts (the Nifty 50 fallback in the legacy harness is removed).

Membership is point-in-time by construction: `universe_membership(as_of, symbol)` says who was in the index at each run. A stock that leaves the index keeps its already-recorded labels (its forward returns are still filled from the price cache), so exits do not create survivorship in the evaluation.

Symbol convention: `<Symbol>.NS` for Yahoo. ISIN is stored so that ticker renames (which happen a few times a year on NSE) can be reconciled; a rename is detected when an ISIN appears under a new symbol and is recorded as `symbol_aliases(isin, old_symbol, new_symbol, from_as_of)`.

### 3.2 Sector taxonomy

> **Seed challenged (partially).** The brief proposes the four-level NSE/AMFI classification (Macro-Economic Sector → Sector → Industry → Basic Industry). The full four-level file is an AMFI Excel published irregularly with no stable URL; parsing it is a moving part with no test oracle. The constituent CSV already carries the **Sector level** (20 groups, confusingly under a header named `Industry`). That is enough for neutralisation and it arrives for free every month.

**Canonical:** `nse_sector` = the `Industry` column of the constituent CSV. Values observed 2026-09-05:

```
Financial Services 101   Capital Goods 63          Healthcare 48        Automobile and Auto Components 38
Consumer Services 29     Fast Moving Consumer Goods 28   Information Technology 27   Chemicals 26
Metals & Mining 18       Power 17                  Oil Gas & Consumable Fuels 17   Consumer Durables 16
Services 14              Construction 13           Construction Materials 11       Realty 11
Telecommunication 10     Textiles 5                Media Entertainment & Publication 5   Diversified 3
```

**Neutralisation group** `sector_group` is derived from `nse_sector` by two rules, applied in order at each `as_of`:

1. *Split Financial Services* (month 3 onward) using Yahoo `industryKey` (verified 2026-09-05; values look like `auto-manufacturers`, `entertainment`, `auto-parts`), because banks, lenders, insurers and exchanges have different accounting and the group is a fifth of the universe:

```
yahoo industryKey                                   sector_group
banks-regional, banks-diversified                   FS: Banks
credit-services, mortgage-finance                   FS: NBFC
insurance-*                                         FS: Insurance
capital-markets, financial-data-stock-exchanges,
asset-management                                    FS: Capital Markets
anything else in Financial Services                 FS: Other
```

2. *Minimum group size* `min_group_size = 8`: any group with fewer than 8 members at `as_of` is merged into `Other`. With the September 2026 composition this merges Textiles, Media and Diversified (13 names).

Month 1 ships rule 2 only (20 groups → 18 + Other). Rule 1 is a registered change (§9.7) so the effect on IC is recorded.

**Fallback chain** for a symbol at `as_of`:

```
1. nse_sector from data/universe/nifty500_<as_of>.csv
2. else the most recent earlier universe file that lists the symbol (flag: sector_carried_forward)
3. else Yahoo `sector` mapped through sector_crosswalk (flag: sector_from_yahoo)
4. else 'Unclassified' (flag: sector_missing; stock scored, marked ineligible for the paper portfolio)
```

`sector_crosswalk` is a static, committed CSV `quant/sectors/crosswalk.csv` with columns `yahoo_sector, yahoo_industry, nse_sector` (about 60 rows; Yahoo's 11 sectors × the industries seen in the universe). It is only used in step 3 and by the legacy migration.

**Point-in-time mapping.** `sector_map(symbol, sector_group, nse_sector, valid_from, valid_to, source, version)`. Each monthly run compares the new CSV with the open rows; a changed classification closes the old row (`valid_to = as_of - 1 day`) and opens a new one. Scoring at `as_of` and *every later re-evaluation of that `as_of`* use the row valid on `as_of`. The legacy migration creates version 0 with `valid_from = 2026-06-01`, `source = backfilled_not_pit`, because no historical CSVs exist; this is the one place the mapping is knowingly not point-in-time and it is flagged.

### 3.3 Sector-level features (month 6)

Computed per `(as_of, sector_group)` from the same tables, stored in `sector_features`, and **kept out of the sector-neutral composite by construction**. They feed a separate, optional "sector overlay" model that is evaluated on its own:

```
sector_mom_6m      equal-weighted mean of members' 6-month total return
sector_breadth     share of members with close > SMA200
sector_flow_proxy  median 3-month change in heldPercentInstitutions across members (crude; Yahoo has no FII/DII flow)
sector_val_spread  median earnings yield of the group minus universe median
```

A genuine sector-flow source (NSDL FPI sector data, published fortnightly as PDF/Excel) is an optional adapter, not a dependency.

### 3.4 Acceptance tests

```
tests/test_sectors.py::test_min_group_size_merges_into_other            (synthetic 3-member group -> 'Other')
tests/test_sectors.py::test_sector_map_versioning_closes_old_row        (reclassify -> two rows, disjoint validity)
tests/test_sectors.py::test_lookup_uses_row_valid_on_as_of              (score date before reclass sees old group)
tests/test_sectors.py::test_fallback_chain_order_and_flags
tests/test_universe.py::test_csv_parse_real_fixture_has_500_rows_20_sectors  (fixture = committed 2026-09-05 file)
tests/test_universe.py::test_rename_detected_by_isin
```

---

## 4. Data layer

### 4.1 Principle: files are the truth, SQLite is the index

> **Seed challenged.** "SQLite for state, committed to git" is the current practice and it will not survive five years of monthly commits: a 5–50 MB binary that changes on every page touched delta-compresses poorly, so the repository history grows by roughly the file size every month. The brief itself allows "regenerate-from-source with a committed manifest".

Decision:

```
Committed to git                                   Not committed (regenerable, git-ignored)
-----------------------------------------------    ----------------------------------------------
data/universe/nifty500_<as_of>.csv                 data/prices/prices_daily.sqlite   (cache, ~150 MB)
data/universe/idx_mom30_<as_of>.csv  (~2 KB)       quant.db                          (rebuilt)
data/universe/idx_qual30_<as_of>.csv (~2 KB)       .cache/yahoo/<isin>/*.json        (raw pulls)
data/monthly/<YYYY-MM>/*.csv   (one folder/run)
data/prices/MANIFEST.json
data/reference/*.csv (crosswalk, calendar)
knowledge/**  (markdown + jsonl)
quant_engine.db  (legacy, frozen, read-only)
```

> **Seed challenged (bulk storage format).** The brief allows Parquet or DuckDB for bulk history. Neither `pyarrow` nor `duckdb` is importable in the working environment (checked 2026-09-05), the interpreter is Python 3.14 (binary-wheel availability for a new interpreter is exactly the kind of moving part this design avoids), and `git-lfs` is not installed. The daily price cache is therefore a **second SQLite file**, `data/prices/prices_daily.sqlite`, git-ignored and rebuilt from Yahoo by `quant prices backfill`. Measured 2026-09-05 on a synthetic 500-symbol × 2,640-day table with the schema in §4.3:
>
> ```
> rows 1,320,000   file 148 MB   build 2.0 s   trailing-400-day extract (200,000 rows) 0.32 s
> one calendar year as csv.gz: 3.8 MB (optional cold archive, also not committed)
> ```
>
> Same query language as `quant.db`, zero new dependencies, and `ATTACH DATABASE` makes joins trivial. Parquet remains an optional `--format parquet` adapter if `pyarrow` is ever installed; nothing depends on it.

`quant.db` is created by `python -m quant db rebuild`, which loads every CSV under `data/` and every JSONL under `knowledge/db/` into the schema in §10.2. The monthly loop writes to SQLite during the run and finishes with `quant db export`, which writes the run's rows back to `data/monthly/<YYYY-MM>/`. `quant db verify` re-imports into a temp DB and asserts row-count and checksum equality per table. Round-trip is tested. Each month adds roughly:

```
prices_monthly           500 rows      ~40 KB
fundamentals_snapshot    500 x ~45     ~1.2 MB   (long format: as_of, symbol, field, value, ...)
factor_values            500 x ~25     ~0.9 MB
scores                   500 x models  ~60 KB
forward_returns          filled later  ~0.2 MB
evaluations              ~300 rows     ~40 KB
                                       --------
                                       ~2.5 MB of CSV text per month, ~30 MB/year, delta-compresses well
```

The legacy `quant_engine.db` stays in git exactly as it is (it is the migration source and the audit trail of what the old engine said). No code writes to it after migration.

### 4.2 Trading calendar and `as_of`

`as_of` is always an NSE **trading day**, normally the last trading day of the month. `quant/data/calendar.py` derives the calendar from the `^CRSLDX` price series in the cache (verified 2026-09-05: daily closes from 2005-09-26) (a day with an index close is a trading day) rather than from a hand-typed holiday list. `last_trading_day_on_or_before(date)` resolves weekends and holidays. The legacy snapshots dated 2026-06-14 (Sunday) and 2026-07-11 (Saturday) resolve to 2026-06-12 and 2026-07-10 for all price lookups; their stored quotes are kept as `quote_legacy`.

Month-end runs execute on the first weekday after month-end (India time), fetch fundamentals *then* (these are as-of the fetch moment, which is the honest PIT date), and label the run with `as_of = last trading day of the month` for prices. The fundamentals rows carry `fetched_at` separately.

### 4.3 Prices, corporate actions, total returns

Source: `yf.download(batch, start=..., end=..., auto_adjust=False, actions=True, group_by='ticker', threads=False)`. Verified 2026-09-05 with yfinance 1.4.1: returns `Open High Low Close Adj Close Volume Dividends Stock Splits`; `Close` is split-adjusted historically by Yahoo, `Adj Close` is additionally dividend-adjusted. Three further facts verified the same day that the implementer must build in: the returned index is tz-aware (`Asia/Kolkata`) and is converted to naive ISO dates once, in `yahoo.py` (`idx.tz_localize(None).normalize()`); `HEROMOTOCO.NS` history reaches back to 2002-07-01 (6,006 rows), so a 2016 start is well inside coverage; and the ZFCVINDIA 6:1 split shows up as `Stock Splits = 6.0` on 2026-06-24 with continuous adjusted closes across it, which is what the split test in §4.7 asserts. Batches of 25 symbols, `time.sleep(1.0)` between batches (the 0.5 s per-request rule from AGENTS.md is kept for the per-ticker `info` calls; batched downloads are one request per batch).

Daily cache schema (`data/prices/prices_daily.sqlite`, table `prices_daily`, `PRIMARY KEY(symbol, date) WITHOUT ROWID`, secondary index on `date`; benchmark and ETF series are stored in the same table under their Yahoo symbols):

```
date DATE, symbol TEXT, open, high, low, close, adj_close, volume, dividend, split_ratio, ingested_at TIMESTAMP
```

Sizing (measured: 2,641 rows for one symbol over 10.7 years):

```
500 symbols x ~2,640 rows  =  ~1.32 M rows  ~=  150 MB SQLite  (not committed; measured on synthetic data, see §4.1)
Backfill time: 500/25 = 20 batches x ~3 s + 20 x 1 s sleep  ~=  90 s   (target; measured and recorded in the first report)
```

`data/prices/MANIFEST.json` (committed) records per symbol: first date, last date, row count, sha256 of the symbol's rows, `downloaded_at`, yfinance version. A re-download that changes history (Yahoo re-adjusts after a new dividend) shows up as a checksum change, which is expected and logged as `dq_events(code='price_history_readjusted')`.

Monthly table (SQLite + committed CSV):

```
prices_monthly(as_of, symbol, close, adj_close, tr_index, turnover_avg_3m_inr, volume_avg_3m,
               n_days_3m, source, PRIMARY KEY(as_of, symbol))
tr_index = adj_close / adj_close(first available month) ; only ratios are ever used
turnover_avg_3m_inr = mean(close x volume) over the trailing 63 trading days  (rupees)
```

`corporate_actions(symbol, ex_date, kind IN ('dividend','split'), value, source)` is filled from the `Dividends` and `Stock Splits` columns and used only for flags.

**Return guard.** `forward_returns.flag`:

```
'ok'                     |ret_1m| <= 60%
'extreme_move'           |ret_1m| >  60% and no split/bonus recorded in the window   (kept, reported)
'ca_suspect'             |ret_1m| >  60% and a split/bonus IS recorded               (should not occur with adjusted
                                                                                      prices; if it does, excluded)
'incomplete'             end-date price missing
```

With adjusted closes the legacy ZFCVINDIA case disappears; the test asserts it.

**Benchmarks stored alongside** (all verified live 2026-09-05): `^CRSLDX` (Nifty 500 price index, daily from 2005-09-26), `^NSEI` (Nifty 50), `^CNX200` (Nifty 200, from 2004-01-01), and two ETFs whose NAV tracks the factor indices the brief asks about: `MOM30IETF.NS` (ICICI Prudential Nifty 200 Momentum 30 ETF, prices from 2024-06-24) and `QUAL30IETF.NS` (ICICI Prudential Nifty 200 Quality 30 ETF, from 2023-08-10). The Motilal Oswal equivalents (`MOMOMENTUM.NS`, `MOQUALITY.NS`) return NaN closes and are not used. Total-return versions of NSE indices are not on Yahoo; the primary benchmark is therefore the **equal-weighted universe total return computed from our own `adj_close`**, which is also the fairest comparison for an equal-weight paper portfolio. `^CRSLDX` is reported as "price index, dividends not included (~1.2%/yr understatement)".

### 4.4 Fundamentals, point-in-time

Long format, one row per field:

```
fundamentals_snapshot(as_of, symbol, field, value REAL, fiscal_period_end TEXT NULL, source TEXT,
                      fetched_at TEXT, dq_flag TEXT, PRIMARY KEY(as_of, symbol, field))
```

Fields pulled per symbol (per-ticker calls: `info`, `financials`, `quarterly_financials`, `balance_sheet`, `cashflow` — 5 HTTP calls, `time.sleep(0.5)` after each symbol; 500 symbols ≈ 25–30 min):

```
from info:        marketCap sharesOutstanding floatShares heldPercentInstitutions heldPercentInsiders
                  averageDailyVolume3Month trailingPE forwardPE priceToBook enterpriseValue enterpriseToEbitda
                  returnOnEquity returnOnAssets debtToEquity currentRatio dividendRate dividendYield payoutRatio
                  beta revenueGrowth earningsQuarterlyGrowth operatingMargins profitMargins grossMargins
                  totalDebt totalCash bookValue ebitda totalRevenue netIncomeToCommon sector industry
                  sectorKey industryKey fiftyDayAverage twoHundredDayAverage
from statements:  annual  (up to 5 FY): Total Revenue, EBIT, Net Income, Diluted EPS, Operating Cash Flow,
                                        Capital Expenditure, Total Assets, Current Liabilities, Total Debt,
                                        Stockholders Equity, Cash And Cash Equivalents
                  quarterly (up to 6Q): Total Revenue, EBIT, Net Income
```

Coverage verified 2026-09-05 on three names, so the implementer knows what `None` looks like before writing the loader: `HEROMOTOCO.NS` (161 `info` keys) lacked only `returnOnEquity`, `returnOnAssets`, `currentRatio`; the small caps `PFOCUS.NS` and `TENNIND.NS` (141–142 keys) additionally lacked `dividendRate`/`dividendYield` (no dividend paid) and one of `earningsQuarterlyGrowth`/`beta`. `institutional_holders` is an empty frame for Indian names and is not used; `heldPercentInstitutions` and `heldPercentInsiders` from `info` (mirrored in `major_holders`) are the ownership inputs. Annual statement frames carry five fiscal years with `EBIT` and `Diluted EPS` rows present; `quarterly_financials` carries six or more quarters (41 rows); `get_shares_full(start="2016-01-01")` returns a dated shares-outstanding series (825 points since 2017-05 for HEROMOTOCO), which is what makes a point-in-time market cap possible for the Tier-B backfill (§4.5). These three `info` dicts are committed as the test fixture `tests/fixtures/yahoo_info_samples.json`.

Each statement value is stored with its `fiscal_period_end` (e.g. `2026-03-31`) so **staleness** is a computed column: `months_stale = months(as_of − fiscal_period_end)`. A value is usable only if `fiscal_period_end <= as_of`; the loader asserts it (it is impossible for a current fetch to violate this, but the assertion is what makes the tier-B backfill in §4.5 safe).

**Unit normalisation happens in exactly one module**, `quant/data/yahoo.py`, with one function per field and a test per function using the live values verified in the brief:

```
dividendYield 3.48   -> 0.0348   (percent -> fraction; reuse quant_math.normalize_yield)
debtToEquity  357    -> 3.57     (percent -> ratio)
heldPercentInstitutions 0.38981 -> unchanged (fraction)
None                 -> NaN, never 0
statements           -> rupees, stored as rupees; crores only in the UI
```

### 4.5 Backfill plan and evidence tiers

Two tiers, always labelled:

```
Tier A  point-in-time by construction: everything fetched at or after 2026-09-30 by the new loop.
Tier B  approximate / survivorship-biased backfill, for priors and for a day-1 learning curve ONLY:
        B1  price-derived factors 2016-01 .. 2026-09 from the daily cache. Universe = today's constituents
            (survivorship bias: names that fell out of the index are missing; expect momentum/quality to look
            better than they were). PIT-correct otherwise (prices do not restate). Size for the
            B1 replay = adj_close x get_shares_full() shares on the date (both point-in-time).
        B2  fundamental factors from the 5 annual statements Yahoo returns, made usable at
            fiscal_period_end + 90 days (SEBI requires audited annual results within 60 days; 90 is the
            conservative lag). Restatement risk: Yahoo shows latest restated figures. ~4 years of history.
```

Tier B results live in `evaluations.universe_scope = 'tierB_current_constituents'` and are drawn dotted in every chart. **No promotion, weight change or scoreboard claim may cite Tier B alone.** Its legitimate uses: choosing the initial `active` set and expected signs (§5.3), sanity-checking the pipeline (12-1 momentum should show a positive 12M IC in India; if it does not, the pipeline is broken), and giving the owner a curve to look at while Tier A accumulates.

### 4.6 Data-quality flags and gates

Per-row flags (`dq_flag`, comma-separated codes) on `fundamentals_snapshot`, `factor_values`, `forward_returns`:

```
imputed | missing | unit_suspect | stale_gt_15m | derived (e.g. ROE from NI/BV) | proxied | ca_suspect |
extreme_move | sector_carried_forward | sector_from_yahoo | legacy_unit_bug_x100 | legacy_unit_bug_1e7 |
legacy_unflagged | series_not_eq
```

Per-run gates (`dq_runs`), evaluated before factors are computed. **Blocking** gates abort the run (nothing scored, nothing exported, `runs.dq_status='blocked'`); **warning** gates proceed and are shown on the dashboard.

```
Gate                                        Threshold          Blocking?
------------------------------------------  -----------------  ---------
price coverage: symbols with month-end px    >= 95% of universe  yes
fundamentals coverage: symbols with >= 10    >= 80% of universe  yes
  non-null info fields
sector mapping coverage                      >= 98%              yes
any dividend yield (fraction) > 0.25         0 symbols           yes   (the 349% bug)
any FCF yield outside +-200%                 0 symbols           yes   (the 1e7 bug)
duplicate (as_of, symbol)                    0                   yes
median statement staleness                   <= 15 months        warn
universe file stale (fetch failed)           --                  warn
share of imputed inputs per factor           <= 20%              warn
factor near-constant (modal share >= 80%)    0 active factors    warn  (reuse eval_portfolio_health logic)
```

A blocked month is a first-class outcome: it is recorded, it appears on the learning curve as a gap, and the `run-month` command can be re-run after the cause is fixed (idempotent: it replaces that `as_of`'s rows in a transaction, refusing to replace a passed run with a blocked one unless `--force`).

### 4.7 Acceptance tests

```
tests/test_yahoo_units.py::test_dividend_yield_percent_to_fraction        (3.48 -> 0.0348)
tests/test_yahoo_units.py::test_debt_to_equity_percent_to_ratio           (357 -> 3.57)
tests/test_yahoo_units.py::test_none_stays_nan_never_zero
tests/test_prices.py::test_batching_25_and_sleep_called                   (mock yf.download; assert 20 calls, 20 sleeps)
tests/test_prices.py::test_month_end_uses_last_trading_day                (fixture calendar with a holiday)
tests/test_prices.py::test_zfcvindia_split_not_a_return                   (fixture: adj closes around 2026-06-24 -> |ret| < 0.6)
tests/test_prices.py::test_manifest_checksum_changes_on_readjustment
tests/test_fundamentals.py::test_fiscal_period_end_never_after_as_of      (loader raises)
tests/test_fundamentals.py::test_staleness_months_computed
tests/test_dq.py::test_blocking_gate_aborts_and_records_run
tests/test_dq.py::test_blocked_run_cannot_replace_passed_run_without_force
tests/test_db_roundtrip.py::test_export_rebuild_verify_identical          (populate temp DB -> export -> rebuild -> verify)
tests/test_calendar.py::test_weekend_as_of_resolves_backward             (2026-06-14 -> 2026-06-12)
```

---

## 5. Factor library

### 5.1 Plugin contract

One Python file per family under `quant/factors/`, each factor a module-level object; a registry collects them. No metaclasses, no entry points — a coding LLM can add a factor by copying a 20-line block.

```python
# quant/factors/base.py
from dataclasses import dataclass, field
from typing import Callable, Literal
import pandas as pd

Status = Literal['candidate', 'active', 'retired', 'quarantined']

@dataclass(frozen=True)
class FactorSpec:
    name: str                      # snake_case, stable forever; bump `version` instead of renaming
    version: int
    family: Literal['momentum', 'low_risk', 'quality', 'value', 'growth', 'ownership', 'legacy']
    direction: int                 # +1: higher raw value -> higher expected excess return; -1: lower is better
    horizon_m: int                 # horizon the hypothesis is stated for (3 or 12)
    hypothesis: str                # one sentence, written BEFORE evaluation
    formula: str                   # human-readable formula
    inputs: tuple[str, ...]        # field names from fundamentals_snapshot / prices_monthly / daily cache
    applies_to_financials: bool    # False -> NaN for sector_group starting with 'FS:' (or nse_sector == 'Financial Services')
    tier_b_backfillable: bool      # True only for price/volume factors (B1) or statement-derived (B2)
    evidence: str                  # citation or 'none (exploratory)'
    compute: Callable[['FactorInputs'], pd.Series] = field(compare=False)   # returns raw value indexed by symbol; NaN allowed

@dataclass
class FactorInputs:
    as_of: str
    symbols: pd.Index
    info: pd.DataFrame             # wide: symbol x field (latest fundamentals_snapshot at as_of)
    statements: pd.DataFrame       # long: symbol, field, fiscal_period_end, value (<= as_of)
    daily: pd.DataFrame            # date x symbol adj_close for trailing 400 trading days (<= as_of)
    daily_close: pd.DataFrame      # unadjusted split-adjusted close (for SMA vs price)
    daily_volume: pd.DataFrame
    monthly: pd.DataFrame          # prices_monthly for trailing 37 months
    history: pd.DataFrame          # earlier fundamentals_snapshot rows (for holding changes)
    sector_group: pd.Series        # symbol -> group at as_of
```

The `compute` function must be pure (no I/O, no network); all inputs arrive in `FactorInputs`. This is what makes every factor unit-testable with a 20-row synthetic frame and what makes the leakage test in §7.5 mechanical (the loader guarantees nothing after `as_of` is in `FactorInputs`; the factor cannot reach past it).

Registry: `quant/factors/registry.py` exposes `REGISTRY: dict[str, FactorSpec]` and `active(as_of)`. Status is **not** in the dataclass; it lives in the `factor_registry` table (and its JSONL mirror) because status changes over time and must be dated. The table also stores the spec's metadata so the DB is self-describing without the code.

### 5.2 Transform pipeline

```
raw(s)  --[applies_to_financials? else NaN]-->  --[winsorise? NO, ranks don't need it]-->
        --[percentile rank within sector_group(s, as_of), average ties]-->  neutral(s) in (0,1)
neutral(s) = (rank_g(raw(s) x direction) - 0.5) / n_g          for non-NaN raw; NaN stays NaN
```

> **Seed challenged (minor).** "Winsorised percentile ranks" — winsorisation is a no-op for a rank. Raw values are stored unwinsorised for display and for future z-score variants; only sector-level features (§3.3) are winsorised at 1/99.

Both `raw` and `neutral` are stored in `factor_values`. Groups use the `min_group_size = 8` rule; a symbol in `Other` is ranked within `Other`.

### 5.3 Initial factor list

Twenty-two factors. `active` at launch means: its sign and horizon come from published cross-sectional evidence, it is included in the equal-weight composite from month 1, and it is still subject to retirement. `candidate` means computed and evaluated from month 1 but excluded from the composite until it passes §9.5. Tier flags say what can be backfilled.

```
name                  family     dir  h   formula                                                   fin?  tierB  status
--------------------  ---------  ---  --  --------------------------------------------------------  ----  -----  ---------
mom_12_1              momentum   +1   12  adj_close[t-21d]/adj_close[t-252d] - 1                    yes   B1     active
mom_6_1               momentum   +1   3   adj_close[t-21d]/adj_close[t-126d] - 1                    yes   B1     active
trend_sma200          momentum   +1   3   close/SMA200(close) - 1   (continuous; replaces death-cross)  yes   B1     active
dist_52w_high         momentum   +1   3   close / max(close, 252d) - 1                              yes   B1     candidate
rev_1m                momentum   -1   1   adj_close[t]/adj_close[t-21d] - 1   (short-term reversal) yes   B1     candidate
vol_12m               low_risk   -1   12  std(daily log ret, 252d) x sqrt(252)                      yes   B1     active
beta_12m              low_risk   -1   12  OLS beta vs ^CRSLDX daily, 252d                           yes   B1     candidate
max_ret_1m            low_risk   -1   1   max daily return in trailing 21d (lottery)                yes   B1     candidate
roce                  quality    +1   12  EBIT_ttm / (Total Assets - Current Liabilities)           no    B2     active
roe                   quality    +1   12  Net Income_ttm / Stockholders Equity                      yes   B2     active
accruals              quality    -1   12  (Net Income - Operating Cash Flow) / Total Assets         no    B2     active
cash_conversion_3y    quality    +1   12  sum(OCF, 3 FY) / sum(Net Income, 3 FY)                    no    B2     candidate
op_margin             quality    +1   12  operatingMargins (info)                                   yes   no     candidate
earnings_yield        value      +1   12  1 / trailingPE  (NaN if PE <= 0)                          yes   no     active
ev_ebitda_yield       value      +1   12  1 / enterpriseToEbitda (NaN if <= 0)                      no    no     active
book_to_price         value      +1   12  1 / priceToBook                                           yes   no     active
fcf_yield             value      +1   12  (OCF - Capex)_latest FY / marketCap                       no    B2     candidate
sales_growth_3y       growth     +1   12  CAGR(Total Revenue, 3 FY)  (reuse quant_math.estimate_growth)  yes  B2  candidate
eps_growth_3y         growth     +1   12  CAGR(Diluted EPS, 3 FY)                                   yes   B2     candidate
rev_growth_yoy_q      growth     +1   3   revenueGrowth (info; latest quarter YoY)                  yes   no     candidate
inst_hold_chg_3m      ownership  +1   3   heldPercentInstitutions[t] - [t-3 runs]  (NaN until 3 runs exist)  yes  no  active
promoter_hold         ownership  +1   12  heldPercentInsiders                                       yes   no     candidate
```

Notes for the implementer:

- `fin?` = `applies_to_financials`. Leverage-type factors (`net_debt_to_ebitda`, `debt_to_equity`) are deliberately **not** in the initial list: the red team found `bs_score` sat on one value for 84% of the universe and `debtToEquity` is `None` or meaningless for a fifth of names. They can be registered later through §9.7.
- The eight legacy scores are migrated as `legacy_quality`, `legacy_growth`, `legacy_valuation`, `legacy_risk`, `legacy_moat`, `legacy_bs`, `legacy_cap_alloc`, `legacy_smart_money`, plus `legacy_trap_score` and `legacy_momentum_multiplier`, family `legacy`, status `retired`, values only for the 2026 snapshots. They are evaluated forever (their labels keep filling) but never computed again.
- `inst_hold_chg_3m` replaces `smart_money`: the legacy version was a 7-day delta against whatever snapshot happened to exist. Three monthly runs are needed before it has a value; that is fine and is flagged `missing` until then.
- `trend_sma200` and the boolean `death_cross = (close < SMA50) & (SMA50 < SMA200)` are both stored; the boolean is a diagnostic column in `factor_values` (`name='flag_death_cross'`, family `legacy`, never in a composite) so the old signal's track record continues to be visible.

Expected-sign priors from Tier B (§4.5) may **demote** an `active` factor to `candidate` before launch if its 2016–2026 sign is wrong; they may not promote a candidate. Whatever is done is recorded as decision `D-0001` (§9).

### 5.4 Pre-registration and lifecycle

```
                 register (spec + knowledge/factors/<name>.md + hypotheses row)
                                |
                                v
   +---------------------> candidate  --(>= 12 labelled 3M periods since registered_on,
   |                            |          sign correct, HAC t >= t_crit(m), not redundant,
   |                            |          coverage >= 80%)-->  PROPOSE promote
   |     (data problem)         v                                        |
   |     quarantined  <----  active  <------------- approve --------------+
   |          |                 |
   |          |   (24m rolling HAC t < 0, or coverage < 60% for 3 months)
   |          |                 v
   +----------+---------->  retired  (values kept, never recomputed, still evaluated)
```

Rules that make the record uncontaminated:

- `registered_on` is the date of the `factor_registry` row. Evaluations of a factor are stored for every `as_of >= registered_on` (Tier A) and, if `tier_b_backfillable`, for earlier `as_of` with `universe_scope='tierB_...'`. The promotion test **only counts Tier A rows with `as_of >= registered_on`**. A factor cannot be "discovered" in the past and then registered.
- Changing a formula creates `name` with `version + 1`; the old version is retired, not overwritten. Both keep their `factor_values`.
- Status changes require a `decision_id` (§9.3). The CLI refuses a status change without one.

### 5.5 Acceptance tests

```
tests/test_factor_contract.py::test_every_registered_factor_has_nonempty_hypothesis_and_direction
tests/test_factor_contract.py::test_compute_is_pure                       (inputs frozen; monkeypatch network -> raises)
tests/test_factor_contract.py::test_financial_exclusion_yields_nan
tests/test_transforms.py::test_neutral_rank_uniform_within_group          (each group's neutral values ~ U(0,1))
tests/test_transforms.py::test_direction_minus_one_reverses_rank
tests/test_transforms.py::test_nan_raw_stays_nan
tests/test_price_factors.py::test_mom_12_1_on_synthetic_path               (known geometric path -> exact value)
tests/test_price_factors.py::test_trend_sma200_zero_when_price_equals_sma
tests/test_price_factors.py::test_vol_12m_matches_numpy_std
tests/test_fund_factors.py::test_roce_uses_ttm_and_latest_bs
tests/test_fund_factors.py::test_accruals_sign_convention
tests/test_registry.py::test_status_change_requires_decision_id
tests/test_registry.py::test_promotion_counts_only_tierA_rows_after_registration
```

---

## 6. Scoring model & weight learning

### 6.1 Family scores and the permanent baseline

```
family_score(s, f, t) = nanmean{ neutral(s, k, t) : k active in family f at t }
                        -> 0.5 if every factor in the family is NaN for s  (flag family_missing)
composite_ew(s, t)    = mean over families f of family_score(s, f, t)               (model_id = 'ew_family_v1')
rank_sector, rank_overall computed on composite; eligible flag from §8.2
```

Averaging inside families first means adding a fourth value factor does not triple the weight of "value". Six families → each carries 1/6 of the baseline regardless of how many factors sit inside. `ew_family_v1` is the **permanent baseline**: it is computed every month for the life of the project, it is never retired, and every other model is reported as a difference from it.

### 6.2 Models table and champion/challenger

```
models(model_id, kind, description, params_json, status IN ('baseline','challenger','live','retired'),
       registered_on, decision_id)

ew_family_v1        baseline    equal family weights                                 (from M1)
ic_shrunk_k24_v1    challenger  §6.3 with k_shrink = 24                                (from M1; equals EW until gate)
legacy_v18          retired     the 8-factor bucketed model as stored, for continuity (migration only)
sector_overlay_v1   challenger  composite_ew + lambda x sector_mom_6m (§3.3)           (from M6, separate hypothesis)
```

Every model in `models` with status ≠ retired is scored every month into `scores`. "Live" = the model whose ranking the dashboard shows first and whose paper portfolio is `PF-LIVE`. `ew_family_v1` is live from month 1. A challenger becomes live only through a proposal (§9.5): ≥ 12 monthly paper periods, mean 3M IC difference vs EW > 0 with HAC t ≥ 1.5 **and** deflated for the number of challengers ever registered, **and** net-of-cost paper return not worse than EW's over the same window. When a challenger becomes live the baseline keeps running; nothing stops.

### 6.3 Learning rule: IC-weighting shrunk hard toward equal

> **Seed accepted, made concrete.** Weights per **family** (6 parameters), not per factor (22). Fewer parameters is the single most effective overfitting control available and it is free.

```python
# quant/model/learn.py
def fit_family_weights(ic_hist: pd.DataFrame,      # index = as_of (monthly), columns = families, values = 3M IC
                       horizon_m: int = 3,
                       k_shrink: float = 24.0,
                       floor_mult: float = 0.5,     # floor = 0.5 / F
                       cap_mult: float = 2.0,       # cap   = 2.0 / F
                       min_n_eff: float = 4.0) -> tuple[dict[str, float], dict]:
    """
    Returns (weights, diagnostics). Deterministic; no randomness.
      F        = number of families
      n_months = rows of ic_hist whose label is complete (as_of + horizon <= last complete month)
      n_eff    = n_months / horizon_m               # de-overlapped count
      alpha    = n_eff / (n_eff + k_shrink)         # 0 -> equal weights; n_eff=4 -> 0.143; n_eff=12 -> 0.333
      ic_bar_f = mean of column f
      raw_f    = max(ic_bar_f, 0); if sum(raw) == 0: raw_f = 1/F for all f; else raw_f /= sum(raw)
      w_f      = (1 - alpha)/F + alpha * raw_f
      if n_eff < min_n_eff: return equal weights exactly, diagnostics['gate'] = 'closed'
      project onto {sum == 1, floor <= w <= cap} (iterative clamp+renormalise, as weight_optimizer.project_weights),
      round to 4 dp, residue to the largest weight so sum == 1.0000 exactly.
    """
```

Worked numbers so the implementer can write the test:

```
F = 6, families: momentum low_risk quality value growth ownership
n_months = 12  ->  n_eff = 4  ->  alpha = 4/28 = 0.1429
ic_bar    = [ 0.06, 0.02, 0.03, -0.01, 0.00, 0.04 ]
raw       = [ 0.06, 0.02, 0.03,  0,    0,    0.04 ] / 0.15 = [0.400, 0.133, 0.200, 0, 0, 0.267]
w         = 0.8571/6 + 0.1429*raw = [0.2000, 0.1619, 0.1714, 0.1429, 0.1429, 0.1810]  (sum 1.0000)
bounds    = [0.0833, 0.3333]  -> none clamped
```

A family with a negative mean IC does not get a negative weight; it drifts toward the floor. Negative weights would let the learner short a family on 4 effective observations, which is exactly the over-confidence the red team caught.

**Weights are re-fit every month but only from labels complete at that month** (§7.2 embargo). `model_weights(model_id, as_of, family, weight, n_eff, alpha)` stores the vector used for each `as_of`, so any month's score can be reproduced.

### 6.4 The gate, stated once

```
Deviation from equal weights is permitted only when n_eff >= 4 at the 3M horizon,
i.e. >= 12 monthly Tier-A snapshots whose 3-month labels are complete  (calendar: month 15 = 2027-12).
Until then ic_shrunk_k24_v1 == ew_family_v1 by construction, and the test asserts it.
```

> **Seed challenged (arithmetic).** The brief says "≥ 12 non-overlapping evaluation periods". At 3M that is 36 months of labels — month 39. At 12M it is 12 years. The design's gate is "12 *monthly* labelled periods, counted as 4 effective", combined with shrinkage that keeps α at 0.14 when the gate opens. The shrinkage is what protects the owner; a hard count of non-overlapping periods alone would keep the gate closed until 2029 and teach nothing in between.

### 6.5 What happens to the death-cross hard kill

Retired as a filter; kept as information.

```
Legacy (retired with legacy_v18)         V2
final = base x trap_mult x mom_mult      composite_ew = mean of family scores; trend_sma200 is one of ~3
mom_mult in {0, 0.8, 1.0}, 0 for ~30%      momentum factors, continuous, sector-neutral
of the universe (rank destroyed)         flag_death_cross stored and evaluated monthly as a diagnostic
trap_score multiplier {0.2,0.5,0.8,1}    no multipliers; 'accruals' and 'roce' carry the quality view
```

The AGENTS.md invariants "Death Cross Multiplier must enforce 0.0x" and "weights in [0.05, 0.30] summing to 1.000" apply to the frozen `legacy_v18` model only. The implementer updates AGENTS.md in month 1 to say so (the file is outside the scope of this spec but inside the implementer's).

### 6.6 Hard filters that remain

```
Filter            Rule                                              Effect
----------------  ------------------------------------------------  ----------------------------------------------
universe          symbol in universe_membership(as_of)              not scored otherwise
price coverage    >= 200 trading days of adj_close before as_of     scored with momentum/low_risk NaN (flag), eligible=0
liquidity         turnover_avg_3m_inr >= 2 crore (bucket A/B/C)     scored; eligible=0 if below (§8.2)
sector            sector_group != 'Unclassified'                    scored; eligible=0 otherwise
series            Series == 'EQ' in the constituent file             scored; eligible=0 otherwise (trade-to-trade names)
```

Filtered names are **scored and evaluated** (IC is reported on `all` and on `eligible` scopes) so the filter's own effect is measurable. Nothing is multiplied by zero.

### 6.7 Acceptance tests

```
tests/test_composite.py::test_family_mean_then_family_mean                 (3 value factors don't triple value's weight)
tests/test_composite.py::test_missing_family_scores_half_and_flags
tests/test_learn.py::test_gate_closed_returns_exact_equal_weights          (n_months = 9 -> all 1/6)
tests/test_learn.py::test_worked_example_matches_table_above               (numbers in §6.3 to 4 dp)
tests/test_learn.py::test_negative_ic_family_never_below_floor
tests/test_learn.py::test_weights_sum_exactly_one_after_rounding
tests/test_learn.py::test_uses_only_complete_labels                        (a row with incomplete label is ignored)
tests/test_models.py::test_challenger_equals_baseline_before_gate
```

---

## 7. Evaluation protocol

### 7.1 What is evaluated, every month

For each `as_of` whose labels at horizon *h* are complete, for each subject (every registered factor, every model, every family score, `flag_death_cross`), for scopes `all` and `eligible`, and for Tier A / Tier B:

```
evaluations(eval_id, as_of, subject_kind IN ('factor','family','model','flag'), subject_id, horizon_m,
            universe_scope, tier, n, ic, ic_naive_t, q1_ret, q2_ret, q3_ret, q4_ret, q5_ret,
            spread_gross, turnover_q5, spread_net, top_decile_hit_rate, computed_at)
```

`ic` = Spearman(neutral score or composite, `ret_excess_sector`). Quintiles are formed **within sector group** then pooled, so `q5_ret - q1_ret` is a sector-neutral spread. `ic_naive_t` is the cross-sectional t under independence, stored only so the HAC number can be compared with it.

Evaluations are append-only and idempotent: re-running for an `as_of` replaces rows with the same key (`as_of, subject, horizon, scope, tier`); the `computed_at` history is kept in `evaluations_log` so a changed number is visible (this is the "how did this number move" audit that the red team could not do).

### 7.2 Walk-forward with embargo

```
                 train window (labels complete)        embargo = h        test month
   |----------------------------------------------|xxxxxxxxxxxxxxx|=========|
   as_of <= T - h                                    (T-h, T]           T
```

For a learned model scored at month *T*, the weight fit may use only `as_of <= T − h` (their labels end at or before *T*). Implemented once, in `quant/evaluation/walkforward.py::labels_available(T, h) -> as_of list`, and every learner calls it; the leakage test in §7.5 plants a future-dependent signal and asserts the learner cannot exploit it.

The walk-forward *report* for a model = the series of ICs of the scores that were actually produced at each *T* with weights fit under the embargo. Because scores are stored monthly as they are produced, the walk-forward series is simply `evaluations` for that model — there is no separate backtest code path that could drift from production. For Tier B a replay function produces the same thing over 2016–2026.

### 7.3 Overlap-aware statistics

Monthly IC series `x_1..x_N` at horizon *h* months: consecutive values share `h−1` months of return window. Newey–West / HAC with Bartlett kernel, lag `L = h − 1`:

```
xbar   = mean(x)
gamma_j = (1/N) * sum_{t=j+1..N} (x_t - xbar)(x_{t-j} - xbar)
S       = gamma_0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gamma_j
se_hac  = sqrt(S / N)
t_hac   = xbar / se_hac
ci90    = xbar +- 1.645 * se_hac
n_eff   = N / h            (reported alongside; the honest sample size)
```

`quant/evaluation/stats.py::hac_mean_test(x: np.ndarray, lag: int) -> HacResult(mean, se, t, ci_lo, ci_hi, n, n_eff)`. About 25 lines of numpy; `statsmodels` is not installed in the working environment and is not to be added for this. Tested against (a) i.i.d. noise → `se_hac ≈ se_naive`, (b) a series built by overlapping-sum of white noise with window *h* → `se_hac` ≈ theoretical `se_naive × sqrt(h)` within tolerance, (c) a constant series → division-by-zero handled (`t = inf` with flag).

Cross-sectional dependence (stocks moving together) is **not** fixed by HAC; it is why `ic_naive_t` is never quoted. The time-series t over months is the statistic of record.

### 7.4 Benchmarks

```
id             definition                                             source                                   note
-------------  -----------------------------------------------------  ---------------------------------------  ------------------------------------------
BM_EW          equal-weighted universe total return, monthly rebal.   our adj_close                            PRIMARY; same basis as the paper portfolios
BM_CW          marketCap-weighted universe total return               our adj_close + info.marketCap           secondary
BM_N500PR      Nifty 500 price index                                  ^CRSLDX                                  dividends excluded (~1.2%/yr understatement)
BM_MOM30_C     EW total return of the current Nifty200 Momentum 30    data/universe/idx_mom30_<as_of>.csv       PIT from month 1; constituents re-fetched monthly
               constituents, rebalanced monthly                       + our adj_close
BM_QUAL30_C    EW total return of the current Nifty200 Quality 30     data/universe/idx_qual30_<as_of>.csv      same
               constituents, rebalanced monthly                       + our adj_close
BM_MOM30_ETF   Momentum-30 ETF price return                           MOM30IETF.NS (from 2024-06-24)           cross-check of BM_MOM30_C; NAV ~ TRI minus fees
BM_QUAL30_ETF  Quality-30 ETF price return                            QUAL30IETF.NS (from 2023-08-10)          cross-check of BM_QUAL30_C
```

> **Seed corrected.** "Nifty 500 Quality 50" is not an NSE index; the published quality indices are Nifty200 Quality 30, Nifty100 Quality 30 and Nifty Midcap150 Quality 50. Verified 2026-09-05: NSE serves *current* constituent lists at stable URLs, in the same five-column layout as the Nifty 500 file, for `ind_nifty200Momentum30_list.csv`, `ind_nifty200Quality30_list.csv`, `ind_nifty500Momentum50_list.csv`, `ind_nifty500Value50_list.csv` and `ind_nifty500MulticapMomentumQuality50_list.csv` (the Nifty100 Quality 30 and Midcap150 Quality 50 URLs returned an HTML page, i.e. not available this way). The design therefore replicates **Nifty200 Momentum 30** and **Nifty200 Quality 30** from their monthly constituent files (~2 KB each, committed to `data/universe/`) and our own adjusted closes, and cross-checks each against its ETF. Replication is point-in-time from the first run onward; there is no free source of *historical* constituents, so these two benchmarks have no Tier-B history and the Tier-B backtest compares against `BM_EW` and `^CRSLDX` only. An adapter for hand-downloaded NSE index-level TRI history files, which would extend them backwards, stays optional (month 6). The earlier idea of "top 30 by our own momentum factor" is dropped: it is a portfolio of our signal, not a benchmark.

### 7.5 Leakage tests (run in CI and every month)

```
shuffle      permute symbols within each as_of for every factor -> assert |mean IC| < 2 se_hac for all factors
plant        add factor 'plant_test' = ret_excess_sector(h=3) + N(0, sigma) with sigma chosen for IC~0.30
             -> assert recovered IC in [0.20, 0.40]; then assert the factor pipeline REFUSES it in production
             (its compute function reads forward data; FactorInputs does not contain any -> KeyError)
embargo      give the learner ic_hist with a row at as_of = T-1 (label incomplete) -> assert ignored
as_of        assert no fundamentals_snapshot row with fiscal_period_end > as_of; no daily row > as_of in FactorInputs
```

`python -m quant test leakage --as-of <date>` runs them against the real DB; failures block the month.

### 7.6 Cost-adjusted spreads

`spread_net = spread_gross − (turnover_q5 + turnover_q1) × cost_one_way(bucket)` where turnover is the fraction of the quintile's names that changed since the previous month and costs come from §8.3. Stored per evaluation row.

### 7.7 Learning-curve measurement

```
learning_curve(model_id, horizon_m, as_of, months_clean, ic_oos_rolling12, hac_t, ci_lo, ci_hi, tier,
               PRIMARY KEY(model_id, horizon_m, as_of, tier))
months_clean(as_of) = number of Tier-A runs with dq_status='passed' and as_of' <= as_of
ic_oos_rolling12    = mean of the last 12 available monthly ICs of the model (walk-forward by construction)
```

One row per model per month; the UI draws `months_clean` on x and `ic_oos_rolling12` on y with the CI band. Per-family and per-factor curves come from the same table with `model_id` replaced by `family:<f>` / `factor:<name>`.

### 7.8 Acceptance tests

```
tests/test_stats.py::test_hac_equals_naive_for_iid
tests/test_stats.py::test_hac_inflates_for_overlapping_sums        (ratio ~ sqrt(h) +- 25% on N=600 synthetic)
tests/test_stats.py::test_hac_constant_series_flagged
tests/test_metrics.py::test_quintiles_formed_within_sector
tests/test_metrics.py::test_ic_matches_scipy_spearmanr
tests/test_walkforward.py::test_labels_available_respects_embargo   (T=2027-06, h=3 -> max as_of 2027-03)
tests/test_leakage.py::test_shuffle_kills_ic
tests/test_leakage.py::test_planted_signal_recovered
tests/test_leakage.py::test_factor_inputs_contain_nothing_after_as_of
tests/test_learning_curve.py::test_months_clean_skips_blocked_runs
```

---

## 8. Portfolio & cost model

### 8.1 Paper portfolios

```
portfolio_id  model             rule                                                        rebalance
------------  ----------------  ----------------------------------------------------------  ----------
PF-LIVE       live model        top 30 by rank_overall among eligible; sector cap 6 of 30;   monthly
                                buffer: keep a holding while rank_overall <= 60; EW
PF-EW         ew_family_v1      same rule                                                   monthly
PF-<chal>     each challenger   same rule                                                   monthly
PF-DEC10      live model        top decile among eligible, EW, no buffer (pure signal)       monthly
```

Holdings are set at month-end close of `as_of`; returns accrue over the next month from `adj_close`. `turnover = 0.5 × Σ|w_new − w_old|`. The buffer rule is what keeps turnover survivable; the expected value with a rank-30/60 buffer on a 3M-horizon composite is 15–30% per month (a target range, to be measured and reported).

### 8.2 Liquidity screen and buckets

Decode: `turnover_avg_3m_inr` is the trailing 63-day mean of (close × volume) in rupees — average daily traded value.

```
bucket   avg daily traded value       eligible   impact (one-way bps)
A        >= 25 crore                   yes        5
B        5 .. 25 crore                 yes        20
C        2 .. 5 crore                  yes        50
D        <  2 crore                    no         --   (scored, evaluated, not held)
```

### 8.3 Cost assumptions (Indian delivery equity, 2026)

```
component                                       one-way bps
STT (delivery, buy and sell)                    10
exchange txn + SEBI + stamp + GST on charges    ~2
brokerage (discount broker, delivery)           0
fixed subtotal                                  12
+ impact by bucket (A/B/C)                      5 / 20 / 50
total one-way                                   17 / 32 / 62
```

`cost(month) = Σ_over trades |Δw| × total_one_way(bucket)`. Kept in `quant.toml [costs]` so the owner can change them; every scoreboard row stores the cost vector used.

### 8.4 Alpha scoreboard (the definition of "alpha" this project reports)

```
alpha_ew(pf, t)    = ret_net(pf, t) - ret(BM_EW, t)             PRIMARY: same universe, same weighting, dividends in both
alpha_index(pf, t) = ret_net(pf, t) - ret(BM_N500PR, t)         secondary; overstates alpha by the index's dividend yield
cumulative, rolling-12, and HAC t of the monthly alpha_ew series (lag 0: monthly returns do not overlap)
```

`portfolio_returns(as_of, portfolio_id, ret_gross, turnover, cost, ret_net, bm_ew, bm_cw, bm_index, alpha_ew, alpha_index)`. The dashboard shows PF-LIVE, PF-EW and every challenger on the same axes; `PF-EW` is always visible so "the learner did nothing useful" cannot hide.

### 8.5 Acceptance tests

```
tests/test_portfolio.py::test_sector_cap_enforced
tests/test_portfolio.py::test_buffer_keeps_rank_45_drops_rank_61
tests/test_portfolio.py::test_turnover_formula_half_sum_abs
tests/test_costs.py::test_bucket_boundaries_inclusive_exclusive        (5 crore -> B; 4.99 -> C)
tests/test_costs.py::test_cost_uses_bucket_at_trade_time
tests/test_scoreboard.py::test_alpha_ew_zero_for_ew_universe_portfolio (PF holding everything EW -> alpha 0 +- rounding)
```

---

## 9. Feedback loop & knowledge base

### 9.1 The monthly cycle

```
   first weekday after month-end, IST                                       human / LLM in the approval seat
   ==========================================================================================================
   python -m quant run-month --as-of 2026-09-30
   |
   |  1 universe      fetch Nifty 500 CSV + 2 benchmark constituent CSVs -> data/universe/
   |                  -> universe_membership, benchmark_membership, sector_map (versioned)
   |  2 prices        yf.download incremental (25/batch) -> prices_daily.sqlite -> prices_monthly,
   |                  corporate_actions, benchmarks_monthly
   |  3 fundamentals  per symbol info+statements (0.5 s sleep) -> fundamentals_snapshot        [~30 min]
   |  4 dq-gate       flags + gates -> dq_runs, dq_events;  BLOCKED? -> write report, stop, exit 2
   |  5 factors       FactorInputs(as_of) -> raw, neutral -> factor_values  (all registered, any status)
   |  6 score         every non-retired model -> scores, model_weights (fit under embargo)
   |  7 labels        fill forward_returns for every older as_of whose end month now has prices
   |  8 evaluate      IC/quintiles/HAC per subject x horizon x scope -> evaluations, learning_curve
   |  9 portfolio     rebalance paper portfolios -> portfolio_holdings, portfolio_returns
   | 10 leakage       shuffle + plant + as_of checks -> dq_events (fail -> exit 2)
   | 11 propose       promotion / retirement / weight-update / quarantine candidates -> proposals (pending)
   | 12 report        knowledge/reports/2026-09.md (auto), ui/data.js
   | 13 export        data/monthly/2026-09/*.csv, knowledge/db/*.jsonl appended, MANIFEST.json
   v
   git add data knowledge ui && git commit -m "run-month 2026-09"          (human runs this)
                                                                            |
   python -m quant proposals list                                           |  reads knowledge/reports/2026-09.md
   python -m quant approve P-2026-09-001 --by human:saurabh --note "..."    |  or reject; LLM may DRAFT the note
   python -m quant apply --as-of 2026-09-30      (re-scores only what the decision changes; new decision row)
   git commit -m "decisions 2026-09"
```

Steps 1–13 change no factor status and no weights on their own: step 6 uses the status and the learning rule already approved; step 11 only writes *pending* proposals. That is the whole approval protocol: **the loop may measure and suggest; only a decision row may change what the loop does next.**

Elapsed time budget: fundamentals ~30 min, everything else < 5 min. `--skip-fundamentals` reuses the month's existing snapshot for re-runs.

### 9.2 Knowledge base schema (SQLite + JSONL mirror under `knowledge/db/`)

```sql
CREATE TABLE hypotheses (
  hyp_id TEXT PRIMARY KEY,             -- 'H-0007'
  registered_on TEXT NOT NULL,         -- YYYY-MM-DD, must be <= first evaluated as_of
  kind TEXT NOT NULL,                  -- 'factor' | 'model' | 'sector_feature' | 'parameter'
  subject TEXT NOT NULL,               -- factor name+version, model_id, or config key
  statement TEXT NOT NULL,             -- one sentence, falsifiable
  expected_sign INTEGER,               -- +1 / -1 / NULL
  horizon_m INTEGER NOT NULL,
  pre_registered_by TEXT NOT NULL,     -- 'human:saurabh' | 'llm:<name>'
  status TEXT NOT NULL,                -- 'open' | 'supported' | 'rejected' | 'withdrawn'
  n_periods INTEGER, t_hac REAL, t_crit REAL, m_tests_at_eval INTEGER,
  evaluated_on TEXT, outcome TEXT, decision_id TEXT
);
CREATE TABLE experiments (
  exp_id TEXT PRIMARY KEY, hyp_id TEXT REFERENCES hypotheses, run_id TEXT REFERENCES runs,
  started_at TEXT, config_json TEXT, result_json TEXT, verdict TEXT, notes TEXT
);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY,        -- 'P-2026-09-001'
  created_on TEXT, run_id TEXT, kind TEXT,   -- 'promote_factor' | 'retire_factor' | 'quarantine_factor' |
                                             -- 'update_weights' | 'promote_model' | 'change_parameter'
  subject TEXT, payload_json TEXT, rationale TEXT, evidence_eval_ids TEXT,
  status TEXT NOT NULL DEFAULT 'pending',    -- 'pending' | 'approved' | 'rejected' | 'expired'
  decided_on TEXT, decided_by TEXT, decision_id TEXT
);
CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY,        -- 'D-0012'
  decided_on TEXT NOT NULL, decided_by TEXT NOT NULL,   -- 'human:...' or 'llm:...'
  kind TEXT NOT NULL, subject TEXT NOT NULL, summary TEXT NOT NULL,
  adr_path TEXT NOT NULL,              -- knowledge/decisions/D-0012-<slug>.md
  proposal_id TEXT, applies_from_as_of TEXT, reverted_by TEXT
);
CREATE TABLE data_quality_events (    -- alias view of dq_events for the knowledge base
  event_id INTEGER PRIMARY KEY, run_id TEXT, as_of TEXT, symbol TEXT, scope TEXT, code TEXT,
  severity TEXT, detail TEXT
);
CREATE TABLE lessons (
  lesson_id TEXT PRIMARY KEY, recorded_on TEXT, source TEXT,   -- 'run' | 'review' | 'incident'
  text TEXT NOT NULL, related_ids TEXT
);
```

Human-readable mirror:

```
knowledge/
  README.md                     how to read this folder
  decisions/D-0001-initial-active-set.md ...     ADR: context, decision, evidence (eval ids), consequences, revert plan
  factors/<name>.md             pre-registration record, updated with each status change
  reports/2026-09.md            auto-generated monthly report (template in §9.6)
  lessons.md                    append-only ledger (one line per lesson, dated, linked)
  proposals/2026-09.md          the month's pending proposals in prose
  db/*.jsonl                    one line per row for every table above (git-friendly, rebuildable)
```

### 9.3 Decision records

Every change to factor status, model status, weights rule parameters, cost table, gate thresholds, or universe rules requires a decision row and an ADR file. `quant approve` writes both; a manual change is done with `quant decide --kind ... --subject ... --summary ... --by human:saurabh` which does the same. The ADR template:

```
# D-0012 — Promote inst_hold_chg_3m to active
Date / decided by / proposal
 ## Context        (what the loop observed; eval ids; the chart)
 ## Decision       (exact status/parameter change; applies_from_as_of)
 ## Evidence       (n_periods, mean IC, HAC t, t_crit at m tests, coverage; Tier A only)
 ## Alternatives   (what else was considered, why not)
 ## Consequences   (what changes in the composite next month; expected effect)
 ## Revert         (the one command that undoes it and what history it leaves)
```

### 9.4 Multiple-testing control

Decode: *m* is the count of hypotheses (factors, model variants, parameter changes) ever registered against the same horizon since inception. Every additional hypothesis tested makes a lucky-looking result more likely; the threshold rises to compensate.

```
budget       <= 6 new hypotheses registered per calendar year (config hypotheses.max_per_year; CLI refuses the 7th)
t_crit(m)    = max(2.0, Phi^{-1}(1 - 0.05 / m))         one-sided 5%, Bonferroni over m
               m=1: 2.00   m=3: 2.13   m=6: 2.39   m=12: 2.64   m=24: 2.87     (scipy.stats.norm.ppf; the test pins these)
recorded     hypotheses.m_tests_at_eval and t_crit are stored at evaluation time so the bar cannot move later
```

This is deliberately blunt. A false-discovery-rate procedure would be more powerful and harder to explain; the budget of 6 per year keeps *m* small enough that Bonferroni is not crippling.

### 9.5 Promotion and retirement criteria (all Tier A, all at 3M unless stated)

```
Promote factor candidate -> active
  n_periods >= 12 monthly labelled evaluations with as_of >= registered_on
  mean IC x direction > 0 and t_hac >= t_crit(m)
  12M sign check: where >= 3 monthly 12M labels exist, mean 12M IC x direction >= 0
  redundancy: |Spearman(neutral_new, neutral_k)| < 0.80 vs every active factor in the same family (latest as_of)
  coverage: non-NaN for >= 80% of eligible symbols in the last 3 runs

Retire factor active -> retired
  24-month rolling t_hac < 0 (at least 12 periods present), or hypothesis formally rejected

Quarantine (any status) -> quarantined
  coverage < 60% for 3 consecutive runs, or a unit/DQ defect found (e.g. the 349% yield case)
  effect: excluded from composites from applies_from_as_of; historical rows flagged, not deleted

Promote model challenger -> live
  >= 12 monthly paper periods; mean(IC_chal - IC_ew) > 0 with t_hac >= t_crit(m_models);
  net paper return >= PF-EW over the same window; passes leakage tests
  and the baseline keeps running regardless

Update weights (for a live learned model)
  automatic in scoring (§6.3) but the vector is only *used* if the gate is open; each month's vector is stored;
  no proposal needed because the rule was approved, not the numbers. Changing the RULE (k_shrink, floor, cap)
  is a 'change_parameter' proposal.
```

### 9.6 Monthly report template (auto-generated, `knowledge/reports/YYYY-MM.md`)

```
# Run 2026-09 (as_of 2026-09-30)
> Verdict line: DQ passed/blocked; months_clean = N; EW composite rolling-12 3M IC = x (HAC t = y, CI);
  learned vs EW diff = z; PF-LIVE alpha_ew YTD = a% (net); proposals pending = k.
 ## 1 Data quality        gates table; flag shares; universe changes (in/out); sector reclassifications
 ## 2 Labels filled       which as_of x horizon became complete this month
 ## 3 Factor table        per factor: status, n_periods, mean IC (1M/3M/12M), HAC t, coverage, redundancy
 ## 4 Models              EW vs challengers: IC, spread gross/net, paper alpha; weight vectors used
 ## 5 Learning curve      the numbers behind the chart (last 12 points)
 ## 6 Multi-bagger KPI    cohorts complete (Tier A: none until 2029-10; Tier B: listed with label)
 ## 7 Proposals           P-ids with one-line rationale and the evidence ids
 ## 8 Lessons             anything the run flagged that a human should remember
 ## 9 Reproduce           git sha, config hash, command line
```

### 9.7 How a new parameter or factor is added safely

```
1. Write knowledge/factors/<name>.md (hypothesis, sign, horizon, formula, inputs, evidence, who, date).
2. Add the FactorSpec in quant/factors/<family>_factors.py with a unit test on synthetic data.
3. python -m quant factor register <name>   -> factor_registry (status candidate, registered_on = today),
                                               hypotheses row (kind factor), CLI checks budget (max 6/yr)
4. From the next run the factor is computed and evaluated; it is not in any composite.
5. If tier_b_backfillable: python -m quant factor backfill <name>  -> Tier-B rows only (labelled).
6. After >= 12 Tier-A labelled periods the loop drafts a promote/reject proposal automatically.
7. Human approves -> decision + ADR -> applies_from_as_of = next run. History before that is untouched.
```

A *parameter* (e.g. `min_group_size`, `k_shrink`, a cost) follows the same path with `kind='parameter'`: it is registered as a hypothesis with the expected effect, changed via a decision, and its `applies_from_as_of` is stored so evaluations before and after can be compared. Config values used in each run are hashed into `runs.config_hash` and the full TOML is copied to `data/monthly/<YYYY-MM>/config.toml`.

### 9.8 Approval protocol

```
Actor            May do                                                        May not do
---------------  ------------------------------------------------------------  ------------------------------------
the loop         ingest, score with approved config, evaluate, fill labels,     change any status/weight rule/param
                 draft proposals and reports, raise dq_events
LLM (any)        draft ADR text, draft proposal rationale, approve proposals   approve promote_factor, promote_model,
                 of kind in approval.allow_llm_for (default: ['quarantine_factor'])   update rule parameters
human            everything; must supply --by human:<handle>                   --
```

`approval.allow_llm_for` is in `quant.toml`; widening it is itself a `change_parameter` decision that only a human can make. Every `decided_by` is stored; the report prints how many decisions in the last 12 months were LLM-made.

### 9.9 Acceptance tests

```
tests/test_knowledge.py::test_register_hypothesis_enforces_yearly_budget
tests/test_knowledge.py::test_t_crit_table                                (m=1,3,6,12,24 to 2 dp)
tests/test_knowledge.py::test_proposal_lifecycle_pending_approved_creates_decision_and_adr
tests/test_knowledge.py::test_llm_cannot_approve_promote_factor
tests/test_knowledge.py::test_jsonl_mirror_roundtrip
tests/test_promotion.py::test_promotion_rule_on_synthetic_evaluations      (11 periods -> no; 12 with t>=t_crit -> proposal)
tests/test_promotion.py::test_redundancy_blocks_promotion
tests/test_report.py::test_monthly_report_renders_all_sections_from_fixture_db
```

---

## 10. Architecture

### 10.1 Package layout

```
quant/
  __init__.py
  __main__.py                 argparse CLI, `python -m quant <group> <command>`; no click/typer dependency
  config.py                   loads quant.toml (tomllib), env overrides QUANT_DB_PATH / QUANT_DATA_DIR
  db/
    schema.sql                full DDL (§10.2); applied idempotently
    core.py                   connect(), apply_schema(), upsert(df, table, keys), replace_as_of(table, as_of, df)
    export.py                 export(as_of) -> data/monthly/YYYY-MM/*.csv ; rebuild() ; verify()
  data/
    calendar.py               trading days from ^CRSLDX; last_trading_day_on_or_before; month_ends
    universe.py               fetch_nifty500(as_of) -> csv path; load_membership(as_of)
    yahoo.py                  ONE place for yfinance calls + unit normalisation; sleep policy; disk cache of raw json
    prices.py                 backfill(start), update(), build_monthly(), manifest()
    fundamentals.py           fetch_snapshot(as_of); statements long-format loader; staleness
    dq.py                     flags, gates, dq_runs, block()
  sectors/
    taxonomy.py               sector_group rules (FS split, min_group_size), sector_map versioning, lookup(as_of)
    crosswalk.csv
    features.py               sector_features (M6)
  factors/
    base.py                   FactorSpec, FactorInputs
    registry.py               REGISTRY, active(as_of), register(), set_status()
    transforms.py             neutral_rank(raw, groups, direction)
    price_factors.py          mom_12_1, mom_6_1, trend_sma200, dist_52w_high, rev_1m, vol_12m, beta_12m, max_ret_1m
    quality_factors.py        roce, roe, accruals, cash_conversion_3y, op_margin
    value_factors.py          earnings_yield, ev_ebitda_yield, book_to_price, fcf_yield
    growth_factors.py         sales_growth_3y, eps_growth_3y, rev_growth_yoy_q
    ownership_factors.py      inst_hold_chg_3m, promoter_hold
    legacy_factors.py         the 8 + 2 legacy series (migration only), flag_death_cross
    inputs.py                 build_factor_inputs(as_of) -- the only code that reads tables for factors
  model/
    composite.py              family_scores(), composite(weights)
    learn.py                  fit_family_weights()
    models.py                 model definitions; score_all(as_of)
  evaluation/
    labels.py                 fill_forward_returns(); multibagger_cohort()
    metrics.py                rank_ic(), quintiles_within_sector(), spreads()
    stats.py                  hac_mean_test()
    walkforward.py            labels_available(T, h); replay_tier_b()
    leakage.py                shuffle_test(), plant_test(), as_of_test()
    learning_curve.py
  portfolio/
    construct.py              rebalance(model_id, as_of) with cap + buffer
    costs.py                  bucket(), cost_one_way()
    scoreboard.py
  knowledge/
    registry.py               hypotheses, budget, t_crit
    proposals.py              draft_proposals(as_of); approve(); reject(); apply()
    decisions.py              decide(); ADR writer
    reports.py                monthly report renderer
    lessons.py
  legacy/
    migrate_legacy.py         quant_engine.db -> new schema (§10.5)
  ui_export.py                writes ui/data.js
tests/                        pytest; network tests marked @pytest.mark.network and skipped by default
  conftest.py                 tmp DB fixture; synthetic universe of 60 symbols x 6 groups x 48 months
  fixtures/nifty500_2026-09-05.csv, idx_mom30_2026-09-05.csv, idx_qual30_2026-09-05.csv, prices_small.csv,
  yahoo_info_samples.json (recorded `info` dicts for HEROMOTOCO / PFOCUS / TENNIND with their None fields as observed)
data/, knowledge/, ui/        as in §4.1
quant.toml
monthly_cron.sh               `python -m quant run-month` with logging; replaces daily_cron.sh
```

**Dependencies:** the M1 build adds **no** package to `requirements.txt`. Everything runs on the standard library (`sqlite3`, `tomllib`, `argparse`, `json`, `hashlib`) plus the pandas / numpy / scipy / yfinance / pytest already installed. This is a design constraint, not an accident: each new binary dependency is a way for the monthly run to stop working on a fresh machine.

The existing top-level scripts (`harness_v16_learning.py`, `weight_optimizer.py`, `eval_portfolio_health.py`, `update_ui_v16.py`, `quant_math.py`, `db_setup.py`, `config.py`, both test files) are **not modified** and keep passing their 58 tests; `quant/` imports the handful of pure functions listed in §10.8. After month 3 they move to `legacy/` untouched.

### 10.2 Database schema (DDL, SQLite)

Phase tags: `[M1]` needed for the first run; `[M3]`, `[M6]` may be created later (the schema file contains them all; empty tables cost nothing).

```sql
-- runs & DQ                                                                   [M1]
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, kind TEXT NOT NULL,   -- 'monthly' | 'legacy' | 'backfill'
  started_at TEXT, finished_at TEXT, git_sha TEXT, config_hash TEXT,
  dq_status TEXT NOT NULL,                                            -- 'passed' | 'passed_with_warnings' | 'blocked' | 'legacy_defects'
  notes TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_asof_kind ON runs(as_of, kind);
CREATE TABLE IF NOT EXISTS dq_runs (run_id TEXT, gate TEXT, value REAL, threshold REAL, passed INTEGER, blocking INTEGER,
  PRIMARY KEY(run_id, gate));
CREATE TABLE IF NOT EXISTS dq_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, as_of TEXT, symbol TEXT,
  scope TEXT, code TEXT, severity TEXT, detail TEXT);

-- universe & sectors                                                          [M1]
CREATE TABLE IF NOT EXISTS universe_membership (as_of TEXT, symbol TEXT, isin TEXT, company_name TEXT, nse_sector TEXT,
  source TEXT, PRIMARY KEY(as_of, symbol));
CREATE TABLE IF NOT EXISTS symbol_aliases (isin TEXT, old_symbol TEXT, new_symbol TEXT, from_as_of TEXT,
  PRIMARY KEY(isin, from_as_of));
CREATE TABLE IF NOT EXISTS sector_map (symbol TEXT, sector_group TEXT NOT NULL, nse_sector TEXT, yahoo_sector TEXT,
  yahoo_industry TEXT, valid_from TEXT NOT NULL, valid_to TEXT, source TEXT, version INTEGER,
  PRIMARY KEY(symbol, valid_from));
CREATE TABLE IF NOT EXISTS sector_features (as_of TEXT, sector_group TEXT, feature TEXT, value REAL,   -- [M6]
  PRIMARY KEY(as_of, sector_group, feature));

-- prices                                                                      [M1]
CREATE TABLE IF NOT EXISTS prices_monthly (as_of TEXT, symbol TEXT, close REAL, adj_close REAL, tr_index REAL,
  turnover_avg_3m_inr REAL, volume_avg_3m REAL, n_days_3m INTEGER, source TEXT, quote_legacy REAL,
  PRIMARY KEY(as_of, symbol));
CREATE TABLE IF NOT EXISTS corporate_actions (symbol TEXT, ex_date TEXT, kind TEXT, value REAL, source TEXT,
  PRIMARY KEY(symbol, ex_date, kind));
CREATE TABLE IF NOT EXISTS benchmarks_monthly (as_of TEXT, bm_id TEXT, level REAL, ret_1m REAL, PRIMARY KEY(as_of, bm_id));
CREATE TABLE IF NOT EXISTS benchmark_membership (as_of TEXT, bm_id TEXT, symbol TEXT, isin TEXT, source TEXT,   -- BM_MOM30_C, BM_QUAL30_C
  PRIMARY KEY(as_of, bm_id, symbol));

-- fundamentals                                                                [M1]
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (as_of TEXT, symbol TEXT, field TEXT, value REAL,
  fiscal_period_end TEXT, source TEXT, fetched_at TEXT, dq_flag TEXT, PRIMARY KEY(as_of, symbol, field));
CREATE INDEX IF NOT EXISTS ix_fund_symbol_field ON fundamentals_snapshot(symbol, field, as_of);

-- factors                                                                     [M1]
CREATE TABLE IF NOT EXISTS factor_registry (factor_name TEXT PRIMARY KEY, version INTEGER, family TEXT, direction INTEGER,
  horizon_m INTEGER, hypothesis TEXT, formula TEXT, inputs_json TEXT, applies_to_financials INTEGER,
  tier_b_backfillable INTEGER, evidence TEXT, status TEXT NOT NULL, registered_on TEXT NOT NULL,
  status_changed_on TEXT, decision_id TEXT, hyp_id TEXT);
CREATE TABLE IF NOT EXISTS factor_values (as_of TEXT, factor_name TEXT, symbol TEXT, raw REAL, neutral REAL,
  sector_group TEXT, dq_flag TEXT, tier TEXT NOT NULL DEFAULT 'A', PRIMARY KEY(as_of, factor_name, symbol));
CREATE INDEX IF NOT EXISTS ix_fv_factor_asof ON factor_values(factor_name, as_of);

-- models & scores                                                             [M1]
CREATE TABLE IF NOT EXISTS models (model_id TEXT PRIMARY KEY, kind TEXT, description TEXT, params_json TEXT,
  status TEXT NOT NULL, registered_on TEXT, decision_id TEXT);
CREATE TABLE IF NOT EXISTS model_weights (model_id TEXT, as_of TEXT, family TEXT, weight REAL, n_eff REAL, alpha REAL,
  gate TEXT, PRIMARY KEY(model_id, as_of, family));
CREATE TABLE IF NOT EXISTS scores (as_of TEXT, model_id TEXT, symbol TEXT, score REAL, rank_sector INTEGER,
  rank_overall INTEGER, sector_group TEXT, eligible INTEGER, liquidity_bucket TEXT, tier TEXT NOT NULL DEFAULT 'A',
  PRIMARY KEY(as_of, model_id, symbol));

-- labels & evaluation                                                         [M1]
CREATE TABLE IF NOT EXISTS forward_returns (as_of TEXT, symbol TEXT, horizon_m INTEGER, end_date TEXT, ret_total REAL,
  ret_excess_sector REAL, ret_excess_universe REAL, flag TEXT, PRIMARY KEY(as_of, symbol, horizon_m));
CREATE TABLE IF NOT EXISTS evaluations (eval_id INTEGER PRIMARY KEY AUTOINCREMENT, as_of TEXT, subject_kind TEXT,
  subject_id TEXT, horizon_m INTEGER, universe_scope TEXT, tier TEXT, n INTEGER, ic REAL, ic_naive_t REAL,
  q1_ret REAL, q2_ret REAL, q3_ret REAL, q4_ret REAL, q5_ret REAL, spread_gross REAL, turnover_q5 REAL,
  spread_net REAL, top_decile_hit_rate REAL, computed_at TEXT,
  UNIQUE(as_of, subject_kind, subject_id, horizon_m, universe_scope, tier));
CREATE TABLE IF NOT EXISTS evaluations_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, eval_key TEXT, old_ic REAL,
  new_ic REAL, changed_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS learning_curve (model_id TEXT, horizon_m INTEGER, as_of TEXT, tier TEXT, months_clean INTEGER,
  ic_oos_rolling12 REAL, hac_t REAL, ci_lo REAL, ci_hi REAL, PRIMARY KEY(model_id, horizon_m, as_of, tier));
CREATE TABLE IF NOT EXISTS multibagger_cohorts (as_of TEXT, model_id TEXT, tier TEXT, n INTEGER, n_2x INTEGER,
  top_decile_n INTEGER, top_decile_2x INTEGER, recall REAL, precision REAL, base_rate REAL, lift REAL,
  PRIMARY KEY(as_of, model_id, tier));

-- portfolio                                                                   [M3]
CREATE TABLE IF NOT EXISTS portfolio_holdings (as_of TEXT, portfolio_id TEXT, symbol TEXT, weight REAL,
  entry_as_of TEXT, liquidity_bucket TEXT, PRIMARY KEY(as_of, portfolio_id, symbol));
CREATE TABLE IF NOT EXISTS portfolio_returns (as_of TEXT, portfolio_id TEXT, ret_gross REAL, turnover REAL, cost REAL,
  ret_net REAL, bm_ew REAL, bm_cw REAL, bm_index REAL, alpha_ew REAL, alpha_index REAL, cost_vector_json TEXT,
  PRIMARY KEY(as_of, portfolio_id));

-- knowledge base: see §9.2 (hypotheses, experiments, proposals, decisions, lessons)   [M1 minimal, M3 full]
```

`as_of` columns are ISO `YYYY-MM-DD` text everywhere (SQLite has no date type; ISO text sorts correctly). All money in rupees. All fractions as fractions (0.0348), never percent, in every table; the UI multiplies.

### 10.3 CLI

```
python -m quant db      init | rebuild | export [--as-of] | verify
python -m quant migrate legacy [--legacy-db quant_engine.db] [--dry-run]
python -m quant universe fetch --as-of YYYY-MM-DD
python -m quant prices  backfill --start 2016-01-01 | update | monthly | manifest
python -m quant fundamentals fetch --as-of YYYY-MM-DD [--symbols A,B]
python -m quant dq      gate --as-of
python -m quant factors compute --as-of [--factor NAME] [--tier B --from 2016-01-31]
python -m quant factor  register NAME | set-status NAME STATUS --decision D-xxxx | backfill NAME
python -m quant score   --as-of [--model ID]
python -m quant labels  fill
python -m quant evaluate --as-of | learning-curve | multibagger
python -m quant portfolio rebalance --as-of
python -m quant test    leakage --as-of
python -m quant propose --as-of
python -m quant proposals list | show P-id
python -m quant approve P-id --by human:NAME [--note TEXT] | reject P-id --by ... --note TEXT
python -m quant decide  --kind K --subject S --summary TEXT --by human:NAME
python -m quant apply   --as-of
python -m quant report  --as-of
python -m quant ui      export
python -m quant run-month --as-of [--skip-fundamentals] [--dry-run] [--force]
```

Exit codes: `0` ok, `1` error, `2` blocked by DQ/leakage gate. Every command prints a one-line summary ending with the row counts it wrote, e.g.:

```
[score] as_of=2026-09-30 models=2 symbols=498 eligible=471 wrote scores=996 model_weights=12 (gate=closed)
[migrate] legacy runs=6 evaluable=4 scores=2543 factor_values=27973 model_weights=96 decisions=1 (D-0000-migration) second_run=no-op
[run-month] as_of=2026-09-30 dq=passed months_clean=1 factors=22 models=2 labels_filled=0 evals_tierA=0 evals_tierB=2904 proposals=0 elapsed=00:38:41
```

The `[migrate]` counts are exact (2,543 legacy rows × 11 legacy series = 27,973; 12 weight rows × 8 families = 96). The `[run-month]` counts other than `factors`, `models` and `months_clean` are illustrative targets; the first real run records the actual line in `knowledge/reports/2026-09.md` §9.

### 10.4 Config (`quant.toml`)

```toml
[paths]        data_dir = "data"   db_path = "quant.db"   knowledge_dir = "knowledge"   ui_dir = "ui"
[universe]     source_url = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"   min_rows = 450
[universe.benchmark_lists]
               mom30 = "https://niftyindices.com/IndexConstituent/ind_nifty200Momentum30_list.csv"
               qual30 = "https://niftyindices.com/IndexConstituent/ind_nifty200Quality30_list.csv"
[sectors]      min_group_size = 8   split_financials = false          # true from M3 via decision
[yahoo]        per_symbol_sleep_s = 0.5   batch_size = 25   batch_sleep_s = 1.0   history_start = "2016-01-01"
               daily_cache = "data/prices/prices_daily.sqlite"   index_symbols = ["^CRSLDX", "^NSEI", "^CNX200", "MOM30IETF.NS", "QUAL30IETF.NS"]
[horizons]     learn_m = 3   report_m = [1, 3, 6, 12, 24, 36]   multibagger_m = 36   multibagger_mult = 2.0
[dq]           price_cov = 0.95   fund_cov = 0.80   sector_cov = 0.98   max_div_yield = 0.25   max_abs_fcf_yield = 2.0
               stale_warn_m = 15   imputed_warn = 0.20   near_constant_share = 0.80   extreme_move = 0.60
[learn]        k_shrink = 24   floor_mult = 0.5   cap_mult = 2.0   min_n_eff = 4
[liquidity]    min_turnover_inr = 2.0e7   bucket_a = 2.5e8   bucket_b = 5.0e7
[costs]        fixed_bps = 12   impact_bps = { A = 5, B = 20, C = 50 }
[portfolio]    n = 30   buffer_rank = 60   sector_cap = 6
[hypotheses]   max_per_year = 6   alpha = 0.05   t_floor = 2.0
[approval]     allow_llm_for = ["quarantine_factor"]
```

### 10.5 Migration of `quant_engine.db`

`python -m quant migrate legacy` is idempotent (keyed on `runs.kind='legacy'`), reads the legacy DB read-only, and performs:

```
1. runs: one row per legacy date group
     2026-06-04  47 rows  kind=legacy dq_status=legacy_defects notes='partial (47 rows); excluded from evaluation'
     2026-06-12 499 rows  ...            notes='full; superseded by 2026-06-14 two days later; excluded from evaluation'
     2026-06-14 499 rows  ... as_of normalised to 2026-06-12 for prices (Sunday)
     2026-07-11 499 rows  ... as_of normalised to 2026-07-10 (Saturday)
     2026-08-14 499 rows
     2026-09-03 500 rows
   Recorded defects on every legacy run: Div_Yield_% x100 (legacy_unit_bug_x100), FCF_Yield_% /1e7
   (legacy_unit_bug_1e7), ROE None treated as 0 in trap score, no Data_Flags (legacy_unflagged),
   sentiment term inside final_score for runs before 2026-09-03, moat/risk hand-picked lists.
2. universe_membership: tickers per legacy date, source='legacy_snapshot', isin from the current CSV where the
   symbol matches (500 of 501 distinct legacy tickers; verified 2026-09-05), nse_sector from the current CSV
   (flag sector_carried_forward).
3. sector_map version 0: from the 2026-09-05 CSV, valid_from='2026-06-01', source='backfilled_not_pit'.
4. prices_monthly for the four normalised as_of dates from the daily cache (adj_close), legacy quote in
   quote_legacy. (Requires `prices backfill` first; the command checks and says so.)
5. fundamentals_snapshot from raw_json: Trailing_PE, ROCE_%, Debt_to_Equity, Inst_Holdings_%, SMA_50, SMA_200,
   Intrinsic_Value, Margin_Of_Safety_%, Div_Yield_% (dq_flag legacy_unit_bug_x100; value stored AS IS, a
   corrected column is NOT invented), FCF_Yield_% (legacy_unit_bug_1e7), ocf_array/fcf_array (crores, 4 FY,
   fiscal_period_end unknown -> NULL, flag 'fy_end_unknown'). Units converted to fractions where the field is a
   known fraction (Inst_Holdings_% / 100).
   Verified 2026-09-05: the 2026-09-03 raw_json has exactly 33 keys and lacks Data_Flags, Industry and
   Market_Cap_Cr (all three were added to the harness after that run). So every legacy row gets
   legacy_unflagged, marketCap stays NULL (not reconstructed), and HEROMOTOCO.NS carries
   Div_Yield_% = 349.0 and FCF_Yield_% = 67904587.19 -- the two unit defects the flags name.
6. factor_values: the 8 scores + trap_score + momentum_multiplier + flag_death_cross as legacy_* (raw = stored
   value; neutral computed fresh within sector_group). factor_registry rows with status 'retired',
   registered_on = '2026-06-14', decision_id = 'D-0000-migration'.
7. scores: model_id='legacy_v18' with score = final_score; model_id='legacy_v18_base' with score = base_score
   where present, otherwise reconstructed as sum(w_in_force x score) with the sentiment term excluded
   (flag 'reconstructed'); rank columns computed.
8. model_weights: 12 rows of active_weights -> model_id='legacy_v18', family = the 8 legacy names,
   as_of = last_updated, gate='n/a'; trained_through preserved in params_json.
9. forward_returns: recomputed from adj_close for h in {1, 3} where end dates exist (06-12->07-10 is 28 days:
   stored as horizon_m=1 with end_date explicit); the old performance_tracking (4,773 rows after the red-team dedupe) is NOT imported (it mixes
   unadjusted quotes and 4-day "periods") but is summarised in the migration ADR.
10. decisions: 'D-0000-migration' with the ADR knowledge/decisions/D-0000-migration.md listing every defect.
```

Acceptance for the migration (these are the tests):

```
tests/test_migrate_legacy.py::test_row_counts                 scores(legacy_v18) == 2543; factor_values == 2543 x 11
tests/test_migrate_legacy.py::test_weekend_as_of_normalised
tests/test_migrate_legacy.py::test_zfcvindia_return_jun_jul_sane      |ret_1m| < 0.60 using adj_close
tests/test_migrate_legacy.py::test_legacy_ic_reproduced_within_tolerance
      Spearman(final_score 2026-08-14, ret 08-14 -> 09-03 from adj_close) within +-0.03 of the red-team +0.117
      (prices differ: adjusted month-end-ish vs run-time quotes; a large gap means a join bug)
tests/test_migrate_legacy.py::test_idempotent_second_run_writes_nothing
tests/test_migrate_legacy.py::test_defect_flags_present_on_every_legacy_row
```

### 10.6 UI changes (vanilla, no build step)

`ui/data.js` keeps `acceptedStocks/rejectedStocks/turnaroundStocks/aiWeights/snapshotMeta` for the existing three tabs (fed from the live model) and adds:

```
const learningCurve  = [{as_of, months_clean, series: {ew_3m, chal_3m, ew_12m, tierB_mom_12m}, ci: {...}}]
const factorTable    = [{name, family, status, registered_on, n_periods, ic_1m, ic_3m, ic_12m, hac_t_3m, coverage}]
const scoreboard     = [{as_of, pf_live_net, pf_ew_net, bm_ew, bm_index, alpha_ew_cum}]
const dqPanel        = {gates: [...], flags: {...}, months_clean, last_blocked}
const sectorTable    = [{sector_group, n, mom_6m, breadth, median_rank_live_top30_share}]
const knowledgeFeed  = [{date, kind, id, summary, by}]     // last 20 decisions/proposals
const modelWeights   = {model_id: {family: weight}}
```

New tabs in `ui/index.html`: **Learning curve** (line chart, CI band, tier-B dotted), **Scoreboard**, **Factors**, **Sectors**, **Knowledge**. Chart.js stays; the implementer vendors `chart.umd.js` into `ui/vendor/` (one static file, still no build step) so the README's "zero-dependency" claim becomes true offline. Every IC shown carries its `n_eff` in a tooltip. Tier-B numbers are never shown without the word "backfill" next to them. The per-stock detail view replaces the "FATAL MULTIPLIER" prose with the factor breakdown: family scores, rank in sector, eligibility reason, data flags.

### 10.7 Data flow

```
 niftyindices CSV ----> data/universe/*.csv ----> universe_membership ----> sector_map (versioned)
                                                                                  |
 yfinance download ---> data/prices/prices_daily.sqlite -> prices_monthly ---------+---> FactorInputs(as_of)
 (25/batch, 1 s)        (+ MANIFEST.json)                corporate_actions        |          |
                                                          benchmarks_monthly      |          v
 yfinance info + ----> .cache/yahoo/*.json ----> fundamentals_snapshot -----------+    factor_values (raw, neutral)
 statements (0.5 s)    (unit-normalised once)      (fiscal_period_end, dq_flag)              |
                                                                                             v
                                   dq_runs / dq_events  <---- gate ---->  BLOCK        scores (per model)
                                                                                             |
 prices_monthly (later months) ----> forward_returns (1..36 m) ---------> evaluations <------+---> portfolio_*
                                                                              |                    |
                                                                              v                    v
                                                      learning_curve, multibagger_cohorts     scoreboard
                                                                              |
                                                                              v
                                                     proposals --(human)--> decisions --> factor_registry / models
                                                                              |
                                                 knowledge/reports/YYYY-MM.md, ui/data.js, data/monthly/YYYY-MM/*.csv
```

### 10.8 Reuse of existing code

```
Reused as-is (imported by quant/)
  quant_math.normalize_yield, calculate_cagr, estimate_growth, sector_tokens
  weight_optimizer.project_weights (bounds projection), rank_ic (wrapped), build_transitions (tests only)
  harness_v16_learning._row_series, _fcf_series (statement parsing; copied into quant/data/fundamentals.py with tests)
  eval_portfolio_health near-constant detection logic (rewritten as a DQ gate)
  db_setup.ensure_schema pattern (idempotent DDL) -> quant/db/core.apply_schema
Reused as data
  quant_engine.db (migration source), ui/index.html + style.css + app.js (extended, not rewritten)
Not reused
  bucketed score_* functions, hand-picked moat/risk lists, DCF/justified P/B (valuation now uses market multiples;
  DCF may return later as a registered candidate), concall_analyzer, momentum multiplier, trap multiplier,
  exp_gradient_step (replaced by fit_family_weights), daily_cron.sh
```

### 10.9 Acceptance test index (target: >= 120 tests at M1)

Sections 2.4, 3.4, 4.7, 5.5, 6.7, 7.8, 8.5, 9.9 and 10.5 list ~75 named tests. The remainder are per-factor synthetic tests (22 factors × 2) and CLI smoke tests (`tests/test_cli.py::test_every_command_has_help`, `test_run_month_on_fixture_db_end_to_end`). All run offline in under 60 seconds; `@pytest.mark.network` tests (live Yahoo unit checks against the values in the brief) run only with `pytest -m network`.

---

## 11. Phased roadmap

### 11.1 Month 1 (ships by 2026-10-31) — "a clean loop that runs"

```
Week 1  quant/db (schema, upsert, export/rebuild/verify), calendar, universe, prices backfill+monthly, yahoo units
        tests: db roundtrip, calendar, units, prices batching, universe fixture
Week 2  fundamentals snapshot, dq gates, sectors (min_group_size only), FactorInputs, transforms,
        price factors (all 8) + value factors from info (3) + roce/roe/accruals + inst_hold_chg_3m (NaN for now)
        tests: factor contract, transforms, each factor on synthetic data, dq gates
Week 3  composite ew_family_v1, ic_shrunk_k24_v1 (gate closed), labels, metrics, HAC, evaluations,
        learning_curve, leakage tests, tier-B replay for price factors 2016-2026
        tests: learn worked example, hac, walkforward, leakage
Week 4  legacy migration, run-month orchestration, report, ui export + Learning-curve/Factors tabs,
        monthly_cron.sh, AGENTS.md update, first real run as_of 2026-09-30
        tests: migrate_legacy, report render, run-month on fixture DB end-to-end (< 60 s offline)
```

Exit criteria = the M1 row in §1.2. Deliberately absent in month 1: paper portfolio (gross quintile spreads only), proposals CLI (decisions written with `quant decide`), sector features, FS split, statement-based growth factors.

### 11.2 Month 3 — "measured, with costs"

Paper portfolios and cost model (§8), proposals/approve/apply workflow (§9), FS split as decision D-0002 with before/after IC, statement-based factors (`cash_conversion_3y`, `fcf_yield`, `sales_growth_3y`, `eps_growth_3y`) registered as candidates, Tier-B2 fundamental backfill (labelled), Scoreboard and Knowledge tabs, old scripts moved to `legacy/`.

### 11.3 Month 6 — "sector-aware beyond neutralisation"

`sector_features`, `sector_overlay_v1` challenger registered as hypothesis H-00xx, NSE index-level TRI history adapter (hand-downloaded files) to extend `BM_MOM30_C`/`BM_QUAL30_C` back before 2026-10, multibagger cohort report for Tier B, first review of retirements (nothing will qualify yet; the report says so).

### 11.4 Month 12 — "a year of clean data"

Twelve Tier-A points on the learning curve; first 12M labels for the October 2026 cohort; annual review ADR summarising every hypothesis' status and the count `m`; decision on whether any candidate meets §9.5 (arithmetic says the earliest possible promotion is month 15).

### 11.5 What the owner sees each month

The chart in §1.4 plus the verdict line at the top of `knowledge/reports/YYYY-MM.md`. If the verdict line says the same thing for six months ("EW IC +0.02, CI includes 0, gate closed, no proposals"), that is the system working, not failing.

### 11.6 If only four weeks of implementation were available

If only four weeks of implementation exist, ship this and nothing else:

```
KEEP  db core + export/rebuild/verify; calendar; universe + sector_map (min_group_size only);
      prices backfill/monthly/manifest; fundamentals from `info` ONLY (no statements);
      dq gates (blocking set only); 8 price factors + earnings_yield + book_to_price + promoter_hold +
      inst_hold_chg_3m; ew_family_v1 (5 families, growth family empty and skipped);
      forward_returns 1/3/12; IC + within-sector quintiles + HAC; shuffle + plant leakage tests;
      learning_curve; legacy migration; run-month; monthly report; ui: Learning-curve + Factors tabs
CUT   statement-based factors (roce, accruals, ...), paper portfolio and costs (report gross spreads),
      proposals/approve CLI (write ADRs by hand with `quant decide`), challenger models (ic_shrunk equals EW
      anyway until month 15), sector features, FS split, Tier-B2, multibagger cohorts, Scoreboard/Knowledge tabs
```

The cut version still satisfies the owner's first five asks in a reduced form (sector-neutral ranking, PIT accumulation, learning curve, decisions on record, factors addable by registration) and is the same codebase the full version grows from; nothing in it is thrown away later.

---

## 12. Risks, failure modes and open questions

### 12.1 Risks and mitigations

```
#  Risk                                                  Likelihood  Mitigation in this design
-  -----------------------------------------------------  ----------  ------------------------------------------------
1  Yahoo Finance changes fields/units or rate-limits       high        all Yahoo access in one module with per-field
   (it has already changed dividendYield units once)                   tests on recorded samples; raw JSON cached per
                                                                       run; DQ gate blocks on unit anomalies; the loop
                                                                       tolerates a blocked month
2  Survivorship in Tier-B backfill misleads the owner      high        Tier B never used for promotion/weights; drawn
                                                                       dotted; word "backfill" mandatory in UI
3  Restated statements in Tier-B2 leak the future          medium      90-day lag; B2 labelled; only used for priors
4  No sector-flow data source without paid feeds           certain     proxy from institutional holdings changes,
                                                                       labelled; NSDL adapter optional
5  Fundamentals coverage for small caps is poor            medium      applies_to_financials + NaN handling; family
   (returnOnEquity None for large names already)                       score falls back to 0.5 with a flag; coverage
                                                                       is a promotion criterion
6  Owner impatience: gate closed for 15 months             high        the design says so on page 1; Tier-B curve and
                                                                       DQ/coverage metrics give monthly movement
7  Overfitting via challenger proliferation                medium      hypothesis budget 6/yr; t_crit(m); baseline
                                                                       never retired; PF-EW always on the scoreboard
8  git repository growth                                   medium      CSV partitions, no binary DB in git; SQLite
                                                                       price cache git-ignored; MANIFEST committed
9  yfinance batch download silently drops symbols          medium      manifest row counts; price coverage gate 95%
10 An implementer "improves" the learning rule             medium      worked-example test pins the arithmetic;
                                                                       rule parameters are decision-gated
11 Month-end fundamentals fetched days later drift         low         fetched_at stored; drift is small relative to
                                                                       a 3M horizon; documented
12 The whole fundamental premise is wrong for India        real        falsification criteria in §1.3; the loop is
   Nifty 500 at 3M (possible)                                          designed to say so by month 39
13 pandas 3.0 / Python 3.14 semantics (copy-on-write,      medium      pin the exact versions that pass the suite in
   default string dtype, tz-aware yfinance indexes)                    requirements.txt; tz stripped in yahoo.py only;
                                                                       no chained assignment; CI runs the same interpreter
14 NSE changes a constituent-file URL or layout            medium      files saved verbatim; loader asserts the 5-column
                                                                       header; fallback = last saved file + `universe_stale`
```

### 12.2 Where this design could be wrong

1. **Family-level weights may be too coarse.** If one value factor is good and another is noise, the family average dilutes it. Mitigation is the redundancy/retirement rule per factor, but a per-factor learner is a legitimate future challenger (registered as a hypothesis, not slipped in).
2. **3M may still be too short for value.** Published value ICs strengthen toward 12M. The 12M sign check in promotion guards against promoting a factor that only works at 3M, but the learning rule itself optimises 3M. If by month 24 the 12M and 3M pictures diverge, a `change_parameter` proposal to `learn_m = 6` is the intended path.
3. **Median-of-sector excess return on 8-member groups is noisy.** `min_group_size = 8` is a judgement; 15 would be safer statistically and worse for sector purity. Registered as a parameter so it can be changed with a record.
4. **Equal-weight paper portfolios overweight small caps** relative to any cap-weighted benchmark. That is why `BM_EW` is primary; a cap-weighted variant is one config line if the owner wants it.
5. **HAC with Bartlett lag `h−1` on 12–36 observations is itself noisy.** The CI bands will be wide for years. That is honest; it is not a defect of the estimator.

### 12.3 Open questions for the owner

```
Q1  Is a 15-month closed gate acceptable, given the shrinkage keeps alpha at 0.14 even after it opens?
Q2  Commit quant.db as well as data/ (convenience) or data/ only (this design)? If both, `db verify` must run in CI.
Q3  Should LLM approval be allowed for 'retire_factor' when the 24-month t < 0 rule fires mechanically?
Q4  Does the owner want a cap-weighted paper portfolio alongside EW?
Q5  Is constituent replication of Momentum-30 / Quality-30 from month 1 enough, or should NSE index-level TRI
    history be hand-downloaded once so those two benchmarks extend backwards?
Q6  Should the 2026-06-12 legacy snapshot be evaluated (it is a full run) or treated as superseded as proposed?
```

### 12.4 The whole argument in one diagram

```
                 what accumulates                         what protects the owner
     +------------------------------------+     +---------------------------------------------+
     | monthly PIT tables (files in git)  |     | EW baseline never retired; every model shown  |
     | adjusted total returns             |     | as a difference from it                       |
     | continuous sector-neutral factors  | --> | shrinkage alpha = n_eff/(n_eff+24); gate      |
     | labels at 1..36 months             |     | closed until 12 labelled months               |
     | evaluations with HAC t             |     | pre-registration; 6 hypotheses/yr; t_crit(m)  |
     | decisions with ADRs                |     | Tier B dotted, never cited alone              |
     +------------------------------------+     +---------------------------------------------+
                        |                                            |
                        v                                            v
              learning curve on screen  <---------------  nothing changes without a decision row
```

**One sentence for leadership:** the new engine records everything point-in-time, measures itself with statistics that respect overlapping returns, and is not allowed to trust its own learned weights until fifteen months of clean data exist — so the chart it shows will be honest even when it is flat.

**Confidence.** That this design can be implemented from this document by a competent coding LLM in the stated phases: 80%. That the resulting engine will show a positive, stable out-of-sample 3M sector-neutral IC for its fundamental composite by month 36: 40% (unchanged from the red team's estimate; the design improves measurement, not the market).

---
