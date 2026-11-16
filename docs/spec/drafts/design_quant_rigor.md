# V2 Engine Design — "Quant Research Rigor" draft

Author lens: point-in-time discipline, statistical validity, leakage prevention, overlap-aware inference, honest benchmarks. Written 2026-09-05 against branch `red-team-review-sep-2026`. Everything below was checked against the actual `quant_engine.db`, the current code, a live pull of the NSE constituent file, and live yfinance 1.4.1 calls; where a claim rests on something not verified, it says so.

Audience: an implementer LLM with no access to this conversation. Paths are repository-relative unless absolute. "Owner" means the person who runs the loop monthly.

Reading order for the implementer: `docs/spec/00_context_brief.md`, `docs/analysis/red_team_review.md`, `AGENTS.md`, this document.

---

## 1. Objective & success metrics

> **Answer up front.** V2 is a monthly, point-in-time, sector-neutral factor engine whose permanent baseline is an equal-weight composite of six pre-registered factors, evaluated at a 3-month horizon for learning and a 12-month horizon for the multi-bagger thesis. Its first job is not to produce alpha; it is to produce a **clean, growing evidence record** whose statistics are correct, and to make every change to the model traceable to that record. Alpha, if it exists, becomes visible on a chart the owner can read; if it does not exist, the same chart says so.

### 1.1 What "success" means, in order of priority

1. **Correctness of the record.** Every score is computed only from data that was public on its `as_of` date; every realised return is a total return adjusted for splits and dividends; every statistic carries an overlap-aware standard error and the number of independent observations behind it. A hostile reviewer re-running `python -m quant evaluate` on a fresh clone gets the same numbers.
2. **A learning curve that exists and is honest.** Out-of-sample predictive power (and, just as important, the *precision* of its estimate) is plotted against months of accumulated clean data, per factor and for the composite, with the backfilled segment visibly separated from the live segment.
3. **Paper alpha, net of costs, against the right null.** The composite's top-decile paper portfolio versus the equal-weight universe (same data, same cost model), plus external benchmarks.
4. **Multi-bagger recall as a slow KPI**, first measurable in September 2029.

### 1.2 Numeric targets (targets, not claims)

All targets are for the **live** segment (data captured monthly from the first V2 run onward). Backfilled statistics are reported but never count toward these.

```
Metric                                                Target                       First date it can be judged
----------------------------------------------------- ---------------------------- ---------------------------
3-month sector-neutral Rank IC, EW composite          mean >= +0.03, 90% CI > 0    2029-09 (36 live months)
12-month sector-neutral Rank IC, EW composite         mean >= +0.05, HAC t >= 1.5  2030-09 (48 live months)
Net top-decile paper return minus EW-universe TR      >= +3.0 %/yr, rolling 36 m   2029-09
36-month multi-bagger lift (recall@decile / 0.10)     >= 1.5x                      2029-09 (cohort 2026-09)
Composite vs 1,000 random-weight composites           >= 75th percentile of IC     2028-09 (24 live months)
Data-quality gate pass rate                           >= 11 of 12 months           rolling
Share of factor inputs imputed or proxied             < 10 % of (stock x factor)   every month
Pre-registered hypotheses tested                      <= 6 per rolling 12 months   every month
```

Why the dates are so far out: the arithmetic in section 7.3. A 3-month horizon evaluated monthly gives roughly one *independent* observation per quarter. Twelve independent observations is the minimum for an information ratio to mean anything (context brief, section 7), and that takes 36 live months.

### 1.3 What would falsify the approach

State these now so nobody moves the goalposts later.

```
F1  After 36 live months the EW composite's 3-month IC has 90% CI containing 0 and point estimate < +0.02
    -> the six-factor set has no demonstrated predictive value in Nifty 500 at this horizon.
F2  The EW composite does not beat the median of 1,000 random-weight composites of the same factors
    -> the "composite" adds nothing over "any mix of these factors"; factor selection, not weighting, matters.
F3  Net-of-cost top-decile paper portfolio has IR < 0 vs EW universe over 36 live months
    -> whatever IC exists is not harvestable at the assumed cost and turnover.
F4  36-month multi-bagger lift <= 1.0 on the first two annual cohorts (2026-09, 2027-09)
    -> the 3-/12-month IC does not translate into the multi-year outcome the owner cares about.
F5  Any leakage test in section 7.6 fails on real data in any month
    -> the record is contaminated; stop, fix, re-run, and log a data_quality_event with severity 'block'.
```

F1–F4 are *inconclusive* until their dates; the dashboard must display "insufficient evidence" rather than a number when `n_ind < 12`.

### 1.4 The one-line contract with the owner

The engine may only ever claim what section 7 has measured out of sample, with its standard error. Any document, UI label or commit message that quotes an IC, spread or alpha must also quote `n_ind` (independent periods) and the data segment (`live`, `backfill`, `legacy`).

---

## 2. Prediction target & horizons

### 2.1 Challenge to the seed: 12 months cannot be the *learning* horizon

The brief seeds "forward 12-month sector-neutral relative return" as primary. From a statistical-validity lens that is the wrong choice for the horizon that *drives decisions*, for one reason: overlap.

```
Monthly cohorts, horizon h months, M live months:
  overlapping IC observations  = M - h
  independent observations     ~ (M - h) / h

h = 12:  36 months -> 24 overlapping obs -> ~2 independent   (useless)
         156 months -> 12 independent                         (13 years)
h = 3:   39 months -> 36 overlapping obs -> 12 independent    (3.25 years)
h = 1:   13 months -> 12 independent                          (fast, but weakly related to compounding)
```

A learning loop whose objective yields two independent observations after three years cannot learn; it can only overfit. Conversely, the 1-month horizon is the one the red team showed to be dominated by a moving-average filter and weakly related to the owner's goal.

### 2.2 Decision: two horizons with distinct roles, plus diagnostics

```
Role                 Horizon        Used for
-------------------- -------------- ------------------------------------------------------------
LEARNING horizon     63 trading d   the only horizon whose statistics may change weights or
                     (~3 months)    factor status; the learning-curve chart's primary panel
THESIS horizon       252 trading d  the dashboard headline; the gate for ever calling anything
                     (~12 months)   "evidence of compounding alpha"; multi-testing family
DIAGNOSTIC horizons  21, 126, 504,  reported, never used for decisions; 756-day horizon feeds
                     756 trading d  the multi-bagger label
```

Why 3 months for learning: it is the shortest horizon at which quality/value/growth signals in published factor research show their effect rather than pure trend autocorrelation, and it delivers four independent observations per year. Why 12 months for the thesis: it is the shortest horizon that is defensibly "about compounding" and still reaches 24 monthly cohorts inside three years for a HAC-corrected mean.

### 2.3 The continuous target

For security *i*, cohort date *t*, horizon *h* trading days:

```
tr_log(i,t,h)            = ln( TRI(i, t+h) / TRI(i, t) )           TRI = total-return index, section 4.3
sector_excess(i,t,h)     = tr_log(i,t,h) - mean_{j in G(i,t)} tr_log(j,t,h)
                                                                    G = neutral group of i at t (section 3)
universe_excess(i,t,h)   = tr_log(i,t,h) - mean_{j in U(t)} tr_log(j,t,h)
```

The **primary target** is `sector_excess` at `h = 63`. Log returns because they add across horizons and are less skewed; the primary statistic is Spearman rank IC anyway, so scale is irrelevant for IC and matters only for spreads and portfolio arithmetic. For spreads, cross-sectionally winsorise `tr_log` at the 1st/99th percentile per cohort (the ZFCVINDIA −84% split artefact is prevented upstream by using TRI; winsorisation is defence in depth).

Delisted or suspended names: if TRI stops before `t+h`, `status = 'delisted_partial'`, and the return is carried to the last available price with a −100% floor not applied (India delistings mostly pay out; suspensions are the real risk). These rows are *included* in IC and spreads; excluding them is survivorship bias. Count them in the monthly report.

### 2.4 How "multi-bagger" becomes a measurable label

```
mb36(i,t)        = 1 if TRI(i, t+756) / TRI(i, t) >= 2.0 else 0        (end-point, unambiguous)
mb36_touch(i,t)  = 1 if max over month-ends m in (t, t+756] of TRI(i,m)/TRI(i,t) >= 2.0   (diagnostic)

base_rate(t)     = mean_i mb36(i,t)
recall@D(t)      = share of {i: mb36=1} that were in composite decile 10 at t
lift(t)          = recall@D(t) / 0.10
precision@D(t)   = share of decile-10 names with mb36=1
```

`lift` is the KPI because `base_rate` swings from single digits to 40%+ across Indian market cycles; recall alone would be regime, not skill. First cohort with a label: `t = 2026-09`, judged 2029-09. Backfilled cohorts (price-factor-only composites, section 4.5) provide an earlier, survivorship-biased read that is labelled as such.

### 2.5 Timing conventions (these prevent an entire class of leaks)

```
as_of        = the last completed NSE trading day at the time the monthly run starts (IST).
               The run is expected in the first five calendar days of each month; as_of is
               usually the last trading day of the previous month, but it is whatever the
               data says, never a calendar assumption.
Prices       usable at as_of: closes with date <= as_of.
Fundamentals usable at as_of: rows with available_from <= as_of  (section 4.4).
Holdings     usable at as_of: captures with captured_at <= as_of + 0 days, i.e. the capture
             made *during this run* is stamped with the run date, which is > as_of, so it is
             NOT usable for this month's score; last month's capture is. Strict, cheap, no leak.
Membership   usable at as_of: latest universe snapshot with as_of_snapshot <= as_of.
Forward ret  IC:            from close(as_of) to close(as_of + h).
             Paper trades:  executed at close(as_of + 1 trading day)   (one-day implementation lag).
```

The holdings rule costs one month of freshness on a signal that updates quarterly. Accept it.

---

## 3. Universe & sector taxonomy

### 3.1 Universe: Nifty 500, snapshotted monthly, keyed by ISIN

Verified 2026-09-05: `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv` returns 500 rows with columns `Company Name, Industry, Symbol, Series, ISIN Code`. The `Industry` column holds NSE's own 20-group sector classification (this is the "Sector" level of NSE's four-level scheme, despite the column header):

```
count  nse_sector                              count  nse_sector
101    Financial Services                      16     Consumer Durables
 63    Capital Goods                           14     Services
 48    Healthcare                              13     Construction
 38    Automobile and Auto Components          11     Construction Materials
 29    Consumer Services                       11     Realty
 28    Fast Moving Consumer Goods              10     Telecommunication
 27    Information Technology                   5     Textiles
 26    Chemicals                                5     Media Entertainment & Publication
 18    Metals & Mining                          3     Diversified
 17    Power
 17    Oil Gas & Consumable Fuels
```

Decisions:

- **Security key = ISIN** (`security_id`). NSE symbols change (renames, mergers); ISINs rarely do. `yahoo_ticker = Symbol + ".NS"` is an attribute with a validity range, not a key.
- **Snapshot the CSV on every monthly run** into `data/universe/nifty500_<as_of>.csv` (committed, ~25 KB) and `universe_snapshots`. NSE reconstitutes semi-annually (effective around end-March and end-September); monthly snapshots capture changes with at most one month of delay, which is acceptable and is recorded.
- **Membership at date d** = the latest snapshot with `as_of <= d`. Scoring never uses a snapshot from the future.
- **History before 2026-06:** no free point-in-time constituent history exists. The four legacy snapshots supply membership for 2026-06 to 2026-09 (section 10.5). Everything earlier uses the *current* list and is stamped `source = 'current_backfill'`. Every statistic computed on such cohorts inherits `data_segment = 'backfill'` and is drawn on a separate, shaded part of every chart. This is survivorship bias by construction; the honest mitigation is labelling, not pretending.
- Optional adapter (not required): parse NSE's semi-annual reconstitution press releases to rebuild membership back to ~2019. Register as a hypothesis-free data task if the owner wants the backfill segment to be less biased.

### 3.2 Canonical sector = NSE sector; Yahoo as fallback and as the sub-splitter for financials

Fallback chain for `nse_sector`:

```
1. NSE CSV `Industry` column for the snapshot in force at d           (source='nse_csv')
2. If the ISIN is missing from every snapshot <= d (should not happen for members):
   the most recent later snapshot's value, flagged 'sector_from_future_snapshot'   (backfill only)
3. If still missing: map Yahoo `sector` via the fixed table below, flagged 'sector_from_yahoo'
4. If still missing: 'UNCLASSIFIED', and the name is excluded from neutralisation groups
   (scored NaN, reported in the exclusions table). Gate: > 1 % UNCLASSIFIED blocks the run.
```

Yahoo → NSE fallback table (only used in step 3; verified Yahoo values from the September snapshot: Financial Services, Industrials, Consumer Cyclical, Basic Materials, Healthcare, Technology, Consumer Defensive, Utilities, Energy, Communication Services, Real Estate):

```
Yahoo sector            -> NSE sector (fallback)
Financial Services      -> Financial Services
Industrials             -> Capital Goods
Consumer Cyclical       -> Consumer Services            (coarse; flagged)
Basic Materials         -> Chemicals                    (coarse; flagged)
Healthcare              -> Healthcare
Technology              -> Information Technology
Consumer Defensive      -> Fast Moving Consumer Goods
Utilities               -> Power
Energy                  -> Oil Gas & Consumable Fuels
Communication Services  -> Telecommunication
Real Estate             -> Realty
```

### 3.3 Neutralisation groups: NSE sector, with financials split and tiny groups pooled

Sector-neutral ranking needs groups large enough to rank within (rule of thumb: ≥ 10 names) and homogeneous enough that "cheap within group" means something. Two adjustments to the raw 20 groups:

1. **Financial Services (101 names) is split three ways using Yahoo `industry`**, because banks, lenders, insurers, exchanges and asset managers have incomparable accounting:
   ```
   FIN_BANKS    Yahoo industry contains 'Bank'
   FIN_LENDERS  'Credit Services', 'Mortgage Finance', 'Financial Conglomerates'
   FIN_OTHER    'Insurance*', 'Capital Markets', 'Asset Management', 'Financial Data & Stock Exchanges', else
   ```
   The implementer must print the resulting counts on first run and record them in the ADR; expected roughly 30/40/30. If any sub-group has < 10 names it merges into FIN_OTHER.
2. **Groups with fewer than 10 members in a cohort are pooled** into `SMALL_POOL` for that cohort (today: Textiles 5, Media 5, Diversified 3 → 13 names). Rows carry `neutral_group_pooled = 1`. The pool is heterogeneous by construction; it is still better than ranking three companies against each other.

`neutral_group` is therefore one of ~22 values and is stored per (security, validity range) in `sector_map`, so reclassifications are point-in-time:

```
security_id  nse_sector          yahoo_industry     neutral_group  valid_from   valid_to     source
INE...       Financial Services  Banks - Regional   FIN_BANKS      2026-10-01   NULL         nse_csv+yahoo
INE...       Capital Goods       ...                Capital Goods  2026-10-01   2027-03-31   nse_csv
INE...       Power               ...                Power          2027-04-01   NULL         nse_csv   <- reclassified
```

On each monthly ingest: if a member's `nse_sector` or derived `neutral_group` differs from the open row, close the old row (`valid_to = as_of - 1 day`), open a new one, and write a `data_quality_events` row (`check_name='sector_reclassified'`, severity `info`). Factor computation for date d joins `valid_from <= d AND (valid_to IS NULL OR d <= valid_to)`.

Size inside sectors is a second, unhandled axis (Nifty 500 spans ₹4,000 Cr to ₹20 lakh Cr). Do **not** double-neutralise in v1 (it fragments groups); instead report every IC also within market-cap terciles as a diagnostic (section 7.2), and keep `size` as a candidate factor.

### 3.4 Sector-level features (a separate, zero-weight block until promoted)

Sector-neutral ranking removes sector bets from the stock composite by design. Sector bets, if wanted, are a *separate* decision with its own evidence. Three features are pre-registered as candidates in the `sector` family; all are computed per `neutral_group` per `as_of`, stored in `sector_features`, and enter no composite until promoted through section 9:

```
feature            formula (per group g at t)                                  backfillable  expected sign
sector_mom_6m      EW mean over members of ln TRI(t)/TRI(t-126) minus universe EW    yes          +
sector_breadth     share of members with close > SMA200(adjusted close)             yes          +
sector_flow_1q     EW mean over members of inst_flow_1q (section 5.3)               no (live)    +
```

Why they are candidates and not active: 22 groups per month is a tiny cross-section. Even with 120 backfilled months, `sector_mom_6m` yields ~120 × 22 observations with strong cross-sectional correlation; `sector_flow_1q` has zero history. Promotion thresholds (section 9.5) apply unchanged. If promoted, a sector feature is applied as a *tilt* on group weights in the paper portfolio (bounded ±25% relative), never as a term inside the stock-level z-score.

---

## 4. Data layer

### 4.1 Data flow

```
                    monthly run (first week of month, IST)
                    ======================================

 niftyindices.com CSV ──► universe_snapshots ──► sector_map (PIT ranges)
                                   │
 yfinance yf.download  ──► data/prices/daily_<year>.parquet (raw close, volume,
   (batches of 25,           dividends, splits; regenerable, NOT committed)
    0.5 s+ between)                │
                                   ├──► corporate_actions (committed CSV + table)
                                   └──► TRI (computed locally) ──► prices_monthly (committed)
                                                                        │
 yfinance Ticker.financials / quarterly_* / get_earnings_dates            │
                       ──► fundamentals (bitemporal: period_end, available_from, fetched_at)
 yfinance Ticker.info  ──► holdings (captured_at), security attributes (mcap, ADV)
 yfinance index/ETFs   ──► benchmark_prices
                                   │
                          data-quality gates ──► data_quality_events; BLOCK stops the run
                                   │
                          factor_values (raw, z per neutral group) ──► scores (per model)
                                   │
                          realised_returns for matured cohorts ──► evaluations ──► reports/UI
```

### 4.2 Storage decision: SQLite for the record, Parquet for regenerable bulk, git for what cannot be regenerated

Measured: one ticker's 10-year adjusted history from yfinance is 2,474 daily rows. For 500 names:

```
rows            ~1.24 M
parquet (zstd)  ~25–40 MB  (date, security_id, open, high, low, close_raw, volume, dividend, split)
git history     every monthly refresh rewrites the current-year file -> 12 x ~5 MB/year of blobs
```

Decision:

```
Committed to git                                         Regenerated on clone (gitignored)
------------------------------------------------------   ---------------------------------------
quant_engine.db (all V2 tables incl. factor_values,      data/prices/daily_<year>.parquet
  scores, realised_returns, evaluations, knowledge)
data/universe/nifty500_<as_of>.csv  (one per month)
data/corporate_actions.csv          (append-only)
data/panel/prices_monthly.csv.gz    (month-end close_raw, TRI, ADV, mcap; ~0.5 MB)
data/prices/MANIFEST.json           (per security: first/last date, row count,
                                     sha256 of the month-end close series)
knowledge/**/*.md
```

Rationale, from the reproducibility angle: the **historical record** (factor values, scores, realised returns, decisions) is what a reviewer needs to audit, and it is small. The **bulk inputs** are needed to recompute daily-frequency factors (volatility) and can be re-fetched; Yahoo occasionally restates raw closes, so `MANIFEST.json` stores a checksum of each security's month-end raw closes and `python -m quant data verify` compares a fresh download to it, logging `source_restated` events with the affected dates. The committed factor values are never recomputed from restated inputs without a decision record.

Git LFS was considered and rejected: it adds a tool dependency for the implementer LLM and the owner's other machines, GitHub's free LFS quota is 1 GB and each monthly refresh would consume ~40 MB of it. If the owner later wants bit-exact daily history in the repository, LFS can be added without changing any code path (the Parquet files simply stop being ignored).

SQLite growth estimate: `factor_values` adds 500 × ~10 factors, `scores` 500 × 3 models, `realised_returns` 500 × 5 horizons per month → ~10 k rows, ~0.6 MB/month uncompressed. Run `VACUUM` before the monthly commit. Commit the DB **once per month**, never per script run (the legacy repo has 12 weight rows because a daily cron committed daily).

Also export the small tables as CSV each month (`data/exports/<table>_<as_of>.csv`) so git diffs are readable text; the DB remains the operational store.

### 4.3 Prices, corporate actions and total returns

Verified live: `yf.download([...], period="10y", auto_adjust=False, actions=True, group_by="ticker", threads=False)` returns `Open, High, Low, Close, Volume, Dividends, Stock Splits` per ticker; with `auto_adjust=True` the ZFCVINDIA 6:1 split of 2026-06-24 disappears from the close series (2,627.9 on 06-19, 2,655.4 on 06-24). We store **unadjusted** facts and adjust locally, because unadjusted closes and explicit action rows do not silently change when a later action occurs, whereas Yahoo's adjusted series is rewritten backward every time.

Total-return index, per security, computed in `quant/data/prices.py::build_tri`:

```
r_t   = ( close_raw_t * split_t + div_t ) / close_raw_{t-1} - 1
split_t = new shares per old share on ex-date t (1.0 otherwise); Yahoo's 'Stock Splits' column
div_t   = cash dividend per (pre-split) share on ex-date t; Yahoo's 'Dividends' column
TRI_0 = 100 ; TRI_t = TRI_{t-1} * (1 + r_t)
```

Bonus issues appear in Yahoo as splits (a 1:1 bonus is `2.0`); rights issues and demergers do **not** appear and produce a price gap. Defence: any `|r_t| > 0.40` on a day with no action row raises a `data_quality_events` row (`suspected_unrecorded_action`, severity `warn`) and the security is excluded from realised returns for cohorts spanning that day until the owner resolves it (`python -m quant data ca add --security INE... --ex-date ... --kind demerger --value <price factor>`). The legacy ±60% monthly filter is retired; it hid the problem instead of recording it.

Batching and throttle: 20 batches of 25 tickers, `time.sleep(2.0)` between batches for backfill; monthly update fetches `period="3mo"` and overwrites the overlap (idempotent upsert keyed by date). Backfill wall-clock: a few minutes.

Benchmarks (all verified to return data on 2026-09-04):

```
benchmark_id     source_ticker    what it is                                       caveat
EW_UNIVERSE      (computed)       EW of universe members' TRI, rebalanced monthly  the correct null for a rank composite
CW_UNIVERSE      (computed)       cap-weighted, from stored mcap                   mcap only live; backfill uses current shares x price
NIFTY500_PR      ^CRSLDX          Nifty 500 price index                            no dividends (~1.2 %/yr understatement)
NIFTY50_ETF      NIFTYBEES.NS     Nifty 50 ETF NAV proxy                           TRI minus expense ratio
MOM30_ETF        MOM30IETF.NS     believed Nifty 200 Momentum 30 ETF               VERIFY underlying on first run
QUAL30_ETF       QUAL30IETF.NS    believed Nifty 200 Quality 30 ETF                VERIFY; closest tradeable proxy to the
                                                                                   brief's "Nifty 500 Quality 50"
LOWVOL_ETF       LOWVOLIETF.NS    believed Nifty 100 Low Volatility 30 ETF         VERIFY
VALUE_ETF        MOVALUE.NS       believed Nifty 500 Value 50 ETF                  VERIFY
MID150_ETF       MID150BEES.NS    Nifty Midcap 150 ETF                             sanity check for size exposure
```

"VERIFY" means: on first ingest the implementer records the AMC's stated underlying index and inception date in `benchmarks.underlying_index` / `benchmarks.verified_on`. ETFs are proxies with tracking error and short histories (several launched 2021–2024); the internal `EW_UNIVERSE` is the benchmark every decision uses.

### 4.4 Fundamentals: bitemporal, with a regulatory-lag availability rule

Verified live for HEROMOTOCO.NS: annual statements for FY2022–FY2026 (5 columns), `quarterly_financials` 6 quarters (41 line items incl. `EBIT`, `Net Income`, `Total Revenue`, `Diluted EPS`), `quarterly_balance_sheet` 3 quarters, `quarterly_cashflow` **empty**, `get_earnings_dates(limit=8)` returns dated announcements back to 2025-02. Coverage varies by name; the harness must tolerate empty frames.

Yahoo keys statements by *period end*, not by *publication date*. Using period end as the availability date is a look-ahead of 1–3 months. Rule, in `quant/data/fundamentals.py::available_from`:

```
if an earnings announcement date for that period end exists in get_earnings_dates:
      available_from = announcement_date + 1 trading day          (basis = 'earnings_date')
else: quarterly:  available_from = period_end + 45 calendar days + 1 trading day   (SEBI LODR Reg. 33)
      annual/Q4:  available_from = period_end + 60 calendar days + 1 trading day
```

This is conservative (a value is used no earlier than it could have been public). Every row also stores `fetched_at` (when *we* captured it). If a later fetch returns a different value for the same `(security, statement, period_end, field)`, a new row is inserted with the new `fetched_at` and a `restatement` event is logged; the old row is never updated. Point-in-time query for date d:

```
rows with available_from <= d
  prefer the version with the largest fetched_at <= d          (what we knew then; live segment)
  else the smallest fetched_at (the earliest version we ever captured), flagged
       pit_basis = 'backfilled_current_version'                 (backfill segment)
```

Fields to store (one row per field; no JSON blobs for anything a factor reads):

```
income (annual + quarterly): Total Revenue, EBIT, EBITDA, Net Income, Diluted EPS, Basic EPS, Interest Expense
balance (annual + quarterly): Total Assets, Current Liabilities, Total Debt, Cash And Cash Equivalents,
                              Stockholders Equity, Ordinary Shares Number
cashflow (annual): Operating Cash Flow, Capital Expenditure, Free Cash Flow (if present)
info snapshot (per run, table `security_attributes`): marketCap, sharesOutstanding, floatShares,
                              averageDailyVolume3Month, enterpriseValue, totalDebt, totalCash, beta
```

Unit rules (carry over the red team's findings): `debtToEquity` from `info` is a percent (divide by 100) — but v1 factors compute leverage from statement rows, not from `info`, to avoid the trap; `dividendYield` from `info` is a percent in yfinance ≥ 1.x — v1 does not use it at all (dividends come from the price actions). Statement values are in rupees; store rupees, never crores.

Trailing-twelve-month EPS: sum of the last four available quarterly `Diluted EPS` where all four have `available_from <= d`; else the latest annual EPS with `available_from <= d`, flagged `ttm_from_annual`.

HTTP budget per monthly run: `info` (1) + `financials` (1) + `balance_sheet` (1) + `cashflow` (1) + `quarterly_financials` (1) + `quarterly_balance_sheet` (1) + `get_earnings_dates` (1) = 7 calls × 500 × 0.5 s ≈ 30 minutes, plus prices (~3 minutes batched). Within the one-hour budget. Annual statements only change once a year; the implementer may fetch `financials/balance_sheet/cashflow` quarterly (months 1, 4, 7, 10) to halve the budget once the first live year is in.

### 4.5 Backfill plan and what can honestly be backfilled

```
Data                          Backfill depth   PIT quality                           Segment label
prices / TRI / ADV            10 years         true PIT (facts do not depend on time)  backfill
universe membership           none             current list -> survivorship bias       backfill (flagged)
sector map                    none             current map applied backward            backfill (flagged)
annual fundamentals           5 fiscal years   current version, lag rule applied       backfill (flagged)
quarterly fundamentals        6 quarters       current version, lag rule applied       backfill (flagged)
institutional holdings        none             not available historically              live only
earnings dates                ~2 years         actual dates                            n/a
```

Consequence for the learning curve: **price-based factors** (`mom_12_1`, `low_vol_12m`, `trend_200`, `sector_mom_6m`, `sector_breadth`) get ~120 monthly backfilled cohorts on day one — survivorship-biased (winners that stayed in Nifty 500) but with correct point-in-time values. **Fundamental factors** get ~4 years of annual-based values with restatement risk. **Holdings** get nothing. The chart draws three lines per factor: backfill (dashed), legacy 2026 (dotted, 3 cohorts, known defects), live (solid). Only the solid line counts.

### 4.6 Data-quality flags and gates

Per-value flags are stored where the value is stored (`coverage_flag` in `factor_values`; `available_from_basis` and `pit_basis` in `fundamentals`; `status` in `realised_returns`). Gates run in `python -m quant data gate --as-of <d>` before scoring; each writes a `data_quality_events` row; any `block` aborts the run with exit code 2.

```
check_name                          rule                                                       severity
universe_size                       members in snapshot >= 450 and <= 520                      block
universe_not_duplicate              snapshot differs from previous OR previous is > 20 days old block
price_coverage                      >= 98 % of members have close_raw on as_of                  block
price_identical_to_previous_cohort  share of members with close_raw == previous cohort < 5 %   block  (the 06-12/06-14 bug)
tri_sanity                          no |daily r| > 0.40 without an action row, else warn+excl.  warn
adv_present                         >= 95 % of members have 3-month ADV                          warn
sector_coverage                     UNCLASSIFIED <= 1 % of members                               block
fundamentals_freshness              >= 90 % of members have an income row available within      warn
                                    200 days before as_of
fundamentals_units                  no |EPS| > 1e5, no negative Total Assets, no Revenue > 1e14  block
holdings_range                      all inst_pct in [0, 1]                                      block
factor_coverage                     each active factor non-NaN for >= 70 % of members            warn (3 consecutive -> block)
imputation_share                    imputed/proxied inputs <= 10 % of (member x active factor)   warn
benchmark_freshness                 EW_UNIVERSE computable; each external benchmark has a close  warn
                                    within 5 trading days of as_of
```

The monthly report prints the gate table verbatim. A blocked month is still a month: the `cohorts` row is written with `gate_status='blocked'` and no scores, so the learning curve shows the gap rather than silently skipping.

---

## 5. Factor library

### 5.1 Plugin contract

File: `quant/factors/base.py`.

```python
from dataclasses import dataclass
import datetime as dt
import pandas as pd

@dataclass(frozen=True)
class FactorSpec:
    factor_id: str            # 'mom_12_1_v1'  == f"{name}_v{version}"; immutable once registered
    name: str                 # 'mom_12_1'
    version: int              # bump => new factor_id; old id keeps its history
    family: str               # 'momentum' | 'risk' | 'quality' | 'value' | 'growth' | 'flow' | 'sector' | 'size'
    hypothesis: str           # one sentence, falsifiable
    expected_sign: int        # +1 or -1 : sign of the expected IC of `raw` vs sector_excess return
    horizon_months: int       # horizon at which the hypothesis is stated (3 or 12)
    neutralise: str           # 'sector' (default) | 'none' (sector-family features only)
    inputs: tuple[str, ...]   # data dependencies, e.g. ('prices.tri', 'fundamentals.income.Net Income')
    min_history_days: int     # e.g. 252 for mom_12_1 ; used for coverage flags
    min_coverage: float       # share of members that must be non-NaN or the factor is not stored this month
    backfillable: bool        # whether values before the first live run are meaningful
    status: str               # 'candidate' | 'shadow' | 'active' | 'retired'
    registered_on: dt.date
    hypothesis_id: str        # 'H-2026-001' ; FK to knowledge.hypotheses
    evidence: str             # prior evidence, with the honest caveat

class FactorContext:
    """Everything a factor may read. Constructed by the engine for one as_of; it physically cannot
    return data with date/available_from > as_of, which is what the PIT truncation test checks."""
    as_of: dt.date
    members: pd.Index                                   # security_ids in the universe at as_of
    def tri(self, lookback_days: int) -> pd.DataFrame:  # dates x security_id, dates <= as_of
    def close_raw(self, lookback_days: int) -> pd.DataFrame
    def adv_inr(self) -> pd.Series
    def mcap_inr(self) -> pd.Series
    def fundamental(self, statement: str, field: str, n_periods: int, freq: str) -> pd.DataFrame
        # security_id x period rank (0 = latest available at as_of), PIT-filtered
    def ttm(self, field: str) -> pd.Series
    def holdings(self, lag_days: int = 0) -> pd.Series   # latest capture with captured_at <= as_of - lag_days
    def neutral_group(self) -> pd.Series                 # security_id -> group at as_of

class Factor:
    spec: FactorSpec
    def compute(self, ctx: FactorContext) -> pd.Series:
        """Raw values indexed by security_id (NaN allowed). No neutralisation, no clipping here."""
```

Registration is by module import in `quant/factors/__init__.py`; `python -m quant factors list` prints the registry and diffs it against the `factor_registry` table (mismatch = error: the code and the record must agree).

### 5.2 Common post-processing (identical for every factor; not overridable)

`quant/factors/postprocess.py::standardise(raw, groups, spec)`:

```
1. coverage: if non-NaN share < spec.min_coverage -> store nothing, log factor_coverage warn
2. winsorise raw at the 1st / 99th percentile *within the whole cross-section*
3. within each neutral_group: rank (average ties) -> uniform (r - 0.5)/n -> z = Phi^-1(u)
   (rank-based normal scores; robust, bounded ~[-2.6, 2.6] for n=25)
4. multiply by spec.expected_sign so that "higher z = expected better"
5. groups with n < 5 non-NaN at this step -> z = NaN, flag 'group_too_small'
6. store (raw, z, coverage_flag) in factor_values
```

Because z is rank-based within group, the old 0–100 five-level buckets, the hand-typed ticker lists, and the "50 for 96% of the universe" degeneracy cannot recur: a factor that is constant within a group produces NaN z and fails coverage visibly.

### 5.3 Initial factor list (v1)

Fewer, cleaner. Six active, one shadow, three candidate stock-level, three candidate sector-level. Each is pre-registered in `knowledge/hypotheses/H-2026-00N.md` before the first live run.

```
factor_id           status     sign  h   family    formula (per security at as_of)                                  evidence / caveat
------------------- ---------- ----- --- --------- ------------------------------------------------------------------ --------------------------------------------
mom_12_1_v1         active     +     3   momentum  TRI(t-21)/TRI(t-252) - 1 ; min 230 obs                            broad international literature; NSE's own
                                                                                                                      Nifty 200 Momentum 30 index; strongest prior
low_vol_12m_v1      active     +     3   risk      -std(daily ln TRI returns, 252 d) ; min 200 obs                    low-volatility anomaly; Nifty 100 Low Vol 30
quality_v1          active     +     12  quality   mean of group-z of: ROE_3y = mean over last 3 FY of NI/avg equity;   quality-minus-junk literature; Nifty 200
                                                   -accruals = -(NI - OCF)/Total Assets (latest FY);                  Quality 30 uses ROE, D/E, EPS variability.
                                                   -leverage = -Total Debt / Stockholders Equity (latest)             For FIN_* groups: ROE_3y and ROA_3y only.
value_v1            active     +     12  value     non-fin: mean of group-z of E/P (TTM EPS / price) and EBITDA/EV    long-run evidence positive; India 2015-2025
                                                   fin: mean of group-z of B/P and E/P                                 weak/negative; the repo's DCF version measured
                                                   (yield forms, so losses rank low without special-casing)          -0.024 over 3 periods. Included as a prior,
                                                                                                                      not as a proven edge.
growth_v1           active     +     12  growth    mean of group-z of: Revenue CAGR 3 FY (positive revenues only);    weakest academic prior; central to the owner's
                                                   (EPS_ttm - EPS_ttm 3 FY ago) / price   (defined through losses)   thesis. Pre-registered with expected sign +.
inst_flow_1q_v1     active     +     3   flow      inst_pct(latest capture <= as_of) - inst_pct(capture 60-120 d      the repo's most consistent factor over 3 periods
                                                   earlier) ; NaN if either missing (never 0)                        (with a definition change in period 1). Live only.
trend_200_v1        shadow     +     1   momentum  close_adj / SMA200(close_adj) - 1                                  direct replacement for the death-cross kill.
                                                                                                                      Promotion test: partial IC controlling mom_12_1.
size_v1             candidate  -     12  size      -ln(mcap)                                                          diagnostic; may become a control
liq_turnover_v1     candidate  -     3   liquidity -ln(ADV_3m / mcap)                                                 illiquidity premium; also the screen input
accruals_v1         candidate  -     12  quality   standalone accruals (to test if quality_v1 should split)           Sloan-style accrual anomaly
sector_mom_6m_v1    candidate  +     3   sector    section 3.4                                                        
sector_breadth_v1   candidate  +     3   sector    section 3.4
sector_flow_1q_v1   candidate  +     3   sector    section 3.4                                                        live only
```

Retired on day one (values migrated for the record, never computed again): `moat` (hand-picked list with hindsight), `risk` (hand-typed), `valuation` via DCF/margin-of-safety (63% zeros, many free parameters), `cap_alloc` (dividend buckets; unit bug), `trap_score` multiplier (its meaningful components — negative cash flow, leverage — live inside `quality_v1` as continuous inputs), `concall_sentiment` (headline keyword count), `momentum_multiplier` hard kill (becomes `trend_200_v1` shadow).

NaN policy: a security's composite is the mean of the z-scores of active factors that are non-NaN, provided at least 4 of 6 are present; otherwise the composite is NaN, `scores.eligible = 0`, `exclusion_reason = 'insufficient_factors'`. Excluded names' forward returns are still realised and reported in a separate "excluded bucket" table every month, so an exclusion rule can never quietly hide or create performance.

### 5.4 Pre-registration

Nothing enters `factor_values` on live data without a row in `hypotheses` and a markdown file. Template `knowledge/hypotheses/H-YYYY-NNN.md`:

```
# H-2026-001  mom_12_1: 12-1 month total-return momentum predicts 3-month sector-relative return
registered_on: 2026-10-01      registered_by: <owner>      family_year: 2026
factor_id: mom_12_1_v1         expected_sign: +1           horizon_months: 3
formula: TRI(t-21)/TRI(t-252) - 1, sector-neutral rank-z
evaluation_start: 2026-10-01   (first live cohort)        min_months: 36 live (or 60 backfill for dev/holdout split)
success_criterion: live 3-month IC HAC t >= threshold(family_year) AND net Q10-Q1 spread > 0
failure_criterion: live 3-month IC HAC t <= -2 over >= 24 cohorts, or coverage < 70 % for 6 months
prior_evidence: <two lines, honest>
holdout_plan: backfill 2016-01..2023-12 = development; 2024-01..registration = holdout; live = confirmation
```

The `hypotheses` row freezes `formula`, `expected_sign`, `horizon_months`, `evaluation_start` and the git SHA of the factor module. Changing any of them means a new version and a new hypothesis.

### 5.5 Status lifecycle

```
candidate ──(registered + code merged)──► shadow ──(promotion criteria, decision approved)──► active
    │                                        │                                                  │
    └──(withdrawn)──► retired ◄──(failure criteria or 6 months no coverage)─────────────────────┘
                                                                                                │
active ──(new version registered)──► active (new id) + old id becomes shadow for 6 months, then retired
```

- `candidate`: registered, may be computed on backfill only, never on live cohorts.
- `shadow`: computed and stored every live month, evaluated like an active factor, weight 0 in every model. Shadow is where evidence accumulates without touching the portfolio.
- `active`: in the composite.
- `retired`: frozen; values retained; `python -m quant factors replay --factor <id>` can recompute for audit only.

The transition rule that matters most: **shadow → active requires a decision record and cannot be made by the code**; **active → retired on failure criteria is proposed automatically and applied only after approval**.

---

## 6. Scoring model & weight learning

### 6.1 Baseline that never goes away: `ew_v1`

```
composite_z(i,t) = mean over active factors f of z_f(i,t)        (NaN policy as in 5.3)
pct_rank(i,t)    = rank of composite_z within the eligible universe / n_eligible
sector_pct_rank  = rank within neutral_group / n_group
decile           = ceil(10 * pct_rank)
```

Equal weight is the champion for as long as it takes a challenger to beat it by the rules below — plausibly years. This is the direct answer to the red team's finding that learned weights lost to equal weights out of sample; a baseline that cannot be beaten is a baseline you keep.

### 6.2 Challenger: `icir_shrunk_v1`

At each `as_of`, using **only cohorts whose 63-day horizon has matured** (end date ≤ as_of; this is the embargo), per active factor f:

```
ic_{f,c}   : Spearman(z_f(.,c), sector_excess(.,c,63)) for realised live cohorts c
mean_f     : mean of ic_{f,c}
sd_f       : Newey-West standard deviation of the monthly ic series, Bartlett lag 2 (h/21 - 1)
icir_f     : mean_f / sd_f
score_f    : clip(icir_f, 0, 1)                       negative evidence -> 0, never negative weight
w_hat_f    : score_f / sum_g score_g   (if sum == 0 -> w_hat = w_eq)

n_ind      : floor(live months with matured 63-day cohorts / 3)      independent periods
lambda     : 0                       if n_ind < 12
             n_ind / (n_ind + 24)    otherwise   (12 -> 0.33, 24 -> 0.50, 48 -> 0.67)

w_f        : (1 - lambda) * w_eq + lambda * w_hat_f
             then clip to [0, 2/K] (K = number of active factors), renormalise to sum 1,
             round to 3 dp with the rounding residue added to the largest weight (keeps the legacy convention)
```

Properties a reviewer will look for:

- **Deterministic and idempotent.** Weights are a pure function of `(model_id, as_of, realised cohorts)`; stored in `model_weights` with `n_ind` and `lambda`; re-running reproduces them. No incremental gradient state, so the legacy "run twice, weights move" bug cannot exist.
- **No decay.** With a dozen independent observations, discarding old ones is throwing away most of the evidence to chase regimes we cannot identify in advance. Regime awareness is a hypothesis for later, not a default.
- **Bounded.** `[0, 2/K]` lets a factor be switched off by evidence but never lets one factor exceed twice its equal share. The legacy `[0.05, 0.30]` bounds are retired; they were set for eight factors and pinned `growth` at the ceiling.
- **Minimum evidence before deviating:** `n_ind >= 12`, i.e. no weight moves before roughly September 2029. Until then the challenger equals the champion, and the dashboard says so.

### 6.3 Third model: `ew_plus_trend_v1` (shadow)

Same as `ew_v1` with `trend_200_v1` included as a seventh equal-weight factor. Exists only to answer the owner's question "did removing the death cross cost us anything?" with a paired test rather than an opinion. Not eligible for champion status; it is a diagnostic model.

Three models total. The count is fixed in v1 to keep the multiple-testing family small.

### 6.4 Champion/challenger protocol

```
every month: score all three models; paper-trade ew_v1 and icir_shrunk_v1 (section 8)
promotion of icir_shrunk_v1 to "headline" requires ALL of:
  n_ind >= 12 (live)
  paired difference of 3-month IC (challenger - champion) has HAC t >= 2.0
  paired difference of net paper return over the same months > 0
  a decision record approved by a human
after promotion: ew_v1 keeps being scored and paper-traded forever; a later reversal uses the same test
```

### 6.5 What happens to the death-cross hard kill and the other filters

```
Legacy rule                          V2 treatment
------------------------------------ ---------------------------------------------------------------
momentum_multiplier 0.0x (kill)      removed as a filter; trend_200_v1 as a shadow factor; monthly
                                     table "would-have-been-killed bucket vs rest" for 24 months
momentum_multiplier 0.8x             removed
trap_score multiplier                removed; components are continuous inputs to quality_v1
Growth>=80 & FCF<0 "turnaround" tab  removed from scoring; the UI may keep a *view* filtered on
                                     growth_v1 decile 10 and negative FCF, labelled as a view
market cap < 100 Cr exclusion        replaced by universe membership (Nifty 500 implies ~4,000 Cr+)
```

Hard filters that remain, none of them signal-based:

```
liquidity screen   3-month ADV >= INR 2 Cr/day  -> portfolio-eligible; below -> scored, evaluated,
                   not held (eligible=0, exclusion_reason='illiquid')
data-quality       fewer than 4 of 6 active factors -> not scored (5.3)
suspected action   unresolved suspected_unrecorded_action inside the horizon -> excluded from
                   realised_returns for that cohort only, status='excluded_ca'
```

Every exclusion bucket's forward return is reported monthly next to the eligible universe. A filter that "helps" will show it there, as evidence, and can then be registered as a hypothesis.

### 6.6 Invariants carried over, replaced, retired

```
Carried over   weights sum to exactly 1.000 (3 dp, residue to the largest); optimizer idempotent;
               pre-screen composite stored alongside portfolio eligibility; 0.5 s Yahoo throttle
Replaced       [0.05, 0.30] bounds -> [0, 2/K]; exponentiated gradient -> shrunk ICIR with lambda
Retired        margin-of-safety clip; death-cross 0.0x; base x trap x momentum reconciliation
```

---

## 7. Evaluation protocol

### 7.1 Cohorts, maturity and the embargo

A **cohort** is one `as_of` with its scored cross-section. A cohort's horizon-h return **matures** when `as_of + h trading days <= latest available close`. `python -m quant evaluate --as-of <d>` realises every (cohort, horizon) that matured since the last run and writes `realised_returns`, then recomputes every statistic from scratch (statistics are cheap; incremental state is a bug factory).

Walk-forward with embargo for anything that learns (the challenger's weights, any future model): at decision date T, only cohorts with `as_of + h <= T` may be used; the next cohort scored with those weights is T itself, whose return window starts at T. The gap between the last training return window's end (≤ T) and the first test window's start (T) is ≥ 0 and the windows never overlap. For the 12-month thesis horizon, weights learned at 3 months are applied to a 12-month evaluation; that is fine because the 12-month statistic is reported, never learned from.

### 7.2 Statistics reported, per subject (factor, model, benchmark), per horizon

```
rank_ic              Spearman(z or composite, sector_excess) per cohort; also vs universe_excess and raw tr_log
mean_ic, hac_se      mean over cohorts; Newey-West (Bartlett) with lag L = round(h/21) - 1   (3 m -> 2, 12 m -> 11)
hac_t                mean_ic / hac_se
n_cohorts, n_ind     count of cohorts; n_ind = floor(n_cohorts / (h/21))
ci90_block           90 % block-bootstrap CI (block length h/21 months, 1,000 resamples)
nonoverlap_mean_min  mean IC on each of the h/21 interleaved non-overlapping sub-series; report mean and min
icir                 mean_ic / sd(ic)          only displayed when n_ind >= 12; else "n/a (n_ind = k)"
hit_rate             share of cohorts with ic > 0
decile_returns       EW mean sector_excess by decile; Q10-Q1 spread (gross and net, section 8)
monotonicity         Spearman(decile number, decile mean return) across 10 deciles
ic_by_cap_tercile    IC within small / mid / large terciles of mcap (size diagnostic)
ic_by_group          IC within each neutral group (small n; shown as a heat-map, not tested)
partial_ic           for shadow factors: IC of residual of z after regressing on active z's
```

Cross-sectional dependence: 500 stocks are not 500 independent draws. Sector demeaning of the return removes the largest common component; the per-cohort t-statistic is still reported only as an upper bound and never used for decisions. Decisions use the **time-series** of cohort ICs (HAC), which is the standard remedy.

### 7.3 Why the numbers will be small for years (put this in the report footer)

```
typical monthly IC std for a real factor          0.08 – 0.12
mean IC worth having                              0.03
ICIR                                              0.25 – 0.40
t = ICIR * sqrt(n_ind);  t = 2 needs n_ind ~ 25 – 64 independent periods

live, h = 3:   n_ind = 12 after 36 months  -> t ~ 0.9 – 1.4   (inconclusive by design)
backfill, h=3: n_ind ~ 38 over 10 years    -> t ~ 1.5 – 2.5   (price factors only; biased sample)
```

The honest framing for the owner: for live-only factors, the first statistically clean verdict is a decade away; the learning curve will mostly show **confidence intervals narrowing**, not point estimates rising. That is still learning. Price-based factors reach usable evidence within the backfill, with the survivorship caveat printed on the chart.

### 7.4 Benchmarks and the definition of the null

```
Null 1  EW_UNIVERSE           the composite is a ranking; a ranking with no information is the EW universe
Null 2  random composites     1,000 draws of Dirichlet(1) weights over the active factors; the EW composite's
                               IC percentile within the draw distribution is reported (F2 in section 1.3)
Null 3  best single factor    the composite should not be worse than its best member out of sample
                               (if it is, the others are dilutive)
External                      section 4.3 ETF/index proxies, for the paper portfolio only
```

### 7.5 Cost-adjusted spreads

Decile spreads are reported gross and net. Net = gross minus `turnover_decile × cost_per_side × 2`, where turnover for decile 10 is the share of names entering/exiting it between consecutive cohorts and cost comes from the ADV bucket table in section 8.3. This is a lower bound on realism (no market impact beyond the bucket assumption).

### 7.6 Leakage tests (pytest on synthetic data in CI; the same functions on real data every month)

```
test_shuffle_ic_is_zero            permute sector_excess across securities within each cohort ->
                                   |mean IC| < 2 * se and |hac_t| < 2 for every factor and model
test_planted_signal_recovered      inject synthetic factor = 0.10*z(sector_excess) + N(0,1) ->
                                   recovered IC in [0.06, 0.14] with hac_t > 4 over 36 synthetic cohorts
test_pit_truncation_invariance     compute factor_values at as_of with the DB physically truncated to rows
                                   with date/available_from/captured_at <= as_of; must equal the untruncated
                                   computation bit-for-bit (live segment)
test_no_future_membership          every scored security at as_of is in a universe snapshot with as_of <= d
test_available_from_lag            every fundamentals row: available_from >= period_end + 45 d (quarterly),
                                   + 60 d (annual) unless basis = 'earnings_date' with a recorded date
test_tri_handles_split             ZFCVINDIA 2026-06-24 6:1: monthly TRI return in [-0.20, +0.20], not -0.84
test_holdings_lag                  scores at as_of never read a holdings capture with captured_at > as_of
test_weights_pure_function         recomputing model_weights for any past as_of reproduces the stored row
test_gate_blocks_duplicate_prices  a cohort whose prices equal the previous cohort's is refused
```

A failure on real data writes a `block` event and stops the run.

### 7.7 Learning-curve measurement

For each live month m (m = 1 is the first V2 cohort), for each subject and horizon, compute the statistics of 7.2 using only cohorts matured by m, and store them in `evaluations` with `window_end = m`. The chart (`ui/` "Learning" page, and `knowledge/reports/learning_curve_<as_of>.json` for the record):

```
x-axis    months of live data (secondary axis: n_ind)
panel A   cumulative mean 3-month IC with 90 % CI band: composite ew_v1, each active factor, shadow factors
panel B   the same for 12 months (starts at month 13)
panel C   CI half-width vs months ("precision curve")
panel D   random-composite percentile of ew_v1 (F2)
overlay   backfill segment (x < 0, shaded) for backfillable factors; legacy 2026 cohorts as three dots
```

The owner's "predictability increases over time" is therefore measured as: (i) the composite's cumulative IC point estimate, (ii) its CI narrowing, and (iii) the *set of active factors* improving through promotion/retirement decisions, each of which is a labelled vertical line on the chart.

---

## 8. Portfolio & cost model

### 8.1 Paper portfolios

```
portfolio_id       model            construction                                        rebalance
EW_TOP_DECILE      ew_v1            eligible names in decile 10 (~45-50), equal weight,  monthly, with hold band
                                    sector weights fall out of sector-neutral ranking
ICIR_TOP_DECILE    icir_shrunk_v1   same                                                monthly, with hold band
EW_TOP_DECILE_Q    ew_v1            three tranches, each rebalanced every third month    quarterly per tranche
EW_UNIVERSE_PF     (benchmark)      all eligible names, equal weight                     monthly
```

Hold band: a name enters when it is in decile 10 and exits only when it falls below the 80th percentile (i.e., decile ≤ 8). This roughly halves turnover relative to strict decile membership; the exact turnover is measured and reported, not assumed.

Execution assumption: trades at the close of `as_of + 1 trading day`. Cash from a sell is redeployed the same day. Positions are equal-weighted at each rebalance; between rebalances they drift.

### 8.2 Liquidity screen

```
ADV_3m (INR)  = mean over the last 63 trading days of close_raw x volume
eligible      ADV_3m >= 2 Cr/day
also report   the share of the top decile with ADV < 10 Cr (a smallcap-tilt warning)
```

The owner's capital scale is unknown (open question, section 12). The screen is set so that a ₹10 lakh position is ≤ 5% of one day's volume in the worst eligible name.

### 8.3 Cost assumptions by liquidity bucket (one-way, applied to both legs)

Indian delivery-trade statutory costs are roughly: STT 0.10% each side, stamp duty 0.015% on buys, exchange and SEBI fees ~0.003%, GST on fees; discount brokerage is near zero. That is ~12 bps per side before market impact. Impact is bucketed by ADV:

```
bucket  ADV_3m (INR)       cost_bps per side   of which impact
L       >= 50 Cr           25                  ~13
M       10 – 50 Cr         40                  ~28
S       2 – 10 Cr          70                  ~58
XS      < 2 Cr             120 (not held)      -
```

These are assumptions, stored in `config/costs.toml`, versioned, and printed in every report. A change is a decision record. Sensitivity: the monthly report also shows net returns at 0.5× and 2× the cost table.

### 8.4 Paper return accounting

```
gross_ret(m)  = sum_i w_i(m-1) * ( TRI_i(m) / TRI_i(m-1) - 1 )   with drift-adjusted weights
turnover(m)   = 0.5 * sum_i | w_i(m)+ - w_i(m)- |                 (one-way)
cost_ret(m)   = sum over traded names of |traded weight| * cost_bps(bucket_i) / 1e4
net_ret(m)    = gross_ret(m) - cost_ret(m)
```

Stored per month in `paper_returns`; positions in `paper_positions`.

### 8.5 The alpha scoreboard

"Alpha" on the dashboard means exactly one thing:

```
alpha_12m  = net_ret(EW_TOP_DECILE, trailing 12 months) - net_ret(EW_UNIVERSE_PF, same months, same cost model)
alpha_ann  = annualised difference since inception
IR         = mean monthly difference / sd(monthly difference) * sqrt(12)
reg_alpha  = intercept of net_pf - rf on (EW_UNIVERSE_PF - rf), monthly, HAC se; rf = 6.5 %/yr assumption
             (configurable; India 91-day T-bill proxy; a data adapter can replace the constant later)
```

Beside it, the same portfolio versus each external benchmark, labelled "proxy, tracking error not modelled". Beside *that*, `n_months` and the verdict word: `insufficient` (< 24 months), `weak positive`, `positive` (IR > 0.5 and t > 2), `negative`.

---

## 9. Feedback loop & knowledge base

### 9.1 The monthly loop

```
                     ┌─────────────────────────────────────────────────────────────────┐
                     │  python -m quant run monthly          (first week of month, IST)│
                     └─────────────────────────────────────────────────────────────────┘
                                             │
   1 INGEST     data universe ─► data prices --update ─► data fundamentals ─► data holdings
                ─► data benchmarks ─► data verify (manifest checksums)
                                             │
   2 GATE       data gate --as-of D  ──► block? ──yes──► cohort row status='blocked', report, STOP (exit 2)
                                             │ no
   3 SCORE      factors compute (active + shadow) ─► scores for ew_v1, icir_shrunk_v1, ew_plus_trend_v1
                (weights for icir_shrunk_v1 recomputed from matured cohorts; lambda printed)
                                             │
   4 REALISE    evaluate: mature cohorts x horizons -► realised_returns ─► paper_returns for month D
                                             │
   5 STATS      evaluations recomputed from scratch: per factor/model/benchmark x horizon, live/backfill/legacy
                leakage tests on real data ─► any failure => block event, STOP
                                             │
   6 PROPOSE    knowledge propose: compares every hypothesis against its criteria and dates;
                writes decisions(status='proposed') for: promote, retire, weight-rule change, cost change
                                             │
   7 REPORT     knowledge/reports/<YYYY-MM>.md + learning_curve json + ui export ─► git commit (one per month)
                                             │
   8 APPROVE    human (or designated LLM, section 9.7) reads the report, runs
                `quant decide approve|reject <id> --by <name> --note "..."`
                                             │
   9 APPLY      `quant decide apply` : status changes take effect from the NEXT cohort; model_versions row
                written; ADR generated at knowledge/decisions/ADR-YYYY-NNN.md; commit
```

Steps 1–7 are automatic and idempotent (re-running on the same day rewrites the same rows). Steps 8–9 are the only places where the model definition changes.

### 9.2 Experiment registry DDL (in `quant_engine.db`)

```sql
CREATE TABLE hypotheses (
  hypothesis_id      TEXT PRIMARY KEY,           -- 'H-2026-001'
  title              TEXT NOT NULL,
  statement          TEXT NOT NULL,              -- falsifiable sentence
  factor_id          TEXT,                       -- NULL for non-factor hypotheses (e.g. cost model)
  expected_sign      INTEGER,
  horizon_months     INTEGER,
  registered_on      TEXT NOT NULL,              -- ISO date
  registered_by      TEXT NOT NULL,
  code_sha           TEXT NOT NULL,              -- git SHA that froze the formula
  evaluation_start   TEXT NOT NULL,              -- first cohort that counts as out-of-sample
  min_months         INTEGER NOT NULL,
  success_criterion  TEXT NOT NULL,
  failure_criterion  TEXT NOT NULL,
  family_year        INTEGER NOT NULL,           -- multiple-testing family
  status             TEXT NOT NULL CHECK (status IN ('registered','evaluating','supported','rejected','withdrawn')),
  resolved_on        TEXT, resolution TEXT, md_path TEXT NOT NULL
);

CREATE TABLE experiments (
  experiment_id  TEXT PRIMARY KEY,               -- 'E-2026-014'
  hypothesis_id  TEXT REFERENCES hypotheses(hypothesis_id),
  kind           TEXT NOT NULL,                  -- 'backfill_dev' | 'backfill_holdout' | 'live' | 'ablation' | 'leakage'
  config_json    TEXT NOT NULL,                  -- frozen parameters
  data_segment   TEXT NOT NULL,                  -- 'live' | 'backfill' | 'legacy'
  window_start   TEXT, window_end TEXT,
  started_on     TEXT NOT NULL, finished_on TEXT,
  result_json    TEXT,                           -- the 7.2 statistics
  verdict        TEXT,                           -- free text + 'supports' | 'contradicts' | 'inconclusive'
  run_id         TEXT REFERENCES runs(run_id)
);

CREATE TABLE evaluations (
  eval_id        INTEGER PRIMARY KEY,
  as_of          TEXT NOT NULL,                  -- window_end month
  subject_kind   TEXT NOT NULL,                  -- 'factor' | 'model' | 'portfolio' | 'benchmark' | 'sector_feature'
  subject_id     TEXT NOT NULL,
  horizon_days   INTEGER NOT NULL,
  metric         TEXT NOT NULL,                  -- 'mean_ic','hac_se','hac_t','n_cohorts','n_ind','ci90_lo','ci90_hi',...
  value          REAL, 
  data_segment   TEXT NOT NULL,
  window_start   TEXT NOT NULL, window_end TEXT NOT NULL,
  method         TEXT NOT NULL,                  -- 'spearman_sector_excess_nw2', ...
  run_id         TEXT REFERENCES runs(run_id),
  UNIQUE (as_of, subject_kind, subject_id, horizon_days, metric, data_segment, window_start, window_end)
);

CREATE TABLE decisions (
  decision_id    TEXT PRIMARY KEY,               -- 'D-2026-003'
  proposed_on    TEXT NOT NULL, proposed_by TEXT NOT NULL,   -- 'system' for auto-proposals
  kind           TEXT NOT NULL,                  -- 'factor_status' | 'model_weights_rule' | 'cost_model' | 'universe_rule' | 'data_fix' | 'other'
  subject        TEXT NOT NULL,                  -- factor_id / model_id / config key
  proposal_json  TEXT NOT NULL,                  -- {"from":"shadow","to":"active","effective_from":"2029-10-01"}
  rationale      TEXT NOT NULL,                  -- must cite experiment_ids and evaluation rows
  status         TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','applied','superseded')),
  decided_on     TEXT, decided_by TEXT, decision_note TEXT,
  applied_on     TEXT, effective_from TEXT,
  adr_path       TEXT
);

CREATE TABLE factor_registry (
  factor_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, family TEXT NOT NULL,
  hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
  expected_sign INTEGER NOT NULL, horizon_months INTEGER NOT NULL, neutralise TEXT NOT NULL,
  inputs_json TEXT NOT NULL, min_history_days INTEGER, min_coverage REAL NOT NULL, backfillable INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('candidate','shadow','active','retired')),
  registered_on TEXT NOT NULL, status_changed_on TEXT NOT NULL, status_decision_id TEXT REFERENCES decisions(decision_id),
  code_ref TEXT NOT NULL, evidence TEXT
);

CREATE TABLE model_versions (
  model_id TEXT NOT NULL, version INTEGER NOT NULL,
  definition_json TEXT NOT NULL,                 -- active factor ids, weight rule, bounds, NaN policy
  valid_from TEXT NOT NULL, valid_to TEXT,
  decision_id TEXT REFERENCES decisions(decision_id), note TEXT,
  PRIMARY KEY (model_id, version)
);

CREATE TABLE data_quality_events (
  event_id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','block')),
  check_name TEXT NOT NULL, subject TEXT, detail_json TEXT, run_id TEXT REFERENCES runs(run_id)
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, as_of TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, git_sha TEXT NOT NULL, code_version TEXT NOT NULL,
  status TEXT NOT NULL, log_path TEXT
);
```

### 9.3 Human-readable records (`knowledge/`)

```
knowledge/
  hypotheses/H-YYYY-NNN.md        one per hypothesis (template in 5.4)
  decisions/ADR-YYYY-NNN.md       generated from decisions rows on apply: context, evidence table, decision,
                                  consequences, "how to reverse"
  reports/YYYY-MM.md              the monthly report (gate table, cohort summary, matured cohorts, statistics
                                  tables, exclusion buckets, proposals, cost sensitivity, footer from 7.3)
  reports/learning_curve_YYYY-MM.json
  lessons.md                      append-only ledger: one dated line per thing learned, linked to an ADR or event
  README.md                       how to read this folder
```

The markdown is generated from the tables, never the other way round, so the two cannot drift.

### 9.4 Multiple-testing control

- **Budget:** at most 6 hypotheses registered per `family_year`. The 7th is refused by `quant hypothesis register` unless a decision record explicitly raises the budget (which is itself logged).
- **Threshold:** the family-wise threshold for "supported" is Bonferroni over the number of hypotheses registered in that family year, on the HAC t-statistic of the primary metric:

```
hypotheses in family year   1      2      3      4      5      6
required |hac_t|            1.96   2.24   2.39   2.50   2.58   2.64
```

- **No peeking:** `evaluation_start` is frozen at registration; statistics on cohorts before it are labelled `development` and excluded from the test. For backfillable factors the split is development (≤ 2023-12), holdout (2024-01 → registration), confirmation (live).
- **Count everything:** the monthly report prints "hypotheses registered YTD: k / 6; withdrawn: j" and the current threshold. An owner who "just tries" a factor outside the registry has, by definition, not tested it.

### 9.5 Promotion and retirement criteria (applied by `knowledge propose`, decided by a person)

```
shadow -> active      hypothesis status 'supported' (9.4 threshold on 3-month live IC, n_ind >= 12)
                      AND partial IC vs current active set has hac_t >= 1.5 (adds something new)
                      AND net Q10-Q1 spread > 0 over the evaluation window
                      AND coverage >= 85 % over the last 12 months
active -> retired     failure criterion met (hac_t <= -2.0 over >= 24 live cohorts)
                      OR coverage < 70 % for 6 consecutive months
                      OR data source discontinued
candidate -> shadow   code merged, hypothesis registered, backfill dev/holdout report attached (any verdict)
shadow -> retired     after 60 live months without reaching 'supported'; or withdrawn by the owner
version bump          any formula/input change -> new factor_id as candidate; the old id runs in shadow for
                      6 months alongside the new one; the paired difference is reported before the old retires
```

### 9.6 Adding a new parameter or factor safely (the exact procedure)

```
1. Write knowledge/hypotheses/H-YYYY-NNN.md from the template. Commit.
2. python -m quant hypothesis register knowledge/hypotheses/H-YYYY-NNN.md
   -> inserts the hypotheses row (status 'registered'), refuses if the yearly budget is spent.
3. Implement quant/factors/<name>.py with FactorSpec(status='candidate', hypothesis_id=...). Add a unit test
   with a hand-computed expected value on a 5-security fixture. Commit; the SHA is written to hypotheses.code_sha.
4. python -m quant factors backtest --factor <id> --segment backfill --split dev,holdout
   -> writes two experiments rows; attaches the report path to the hypothesis.
5. python -m quant decide propose --kind factor_status --subject <id> --to shadow  (or auto-proposed at step 6 of
   the loop); a person approves; from the next cohort the factor is computed live with weight 0.
6. Wait. The monthly loop evaluates it like everything else. Nothing about the past is recomputed.
7. When criteria in 9.5 are met the loop proposes promotion; approval writes a model_versions row with
   valid_from = next cohort. Historical scores keep their model_version; the record is append-only.
```

Contamination is prevented structurally: (a) a candidate never touches live cohorts; (b) promotion changes the model *from a date*, never retroactively; (c) `scores` and `factor_values` are keyed by `as_of` and `factor_id`/`model_id`, so the old and new definitions coexist in the record.

### 9.7 Approval protocol

```
Automatic (no approval)            Proposed, needs approval            Approver
--------------------------------   ----------------------------------  -----------------------------------
ingest, gates, scoring with the    shadow -> active                    human only
current definitions, realising     challenger -> headline              human only
returns, statistics, reports,      active -> retired                   human, or LLM if failure criterion met
data_quality_events, candidate     cost model / liquidity screen edits human only
computation on backfill,           universe / sector-map rule changes  human only
auto-proposals                     candidate -> shadow                 LLM allowed
                                   data fixes (corporate action add)   LLM allowed with the evidence attached
```

An LLM approver acts through the same CLI (`--by "llm:<model-name>"`), and its note must cite the experiment ids. The report lists who approved what. This split keeps the irreversible-ish decisions (what goes into the live composite) with the owner while letting routine bookkeeping run unattended.

---

## 10. Architecture

### 10.1 Package layout

```
quant/
  __init__.py
  __main__.py                # python -m quant
  cli.py                     # argparse subcommands; every command has --as-of and --dry-run
  config.py                  # paths (repo-relative, env override QUANT_DB_PATH, QUANT_DATA_DIR), thresholds from config/*.toml
  store.py                   # sqlite connection (Row factory, WAL), parquet IO, run_id context
  data/
    universe.py              # fetch_nse_csv(), snapshot_universe(as_of), members_at(d)
    prices.py                # backfill(), update(), build_tri(), adv(), manifest verify()
    corporate_actions.py     # detect_unrecorded(), add(), apply()
    fundamentals.py          # fetch(), available_from(), pit_frame(as_of, ...)
    holdings.py              # capture(), pit_series(as_of, lag_days)
    benchmarks.py            # fetch(), ew_universe_tri(), cw_universe_tri()
    quality.py               # gates(as_of) -> list[Event]; raises Blocked
  sectors/
    taxonomy.py              # nse_sector(), yahoo_fallback(), neutral_group(), update_sector_map(as_of)
    features.py              # sector_mom_6m(), sector_breadth(), sector_flow_1q()
  factors/
    base.py                  # FactorSpec, FactorContext, Factor, registry
    postprocess.py           # standardise()
    mom_12_1.py  low_vol_12m.py  quality_v1.py  value_v1.py  growth_v1.py  inst_flow_1q.py
    trend_200.py  size.py  liq_turnover.py  accruals.py
  model/
    composite.py             # score(model_id, as_of) for ew_v1 / ew_plus_trend_v1
    learning.py              # icir_shrunk_weights(model_id, as_of) -> weights, n_ind, lambda
    versions.py              # definition_at(model_id, d)
  evaluation/
    returns.py               # realise(as_of, horizons) -> realised_returns
    ic.py                    # rank_ic(), newey_west_se(), block_bootstrap_ci(), nonoverlap_series()
    spreads.py               # decile_table(), net_spread()
    nulls.py                 # random_composites(), best_single_factor()
    leakage.py               # the 7.6 tests as functions usable on real data
    learning_curve.py        # cumulative statistics per window_end
  portfolio/
    construct.py             # top_decile_with_band(), tranches()
    costs.py                 # bucket(), cost_bps()
    paper.py                 # roll_forward(month)
    scoreboard.py            # alpha metrics
  knowledge/
    registry.py              # hypotheses/experiments/decisions API
    propose.py               # criteria checks -> decisions
    reports.py               # monthly markdown + json
    adr.py
  migrate/
    legacy_v18.py            # section 10.5
  ui_export.py               # writes ui/data.js (+ ui/data_learning.js, ui/data_scoreboard.js)
config/
  engine.toml                # horizons, thresholds, NaN policy, bounds, lambda constant
  costs.toml                 # 8.3 table
  benchmarks.toml            # 4.3 table
data/                        # 4.2
knowledge/                   # 9.3
tests/
  test_factors_*.py  test_postprocess.py  test_ic.py  test_leakage.py  test_gates.py  test_learning.py
  test_migration.py  test_prices_tri.py  fixtures/
ui/                          # unchanged stack; new pages (10.6)
legacy/                      # harness_v16_learning.py, weight_optimizer.py, quant_math.py, eval_portfolio_health.py,
                             # update_ui_v16.py, concall_analyzer.py, db_setup.py, v15 files: moved, not deleted,
                             # importable for test_migration.py only
```

The legacy scripts keep working against the legacy tables (untouched) until the owner deletes them; nothing in V2 imports them except the migration test.

### 10.2 Core schema DDL (V2 tables added to `quant_engine.db`; legacy tables untouched)

```sql
CREATE TABLE security_master (
  security_id TEXT PRIMARY KEY,                -- ISIN
  symbol TEXT NOT NULL, yahoo_ticker TEXT NOT NULL, company_name TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE symbol_history (
  security_id TEXT NOT NULL, symbol TEXT NOT NULL, yahoo_ticker TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL,
  PRIMARY KEY (security_id, valid_from)
);
CREATE TABLE universe_snapshots (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL, symbol TEXT NOT NULL,
  company_name TEXT, nse_sector TEXT NOT NULL, series TEXT,
  source TEXT NOT NULL CHECK (source IN ('nse_csv','legacy_db','current_backfill')),
  source_sha256 TEXT, PRIMARY KEY (as_of, security_id)
);
CREATE TABLE sector_map (
  security_id TEXT NOT NULL, nse_sector TEXT, yahoo_sector TEXT, yahoo_industry TEXT,
  neutral_group TEXT NOT NULL, sector_source TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, PRIMARY KEY (security_id, valid_from)
);
CREATE TABLE corporate_actions (
  security_id TEXT NOT NULL, ex_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('split','dividend','demerger','rights','other')),
  value REAL NOT NULL,                          -- split: new/old ; dividend: INR/share ; demerger/rights: price factor
  source TEXT NOT NULL, fetched_at TEXT NOT NULL, note TEXT,
  PRIMARY KEY (security_id, ex_date, kind)
);
CREATE TABLE prices_monthly (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL,
  close_raw REAL, tri REAL, adv_3m_inr REAL, mcap_inr REAL, shares_out REAL,
  PRIMARY KEY (as_of, security_id)
);
CREATE TABLE fundamentals (
  security_id TEXT NOT NULL, statement TEXT NOT NULL, freq TEXT NOT NULL CHECK (freq IN ('A','Q')),
  period_end TEXT NOT NULL, field TEXT NOT NULL, value REAL,
  available_from TEXT NOT NULL, available_from_basis TEXT NOT NULL CHECK (available_from_basis IN ('earnings_date','regulatory_lag')),
  fetched_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (security_id, statement, period_end, field, fetched_at)
);
CREATE INDEX ix_fund_pit ON fundamentals (security_id, field, available_from);
CREATE TABLE holdings (
  security_id TEXT NOT NULL, captured_at TEXT NOT NULL,
  inst_pct REAL, insider_pct REAL, inst_float_pct REAL, inst_count INTEGER, source TEXT NOT NULL,
  PRIMARY KEY (security_id, captured_at)
);
CREATE TABLE security_attributes (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL,
  mcap_inr REAL, shares_out REAL, float_shares REAL, adv_3m_inr REAL, ev_inr REAL, beta REAL,
  yahoo_sector TEXT, yahoo_industry TEXT, PRIMARY KEY (as_of, security_id)
);
CREATE TABLE benchmarks (
  benchmark_id TEXT PRIMARY KEY, name TEXT NOT NULL, source_ticker TEXT, kind TEXT NOT NULL,
  underlying_index TEXT, is_total_return INTEGER, verified_on TEXT, notes TEXT
);
CREATE TABLE benchmark_prices (benchmark_id TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL, PRIMARY KEY (benchmark_id, date));
CREATE TABLE sector_features (as_of TEXT NOT NULL, neutral_group TEXT NOT NULL, feature_id TEXT NOT NULL, value REAL, PRIMARY KEY (as_of, neutral_group, feature_id));
CREATE TABLE factor_values (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL, factor_id TEXT NOT NULL,
  raw REAL, z REAL, coverage_flag TEXT, run_id TEXT NOT NULL,
  PRIMARY KEY (as_of, security_id, factor_id)
);
CREATE TABLE model_weights (
  as_of TEXT NOT NULL, model_id TEXT NOT NULL, factor_id TEXT NOT NULL,
  weight REAL NOT NULL, n_ind INTEGER NOT NULL, lambda REAL NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY (as_of, model_id, factor_id)
);
CREATE TABLE scores (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL, model_id TEXT NOT NULL, model_version INTEGER NOT NULL,
  composite_z REAL, pct_rank REAL, sector_pct_rank REAL, decile INTEGER,
  n_factors_used INTEGER, eligible INTEGER NOT NULL, exclusion_reason TEXT, run_id TEXT NOT NULL,
  PRIMARY KEY (as_of, security_id, model_id)
);
CREATE TABLE cohorts (
  as_of TEXT PRIMARY KEY, n_members INTEGER, n_scored INTEGER, n_eligible INTEGER,
  gate_status TEXT NOT NULL CHECK (gate_status IN ('passed','passed_with_warnings','blocked')),
  data_segment TEXT NOT NULL CHECK (data_segment IN ('live','backfill','legacy')), run_id TEXT
);
CREATE TABLE realised_returns (
  as_of TEXT NOT NULL, security_id TEXT NOT NULL, horizon_days INTEGER NOT NULL,
  end_date TEXT, tr_log REAL, sector_excess REAL, universe_excess REAL,
  status TEXT NOT NULL CHECK (status IN ('ok','delisted_partial','excluded_ca','missing')),
  PRIMARY KEY (as_of, security_id, horizon_days)
);
CREATE TABLE paper_positions (as_of TEXT, portfolio_id TEXT, security_id TEXT, weight REAL, action TEXT, cost_bps REAL, PRIMARY KEY (as_of, portfolio_id, security_id));
CREATE TABLE paper_returns (month_end TEXT, portfolio_id TEXT, gross_ret REAL, cost_ret REAL, net_ret REAL, turnover REAL, n_positions INTEGER, PRIMARY KEY (month_end, portfolio_id));
-- plus the knowledge tables from 9.2
```

### 10.3 CLI

```
python -m quant data universe   --as-of D              snapshot NSE CSV; update security_master, sector_map
python -m quant data prices     --backfill --years 10  | --update
python -m quant data ca         detect --as-of D | add --security ISIN --ex-date D --kind K --value V
python -m quant data fundamentals --update [--annual]  (annual statements quarterly by default after year 1)
python -m quant data holdings   --capture
python -m quant data benchmarks --update
python -m quant data verify                              manifest checksums; logs source_restated
python -m quant data gate       --as-of D                exit 0 pass, 1 warnings, 2 blocked
python -m quant factors list | compute --as-of D [--factor ID] | backtest --factor ID --segment backfill --split dev,holdout
python -m quant score           --as-of D [--model ID]
python -m quant evaluate        --as-of D                realise + statistics + leakage on real data
python -m quant paper           --month YYYY-MM
python -m quant learn           --as-of D --model icir_shrunk_v1 [--explain]
python -m quant hypothesis      register FILE | list | show ID
python -m quant decide          list | propose ... | approve ID --by NAME --note TEXT | reject ID ... | apply
python -m quant report          --month YYYY-MM
python -m quant ui export
python -m quant migrate legacy  [--dry-run]
python -m quant test leakage    [--as-of D]
python -m quant run monthly     [--as-of D] [--stop-after STEP]
```

Every command prints the `run_id`, the `as_of` it resolved, and the git SHA; `--dry-run` writes nothing and prints what it would write.

### 10.4 Configuration (`config/engine.toml`, excerpt)

```toml
[horizons]
learning_days = 63
thesis_days   = 252
diagnostic_days = [21, 126, 504, 756]

[universe]
min_members = 450
max_members = 520
min_group_size = 10

[factors]
winsor_pct = 0.01
min_factors_for_composite = 4
min_coverage_default = 0.70

[learning]
model_id = "icir_shrunk_v1"
min_n_ind = 12
lambda_k = 24
weight_ceiling_multiple = 2.0
nw_lag_learning = 2

[portfolio]
top_decile = 10
exit_percentile = 0.80
min_adv_inr = 2.0e7
lag_days = 1

[knowledge]
hypotheses_per_year = 6
```

### 10.5 Migration of the existing `quant_engine.db`

Facts established by inspection (read-only) on 2026-09-05:

```
date         rows   note
2026-06-04    47    partial run (Nifty 50 fallback)                      -> imported as data_segment='legacy', gate_status='blocked'
2026-06-12   499    first full run                                        -> legacy cohort L1 (prices are Friday 06-12 closes)
2026-06-14   499    IDENTICAL prices to 06-12 (100 % match), scores differ -> duplicate: NOT a cohort; recorded as a re-score of L1
2026-07-11   499    full                                                  -> legacy cohort L2
2026-08-14   499    full                                                  -> legacy cohort L3
2026-09-03   500    full; pre-fix harness (no Data_Flags, Div_Yield x100) -> legacy cohort L4
raw_json     33 keys in every snapshot; no Market_Cap_Cr, Industry or Data_Flags in any of them
active_weights   12 rows; runs 11 and 12 same day/same data; growth pinned at 0.30
performance_tracking   4,773 rows incl. forward dates 06-11, 06-18, 06-22, 07-10 from "bypass" runs -> not trusted
```

`python -m quant migrate legacy` does, idempotently:

1. `security_master` / `symbol_history`: map each legacy `ticker` to an ISIN via the current NSE CSV; tickers not found (delisted/renamed, e.g. `JBCHEPHARM.NS` dropped in August) are looked up in older snapshots or assigned a synthetic `LEGACY:<symbol>` id with a `data_quality_events` info row.
2. `universe_snapshots`: one snapshot per legacy cohort (`source='legacy_db'`), `nse_sector` from the current CSV (flagged `sector_from_current`), Yahoo sector from `raw_json.Sector`.
3. `cohorts`: L1–L4 with `data_segment='legacy'`, `as_of` = the actual trading date of the prices (06-12, 07-10 or 07-11 depending on the run time — the implementer checks the day of week; 07-11 was a Saturday, so prices are 07-10 closes; 08-14 Friday; 09-03 Thursday, possibly intraday). Store the original run date in `cohorts.note`.
4. `factor_values`: the eight legacy 0–100 scores as `factor_id = 'legacy_<name>_v18'`, `raw` = stored score, `z` = rank-z within the *current* neutral group, `coverage_flag='legacy_bucketed'`; `momentum_multiplier` and `trap_score` likewise as `legacy_momentum_mult_v18`, `legacy_trap_v18`.
5. `scores`: `final_score` and `base_score` (NULL before September) as `model_id='legacy_v18'`, `eligible = final_score > 0`.
6. `model_versions`: 12 rows `legacy_v16..v18` with the weight vectors and the notes from `docs/analysis/historical_runs_log.md`.
7. `realised_returns` for L1–L4 are **recomputed from the new TRI store**, not copied from `performance_tracking`. The legacy `price` column is ignored for returns.
8. `data_quality_events` (as_of per cohort, severity `warn`): `legacy_div_yield_x100`, `legacy_roe_none_as_zero`, `legacy_no_data_flags`, `legacy_duplicate_snapshot_0612_0614`, `legacy_partial_0604`, `legacy_sentiment_in_score` (L1–L3 carry up to +10 points), `legacy_unadjusted_prices`.
9. Legacy tables stay in place, unmodified. `tests/test_migration.py` asserts row counts (499/499/499/500 scores under `legacy_v18`; 4 legacy cohorts; the 06-14 duplicate absent) and that L1→L2 TRI returns for ZFCVINDIA lie in [−20%, +20%].

The three legacy transitions appear on every chart as three dotted points labelled "legacy (known defects)". They are never included in `n_ind`.

### 10.6 UI changes (vanilla HTML/JS/CSS, no build step)

`ui/index.html` gains a top-level nav with five pages; `ui/app.js` is split into `ui/js/{screener,learning,scoreboard,factors,knowledge}.js`; data arrives as separate `ui/data_*.js` files written by `quant ui export` so the screener payload (currently 1.3 MB) does not grow.

```
Screener    latest cohort: top decile for the champion, per-name factor z's, exclusion reason, sector group;
            the old plain-English blurbs may stay but every number shown must come from factor_values/scores
Learning    the 7.7 chart (Chart.js already loaded from CDN; README's "zero-dependency" claim is corrected)
Scoreboard  8.5 metrics, paper vs benchmarks, cost sensitivity, verdict word with n_months
Factors     registry table: status, hypothesis link, live/backfill mean IC ± CI, coverage, last 12 cohort ICs
Knowledge   decisions list (status, who, when), gate history, hypotheses budget YTD, links to ADR markdown
```

Every page footer shows `as_of`, `run_id`, `git_sha`, and the sentence "Statistics with n_ind < 12 are not evidence."

---

## 11. Phased roadmap

### 11.1 Milestones

```
Month 1 (by 2026-10-05, first live cohort)
  data layer: universe snapshot, sector_map, 10-year price backfill, TRI, corporate_actions, manifest;
  fundamentals bitemporal capture; holdings capture; benchmarks; all gates
  factors: 6 active + trend_200 shadow; postprocess; registry == code check
  models: ew_v1, ew_plus_trend_v1 (icir_shrunk_v1 scaffolded; lambda = 0)
  evaluation: realise, IC/HAC/bootstrap, nulls, leakage tests (CI + real data)
  knowledge: all tables; 7 hypotheses registered (H-2026-001..007); first monthly report
  migration: legacy cohorts imported; test_migration green
  deliverable: knowledge/reports/2026-10.md with backfill dev/holdout statistics for price factors

Month 3 (2026-12)
  paper portfolios + cost model + scoreboard; UI pages Learning/Scoreboard/Factors/Knowledge;
  sector features computed (candidates) on backfill and live; CSV exports; VACUUM+commit routine
  first 63-day cohort (2026-10) matures in January -> first live IC point appears in month 4

Month 6 (2027-03)
  3 live matured 3-month cohorts; learning-curve live segment visible; first auto-proposals possible
  (data fixes only; no status changes can qualify yet); annual statements refresh cadence set

Month 12 (2027-09)
  9 matured 3-month cohorts (n_ind = 3); 12-month horizon first point in month 13;
  first full-year multiple-testing family closed (k <= 6); size/liquidity candidates reported;
  owner review of the whole record; decide whether to fund an optional PIT fundamentals or holdings adapter

Month 36 (2029-09)   n_ind = 12: challenger may first deviate; F1–F3 first judged; first mb36 cohort labelled
```

### 11.2 The chart the owner will look at

One figure, `ui` Learning page, panel A: x = months of live data (0 at 2026-10), y = cumulative mean 3-month sector-neutral Rank IC, one thick line for `ew_v1` with a 90% band, thin lines per active factor, dashed lines for shadow factors, the backfill segment to the left of 0 shaded grey with its own scale note "survivorship-biased", three dotted points for the 2026 legacy cohorts, vertical markers for every applied decision, and a horizontal band showing the 25th–75th percentile of the random-composite null. Panel C beneath it shows the band width shrinking. That figure *is* the learning curve; if the thick line is inside the null band after 36 months, the approach has not worked, and the figure will say so without anyone editing it.

### 11.3 What to cut if only 4 weeks of implementation are available

Keep (in this order, stop when time runs out):

```
week 1  security_master/universe/sector_map; price backfill + TRI + corporate_actions + manifest; gates
week 2  fundamentals bitemporal (annual + quarterly) with the lag rule; postprocess; mom_12_1, low_vol_12m,
        quality_v1, value_v1 (skip growth_v1 and inst_flow_1q -> month 2); ew_v1; PIT truncation test
week 3  realised_returns; IC + HAC + bootstrap; nulls; leakage tests; hypotheses/decisions/evaluations tables;
        hypothesis register for the 4 factors; migration of legacy cohorts
week 4  monthly report markdown; `quant run monthly`; CSV exports; minimal ui export (a JSON + one Learning chart)
```

Cut without regret for now: paper portfolio and cost model (report gross decile spreads with a "gross" label), challenger model and `learn` command (lambda is 0 for three years anyway), sector features, ETF benchmarks (keep EW_UNIVERSE and ^CRSLDX), the Scoreboard/Factors/Knowledge UI pages, holdings capture (it costs a month of history per month delayed — start it in week 1 as a 20-line script if at all possible, even with nothing reading it).

What must not be cut even under pressure: bitemporal fundamentals, TRI from raw facts, gates, the PIT truncation test, hypotheses registration before the first live cohort. Those are the parts that cannot be retrofitted without invalidating the record.

---

## 12. Risks, failure modes and open questions

### 12.1 Data-source risks

1. **Yahoo Finance is unofficial and restates.** Mitigation: raw facts stored, manifest checksums, `source_restated` events, committed monthly panel. Residual: a silent change in field semantics (the `dividendYield` episode). Every `info` field V2 reads is listed in one place (`quant/data/fundamentals.py::INFO_FIELDS`) with its unit and a range assertion.
2. **Fundamentals are not truly point-in-time in the backfill.** The regulatory-lag rule is conservative on timing but cannot undo restatements already baked into Yahoo's current numbers. Mechanism: *restatement bias* — historical values reflect later corrections, flattering quality/value factors in the backfill. Backfill statistics for fundamental factors are development evidence only.
3. **Institutional holdings from Yahoo for Indian names have unclear provenance and frequency.** Verified value for HEROMOTOCO (38.98% institutions, 257 holders) looks like an aggregated shareholding pattern, but the update lag is unknown. Mitigation: log every change date; after 12 months the report states the observed update cadence; if it is not quarterly, `inst_flow_1q_v1` is re-specified as a new version. Optional adapter: NSE shareholding-pattern files.
4. **Universe history is survivorship-biased before June 2026.** Mechanism: *survivorship* — names that left the index (often after collapsing) are absent, so backfilled momentum/low-vol IC is biased upward for "winners keep winning". Mitigation: segment labelling; optional reconstitution-press-release adapter.
5. **Corporate actions Yahoo does not carry** (demergers, rights). Mitigation: gap detection + manual resolution + exclusion until resolved. Residual: an unresolved gap silently shrinks the sample; the exclusions table makes it visible.

### 12.2 Statistical failure modes

6. **Small effective N for years** (7.3). The owner may be tempted to act on n_ind = 4. The design's answer is procedural (lambda = 0, thresholds, verdict words) rather than persuasive; the risk is that procedures get bypassed. Mitigation: the CLI refuses to apply a status change whose criteria are not met unless `--override` is given, and an override writes an ADR with the word OVERRIDE in its title.
7. **Multiple testing through the back door**: informal experiments in notebooks, or changing a formula "slightly". Mitigation: registry == code check, code SHA frozen in hypotheses, the yearly budget. Residual: nothing stops a determined owner; the record will at least show the version churn.
8. **Cross-sectional dependence** understates standard errors. Mitigation: sector-demeaned returns, time-series HAC, block bootstrap; per-cohort t only as an upper bound. Residual: factor crowding episodes (2018 India smallcap unwind) produce clustered IC shocks that HAC with lag 2 under-covers; the 12-month horizon's lag-11 HAC is the check.
9. **The learning rule may still lose to equal weights** even at n_ind ≥ 12, exactly as the red team measured. That is a legitimate outcome, not a failure of the design; the champion/challenger protocol keeps equal weights in that case.

### 12.3 Engineering and process risks

10. **SQLite in git**: monthly commits of a growing binary. Mitigation: monthly cadence, VACUUM, CSV exports; ~6 MB/year growth. Residual: repository size in five years ~0.5–1 GB of history; acceptable, and Parquet is outside git.
11. **One-hour laptop budget** is dominated by 7 sequential HTTP calls × 500 names. Mitigation: quarterly cadence for annual statements, batched prices. Residual: Yahoo throttling on a bad day pushes past an hour; the run is resumable per step (`--stop-after`, idempotent upserts).
12. **The implementer LLM may "simplify" the bitemporal table** into a wide latest-value table because it is easier. The PIT truncation test exists to make that shortcut fail loudly; it must be written in week 2, not last.
13. **Approval bottleneck**: a monthly human step that never happens. Mitigation: proposals are rare by construction (criteria take years); routine months need no approval at all.

### 12.4 Open questions for the owner (answers become ADRs)

```
Q1  Capital scale for the paper portfolio (drives the ADV screen and impact buckets). Default assumed: INR 10 lakh/name.
Q2  Who is the approver of record, and may an LLM approve candidate->shadow and data fixes (9.7)? Default: yes.
Q3  Is a paid point-in-time fundamentals or shareholding feed acceptable as an *optional* adapter in year 2?
Q4  Should the backfill universe be improved via NSE reconstitution releases (manual, ~2 days of work)?
Q5  Risk-free rate source for reg_alpha: constant 6.5 % vs a T-bill series adapter.
Q6  Growth_v1's second leg (ΔEPS/price) vs a simpler revenue-only growth factor: the owner's thesis should choose,
    before registration, since it cannot be changed afterwards without a new version.
Q7  Whether the UI keeps the "turnaround" view; it is a view, not a model, but users may read it as a recommendation.
```

### 12.5 Confidence

```
that this design's record would survive a hostile methodological review        80 %
that the six-factor EW composite shows a 3-month live IC with 90 % CI > 0 by 2029-09   35 %
```

The gap between the two numbers is the point: the first is about the process and is under our control; the second is about the market and is not. The design is built so that either outcome is measured correctly.

---

## Appendix A. Data flow, compact

```
NSE CSV ─┐
Yahoo prices ──► raw facts (parquet, regenerable) ──► TRI ──► prices_monthly ─┐
Yahoo statements ──► fundamentals (period_end, available_from, fetched_at) ───┤
Yahoo info ──► holdings (captured_at), security_attributes ───────────────────┤
                                                                              ▼
                                   gates ──► FactorContext(as_of) ──► factor_values(raw, z)
                                                                              │
                              model_versions/model_weights ──► scores(model_id) ──► paper_positions
                                                                              │
                        later closes ──► realised_returns ──► evaluations ──► proposals ──► decisions ──► ADRs
```

## Appendix B. Monthly report skeleton (`knowledge/reports/YYYY-MM.md`)

```
1. Run: as_of, run_id, git_sha, duration, gate table (all checks, severity, detail)
2. Cohort: members, scored, eligible, exclusions by reason with their forward returns (when matured)
3. Matured this month: cohorts x horizons realised; delisted/excluded counts
4. Statistics (live): per factor/model/horizon: mean IC, HAC se, t, n_cohorts, n_ind, CI90, hit rate
   (backfill and legacy in separate tables with their caveat lines)
5. Nulls: random-composite percentile, best single factor
6. Deciles: gross and net Q10-Q1 at 63 d and 252 d; monotonicity
7. Paper: month return, turnover, costs, scoreboard vs EW_UNIVERSE and proxies, verdict word
8. Would-have-been-killed bucket vs rest (24-month diagnostic)
9. Proposals generated, with the criteria lines they satisfied; approvals pending
10. Hypotheses budget YTD; threshold in force
11. Footer: the 7.3 paragraph, verbatim
```
