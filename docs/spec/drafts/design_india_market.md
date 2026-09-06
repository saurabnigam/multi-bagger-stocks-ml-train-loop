# V2 Engine Design — Indian Market Practitioner Draft

```
Author lens   : NSE/AMFI sector taxonomy, data a retail-scale project can actually fetch
                (yfinance, niftyindices.com CSVs, nsearchives.nseindia.com files, quarterly
                results), corporate actions, NSE liquidity tiers and the real cost stack,
                factors with Indian evidence, market microstructure.
Written       : 2026-09-05, branch red-team-review-sep-2026
Audience      : an implementing LLM that has only the documents in this repository.
Status        : draft input to the master spec. Every number labelled TARGET is a target.
                Nothing in this document claims out-of-sample performance.
Read before   : docs/spec/00_context_brief.md, docs/analysis/red_team_review.md, AGENTS.md
Code skimmed  : quant_math.py, harness_v16_learning.py, weight_optimizer.py,
                eval_portfolio_health.py, db_setup.py, config.py, update_ui_v16.py
```

> **The design in three sentences.** Rank the Nifty 500 every month inside 15 NSE-sector groups using a small set of pre-registered, continuous, sector-neutral factors that already have Indian evidence (12-1 momentum, distance from the 200-day average, low volatility, ROCE, cash conversion, leverage, earnings growth and earnings momentum, free-cash-flow yield), hold the composite at a hierarchical equal weight until at least 36 clean live months exist, and spend the first three years building the one asset this project has never had: a point-in-time, corporate-action-correct, cost-aware evidence base that grows by one honest observation per month. The multi-bagger goal is expressed as a 12-month sector-relative total-return label for learning and evaluation, and as a 36-month "doubled" recall for the slow scoreboard. The monthly loop proposes; a named approver disposes; the knowledge base remembers why.

Facts verified live on 2026-09-05 that the rest of this document relies on:

```
niftyindices.com/IndexConstituent/ind_nifty500list.csv
  columns              : Company Name, Industry, Symbol, Series, ISIN Code
  rows                 : 500, all Series = EQ
  "Industry" column    : is the NSE *Sector* level (20 distinct values), NOT Industry
  sector counts        : Financial Services 101, Capital Goods 63, Healthcare 48,
                         Automobile and Auto Components 38, Consumer Services 29, FMCG 28,
                         Information Technology 27, Chemicals 26, Metals & Mining 18,
                         Power 17, Oil Gas & Consumable Fuels 17, Consumer Durables 16,
                         Services 14, Construction 13, Construction Materials 11, Realty 11,
                         Telecommunication 10, Textiles 5, Media Entertainment & Publication 5,
                         Diversified 3
also fetchable (HTTP 200, plain GET with a browser User-Agent):
  ind_nifty200momentum30_list.csv (30 rows)   ind_nifty500quality50_list.csv (50 rows)
  ind_nifty100quality30list.csv (30)          ind_niftymidcap150list.csv (150)
  ind_niftysmallcap250list.csv (250)          ind_niftytotalmarket_list.csv (754)
  ind_nifty100list.csv (100)                  ind_niftymicrocap250_list.csv (254)

www.nseindia.com/api/quote-equity?symbol=X   : HTTP 403 without a browser session.
  => the four-level NSE classification (Macro / Sector / Industry / Basic Industry)
     is NOT retail-fetchable by script. Only the Sector level is. Design accordingly.

nsearchives.nseindia.com (HTTP 200, plain GET):
  products/content/sec_bhavdata_full_04092026.csv   397 KB, columns include
     SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN/HIGH/LOW/LAST/CLOSE_PRICE, AVG_PRICE,
     TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
  content/cm/BhavCopy_NSE_CM_0_0_0_20260904_F_0000.csv.zip   204 KB
  content/equities/EQUITY_L.csv   (SYMBOL, NAME, SERIES, DATE OF LISTING, ISIN, FACE VALUE)
  content/equities/bulk.csv       (bulk deals, daily)

yfinance 1.4.1, Python 3.14, pandas 3.0.3, numpy 2.4.6 (pyarrow and duckdb NOT installed;
git-lfs NOT installed)
  Ticker.history(auto_adjust=True)  : split AND dividend back-adjusted.
     ZFCVINDIA 6:1 split 2026-06-24 -> no cliff (2676 -> 2655).
     HEROMOTOCO Rs 75 dividend ex 2026-07-24 -> raw 4977.70, adjusted 4905.54 on 07-20.
  Ticker.splits / Ticker.dividends  : present, IST-stamped.
  Ticker.earnings_dates             : present for large caps (8 quarters incl. reported EPS).
  Ticker.get_shares_full()          : point-in-time share counts (sparse dates).
  Ticker.quarterly_financials       : 6 quarters, rows incl. Total Revenue, EBIT, Net Income, Diluted EPS.
  Ticker.quarterly_balance_sheet    : 3 half-years.
  Ticker.quarterly_cashflow         : EMPTY for HEROMOTOCO (annual cash flow only).
  Ticker.institutional_holders      : EMPTY. info.heldPercentInstitutions = 0.3905 (no as_of).
  info.returnOnEquity / currentRatio: None.  info.debtToEquity = 3.57 (percent units).
  yf.download(list, auto_adjust=True, group_by='ticker', threads=False): works for .NS lists.
  Index tickers: ^CRSLDX (Nifty 500, from 2005-09), ^NSEI, ^CNX200 (Nifty 200).
  ETF proxies: MOM30IETF.NS (Nifty 200 Momentum 30, from 2024-06-24),
               QUAL30IETF.NS (Nifty 100 Quality 30, from 2023-08-10),
               LOWVOLIETF.NS (from 2018-05), MID150BEES.NS (from 2019-02), NIFTYBEES.NS (from 2009).
  History depth: GENUSPOWER from 2005-07; recent IPO SAGILITY from 2024-11.

quant_engine.db (read-only inspection)
  snapshots: 2026-06-04 (47 rows), 06-12 (499), 06-14 (499), 07-11 (499), 08-14 (499), 09-03 (500)
  weekdays : 06-14 Sunday, 07-11 Saturday, 08-14 Friday, 09-03 Thursday
  09-03 raw_json still carries Div_Yield_% = 349.0 and FCF_Yield_% = 6.8e7 for HEROMOTOCO,
     has no Market_Cap_Cr, no Industry, no Data_Flags, ROE_% None for all 500
     => the fixed harness has not been re-run; every stored snapshot is "legacy_v1".
  Yahoo sectors in the 09-03 snapshot: 12 values (Industrials 91, Consumer Cyclical 69,
     Basic Materials 62 ...) which do not map 1:1 onto the 20 NSE sectors.
  performance_tracking: 4,773 rows over 8 forward dates (recomputable; not migrated as truth).
  Universe churn between snapshots: 0, 1, 1 tickers.
```

---

## 1. Objective & success metrics

### 1.1 Objective in one sentence

Build a monthly-run engine that (a) ranks Nifty 500 stocks inside their NSE sector group by expected 12-month sector-relative total return, (b) records every input and output point-in-time so that each month adds one clean out-of-sample observation, and (c) can show, on one chart, whether its out-of-sample predictive power is rising as those observations accumulate — with the multi-bagger ambition measured as the share of eventual 36-month doublers that were ranked in the top decile at the start.

### 1.2 What "learning" means here, and in what order it happens

An owner who runs this monthly for years will see three kinds of learning, in this order. Confusing them is how the V18 documentation over-claimed.

```
Year 1   : the system learns about ITS DATA.   Which fields are missing for which sectors,
           which corporate actions break returns, which factors have coverage < 80%,
           what a month costs in HTTP calls. This learning is measurable from month 2.
Year 2-3 : the system learns WHICH FACTORS HOLD UP out of sample at 1-3-12 months,
           and the paper portfolio starts to have a cost-aware track record.
Year 3+  : the system may learn WEIGHTS. Not before. The arithmetic in section 2.1
           makes weight learning from 12-month labels a year-4 event.
```

The learning curve the owner wants (out-of-sample IC vs months of clean data) is real from month 2 for the 1-month horizon, from month 4 for the 3-month horizon, and from month 13 for the 12-month horizon. A "backfilled" track for price-derived factors exists from day one and is shown on the same chart in a different colour, clearly labelled as survivor-biased (section 2.5).

### 1.3 Numeric targets and timelines

All values are TARGETS. None has been measured. `IC` = Spearman rank correlation between the composite score and the subsequent sector-relative total return across stocks on one date. `HAC t` = t-statistic with Newey–West standard errors (overlapping periods), defined exactly in section 7.2.

```
Metric (live track, sector-neutral composite)          Month 6      Month 12      Month 24      Month 36
--------------------------------------------------------------------------------------------------------
Data-quality gate pass rate (months passed / run)      >= 5/6       >= 11/12      >= 23/24      >= 35/36
Share of factor inputs imputed (universe median)       <= 15%       <= 10%        <= 8%         <= 8%
Universe coverage (scored / index constituents)        >= 96%       >= 98%        >= 98%        >= 98%
1-month IC, cumulative mean (90% CI reported)          measured     >= +0.02      >= +0.02      >= +0.02
3-month IC, cumulative mean                            measured     measured      >= +0.03      >= +0.03
12-month IC, cumulative mean                           n/a          n/a           measured      >= +0.04, HAC t >= 1.5
Net-of-cost top-quintile minus EW-universe, 12 m       n/a          n/a           measured      > 0 (any t)
36-month doubler recall @ top decile                   n/a          n/a           n/a           first value; TARGET >= 2x base rate
Hypotheses tested YTD (budget)                         <= 3         <= 6          <= 6/yr       <= 6/yr
Monthly run wall-clock on a laptop                     <= 60 min    <= 45 min     <= 45 min     <= 45 min
```

Why +0.04 at 12 months and not +0.10: a composite of a few validated factors in a 500-stock, sector-neutral, mid/small-cap-heavy universe typically shows 12-month ICs of 0.03–0.08 in published Indian and emerging-market work; +0.10 sustained would be exceptional. Stating +0.10 as a target would repeat the V18 mistake.

Base rate for the doubler KPI: over rolling 36-month windows since 2010, roughly 10–20% of Nifty 500 names doubled (higher in 2020–2024, lower in 2011–2013 and 2018–2020). The target is stated relative to the base rate measured in the same window, not as an absolute, because the base rate swings by regime.

### 1.4 What would falsify the approach

The approach is falsified — and the owner should stop adding factors and start questioning the premise — if, on the live track:

```
F1  After 36 clean months, the 12-month composite IC has HAC t < 1.0 AND the 3-month IC
    cumulative mean is < +0.01. (No detectable ranking skill at any horizon.)
F2  After 24 months, the sector-neutral composite does not beat the equal-weight universe
    net of cost in the paper portfolio, AND the shuffle test (7.5) passes. (Skill absent,
    not a bug.)
F3  The learning-curve slope (IC vs months, section 7.7) is not positive by month 24 for
    the composite even though data-quality gates pass. (More data is not helping.)
F4  Every active factor's live IC has the opposite sign to its pre-registered sign for
    24 months. (Priors were wrong for this market.)
```

F1 and F2 do not falsify "factor investing works in India"; they falsify "this retail-data implementation can detect it". That distinction matters for the decision the owner would take (change data sources vs abandon).

### 1.5 An India-specific reason the 12-month horizon is also the right *investing* horizon

Indian capital-gains tax treats equity holdings under 12 months as short-term (20% since the July 2024 budget) and over 12 months as long-term (12.5% above the annual exemption). A strategy that turns over monthly is taxed at 20% on gains; a 12-month-plus holder pays 12.5%. On a 15% gross return that is a 1.1 percentage-point annual difference before any transaction costs. The prediction horizon, the rebalance cadence (section 8.4) and the tax regime therefore point the same way: rank for 12 months, hold with a buffer, trade little.

---

## 2. Prediction target & horizons

### 2.1 The seed hypothesis is right about the horizon and wrong about the arithmetic

Seed hypothesis 1 says: primary target = 12-month sector-neutral relative return. Seed hypothesis 4 says: weights may deviate from equal only once ≥ 12 non-overlapping evaluation periods exist. Together, at a 12-month horizon, that is 12 years of live data before the first weight change. That is not a guard rail, it is a permanent lock, and it should be stated as such rather than discovered in year 5.

The problem is the number of *independent* observations. Twelve-month labels observed monthly overlap eleven-twelfths with their neighbours.

```
Suppose the IC of one factor at 12 months has a true mean of +0.05 and a month-to-month
standard deviation of 0.12 (typical for 12-month ICs on ~500 stocks).

months of live data   overlapping 12m obs   independent obs (n_eff = months/12)   SE of mean IC
        24                    12                          1.0                        0.120
        36                    24                          2.0                        0.085
        60                    48                          4.0                        0.060
       120                   108                          9.0                        0.040

To tell +0.05 from 0 at t = 2 you need SE <= 0.025  =>  n_eff ~ 23  =>  ~23 years.
```

For 1-month labels the same arithmetic gives SE ≈ 0.08/√36 = 0.013 after 36 months. So the 1- and 3-month horizons are where the learning curve becomes visible first, and the 12-month horizon is where the objective lives. The design therefore:

1. Keeps 12-month sector-relative total return as the **primary label** for the scoreboard and for any weight learning (it is what the owner is trying to predict).
2. Computes and reports 1, 3, 6, 24 and 36-month labels on every snapshot; the 1- and 3-month ICs are the **early-warning instruments**, not the objective.
3. Replaces the "≥ 12 non-overlapping periods" gate with an **effective-sample-size shrinkage** (section 6.3) plus a hard floor of 36 live months, so the rule is honest about how little 12-month evidence exists rather than pretending a monthly overlapping observation is a period.
4. Adds a **backfilled price-factor track** (section 2.5) that supplies ~10 years of 12-month labels for factors that need only prices and volumes, explicitly tagged as survivor-biased, so the price factors carry informed priors while fundamental factors start from zero.

### 2.2 Label definitions (exact)

All returns are **total returns from split- and dividend-adjusted closes** (`Ticker.history(auto_adjust=True)` semantics, cross-checked as in 4.4). All dates are NSE trading days. `as_of` is the last NSE trading day of the calendar month.

```
P_adj(i, d)           adjusted close of stock i on trading day d
h                     horizon in months, h in {1, 3, 6, 12, 24, 36}
d_h                   last NSE trading day of the month as_of + h months
r(i, as_of, h)        = ln( P_adj(i, d_h) / P_adj(i, as_of) )               log total return
g(i, as_of)           sector group of stock i at as_of (section 3.5), fixed at as_of
r_grp(g, as_of, h)    = median over i in g of r(i, as_of, h)                 group median, same stocks
L_h(i, as_of)         = r(i, as_of, h) - r_grp(g(i,as_of), as_of, h)          SECTOR-RELATIVE LABEL

Delisting / suspension before d_h:
  if the stock was acquired or merged: use the last available adjusted close and mark
     label_status = 'terminated_corporate_event'; include in labels (a genuine outcome).
  if trading was suspended / GSM-stage: use last close, mark 'suspended'; include.
  if data is simply missing: label NULL, status 'missing', excluded, counted in gate 4.6.
Membership: labels are computed for every stock that was in the scored universe at as_of,
  regardless of whether it left the Nifty 500 later. Never recompute a label with a later
  universe list (that is survivorship bias entering through the back door).
```

Log returns are used for the label because 12-month arithmetic returns in Indian small caps are strongly right-skewed (a +300% name dominates a quintile mean). Quintile and portfolio results are reported in arithmetic returns because that is what a portfolio earns; the label used for ranking statistics is the log form. Spearman IC is invariant to this choice; quintile means are not.

Primary label: `L_12`. Median rather than mean for the group adjustment because a single demerger or 5x name should not move the whole group's baseline.

### 2.3 How the multi-bagger goal becomes measurable

"Multi-bagger" is a 3–5 year outcome; the engine is trained on 12-month labels. The bridge is a slow KPI that is *only observed*, never optimised:

```
MB36(i, as_of)   = 1 if exp(r(i, as_of, 36)) >= 2.0       (doubled in 36 months, total return)
MB36_rel(i, as_of) = 1 if L_36(i, as_of) is in the top decile of its as_of cross-section

Recall@D(as_of)  = ( # stocks with MB36 = 1 AND composite decile at as_of = 10 )
                   / ( # stocks with MB36 = 1 )
Base rate        = ( # MB36 = 1 ) / ( # scored )     -> reported next to Recall so the reader
                                                        sees "2.3x base rate", not "31%"
Lift             = Recall@D / 0.10                    (a random decile recalls 10%)
Precision@D      = ( # MB36 = 1 in decile 10 ) / ( # in decile 10 )
```

First observation: as_of = go-live month + 36 months. Until then the dashboard shows the base rate from the backfilled track only (survivor-biased, labelled). The reason not to optimise for MB36 directly: with ~50–100 doublers per cohort and cohorts every month that overlap 35/36, there is no statistical power for a binary target for a decade. The 12-month rank label is the densest signal that is still aligned with the goal (a stock that doubles in 36 months has, on average, an above-median 12-month sector-relative return in each of the three years).

### 2.4 Horizon table

```
h (months)  first observable   role                                     statistics used
1           go-live + 1        early warning; microstructure check      IC, HAC lag 0
3           go-live + 3        early warning; earnings-momentum check   IC, HAC lag 2
6           go-live + 6        diagnostic                               IC, HAC lag 5
12          go-live + 12       PRIMARY: scoreboard + weight learning    IC, HAC lag 11, n_eff
24          go-live + 24       diagnostic                               IC, HAC lag 23
36          go-live + 36       slow KPI: MB36 recall/precision/lift     counts, lift, Wilson CI
```

### 2.5 Two evidence tracks, never mixed

```
track = 'live'       inputs recorded at as_of by the monthly run, all factors, gold standard.
                     starts at V2 go-live (first V2 month-end snapshot).
track = 'backfill'   price/volume-derived inputs only (momentum, trend, volatility, 52-week
                     high, liquidity, sector momentum, size), reconstructed from yfinance
                     adjusted history for the CURRENT Nifty 500 + Nifty Total Market list.
                     10 years. Survivor-biased: stocks that were in the index in 2018 and
                     were delisted or fell out are missing; stocks that entered later have
                     history from before their inclusion (look-ahead in membership).
track = 'legacy'     the four 2026 snapshots migrated from quant_engine.db (section 10.5),
                     with their defects flagged. Reported, never used for learning.
```

Every evaluation row carries `track`. The learning curve chart plots the live track as solid lines, backfill as dashed, legacy as points. Weight learning reads the live track only; the backfill track may inform the *prior* for price factors with a fixed 0.5 discount on its ICIR (section 6.3).

### 2.6 The as_of calendar (IST) and the results seasons

```
as_of        : last NSE trading day of the month (NSE holiday list is fetched once a year from
               yfinance's ^NSEI trading days; a hard-coded fallback list lives in quant.toml).
run window   : the first Saturday or Sunday after as_of, 06:00-12:00 IST. Prices for as_of are
               final; yfinance 'info' fields are whatever Yahoo shows that weekend.
fundamentals : SEBI LODR Reg 33 - quarterly results within 45 days of quarter end; audited
               annual results within 60 days of FY end (31 March -> 30 May).
               Reg 31 - shareholding pattern within 21 days of quarter end.
               => Q1 (Jun) results land Jul 15-Aug 14; Q2 (Sep) Oct 15-Nov 14;
                  Q3 (Dec) Jan 15-Feb 14; Q4+annual (Mar) Apr 15-May 30.
               The as_of snapshots of end-Aug, end-Nov, end-Feb and end-May therefore carry
               fresh quarterly data; the others carry the same fundamentals with newer prices.
index review : Nifty 500 reconstitution twice a year (effective last trading day of March and
               September, announced ~4 weeks earlier). Membership is stored monthly; the
               review months are where universe churn concentrates.
```

Point-in-time rule for any fundamental value: it is usable at `as_of` only if `known_at <= as_of` (section 4.3). The legacy harness read whatever Yahoo showed "today" and stamped it with today's date, which silently mixed the FY26 annual report into an as_of that predates its publication for any backfill. V2 stores `known_at` explicitly.

---

## 3. Universe & sector taxonomy

### 3.1 Universe

```
Primary universe   : Nifty 500 constituents as published in ind_nifty500list.csv on the
                     run date, keyed by ISIN (Symbol changes on rebrands: e.g. ZOMATO ->
                     ETERNAL; ISIN does not).
Extended universe  : Nifty Total Market (754 names) fetched and stored monthly but NOT scored
                     in v2.0. Purpose: (a) a stock that drops out of the Nifty 500 keeps its
                     price history and labels (no survivorship), (b) v2.1 may widen the
                     scored set to Total Market with a liquidity screen.
Identity           : isin TEXT is the primary key across time. symbol and yahoo_ticker
                     (symbol + '.NS') are attributes with valid_from/valid_to.
Listing date       : from EQUITY_L.csv (DATE OF LISTING) - used to refuse factor values that
                     need more history than exists (e.g. MOM_12_1 for an 8-month-old IPO).
```

Why not widen to Total Market immediately: the bottom 250 names by market cap trade ₹1–5 crore a day; the cost model in section 8 says most of them are un-investable at any size that matters, and the yfinance call budget (7 calls × 750 × 0.5 s ≈ 45 min) would eat the entire runtime target on its own.

### 3.2 Canonical sector source: NSE Sector level (20 values), not the four-level hierarchy

Seed hypothesis 2 asks for the NSE/AMFI four-level classification (Macro-Economic Sector → Sector → Industry → Basic Industry). From a retail-data lens that is the right taxonomy and the wrong dependency:

```
Level              Free, scriptable source?                          Verdict
Macro-Economic     derivable from Sector by a fixed table            fine, derived
Sector (20)        YES - niftyindices constituent CSVs, column       CANONICAL
                   misleadingly named "Industry"
Industry           NSE quote API only (HTTP 403 to scripts);         optional adapter, never
                   NSE "Industry Classification" PDF/XLSX is         a dependency
                   not versioned or stable to parse
Basic Industry     same as Industry                                  same
```

The AMFI list (used by mutual funds for large/mid/small classification) is a *size* classification refreshed half-yearly, not a sector one; it is useful as a size-bucket source (section 3.7) and is a plain XLSX/PDF on amfiindia.com — treat as optional.

So: **canonical = NSE Sector from the constituent CSV, stored monthly, keyed by ISIN**, with Yahoo `sector`/`industry` stored alongside as attributes. Yahoo industry is used for exactly one structural purpose: splitting the 101-name Financial Services sector into banks / lenders-and-insurers / capital-market-and-other, because those three have different accounting (no EBIT, no FCF, regulatory capital) and different return drivers.

### 3.3 Versioned, point-in-time mapping

```sql
-- quant/db/schema.sql (excerpt)

CREATE TABLE IF NOT EXISTS security_master (
  isin            TEXT PRIMARY KEY,
  company_name    TEXT NOT NULL,
  listing_date    TEXT,                 -- from EQUITY_L.csv, 'YYYY-MM-DD'
  face_value      REAL,
  first_seen      TEXT NOT NULL,        -- first as_of this ISIN appeared in any list we store
  last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_history (       -- symbol renames, ticker changes
  isin            TEXT NOT NULL REFERENCES security_master(isin),
  nse_symbol      TEXT NOT NULL,
  yahoo_ticker    TEXT NOT NULL,        -- nse_symbol + '.NS' unless overridden
  valid_from      TEXT NOT NULL,
  valid_to        TEXT,                 -- NULL = current
  PRIMARY KEY (isin, valid_from)
);

CREATE TABLE IF NOT EXISTS universe_membership (  -- one row per (as_of, list, isin)
  as_of           TEXT NOT NULL,
  index_name      TEXT NOT NULL,        -- 'NIFTY500','NIFTYTOTALMARKET','NIFTY200MOM30',
                                        -- 'NIFTY500QUALITY50','NIFTYMIDCAP150','NIFTYSMALLCAP250'
  isin            TEXT NOT NULL,
  nse_symbol      TEXT NOT NULL,
  nse_sector      TEXT NOT NULL,        -- the CSV "Industry" column, verbatim
  source_file     TEXT NOT NULL,        -- 'ind_nifty500list.csv'
  source_sha256   TEXT NOT NULL,        -- hash of the file as downloaded
  fetched_at      TEXT NOT NULL,
  PRIMARY KEY (as_of, index_name, isin)
);

CREATE TABLE IF NOT EXISTS sector_map (           -- point-in-time classification per stock
  isin            TEXT NOT NULL,
  valid_from      TEXT NOT NULL,        -- as_of on which this classification first applied
  valid_to        TEXT,                 -- NULL = current
  nse_sector      TEXT NOT NULL,
  nse_macro       TEXT NOT NULL,        -- derived via sector_group_def
  yahoo_sector    TEXT,
  yahoo_industry  TEXT,
  sector_group    TEXT NOT NULL,        -- one of the 15 groups in 3.5, per sector_group_def version
  group_def_ver   INTEGER NOT NULL,     -- FK sector_group_def.version
  source          TEXT NOT NULL,        -- 'nse_csv' | 'carry_forward' | 'yahoo_crosswalk' | 'manual'
  PRIMARY KEY (isin, valid_from)
);

CREATE TABLE IF NOT EXISTS sector_group_def (     -- the grouping rules themselves, versioned
  version         INTEGER NOT NULL,
  nse_sector      TEXT NOT NULL,
  yahoo_industry_pattern TEXT,          -- NULL = any; else a regex on yahoo_industry
  sector_group    TEXT NOT NULL,
  nse_macro       TEXT NOT NULL,
  min_group_size  INTEGER NOT NULL DEFAULT 15,
  merge_into      TEXT,                 -- fallback group if the group is smaller than min at an as_of
  registered_on   TEXT NOT NULL,
  note            TEXT,
  PRIMARY KEY (version, nse_sector, yahoo_industry_pattern)
);
```

Rules:

```
R1  Every monthly run inserts universe_membership rows for every list it fetched, with the
    file hash. The raw CSVs are also kept under data/raw/niftyindices/YYYY-MM-DD/ (git-ignored,
    hash in manifest; see 4.7).
R2  sector_map gets a new row for an ISIN only when its (nse_sector, yahoo_industry-derived
    group) changes between consecutive as_of dates. valid_to of the old row = new valid_from.
    This is how reclassifications are handled point-in-time: scoring at as_of joins
    sector_map ON valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of).
R3  A reclassification is logged to data_quality_events (kind='sector_reclass') so the
    monthly report lists them. NSE reclassifies a handful of names a year.
R4  Historical (backfill) months before the first stored CSV use the EARLIEST stored
    classification (source='carry_back'), flagged. There is no free point-in-time sector
    history; this is a known, recorded approximation.
R5  Changing sector_group_def creates a new version; old scores keep their group_def_ver.
    Re-scoring history under a new version is an experiment (section 9), never an overwrite.
```

### 3.4 Fallback chain when a stock has no NSE sector at an as_of

```
1. universe_membership row for this as_of (NIFTY500, else NIFTYTOTALMARKET)      source='nse_csv'
2. else the most recent earlier sector_map row for the ISIN                       source='carry_forward'
3. else Yahoo sector via the crosswalk below                                      source='yahoo_crosswalk'
4. else sector_group = 'UNCLASSIFIED'  -> ranked against the whole universe, flagged,
   excluded from the paper portfolio                                              source='none'
```

Yahoo → NSE-sector crosswalk (one-to-many where Yahoo is coarser; the crosswalk picks the modal NSE sector observed for that Yahoo sector among classified names, recomputed monthly and stored in `sector_map` provenance):

```
Yahoo sector              typical NSE sectors observed in the Nifty 500
Financial Services     -> Financial Services
Industrials            -> Capital Goods | Construction | Services
Consumer Cyclical      -> Automobile and Auto Components | Consumer Durables | Consumer Services | Textiles | Realty
Basic Materials        -> Chemicals | Metals & Mining | Construction Materials
Healthcare             -> Healthcare
Technology             -> Information Technology
Consumer Defensive     -> Fast Moving Consumer Goods
Utilities              -> Power
Energy                 -> Oil Gas & Consumable Fuels
Communication Services -> Telecommunication | Media Entertainment & Publication
Real Estate            -> Realty
```

Because it is one-to-many, the crosswalk is a last resort. Expected usage: 0–2 names per month (new listings between index reviews).

### 3.5 Neutralisation groups (version 1)

Twenty NSE sectors is too fine for within-group ranking: Textiles (5), Media (5), Diversified (3) and Telecommunication (10) cannot support quintiles. Version 1 defines 15 groups with a minimum size of 21 today; the counts are from the 2026-09-05 CSV and will drift.

```
group id            NSE sector(s)                                  split rule                       n (2026-09)
G01_FIN_BANK        Financial Services                             yahoo_industry ~ 'Banks'          ~30
G02_FIN_LEND_INS    Financial Services                             yahoo_industry ~ 'Credit Services|Mortgage|Insurance|Financial Conglomerates'  ~45
G03_FIN_MKT_OTHER   Financial Services                             remaining (Capital Markets, Asset Management, Exchanges & Data, Fintech, missing)  ~26
G04_CAPGOODS        Capital Goods                                                                    63
G05_HEALTH          Healthcare                                                                       48
G06_AUTO            Automobile and Auto Components                                                   38
G07_CONS_SERV       Consumer Services + Media Entertainment & Publication                            34
G08_FMCG            Fast Moving Consumer Goods                                                       28
G09_IT              Information Technology                                                           27
G10_CHEM            Chemicals                                                                        26
G11_MATERIALS       Metals & Mining + Construction Materials                                         29
G12_ENERGY_UTIL     Power + Oil Gas & Consumable Fuels                                               34
G13_CONS_DURABLES   Consumer Durables + Textiles                                                     21
G14_CONSTR_REALTY   Construction + Realty                                                            24
G15_SERV_TELECOM    Services + Telecommunication + Diversified                                       27
                                                                                          total      500
```

The three financial counts are approximate (Yahoo industry is not in the stored legacy snapshots, so they could not be counted from the DB); the implementer must print the actual split on the first run and record it in the month-1 report. Merge fallbacks when a group falls below 15 at an as_of: G13 → G07, G14 → G04, G15 → G07, G03 → G02, G10 → G11; any other → 'UNCLASSIFIED' handling. Macro sectors (for reporting only): Financials {G01–G03}, Industrials {G04, G14, G15}, Consumer {G06, G07, G08, G13}, Healthcare {G05}, Technology {G09}, Materials & Energy {G10, G11, G12}.

Why 15 and not the 12 Yahoo sectors: the Yahoo taxonomy puts Bajaj Finance and HDFC Bank together (fine) but also puts Larsen & Toubro, InterGlobe Aviation and Container Corporation together as "Industrials" and puts Titan, Maruti and Zomato together as "Consumer Cyclical". The NSE grouping is closer to how Indian fund managers and sell-side sector teams are organised, which is what sector-relative return dispersion reflects.

### 3.6 Neutralisation method

For each factor f, each as_of, each group g with n_g eligible stocks:

```
1. raw_f(i)      raw factor value (section 5), NaN allowed
2. winsorise     within the WHOLE universe at the 1st/99th percentile (so one demerger does
                 not define a group's extreme); Financials are exempt from factors marked
                 financials_na (they get NaN, not a winsorised value)
3. rank          within group g, average ranks for ties: rk(i) in [1, n_g]
4. gaussianise   z_f(i) = Phi^-1( (rk(i) - 0.5) / n_g )         van der Waerden scores
                 => every group has mean 0, sd ~1 regardless of n_g; a group of 21 and a
                    group of 63 contribute comparably
5. missing       z_f(i) = 0 (group mean) AND imputed_f(i) = 1; a stock with more than
                 max_missing_share (quant.toml, default 0.40) of ACTIVE factors imputed is
                 eligible = 0 for the composite (still stored, still labelled)
6. size control  (v2.1, off by default) regress z_f on ln(mcap) within group, keep residual.
                 v2.0 reports the composite's ln(mcap) correlation instead (a diagnostic).
```

Sector-relative labels (2.2) plus sector-neutral factors mean the composite makes no sector bets. Sector bets are made *only* by the sector sleeve in 3.7, so their contribution is separately measurable.

### 3.7 Sector-level features and the sector sleeve

Sector-level signals are computed per group per as_of, stored in `sector_features`, and enter the final score through a separate, capped sleeve:

```
final_score(i) = (1 - w_sleeve) * composite_neutral(i) + w_sleeve * sector_tilt(g(i))
w_sleeve       = 0.00 at launch (sleeve in shadow); pre-registered cap 0.20
```

```sql
CREATE TABLE IF NOT EXISTS sector_features (
  as_of           TEXT NOT NULL,
  sector_group    TEXT NOT NULL,
  n_members       INTEGER NOT NULL,
  mom_6_1         REAL,     -- equal-weight mean of members' 6-month log TR, skipping last month
  mom_12_1        REAL,
  breadth_200     REAL,     -- share of members with close > SMA200
  breadth_52w     REAL,     -- share of members within 10% of 52-week high
  earn_breadth    REAL,     -- share of members with TTM net profit YoY growth > 0 (known_at <= as_of)
  pe_median       REAL,     -- median trailing P/E of members with positive earnings
  pe_z_5y         REAL,     -- (pe_median - 5y mean of pe_median) / 5y sd   [NULL until 24 months of history]
  turnover_share  REAL,     -- group turnover / universe turnover, 60-day (from bhavcopy)
  deliv_pct_60    REAL,     -- group mean delivery %, 60-day (from bhavcopy)
  fii_flow_proxy  REAL,     -- NULL in v2.0 (see note)
  PRIMARY KEY (as_of, sector_group)
);
```

Sector flows: NSDL publishes FPI sector-wise flows fortnightly, and NSE publishes daily FII/DII cash totals, but neither is machine-readable at sector granularity without fragile PDF parsing. `fii_flow_proxy` is reserved; the retail-feasible proxy for "money moving into a sector" is the change in the sector's share of total turnover plus its delivery percentage, both from bhavcopy. Register `SECT_FLOW_PROXY` as a candidate factor (section 5.4); do not build a PDF parser in year 1.

Sector momentum has real evidence in India (sector rotation is pronounced: PSU banks 2022–24, capital goods 2023–24, IT 2020–21, pharma 2020) but it is a *different bet* from stock selection with a different drawdown profile (sector momentum crashes hard at turns, e.g. March 2020, Jan–Mar 2025 in smallcaps). Keeping it in a capped sleeve that starts at zero weight and must earn promotion is the right posture.

---

## 4. Data layer

### 4.1 Data flow

```
                      MONTHLY (run window after last NSE trading day, IST weekend)
 niftyindices.com ──► universe ──► universe_membership, sector_map, security_master ────────┐
  (8 CSVs, ~1 MB)                                                                             │
                                                                                              │
 yfinance ──────────► prices ────► data/prices/year=YYYY/*.parquet  (adj OHLCV, splits, divs) │
  yf.download                                   │                                              │
  batches of 25,                                ▼                                              │
  1 s sleep                       monthly_total_return.parquet  (committed; 4.7)              │
                                                                                              ▼
 yfinance ──────────► fundamentals ► fundamentals_pit (item, period_end, value, known_at)   snapshot
  Ticker.info,                     ► holdings_pit (inst %, insider %, shares, known_at)     builder
  financials, cashflow,            ► market_snapshot (price, mcap, pe, pb, ev, sma50/200)  (as_of)
  balance_sheet, quarterly_*,                                                                 │
  earnings_dates, shares_full                                                                 │
  0.5 s sleep per call                                                                        │
                                                                                              ▼
 nsearchives.nseindia.com ► bhav ─► data/bhav/YYYY/sec_bhavdata_full_DDMMYYYY.csv.gz     factor_values
  ~22 files/month, 400 KB each          ► liquidity_daily (turnover, trades, deliv %)     (raw + z, per
                                                                                          factor, as_of)
                                                                                              │
                                              ┌───────────────────────────────────────────────┤
                                              ▼                                               ▼
                                     labels (h = 1..36, when matured)                   scores (composite,
                                              │                                          sleeve, final,
                                              ▼                                          eligible, tier)
                                     evaluations, learning_curve, portfolio_paper ◄──────────┘
                                              │
                                              ▼
                                   knowledge/reports/YYYY-MM.md, proposals, decisions, ui/data.js
```

### 4.2 Sources, cadence and call budget

```
source                          what                                cadence   calls/month   time @ throttle
niftyindices CSVs               8 constituent lists                 monthly   8             10 s
yfinance yf.download            adj OHLCV, 550 tickers, 3 months    monthly   22 batches    ~1 min
  (backfill: 10 years, once)    same, period='max'                  once      22 batches    ~5 min + retries
yfinance Ticker per stock       info; financials; balance_sheet;    monthly   7 x 550       ~35 min at 0.5 s
                                cashflow; quarterly_financials;                              (the dominant cost)
                                earnings_dates; get_shares_full
nsearchives bhavcopy            sec_bhavdata_full for the month     monthly   ~22           30 s
nsearchives EQUITY_L.csv        listing dates / ISIN                monthly   1             2 s
Total                                                                                        ~40 min
```

Throttle rules (carried over from AGENTS.md prime directive 1, extended): `>= 0.5 s` between per-ticker yfinance calls; `yf.download` batches of ≤ 25 tickers with `threads=False` and `>= 1.0 s` between batches; exponential back-off starting at 30 s on HTTP 429 with a hard stop after 5 retries, after which the run is marked `partial` and no `scores` rows are written. Quarterly statements are fetched for every stock only in the four post-results months (Aug, Nov, Feb, May); other months fetch `info` + prices only, which cuts the dominant cost to ~10 min.

### 4.3 Point-in-time storage with `as_of` and `known_at`

Two timestamps on every stored input:

```
period_end   the accounting date the value describes (FY end, quarter end), or the price date
known_at     the first date the value could have been known publicly
fetched_at   when we actually pulled it (provenance; never used in scoring)
```

```sql
CREATE TABLE IF NOT EXISTS fundamentals_pit (
  isin            TEXT NOT NULL,
  statement       TEXT NOT NULL,      -- 'income_a','income_q','balance_a','balance_q','cashflow_a'
  item            TEXT NOT NULL,      -- yfinance row label, verbatim: 'Total Revenue','EBIT','Net Income',
                                      -- 'Diluted EPS','Operating Cash Flow','Capital Expenditure',
                                      -- 'Total Assets','Current Liabilities','Total Debt','Cash And Cash Equivalents'...
  period_end      TEXT NOT NULL,      -- 'YYYY-MM-DD'
  value           REAL,               -- rupees (yfinance native), NULL allowed
  known_at        TEXT NOT NULL,
  known_at_source TEXT NOT NULL,      -- 'earnings_dates' | 'lodr_45d' | 'lodr_60d' | 'first_fetch'
  fetched_at      TEXT NOT NULL,
  source          TEXT NOT NULL DEFAULT 'yfinance',
  PRIMARY KEY (isin, statement, item, period_end)
);

CREATE TABLE IF NOT EXISTS holdings_pit (
  isin            TEXT NOT NULL,
  period_end      TEXT NOT NULL,      -- quarter end the pattern refers to (best effort)
  inst_pct        REAL,               -- info.heldPercentInstitutions (fraction)
  insider_pct     REAL,               -- info.heldPercentInsiders (promoter proxy, fraction)
  shares_out      REAL,               -- get_shares_full latest <= as_of
  known_at        TEXT NOT NULL,
  known_at_source TEXT NOT NULL,      -- 'lodr_21d' | 'first_fetch'
  fetched_at      TEXT NOT NULL,
  PRIMARY KEY (isin, period_end)
);

CREATE TABLE IF NOT EXISTS market_snapshot (     -- what Yahoo 'info' said at the run, per as_of
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  close_adj       REAL NOT NULL,      -- from the price store, as_of trading day
  close_raw       REAL NOT NULL,      -- unadjusted, for cross-checks and UI
  mcap_inr        REAL,               -- info.marketCap (rupees)
  shares_out      REAL,
  trailing_pe     REAL, forward_pe REAL, price_to_book REAL,
  ev_inr          REAL, ev_to_ebitda REAL,
  dividend_rate   REAL,               -- rupees/share (unambiguous); NEVER store dividendYield
  sma50 REAL, sma200 REAL, beta REAL,
  yahoo_sector TEXT, yahoo_industry TEXT,
  info_json       TEXT,               -- full info dict, for forensics
  fetched_at      TEXT NOT NULL,
  PRIMARY KEY (as_of, isin)
);
```

`known_at` assignment, in order of preference:

```
1. earnings_dates has a row with Reported EPS not NaN whose date is within 60 days after
   period_end                               -> known_at = that date (IST date), source 'earnings_dates'
2. quarterly statement                      -> known_at = period_end + 45 days, source 'lodr_45d'
3. annual statement (FY ends 31 Mar)        -> known_at = period_end + 60 days, source 'lodr_60d'
4. holdings                                 -> known_at = quarter_end + 21 days, source 'lodr_21d'
5. anything first seen by us AFTER the rule-based date (Yahoo restated it late)
                                            -> known_at = max(rule date, first fetched_at), source 'first_fetch'
Rule 5 is the conservative choice: if we cannot prove it was public earlier, we assume it was not.
```

At scoring time, factor inputs are read with `known_at <= as_of` and the latest `period_end` satisfying that. This is what makes a fundamental factor value at as_of = 2026-08-31 use FY26 annual data (known by 30 May) and Q1 FY27 quarterly data only for companies that reported by 31 August.

### 4.4 Corporate actions and total returns

```
Price store         : yfinance adjusted OHLCV (auto_adjust=True) = total-return price series.
                      Verified: splits (ZFCVINDIA 6:1) and cash dividends (HEROMOTOCO Rs 75) are
                      back-adjusted. Bonus issues appear as splits in Yahoo's model. Rights issues
                      and demergers are adjusted by Yahoo with variable quality.
Corporate actions   : Ticker.splits and Ticker.dividends stored monthly into corporate_actions.
Unadjusted check    : bhavcopy CLOSE_PRICE is the exchange's unadjusted close. For every
                      stock-month we compute
                        r_unadj   = CLOSE(d_1) * split_factor(as_of..d_1) / CLOSE(as_of) - 1
                                    + sum(dividends ex in (as_of, d_1]) / CLOSE(as_of)
                        r_adj     = P_adj(d_1) / P_adj(as_of) - 1
                      and flag |r_adj - r_unadj| > 0.02 as 'ca_mismatch'.
Quarantine          : a stock-month flagged ca_mismatch has its 1-month label set NULL with
                      reason; longer labels that span the month are NULL too until a human or
                      LLM reviewer records the corporate action in corporate_actions with
                      kind in ('demerger','rights','scheme') and an explicit adjustment factor,
                      after which labels are recomputed. This REPLACES the legacy +-60% filter,
                      which threw away real moves (Indian small caps do move 60% in a month:
                      upper circuits of 5%/day compound to +200% in 25 sessions).
```

```sql
CREATE TABLE IF NOT EXISTS corporate_actions (
  isin            TEXT NOT NULL,
  ex_date         TEXT NOT NULL,
  kind            TEXT NOT NULL,      -- 'split','bonus','dividend','rights','demerger','scheme','buyback','manual_adj'
  ratio           REAL,               -- split/bonus factor (6.0 for 6:1), NULL for cash
  cash_per_share  REAL,               -- dividends
  adj_factor      REAL,               -- multiplicative factor applied to pre-ex prices; NULL = Yahoo's
  source          TEXT NOT NULL,      -- 'yfinance' | 'manual' | 'bhav_reconcile'
  note            TEXT,
  recorded_at     TEXT NOT NULL,
  PRIMARY KEY (isin, ex_date, kind)
);
```

Demergers are the one class where free data is weak (Jio Financial out of Reliance 2023, ITC Hotels 2025, Tata Motors CV/PV 2025). Yahoo typically adjusts the parent's history downward on the ex-date so the holder's total return is understated by the spun-out value. The monthly report lists every `ca_mismatch`; a manual `adj_factor` fixes each within the month. Expect 2–6 a year in the Nifty 500.

### 4.5 Backfill plan for price history

```
Step 1  once   : yf.download(all current Nifty Total Market tickers, period='max',
                 auto_adjust=True, actions=True), batches of 25, threads=False, 1 s sleep.
                 ~754 tickers -> ~31 batches -> ~5-10 min incl. retries. Write Parquet per year.
Step 2  once   : Ticker.splits/.dividends for each (0.5 s each, ~7 min) -> corporate_actions.
Step 3  once   : index/ETF series: ^CRSLDX, ^NSEI, ^CNX200, NIFTYBEES.NS, MID150BEES.NS,
                 MOM30IETF.NS, QUAL30IETF.NS, LOWVOLIETF.NS -> benchmarks_daily.
Step 4  monthly: incremental download of the last 90 days per ticker (overlap re-verifies
                 adjustments: if the adjusted close for an OLD date changed by > 0.1% since
                 the last pull, a new corporate action occurred -> re-download 'max' for
                 that ticker, log data_quality_events kind='readjustment').
Step 5  monthly: bhavcopy for the month's trading days -> liquidity_daily. Optional one-off
                 backfill of bhavcopy for 24 months (~500 files, 200 MB) to seed liquidity
                 tiers and delivery factors before go-live.
```

What the backfill gives and does not give:

```
gives      : 10 years of daily total-return prices for every stock CURRENTLY in Nifty Total
             Market (survivors), sufficient for the 'backfill' evidence track of price factors
             and for benchmark reconstruction.
gives      : realistic liquidity tiers from 2 years of bhavcopy.
does NOT   : delisted / merged names (yfinance drops them), so backfill momentum/low-vol ICs
             are biased upward by an unknown amount (typical literature estimate: 1-3 IC
             points at 12 months for momentum in small caps). Recorded as a known bias on the
             track, never corrected by guesswork.
does NOT   : point-in-time fundamentals. yfinance exposes 4 annual and 6 quarterly periods
             with no history of restatements. Fundamental factors have NO backfill track.
             (A future optional adapter could ingest a purchased point-in-time dataset; the
             schema above already has known_at for it.)
```

### 4.6 Data-quality flags and gates

Two levels. **Row flags** travel with the value (`factor_values.flags`, `market_snapshot.info_json`). **Run gates** decide whether the month's `scores` are written at all.

```
Run gates (hard; any failure => run status 'blocked', no scores, no labels, report still written)
G1  universe_rows       Nifty 500 CSV parsed >= 480 rows AND sha256 differs from last month OR
                        month is not an index-review month (a stale CSV in a review month is a failure)
G2  price_coverage      adjusted close on as_of present for >= 98% of universe
G3  price_freshness     max price date in store >= as_of (no stale store)
G4  ca_reconcile        ca_mismatch count <= 10 (more => Yahoo re-adjusted en masse or bhav broken)
G5  fundamentals_cov    for each ACTIVE fundamental factor, non-NULL raw >= 85% of eligible
                        (non-financial) universe
G6  unit_sanity         0 <= dividend_rate/close <= 0.25; 0 <= debt/equity <= 50;
                        -1000 < trailing_pe < 1000; 0 <= inst_pct <= 1; mcap_inr > 0;
                        violations <= 5 stocks each (violators get flagged, not the run)
G7  duplicates          zero duplicate (as_of, isin) in market_snapshot / factor_values
G8  leakage_smoke       shuffle test (7.5) on this month's scores vs LAST month's matured
                        1-month labels: |IC| < 0.01 over 20 permutations
G9  schema_version      quant.toml schema_version == db schema_version

Row flags (soft; stored, counted, reported)
  imputed:<factor>        z set to group mean
  proxied:<field>         e.g. roce_from_roe, ebit_from_operating_income
  stale:<field>:<days>    fundamental older than 400 days (annual) / 200 days (quarterly)
  ca_mismatch             see 4.4
  low_history:<factor>    fewer trading days than the factor's lookback (IPO)
  circuit_days:<n>        days in the month with high == low (locked circuit), from bhavcopy
  sector_reclass          sector_group changed at this as_of
  unit_fix:<field>        a value was rescaled by a unit heuristic (must be 0 after month 3)
```

Every gate result is stored in `run_log` and `data_quality_events`; the pass rate is a dashboard metric (1.3).

### 4.7 Storage format and what is committed to git

Decision: SQLite for all state that is small or must be queried relationally; Parquet for daily bars; a small committed Parquet of month-end total-return prices so evaluation is reproducible from a fresh clone without touching the network.

```
path                                        format    size (10 y, 750 names)   git
quant_engine.db                             SQLite    ~30-80 MB by year 3      committed (as today)
data/prices/year=YYYY/prices.parquet        Parquet   ~25 MB total (zstd)      NOT committed
data/bhav/YYYY/sec_bhavdata_full_*.csv.gz   gzip CSV  ~70 MB / year            NOT committed
data/raw/niftyindices/YYYY-MM-DD/*.csv      CSV       ~1 MB / month            NOT committed
data/monthly_total_return.parquet           Parquet   < 1 MB (750 x ~130)      COMMITTED
data/benchmarks_monthly.parquet             Parquet   < 100 KB                 COMMITTED
data/MANIFEST.json                          JSON      list of every non-committed file with
                                                      sha256, rows, first/last date, fetched_at   COMMITTED
```

Justification: git-lfs is not installed on the owner's machine and the brief forbids assuming paid services; a 25 MB Parquet in plain git would be re-committed on every monthly append and bloat history. The committed monthly matrix plus `MANIFEST.json` means `python -m quant prices --rebuild` regenerates the store from yfinance and verifies row counts against the manifest; the evaluation layer (labels, IC, learning curve) reads only `monthly_total_return.parquet` + SQLite, so a clone can reproduce every reported number offline. `pyarrow` is added to `requirements.txt` (pure pip wheel, no system deps). If pyarrow proves unavailable in some environment, the fallback is gzip CSV with identical columns (`quant.toml: storage.format = 'csv.gz'`); the code path is the same pandas call.

`quant_engine.db` growth estimate: `factor_values` at 500 stocks × 20 factors × 12 months ≈ 120k rows/year ≈ 10 MB/year uncompressed; fine for git for a decade. `market_snapshot.info_json` is the largest column (~4 KB × 500 × 12 ≈ 24 MB/year); it is stored gzip-compressed as BLOB (`zlib`), which brings it to ~5 MB/year.

---

## 5. Factor library

### 5.1 Plugin contract

Every factor is one Python module under `quant/factors/` exposing a single `Factor` instance. Metadata lives in code (so it is versioned with the formula) and is mirrored into `factor_registry` on `python -m quant factors sync`.

```python
# quant/factors/base.py
from dataclasses import dataclass, field
from typing import Callable, Literal
import pandas as pd

Family = Literal["MOMENTUM", "QUALITY", "GROWTH", "VALUE", "FLOW", "SECTOR", "CONTROL"]
Status = Literal["proposed", "registered", "shadow", "active", "probation", "retired"]

@dataclass(frozen=True)
class FactorSpec:
    factor_id: str                 # 'MOM_12_1' - stable, never reused
    version: int                   # bump on ANY formula/input change -> new factor_versions row
    family: Family
    hypothesis: str                # one paragraph, plain English, written BEFORE evaluation
    expected_sign: Literal[+1, -1] # sign of IC vs L_12 the author expects
    horizon_months: int            # horizon the hypothesis is about (1, 3, 6 or 12)
    inputs: tuple[str, ...]        # canonical input names, e.g. ('close_adj', 'ebit_a', 'total_assets_a')
    lookback_days: int             # trading days of history required (0 for point values)
    financials_na: bool            # True => Financial Services groups get NaN (not scored on it)
    higher_is_better: bool         # orientation of the RAW value (before expected_sign)
    evidence: str                  # 3-6 lines: India-specific evidence and the caveats
    registered_on: str             # 'YYYY-MM-DD'
    preregistration_id: str        # 'H-2026-001' (hypotheses table)

@dataclass
class Factor:
    spec: FactorSpec
    compute: Callable[["Panel", str], pd.Series]   # (panel, as_of) -> raw value indexed by isin

    def __call__(self, panel: "Panel", as_of: str) -> pd.Series:
        s = self.compute(panel, as_of)
        assert s.index.name == "isin"
        return s.astype("float64")
```

```python
# quant/factors/mom_12_1.py  (illustrative, complete)
from quant.factors.base import Factor, FactorSpec
import numpy as np

SPEC = FactorSpec(
    factor_id="MOM_12_1", version=1, family="MOMENTUM",
    hypothesis=("Stocks with the highest total return over the past 12 months, excluding the most "
                "recent month, continue to outperform their sector peers over the next 6-12 months, "
                "because information diffuses slowly and Indian institutional flows chase winners."),
    expected_sign=+1, horizon_months=12,
    inputs=("close_adj",), lookback_days=273, financials_na=False, higher_is_better=True,
    evidence=("Nifty 200 Momentum 30 index (NSE, base 2005) has outperformed Nifty 200 over most 5-year "
              "windows since 2010 before costs; IIMA four-factor library (Agarwalla, Jacob, Varma 2013) "
              "reports a positive WML premium in India 1994-2013; momentum crashes in 2008-09 and Mar 2020. "
              "Caveat: survivor-biased backfill overstates it; small-cap momentum reverses hard at turns."),
    registered_on="2026-09-05", preregistration_id="H-2026-001",
)

def compute(panel, as_of):
    px = panel.adj_close_window(as_of, days=273)           # DataFrame: index trading days, columns isin
    p_t1 = px.iloc[-22]                                     # ~1 month before as_of (skip)
    p_t12 = px.iloc[0]
    raw = np.log(p_t1 / p_t12)
    raw[px.notna().sum() < 240] = np.nan                    # low_history -> NaN, flagged upstream
    return raw.rename_axis("isin")

FACTOR = Factor(SPEC, compute)
```

`Panel` (in `quant/data/panel.py`) is the only way a factor reads data; it exposes `adj_close_window`, `raw_close_window`, `volume_window`, `turnover_window`, `fundamental(item, statement, as_of, n_periods)`, `holding(field, as_of, n_quarters)`, `market(field, as_of)` and enforces `known_at <= as_of` internally. A factor cannot bypass point-in-time discipline because it has no other data access. This is the single most important architectural rule in the document.

### 5.2 Shared transforms

```
quant/factors/transform.py
  winsorise(s, lo=0.01, hi=0.99)                 universe-wide quantile clip
  group_gauss_rank(s, groups)                    steps 3-4 of 3.6
  ttm(quarterly_series)                          sum of last 4 quarters (NaN if < 4)
  cagr(series_oldest_to_newest, years)           NaN if either endpoint <= 0 (never impute)
  safe_div(a, b)                                 NaN if b <= 0 or missing
```

No factor may impute a growth rate, an ROE or a yield. If the inputs are not there, the value is NaN and the row is flagged. The legacy code's `+15%` default growth and `None -> 0` ROE are precisely the errors this rule prevents.

### 5.3 Initial factor list (version 1, registered 2026-09-05)

`dir` is the expected sign of the Spearman IC against `L_12`. `fin` = NaN for financial groups. All fundamentals are latest values with `known_at <= as_of`; `_a` = annual, `_q` = quarterly.

```
id               family    formula (raw)                                                     dir  h   fin  status@launch
MOM_12_1         MOMENTUM  ln(P_adj[t-22] / P_adj[t-273])                                    +    12  no   active
TREND_200        MOMENTUM  ln(P_adj[t] / mean(P_adj[t-200..t]))   (replaces death cross)     +    3   no   active
HIGH_52W         MOMENTUM  P_adj[t] / max(P_adj[t-252..t])                                   +    6   no   shadow
REV_1M           MOMENTUM  -ln(P_adj[t] / P_adj[t-22])                                       +    1   no   shadow
LOWVOL_252       QUALITY   -std(daily ln returns, 252 d)                                     +    12  no   active
ROCE_TTM         QUALITY   ttm(EBIT_q) / (TotalAssets_a - CurrentLiabilities_a); fallback    +    12  yes  active
                           EBIT_a / (TA_a - CL_a) flagged proxied:roce_annual
CASH_CONV_3Y     QUALITY   sum(OCF_a, 3 y) / sum(NetIncome_a, 3 y); NaN if denominator <= 0  +    12  yes  active
LEVERAGE         QUALITY   -(TotalDebt_a - Cash_a) / ttm(EBITDA) ; NaN if EBITDA <= 0        +    12  yes  active
ACCRUALS         QUALITY   -(NetIncome_a - OCF_a) / TotalAssets_a                            +    12  yes  shadow
ROE_STAB_3Y      QUALITY   mean(ROE_a, 3 y) / std(ROE_a, 3 y); ROE_a = NI_a / Equity_a       +    12  no   shadow
EPS_G_3Y         GROWTH    cagr(DilutedEPS_a, 3 y)  (NaN if either endpoint <= 0)            +    12  no   active
EARN_MOM_Q       GROWTH    (ttm(NI_q) - ttm(NI_q lagged 4 q)) / |ttm(NI_q lagged 4 q)|       +    3   no   active
REV_G_3Y         GROWTH    cagr(TotalRevenue_a, 3 y)                                         +    12  no   shadow
FCF_YIELD        VALUE     mean(OCF_a + CapEx_a, 3 y) / EV      (CapEx negative in yfinance) +    12  yes  active
EY_EBIT          VALUE     ttm(EBIT_q) / EV                                                  +    12  yes  active
PB_INV           VALUE     -ln(price_to_book)                                                +    12  no   shadow
DIV_YIELD        VALUE     dividend_rate / close_raw    (rupees/share over price)            +    12  no   shadow
INST_CHG_Q       FLOW      inst_pct[latest] - inst_pct[latest - 1 quarter]  (own history)    +    6   no   shadow (needs 2 q of live history)
DELIV_PCT_60     FLOW      mean(DELIV_PER, 60 d) from bhavcopy                               +    3   no   shadow
PROMOTER_PLEDGE  FLOW      pledged / promoter holding          (NO free source yet)           -    12  no   proposed
SECT_MOM_6       SECTOR    sector_features.mom_6_1 of the stock's group                      +    6   no   shadow (sleeve)
SECT_BREADTH     SECTOR    sector_features.breadth_200                                       +    3   no   shadow (sleeve)
SIZE             CONTROL   ln(mcap_inr)                                                      n/a  -   no   control (never weighted)
LIQ_TURNOVER     CONTROL   ln(median daily TURNOVER_LACS, 60 d)                              n/a  -   no   control + screen
BETA_252         CONTROL   slope of daily returns on ^CRSLDX, 252 d                          n/a  -   no   control
```

Active at launch: 10 factors in 4 families (Momentum 2, Quality 4, Growth 2, Value 2). Flow has no active member at launch because `INST_CHG_Q` needs two quarters of our own point-in-time holdings history before its first value is honest; it is the first factor scheduled for promotion review (month 7).

Why these, from the India lens:

```
MOMENTUM   Strongest and most consistent published evidence in India (NSE factor indices; IIMA
           library; several SSRN studies 2010-2023). Retail flows and index-inclusion buying
           reinforce it. Crash risk is real; that is a portfolio-construction issue, not a
           reason to exclude the factor.
QUALITY    Second strongest. India's quality premium (high ROCE, low leverage, stable earnings)
           has been persistent and is the basis of most long-only "compounder" franchises.
           Nifty 100 Quality 30 / Nifty 500 Quality 50 have long-run excess returns before
           costs. Low volatility in India behaves like a quality proxy.
GROWTH     Earnings momentum / post-earnings drift is well documented in India because analyst
           coverage below the top 150 is thin and results are absorbed slowly. 3-year EPS CAGR is
           the "compounder" half; TTM YoY change is the "inflection" half.
VALUE      Weakest Indian evidence since ~2012 for simple P/E and P/B; cheap in India has often
           meant PSU, commodity or governance-discounted. Cash-flow-based value (FCF yield,
           EBIT/EV) has held up better and is less exposed to accounting choices. Kept active
           but the family will be watched for retirement first.
FLOW       Institutional accumulation is a live driver in mid caps, but the only free field
           (heldPercentInstitutions) has no timestamp; we create the timestamp by storing it
           monthly ourselves. Delivery percentage is NSE-specific and cheap; evidence is
           anecdotal, hence shadow.
```

### 5.4 Candidate factors (proposed, not yet registered) with the data problem named

```
PROMOTER_PLEDGE     BSE/NSE shareholding pattern pages; needs a scraper with a browser session -> blocked
PROMOTER_BUY        insider trading disclosures (SAST/PIT), NSE 'corporates' CSV needs session -> blocked
BULK_BLOCK_DEALS    nsearchives bulk.csv / block.csv are fetchable: count of net institutional buys
                    per stock in 60 d. Register in month 3 once 60 d of bulk.csv is stored.
SECT_FLOW_PROXY     change in group's turnover share + delivery % (3.7). Register in month 3.
ANALYST_REV         forward EPS revisions - Yahoo forwardPE exists but has no history; we could
                    store it monthly and use its change. Register in month 6 after 6 months stored.
AMFI_SIZE_MIGRATION large->mid->small AMFI reclassification (semi-annual XLSX) as a flow event.
```

### 5.5 Pre-registration record

Nothing is computed against out-of-sample labels until this row exists. The record is a row in `hypotheses` (DDL in 9.2) and a markdown file `knowledge/hypotheses/H-YYYY-NNN.md` with this template:

```
Title      : H-2026-001  MOM_12_1 v1
Registered on   : 2026-09-05      by : owner (human)
Family / horizon: MOMENTUM / 12 months
Formula         : ln(P_adj[t-22] / P_adj[t-273]), sector-gauss-ranked within 15 groups
Expected sign   : +
Success criteria (pre-committed):
  - live-track IC_12 cumulative mean >= +0.02 with HAC t >= 1.5 after 24 live months, OR
  - live-track IC_3 cumulative mean >= +0.02 with HAC t >= 2.0 after 24 live months
Failure criteria:
  - IC_12 cumulative mean <= 0 after 24 live months with 90% CI upper bound < +0.02
Correlation constraint: |rho| with any ACTIVE factor's z <= 0.70 (checked monthly)
Why it might fail in India: momentum crashes at regime turns; small-cap circuit-locked names
  produce fake momentum; index-inclusion front-running.
What would make me retire it: 24-month rolling IC_12 < 0 with HAC t <= -1.5.
Backfill prior: allowed (price-only). Discount 0.5.
```

### 5.6 Status lifecycle

```
 proposed ──register──► registered ──first live month──► shadow ──promote──► active
                          (hypothesis row,                 (computed,           (weighted in
                           no OOS data yet)                 stored, IC          composite)
                                                            tracked,              │
                                                            weight 0)             │ 24-m rolling IC
                                                               │                  │ fails (9.4)
                                                               │ fails 9.4        ▼
                                                               ▼               probation ──6 m──► retired
                                                            retired            (weight halved,    (weight 0,
                                                                                still stored)     still stored)
Rules
  - Every transition is a decisions row + ADR file; the factor's rows in factor_values are
    never deleted (retired factors keep being computed and stored for 24 more months so that
    "we retired it and then it worked" is measurable).
  - A formula change = new version = back to 'registered' for the new version; the old version
    keeps its status until the new one is promoted (both are computed in parallel).
  - Maximum ACTIVE factors: 14 (quant.toml). Maximum shadow: 12. Beyond that, retire first.
```

### 5.7 Mapping the eight legacy factors

```
legacy factor      what it was                                       V2 disposition
quality_score      bucketed ROCE + FCF conversion                    -> ROCE_TTM + CASH_CONV_3Y (continuous)
growth_score       bucketed composite CAGR with +15% imputation       -> EPS_G_3Y + REV_G_3Y + EARN_MOM_Q (no imputation)
valuation_score    DCF margin of safety + PEG, 63% scored 0           -> FCF_YIELD + EY_EBIT (DCF retired as a factor;
                                                                        DCF value may remain a UI explainer)
risk_score         hand-typed ticker lists, 85% constant              -> RETIRED. Not a factor; hindsight lists.
moat_score         18 hand-picked names, 96% constant                 -> RETIRED. Same reason. ROCE stability is the
                                                                        measurable moat proxy (ROE_STAB_3Y shadow).
bs_score           two debt thresholds, 84% constant                  -> LEVERAGE (continuous, EBITDA-scaled)
cap_alloc_score    dividend yield buckets (unit bug)                  -> DIV_YIELD shadow; payout ratio dropped
smart_money_score  holdings level + delta buckets                     -> INST_CHG_Q shadow (delta only; level is a
                                                                        size proxy)
trap_score         0/20/40 penalties, multiplier 0.2-1.0             -> RETIRED as multiplier. Its components
                                                                        (negative FCF with profits, D/E, profit
                                                                        decline) are already continuous inputs.
momentum_multiplier death cross 0/0.8/1.0                             -> TREND_200 (continuous, weighted, learnable)
headline sentiment  keyword count, up to +10 pts outside weights     -> RETIRED. No free reliable source.
```

---

## 6. Scoring model & weight learning

### 6.1 Composite

```
For stock i at as_of t, with active factors f in F, families k in K, z_f(i) from 3.6:

  family_score_k(i) = sum_{f in k} w_f|k * z_f(i)            weights within family sum to 1
  composite(i)      = sum_{k in K}  W_k * family_score_k(i)  family weights sum to 1
  composite_neutral = group_gauss_rank(composite)            re-ranked within group so the
                                                             output is again N(0,1) per group
  final(i)          = (1 - w_sleeve) * composite_neutral(i) + w_sleeve * sector_tilt(g(i))
  rank(i)           = 1..N over eligible stocks by final, descending; decile, quintile stored
  eligible(i)       = passes liquidity screen (8.2) AND data-quality (3.6 step 5) AND not
                      'UNCLASSIFIED'; ineligible stocks get final and rank_all but rank = NULL
```

Missing z (imputed = group mean = 0) contributes nothing, which is the correct neutral choice for a rank composite; a stock missing an entire family gets that family's score = 0 and a flag, and is ineligible if > 40% of active factors are missing.

### 6.2 Baseline (permanent): hierarchical equal weight

```
model_id = 'EW_HIER_v1'    status: champion at launch, permanent reference forever
  W_k     = 1 / |K|            for every family with at least one active factor
  w_f|k   = 1 / |k|            equal within family
  w_sleeve = 0
At launch: K = {MOMENTUM, QUALITY, GROWTH, VALUE}, W_k = 0.25 each;
  Momentum: MOM_12_1 0.125, TREND_200 0.125
  Quality : LOWVOL_252, ROCE_TTM, CASH_CONV_3Y, LEVERAGE  0.0625 each
  Growth  : EPS_G_3Y 0.125, EARN_MOM_Q 0.125
  Value   : FCF_YIELD 0.125, EY_EBIT 0.125
```

Why hierarchical rather than flat equal weight: with 4 quality factors and 2 momentum factors, flat 1/10 weights would make the composite 40% quality by construction, and any later change in the factor count would silently re-weight families. Hierarchical equal weight makes "add a factor to a family" a within-family change.

Two more permanent references are computed every month but never promoted:

```
'EW_FLAT_v1'    flat 1/|F| weights (the brief's literal equal-weight baseline; reported for honesty)
'INDIA_PRIOR_v1' family weights fixed and pre-registered 2026-09-05:
                 Momentum 0.30, Quality 0.30, Growth 0.25, Value 0.15 (equal within family).
                 This is the practitioner's prior. It is a CHALLENGER, not the champion, because
                 it is an opinion and the brief requires the opinion to earn its place.
```

### 6.3 Learning rule (challenger `IC_SHRUNK_v1`)

Recomputed at quarter ends (Mar, Jun, Sep, Dec as_of), using the live track only, primary label `L_12`:

```
inputs, per factor f (over all live months m with a matured 12-month label):
  IC_f(m)        Spearman(z_f at m, L_12 at m) over eligible stocks
  ICbar_f        mean_m IC_f(m)
  s_f            HAC standard error of ICbar_f (Newey-West, Bartlett, lag 11)
  n_months       count of m
  n_eff          n_months / 12                         independent 12-month observations
  ICIR_f         ICbar_f / (s_f * sqrt(n_months))      an ICIR on the HAC scale

shrinkage (James-Stein flavour):
  lambda         = n_eff / (n_eff + n0),   n0 = 6      (quant.toml; 6 independent years to
                                                        reach half-weight on the evidence)
  prior_f        = 0.5 * ICIR_f(backfill track)  if the factor has a backfill track, else 0
  score_f        = lambda * ICIR_f(live) + (1 - lambda) * prior_f

update (multiplicative, within family; families updated the same way on family_score ICs):
  w_f|k  <- w_f|k^EW * exp(kappa * score_f)      kappa = 0.5
  clip   w_f|k in [0.5, 2.0] * w_f|k^EW           no factor may be more than doubled or halved
  renormalise within family; same for W_k with the same bounds and kappa
  a factor with expected_sign * ICbar_f < 0 gets score_f = min(score_f, 0) (never rewarded
  for working the wrong way; that is a retirement question, 9.4)
```

Worked numbers so the reader can see how slowly this moves:

```
month 24 live: n_months = 12, n_eff = 1.0, lambda = 0.14   (matured labels = months live - 12)
   a factor with live ICIR 1.0 and backfill ICIR 0.8 -> score = 0.14*1.0 + 0.86*0.4 = 0.48
   -> weight multiplier exp(0.5*0.48) = 1.27  (before renormalisation; then clipped by others)
month 60 live: n_eff = 4.0, lambda = 0.40 -> live evidence starts to dominate the prior
month 120    : n_eff = 9.0, lambda = 0.60
```

That is by design. Under this rule the learned challenger will look almost identical to the champion for the first two years. If that feels too slow, the honest alternative is not a faster rule; it is better (purchased) point-in-time data that lengthens the live-equivalent history.

### 6.4 Bounds and invariants (replace the legacy 5%–30% rule)

```
I1  sum of family weights = 1.000 exactly (3 dp; rounding residue to the largest family)
I2  sum of within-family weights = 1.000 per family
I3  every active factor weight in [0.5, 2.0] x its hierarchical equal weight
I4  w_sleeve in [0, 0.20]
I5  a challenger's weights are written to model_versions, never applied to the live
    scoring path until a decisions row promotes it (9.7)
I6  the scoring path records model_id and model_version on every scores row, so any
    month can be re-attributed exactly
I7  the champion's scores and every challenger's scores are stored every month
    (scores has one row per (as_of, isin, model_id))
```

### 6.5 Minimum evidence before deviating from equal weights

Seed hypothesis 4's "≥ 12 non-overlapping periods" is replaced by three conditions that must all hold before `IC_SHRUNK_v1` (or `INDIA_PRIOR_v1`) may replace `EW_HIER_v1` as champion:

```
E1  >= 36 live months with matured L_12 for the composite (i.e. >= 48 months after go-live)
E2  the challenger's composite IC_12 minus the champion's, as a monthly series over the shadow
    period, has mean > +0.01 and HAC (lag 11) t >= 1.5;  AND the same difference on IC_3 has
    mean > 0 (a challenger that helps at 12 m and hurts at 3 m is a costs problem)
E3  net-of-cost paper-portfolio excess return of challenger over champion > 0 over the same
    window (8.5)
```

E1 makes the first possible champion change the month-48 review. Until then the loop still learns: which factors to retire (9.4 needs only 24 months), which data fields to fix, what costs are. Writing this down now prevents the year-2 temptation to "just let it learn".

### 6.6 Champion / challenger set at launch, and how a model is promoted

```
model_id          role        weights                      scored monthly   paper portfolio   eligible for promotion
EW_HIER_v1        champion    6.2                          yes              yes (live)        n/a
EW_FLAT_v1        reference   flat                         yes              yes (paper)       never
INDIA_PRIOR_v1    challenger  6.2 prior                    yes              yes (paper)       after E1-E3
IC_SHRUNK_v1      challenger  6.3, refit quarterly         yes              yes (paper)       after E1-E3
MOM_ONLY_v1       reference   MOM_12_1 + TREND_200 only    yes              yes (paper)       never (a diagnostic:
                                                                                              "is the composite better
                                                                                              than just momentum?")
```

Promotion is a `decisions` row (kind='model_promotion') created by `python -m quant approve`. The demoted champion continues to be scored as a reference forever; the alpha scoreboard always shows the original `EW_HIER_v1` line so a promotion that later fails is visible.

### 6.7 The death-cross hard kill, and which hard filters remain

The 0.0× multiplier is removed. `TREND_200` carries the same information continuously and can be weighted, learned, retired. The red-team finding that the multiplier "alone" scored +0.125 in one month and −0.033 in another is exactly the behaviour of a momentum-family factor, and it now competes for weight like one. The UI keeps a "below 200-day average" badge as an explainer, not a filter.

Hard filters that remain, and what happens to filtered names:

```
filter                 rule                                          filtered names are...
liquidity (8.2)        60-d median turnover < Rs 3 crore OR            scored, stored, labelled; eligible = 0;
                       > 10 zero-volume days in 60 d OR                evaluated as a separate cohort
                       > 15 circuit-locked days in 60 d                 ('excluded_liquidity') every month
data quality (3.6)     > 40% of active factors imputed OR               same, cohort 'excluded_data'
                       no price on as_of
classification (3.4)   sector_group = 'UNCLASSIFIED'                    same, cohort 'excluded_sector'
history                listing_date > as_of - 400 days                  scored on factors that exist;
                                                                        eligible only if <= 40% missing
```

Every excluded cohort's forward return is reported next to the eligible universe so that the June-2026 lesson ("the killed stocks were the best performers") can never again go unnoticed. No filter is applied on trend, valuation, profitability or "trap" logic.

### 6.8 Explainability output kept from V1

For each stock and month the UI receives the per-factor z, the family scores, the composite, the rank, the eligible flag with reason, and the sector group — a strict superset of what the old "bull case / bear risk" text used. The plain-English generator in `update_ui_v16.py` is rewritten to read these columns; the DCF intrinsic value may still be computed and shown as an explainer with the caption "not part of the ranking".

---

## 7. Evaluation protocol

### 7.1 What is computed at every monthly run

```
for each as_of m' <= as_of - h months that now has a matured horizon h (h in 1,3,6,12,24,36):
  for each subject s in {every factor z (active, shadow, retired<24m), every family score,
                         every model's composite/final, sector sleeve, excluded cohorts}:
    IC_h(s, m')            Spearman(s at m', L_h at m') over eligible stocks
    IC_h_all(s, m')        same over all scored stocks incl. excluded cohorts
    decile_returns(s, m')  mean arithmetic TR by decile of s, eligible only
    spread_Q5_Q1(s, m')    top - bottom quintile mean arithmetic TR
    spread_net(s, m')      long-only: top quintile mean TR - eligible EW mean TR - cost drag (7.6)
  cohort_returns(m')       mean TR of eligible vs each excluded cohort
  coverage(s, m')          share of eligible stocks with non-imputed s
  corr_matrix(m')          Spearman among active + shadow z (for the 0.70 constraint)
then, for every subject and horizon, the cumulative series statistics of 7.2 and the
learning-curve row of 7.7 are refreshed.
```

Everything is written to `evaluations` (DDL 9.2) with `track`, `model_id`, `subject_id`, `horizon_m`, `as_of`. Nothing is overwritten: a re-evaluation after a data fix writes a new `eval_run_id`, and the report shows the diff.

### 7.2 Statistics (exact)

```
IC series           x_m = IC_h(s, m), m = 1..n (monthly, overlapping for h > 1)
mean                xbar = (1/n) sum x_m
HAC variance        Newey-West with Bartlett kernel, L = h - 1 lags:
                    V = (1/n) [ gamma_0 + 2 sum_{l=1..L} (1 - l/(L+1)) gamma_l ],
                    gamma_l = (1/n) sum_{m>l} (x_m - xbar)(x_{m-l} - xbar)
HAC t               t = xbar / sqrt(V)
90% CI              xbar +- 1.645 sqrt(V)
n_eff               n / h    (reported next to n; a 12-month series of 36 months has n_eff 3)
block bootstrap CI  moving blocks of length h, 2,000 resamples; reported when n >= 3h,
                    because the HAC estimate is unreliable when n is close to L
cross-sectional t   NOT reported for IC (500 correlated stocks make it meaningless, as the
                    red-team noted). It is used only inside the shuffle test as a null.
ICIR                xbar / sd(x)  -- reported only when n_eff >= 6, else printed as 'n/a (n_eff=k)'
```

The V18 code printed `ic * sqrt((n-2)/(1-ic^2))` with n = 500 stocks; that statistic must not appear anywhere in V2 output.

### 7.3 Walk-forward with embargo

Used for (a) the learning rule's shadow evaluation and (b) any experiment that fits parameters.

```
      training window (labels matured)        embargo = h        test month
 |---------------------------------------|~~~~~~~~~~~~~~~~~~|=======|
 m0                                     m_train_end          m_test = m_train_end + h
                                                             (its label matures at m_test + h)

Rule: a parameter fitted for test month m_test may use only labels whose maturity date
      m' + h <= m_test - 0. Since the label of month m' matures at m' + h, the last usable
      m' is m_test - h. Hence the "embargo" is automatic when you index by maturity, and the
      implementation enforces it in one place:
        Panel.matured_labels(as_of=m_test, h) -> rows with m' + h <= m_test
      Anything that reads labels without going through that method fails the leakage test 7.5 (c).
Expanding window (not rolling): the point of the project is accumulation.
Quarterly refit for IC_SHRUNK_v1; each refit's weights stored with fitted_through = m_test - h.
```

### 7.4 Benchmarks (exact construction, all total return, all monthly)

```
id                 construction                                                   available from
EW_UNIVERSE        equal-weight mean TR of the ELIGIBLE scored universe at m', held h      backfill 2016-
EW_ALL             same over all scored stocks incl. excluded cohorts                      backfill 2016-
N500_PRICE         ^CRSLDX price index (no dividends)                                      2005-
N500_TR_PROXY      ^CRSLDX + 1.2%/yr accrued daily (Nifty 500 dividend yield 1.0-1.4%
                   2015-2025; quant.toml benchmarks.n500_div_yield) -- labelled 'proxy'      2005-
N50_TR_ETF         NIFTYBEES.NS adjusted close (fees ~0.05%)                                2009-
MID150_TR_ETF      MID150BEES.NS adjusted close                                             2019-
MOM30_TR_ETF       MOM30IETF.NS adjusted close (Nifty 200 Momentum 30)                      2024-06
MOM30_RECON        equal-weight TR of the 30 names in ind_nifty200momentum30_list.csv as
                   stored each month, rebalanced when the list changes (semi-annual)        our first store
QUAL30_TR_ETF      QUAL30IETF.NS (Nifty 100 Quality 30)                                    2023-08
QUAL50_RECON       equal-weight TR of ind_nifty500quality50_list.csv names, same method     our first store
LOWVOL30_TR_ETF    LOWVOLIETF.NS                                                            2018-05
SECTOR_MATCHED_EW  for a portfolio: EW of each holding's sector group, weighted by the
                   portfolio's group weights -> isolates stock selection from sector tilt   backfill 2016-
```

The brief's "Nifty 200 Momentum 30 / Nifty 500 Quality 50 proxies" are therefore two things each: the ETF (true TR, short history, includes fees and tracking error) and a reconstruction from the published constituent list (equal-weight, not the index's own weighting, but long enough to matter once we have stored 24 lists). Both are reported; neither is called "the index".

### 7.5 Leakage tests (run monthly; any failure marks the month `not_clean` for the learning curve)

```
(a) shuffle test        permute L_h across stocks within each month, recompute composite IC,
                        200 permutations: require |mean IC| < 0.005 and the observed IC's
                        rank among permutations to be reported (a p-value that respects
                        cross-sectional correlation).
(b) planted signal      replace one shadow factor's z by  rho * gauss_rank(L_12) + sqrt(1-rho^2) * noise,
                        rho = 0.10, run the full pipeline: recovered IC in [0.07, 0.13] at h=12.
                        Proves the pipeline can see a signal of the size we hope for.
(c) forward-shift test  shift every fundamentals_pit.known_at by -90 days (illegal earlier
                        knowledge) and recompute fundamental factor ICs at h=3: any factor whose
                        IC_3 RISES by > 0.02 was already using information it should not have
                        (or the known_at rule is too conservative; either way, investigate).
                        Also +90 days: ICs should fall slightly, never rise.
(d) label-method test   recompute L_h with median vs mean group adjustment and with the
                        universe fixed at m' vs at m'+h: the survivorship variant must show
                        HIGHER mean TR (it always does); if it does not, membership is wrong.
(e) corporate-action    inject a synthetic 1:5 split and a Rs 100 dividend into a copy of one
    test                ticker's raw series, run the adjuster: total return unchanged to 1e-6;
                        run the reconciler on the un-adjusted copy: must flag ca_mismatch.
(f) as_of boundary      assert no factor value at as_of uses a price dated > as_of or a
                        fundamental with known_at > as_of (a SQL assertion over factor_values
                        provenance columns).
```

### 7.6 Cost-adjusted spreads

Long/short quintile spreads are a diagnostic only: retail cannot short Nifty 500 names beyond the ~180 in the F&O segment, and the bottom quintile is the least liquid. The reported "spread" is therefore long-only and net:

```
spread_net_h(m')  = mean TR(top quintile, eligible) - mean TR(EW_UNIVERSE)
                    - cost_drag_h
cost_drag_h       = expected one-way turnover into the top quintile over h months
                    x round-trip cost by tier (8.3), computed from the ACTUAL quintile
                    membership changes between m' and m'+h in the stored scores
                    (not an assumed 100%)
```

For h = 12 with the buffer rule in 8.4, quintile turnover is typically 60–90% per year in momentum-heavy composites; at a blended 0.7% round trip that is 0.4–0.6% of drag, which is of the same order as the spread we are trying to detect. That is why the cost model is in scope from month 1, not month 12.

### 7.7 Learning-curve measurement

The chart the owner will look at is a plot of cumulative out-of-sample IC against months of clean data, per horizon, for the composite and each family, with a CI band. It is computed from stored data, never from a re-run:

```sql
CREATE TABLE IF NOT EXISTS learning_curve (
  computed_at     TEXT NOT NULL,        -- run date
  track           TEXT NOT NULL,        -- 'live' | 'backfill' | 'legacy'
  model_id        TEXT NOT NULL,        -- 'EW_HIER_v1' ... or 'FACTOR' for single factors
  subject_id      TEXT NOT NULL,        -- 'composite' | family name | factor_id
  horizon_m       INTEGER NOT NULL,
  n_clean_months  INTEGER NOT NULL,     -- x-axis: months with matured labels AND gates passed
  n_eff           REAL NOT NULL,
  ic_cum_mean     REAL NOT NULL,        -- y-axis
  ic_hac_se       REAL,
  ic_ci_lo REAL, ic_ci_hi REAL,
  icir            REAL,                 -- NULL when n_eff < 6
  spread_net      REAL,                 -- cumulative mean long-only net spread (7.6)
  slope_12m       REAL,                 -- OLS slope of ic_cum_mean over the last 12 points
  PRIMARY KEY (computed_at, track, model_id, subject_id, horizon_m, n_clean_months)
);
```

```
x-axis    n_clean_months (only months that passed all gates and leakage tests count)
y-axis    ic_cum_mean with the 90% HAC band; a dotted horizontal at 0 and at the target (+0.04 for h=12)
lines     composite (thick), MOMENTUM, QUALITY, GROWTH, VALUE (thin), MOM_ONLY_v1 reference (grey)
panels    h = 1, 3, 12 side by side; backfill track dashed on the h = 12 panel for price factors
"learning"  the owner reads: (1) does the band narrow as x grows (it must; this is data
            accumulating), (2) does the centre stay above 0 (skill), (3) is slope_12m >= 0
            for the composite after month 24 (predictability rising, the stated goal).
            A band that narrows around zero is a clean negative result, not a failure of the
            system.
```

An honest caveat printed under the chart: cumulative mean IC is a *convergence* plot; it cannot rise indefinitely. "Predictability increases over time" means the estimate converges to a positive value and its confidence interval excludes zero, plus any improvements from factor retirements and data fixes, which appear as step changes annotated with the decision id.

---

## 8. Portfolio & cost model

### 8.1 Paper portfolio (one per model; the champion's is "the" portfolio)

```
universe          eligible stocks at as_of (6.7)
selection         top 30 by final rank, subject to: max 6 names per sector group (20%),
                  max 40% in any one macro sector, min 8 groups represented
weighting         equal weight at entry (1/30); no re-weighting between rebalances (drift)
entry price       as_of close (the model is run after the close; assume execution at the
                  next session's VWAP proxied by bhavcopy AVG_PRICE of as_of + 1 trading day,
                  which is available by the time labels are computed). Slippage vs close is
                  therefore measured, not assumed, and reported separately.
holding rule      see 8.4 (buffer)
cash              residual cash earns 0 (conservative); dividends are reinvested into the
                  paying stock (matches adjusted-price total return)
size assumption   Rs 50 lakh notional (quant.toml portfolio.notional_inr); costs and impact
                  tiers are stated for this size; the report also prints the largest notional
                  at which every holding stays under 2% of its 60-day ADV.
```

### 8.2 Liquidity screen and tiers (NSE-specific, from bhavcopy)

Turnover is `TURNOVER_LACS / 100` crore per day, median over the trailing 60 trading days; delivery share is `DELIV_PER`. Thresholds are for the ₹50 lakh notional; they scale with notional in `quant.toml`.

```
tier   60-d median turnover      approx. share of Nifty 500   eligible   impact assumption (one-way)
A      >= Rs 100 crore/day       ~35%  (large caps, F&O)      yes        10 bp
B      Rs 25 - 100 crore/day     ~35%                          yes        25 bp
C      Rs 3 - 25 crore/day       ~28%                          yes        50 bp
D      <  Rs 3 crore/day         ~2%   (tail small caps)       NO         n/a (excluded cohort)
also excluded: > 10 zero-volume days in 60 d; > 15 circuit-locked days (high == low) in 60 d;
               SERIES != 'EQ' (BE/BZ/T2T segments have no intraday and worse liquidity)
```

The shares are estimates from experience; the implementer prints the actual tier distribution in the month-1 report and the thresholds are revisited (as a decision, 9.7) if tier D exceeds 5%. Impact assumptions are conservative for a ₹1.7 lakh position (1/30 of ₹50 lakh) in a ₹3 crore/day name (≈ 5% of ADV, which is on the high side; the 50 bp is for that case).

### 8.3 Cost stack (delivery-based equity, NSE, retail discount broker, September 2026)

```
component                          buy side      sell side    basis
STT (delivery)                     0.100%        0.100%       turnover
stamp duty                         0.015%        --           buy turnover
NSE transaction charge             0.00297%      0.00297%     turnover
SEBI turnover fee                  0.0001%       0.0001%      turnover
GST 18% on (brokerage + exchange + SEBI)  ~0.0006%  ~0.0006%
brokerage                          0 (Zerodha-type delivery) ; configurable up to 0.5%
DP charge on sell                  --            Rs ~15 flat per scrip (0.001% at Rs 1.7 lakh)
statutory total                    ~0.119%       ~0.104%      => ~0.22% round trip
impact (8.2)                       tier A 0.10% / B 0.25% / C 0.50% each way
ROUND TRIP by tier                 A 0.42%   B 0.72%   C 1.22%
```

Annual drag = turnover × blended round trip. At 80% annual turnover and a B/C-heavy book (blend ~0.9%): ≈ 0.7% a year. The model lives or dies on whether its 12-month spread exceeds that.

Cost calibration from free data (month 3 onward): bhavcopy gives `HIGH_PRICE`, `LOW_PRICE`, `AVG_PRICE`, `NO_OF_TRADES`. `median((HIGH - LOW) / AVG_PRICE)` by tier is a crude daily-range proxy for the cost of demanding liquidity; `|CLOSE - AVG_PRICE| / AVG_PRICE` is the median intraday drift. The report prints both per tier so the assumed impact numbers can be replaced by measured ones in a decision.

### 8.4 Turnover control and rebalance cadence

```
review cadence      monthly (every as_of), because labels and evaluation are monthly
trade cadence       buffer rule: a holding is SOLD only if its rank falls below 60 (2x the
                    entry cut-off) OR it becomes ineligible OR a hard corporate event; a
                    vacancy is filled by the highest-ranked non-holding. Expected turnover
                    50-90%/yr for a momentum-heavy composite vs 150-250% for strict monthly
                    top-30 replacement.
full re-equalise    once a year (March as_of, after the Nifty review), to cap concentration
                    drift; otherwise let winners run (this is the multi-bagger thesis and the
                    tax regime in 1.5 speaking with one voice).
tax lot note        the paper P&L is reported pre-tax; the report additionally prints the
                    share of sells that were < 12 months held (STCG exposure).
```

### 8.5 Alpha scoreboard (exact definition)

"Alpha" is a word the dashboard may use only in the row labels below, each of which is a specific arithmetic difference, net of the 8.3 costs, computed on the paper portfolio's actual monthly TR series:

```
row                          definition                                                   what it isolates
excess vs N500_TR_PROXY      port_TR - N500_TR_PROXY                                       "did it beat the market"
excess vs EW_UNIVERSE        port_TR - EW_UNIVERSE                                         selection vs an equal-weight
                                                                                           holder of the same universe
excess vs SECTOR_MATCHED_EW  port_TR - SECTOR_MATCHED_EW                                   stock selection net of
                                                                                           sector tilt (the one the
                                                                                           factor model is accountable for)
excess vs MOM30 / QUAL50     port_TR - MOM30_RECON, - QUAL50_RECON                         "is this better than buying
                                                                                           the factor index"
tracking error               sd(monthly excess) x sqrt(12)
IR                           annualised mean excess / tracking error   (printed only with n >= 24 months)
max drawdown, hit rate       standard; and the same for every challenger and reference model
```

Rule for the word "alpha" in any generated report: a row may be described as alpha only when n ≥ 24 months and its HAC t ≥ 2.0; otherwise the report prints "excess return (not yet distinguishable from zero)". This rule is implemented in the report generator, not left to the author.

---

## 9. Feedback loop & knowledge base

### 9.1 The monthly cycle

```
                         as_of = last NSE trading day of month M          (IST)
                                         │
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 1 INGEST     python -m quant ingest --as-of YYYY-MM-DD                │  auto
        │   universe CSVs -> prices (incremental) -> bhavcopy -> fundamentals   │
        │   -> corporate actions -> reconcile -> gates G1..G9                   │
        └────────────────────────────────┬──────────────────────────────────────┘
                         gates pass?  ───┼─── no ──► run_log status='blocked'; report written;
                                         │           data_quality_events; STOP (no scores)
                                        yes
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 2 SCORE      python -m quant score --as-of ...                        │  auto
        │   factor_values (raw, z, flags) for active+shadow+retired<24m         │
        │   scores for every model_id (champion, challengers, references)       │
        │   paper portfolio trades for every model (buffer rule)                │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 3 EVALUATE   python -m quant evaluate --as-of ...                     │  auto
        │   labels for every earlier as_of whose h matured this month           │
        │   evaluations rows, leakage tests (a)-(f), learning_curve refresh     │
        │   scoreboard rows, cohort returns, correlation matrix                 │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 4 KNOWLEDGE  python -m quant kb update --as-of ...                    │  auto
        │   hypotheses.status checks (9.4 criteria), experiments closed,        │
        │   knowledge/reports/YYYY-MM.md generated, lessons ledger appended     │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 5 PROPOSE    python -m quant propose --as-of ...                      │  auto
        │   rule-based proposals (9.5): promote / probation / retire / weights  │
        │   refit / threshold changes / data fixes -> proposals rows +          │
        │   knowledge/proposals/YYYY-MM.md (each with evidence and a cost)      │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 6 REVIEW     human (or LLM as first reader) reads the report and      │  manual
        │   proposals; may run experiments: python -m quant experiment run ...  │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 7 APPROVE    python -m quant approve P-YYYY-MM-NN --by human:<name>   │  manual
        │              python -m quant reject  P-YYYY-MM-NN --by ... --reason   │
        │   -> decisions row, ADR file knowledge/decisions/ADR-YYYY-MM-NN.md,   │
        │      status changes applied to factor_registry / model_versions       │
        │      EFFECTIVE FROM THE NEXT as_of (never retroactively)              │
        └────────────────────────────────┬──────────────────────────────────────┘
        ┌────────────────────────────────▼──────────────────────────────────────┐
        │ 8 RECORD     python -m quant ui-export && git add -A && git commit    │  auto
        │   -m "quant: month YYYY-MM" ; git push                                │
        └───────────────────────────────────────────────────────────────────────┘
        `python -m quant run-month --as-of ...` executes 1-5 and 8 (8 without push unless
        --push) and prints the proposals; 6-7 are the human part.
```

Time budget on a laptop: step 1 ≈ 35–40 min (yfinance-bound), steps 2–5 ≈ 3 min, step 8 ≈ 1 min.

### 9.2 Knowledge-base tables (DDL)

```sql
CREATE TABLE IF NOT EXISTS hypotheses (               -- pre-registration; one per factor version / rule change
  hypothesis_id   TEXT PRIMARY KEY,                    -- 'H-2026-001'
  kind            TEXT NOT NULL,                       -- 'factor' | 'model' | 'universe_rule' | 'cost_model' | 'label'
  subject_id      TEXT NOT NULL,                       -- factor_id / model_id / rule name
  subject_version INTEGER NOT NULL,
  statement       TEXT NOT NULL,                       -- plain-English hypothesis
  expected_sign   INTEGER,                             -- +1 / -1 / NULL
  horizon_m       INTEGER,
  success_criteria TEXT NOT NULL,                      -- verbatim pre-commitment (5.5)
  failure_criteria TEXT NOT NULL,
  registered_on   TEXT NOT NULL,
  registered_by   TEXT NOT NULL,                       -- 'human:<name>' | 'llm:<model>'
  first_oos_as_of TEXT,                                -- first as_of evaluated after registration
  status          TEXT NOT NULL,                       -- 'open' | 'supported' | 'rejected' | 'withdrawn'
  closed_on       TEXT, closed_by_decision TEXT,       -- FK decisions.decision_id
  md_path         TEXT NOT NULL                        -- knowledge/hypotheses/H-2026-001.md
);

CREATE TABLE IF NOT EXISTS factor_registry (           -- mirror of FactorSpec + status
  factor_id       TEXT NOT NULL,
  version         INTEGER NOT NULL,
  family          TEXT NOT NULL,
  expected_sign   INTEGER NOT NULL,
  horizon_m       INTEGER NOT NULL,
  financials_na   INTEGER NOT NULL,
  inputs          TEXT NOT NULL,                       -- JSON list
  hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
  status          TEXT NOT NULL,                       -- 5.6 lifecycle
  status_since    TEXT NOT NULL,
  code_sha256     TEXT NOT NULL,                       -- hash of the factor module source
  PRIMARY KEY (factor_id, version)
);

CREATE TABLE IF NOT EXISTS model_versions (
  model_id        TEXT NOT NULL,                       -- 'EW_HIER_v1', 'IC_SHRUNK_v1' ...
  version         INTEGER NOT NULL,
  role            TEXT NOT NULL,                       -- 'champion' | 'challenger' | 'reference' | 'legacy'
  weights_json    TEXT NOT NULL,                       -- {family: {factor_id: w}}, W_k, w_sleeve
  fitted_through  TEXT,                                -- last label maturity used (7.3); NULL for fixed models
  created_on      TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  decision_id     TEXT,                                -- promotion decision if role = champion
  note            TEXT,
  PRIMARY KEY (model_id, version)
);

CREATE TABLE IF NOT EXISTS experiments (                -- anything that evaluates against OOS labels
  experiment_id   TEXT PRIMARY KEY,                    -- 'X-2026-014'
  hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
  kind            TEXT NOT NULL,                       -- 'shadow_eval' | 'walk_forward' | 'ablation' | 'leakage' | 'cost_calib' | 'rescoring'
  config_json     TEXT NOT NULL,                       -- everything needed to re-run
  code_sha256     TEXT NOT NULL,                       -- git commit or file hash
  track           TEXT NOT NULL,
  as_of_from TEXT NOT NULL, as_of_to TEXT NOT NULL,
  run_at          TEXT NOT NULL,
  run_by          TEXT NOT NULL,
  result_json     TEXT,                                -- headline numbers
  verdict         TEXT,                                -- 'pass' | 'fail' | 'inconclusive'
  counts_toward_budget INTEGER NOT NULL DEFAULT 1,     -- multiple-testing ledger (9.5)
  md_path         TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
  eval_run_id     INTEGER NOT NULL,                    -- one per evaluate invocation
  track           TEXT NOT NULL,
  as_of           TEXT NOT NULL,                       -- the snapshot month m'
  horizon_m       INTEGER NOT NULL,
  model_id        TEXT NOT NULL,                       -- or 'FACTOR' / 'FAMILY' / 'COHORT'
  subject_id      TEXT NOT NULL,
  n_stocks        INTEGER NOT NULL,
  ic              REAL, ic_all REAL,
  q1 REAL, q2 REAL, q3 REAL, q4 REAL, q5 REAL,         -- quintile mean arithmetic TR
  spread_q5_q1    REAL, spread_net REAL, cost_drag REAL,
  d10_minus_ew    REAL,                                -- top decile minus EW eligible
  coverage        REAL,
  label_status_counts TEXT,                            -- JSON: {'ok':480,'terminated_corporate_event':2,...}
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (eval_run_id, track, as_of, horizon_m, model_id, subject_id)
);

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id     TEXT PRIMARY KEY,                    -- 'P-2026-10-03'
  as_of           TEXT NOT NULL,
  kind            TEXT NOT NULL,                       -- 'promote_factor' | 'probation' | 'retire' | 'promote_model' |
                                                       -- 'refit_weights' | 'threshold_change' | 'data_fix' | 'register_hypothesis'
  subject_id      TEXT NOT NULL,
  evidence_json   TEXT NOT NULL,                       -- the numbers that triggered it, with n, n_eff, t
  rule_id         TEXT NOT NULL,                       -- which 9.5 rule fired
  proposed_by     TEXT NOT NULL,                       -- 'system' | 'llm:<model>' | 'human:<name>'
  status          TEXT NOT NULL,                       -- 'open' | 'approved' | 'rejected' | 'expired'
  decision_id     TEXT,
  md_path         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (                  -- append-only
  decision_id     TEXT PRIMARY KEY,                    -- 'D-2026-10-03'
  decided_on      TEXT NOT NULL,
  decided_by      TEXT NOT NULL,                       -- 'human:<name>' required for classes in 9.7
  kind            TEXT NOT NULL,                       -- same vocabulary as proposals.kind + 'schema_migration' | 'other'
  subject_id      TEXT NOT NULL,
  proposal_id     TEXT,
  outcome         TEXT NOT NULL,                       -- 'approved' | 'rejected' | 'deferred'
  effective_from  TEXT NOT NULL,                       -- next as_of; never in the past
  rationale       TEXT NOT NULL,
  adr_path        TEXT NOT NULL,                       -- knowledge/decisions/ADR-2026-10-03.md
  reverted_by     TEXT                                 -- decision_id that reverted this one, if any
);

CREATE TABLE IF NOT EXISTS data_quality_events (
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of           TEXT NOT NULL,
  severity        TEXT NOT NULL,                       -- 'gate_fail' | 'warn' | 'info'
  kind            TEXT NOT NULL,                       -- 'G1'..'G9' | 'ca_mismatch' | 'readjustment' | 'sector_reclass' | 'unit_fix' | ...
  isin            TEXT,
  detail_json     TEXT NOT NULL,
  resolved_by     TEXT,                                -- decision_id or 'auto'
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
  run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of           TEXT NOT NULL,
  step            TEXT NOT NULL,                       -- 'ingest' | 'score' | 'evaluate' | 'kb' | 'propose' | 'ui'
  started_at TEXT NOT NULL, finished_at TEXT,
  status          TEXT NOT NULL,                       -- 'ok' | 'blocked' | 'partial' | 'error'
  http_calls      INTEGER, http_429s INTEGER,
  code_sha256     TEXT NOT NULL,
  detail_json     TEXT
);
```

### 9.3 Human-readable records

```
knowledge/
  README.md                        how to read this folder; the lifecycle diagram; the budget rules
  hypotheses/H-YYYY-NNN.md         pre-registrations (template 5.5)
  decisions/ADR-YYYY-MM-NN.md      one per decision (template below)
  proposals/YYYY-MM.md             the month's proposals with evidence tables
  reports/YYYY-MM.md               auto-generated monthly report (sections: gates, universe
                                   & sector changes, factor coverage, IC tables per horizon,
                                   learning-curve numbers, scoreboard, cohorts, ca_mismatch
                                   list, proposals, budget ledger)
  lessons.md                       append-only ledger: one line per lesson, dated, linked to
                                   the decision or event that taught it
```

ADR template (kept short on purpose; the numbers live in the DB):

```
Title         : ADR-2026-10-03  Retire RISK_SCORE (legacy) / register LEVERAGE v1
Status        : accepted        Decided by : human:saurabh      Effective from : 2026-10-30
Context       : legacy risk_score was 85% constant (red_team_review.md s4) ...
Decision      : ...
Evidence      : proposals P-2026-10-03 (evaluations eval_run_id 7): n=..., IC=..., HAC t=...
Alternatives  : keep as shadow (rejected because it has no formula, only lists)
Consequences  : factor count active 10 -> 10; families unchanged; UI badge removed
Revisit when  : 2027-10 or if LEVERAGE fails probation
```

### 9.4 Promotion and retirement criteria (numbers)

```
shadow -> active (factor)
  P1  >= 12 live months of matured labels at the factor's pre-registered horizon h
      (h <= 3: 12 months; h = 6: 18 months; h = 12: 24 months)
  P2  cumulative mean IC_h * expected_sign >= +0.02 AND HAC t >= t_crit(k) (9.5)
  P3  IC_h has the expected sign in >= 60% of months
  P4  max |Spearman| with any active factor's z <= 0.70 (else it is a version of an existing
      factor; propose replacing, not adding)
  P5  coverage >= 85% of eligible non-financial universe (or of all, if financials_na = 0)
  P6  adding it to its family at equal within-family weight does not reduce the composite's
      cumulative IC_12 on the shadow period by more than 0.005 (an ablation experiment row)

active -> probation
  R1  24-month rolling mean IC_h * expected_sign < 0 AND HAC t <= -1.0,   OR
  R2  coverage < 80% for 3 consecutive months,                             OR
  R3  |Spearman| with another active factor > 0.85 for 6 months (redundancy)
  probation = within-family weight halved for 6 months, still stored

probation -> retired           R1 still true at the end of probation, or R2 persists
probation -> active            R1 false for 6 consecutive months with HAC t >= 0
retired                        computed and stored for 24 more months; then archived (dropped
                               from computation, rows kept)

model promotion                E1-E3 in 6.5
sector sleeve activation       w_sleeve 0 -> 0.10 requires the sleeve's own P1-P3 at h = 6 plus
                               a paper portfolio with the sleeve beating without-sleeve net of
                               cost over 24 months; 0.10 -> 0.20 requires the same again
```

### 9.5 Multiple-testing control

```
budget               6 new hypotheses per calendar year that consume OOS labels
                     (counts_toward_budget = 1). Ablations of already-active factors, leakage
                     tests, cost calibrations and re-evaluations after data fixes do not count.
ledger               experiments table; the report prints "k = hypotheses tested in the
                     trailing 24 months" and the resulting threshold.
threshold            t_crit(k) = Phi^-1(1 - 0.05 / k) one-sided:
                     k=1 1.64   k=2 1.96   k=3 2.13   k=6 2.39   k=12 2.64   k=24 2.86
                     applied in P2 (and in E2 for models, with k = number of model
                     challengers evaluated in the trailing 24 months).
deflation note       the report also prints the expected maximum IC of k independent null
                     factors given the observed IC dispersion (E[max] ~ sd * Phi^-1(1 - 1/(k+1)))
                     next to the best candidate's IC, so "our best shadow factor has IC 0.04"
                     is read against "the best of 6 null factors would show ~0.035".
no peeking           a shadow factor's IC is computed monthly (it must be, for P1-P3) but a
                     promotion proposal is generated by the rules, not by a human noticing
                     a good month; proposals fire only at the pre-registered review month.
```

### 9.6 How a new parameter is added safely (checklist the CLI enforces)

```
1  python -m quant hypothesis new --kind factor --id DELIV_PCT_60 --family FLOW ...
     -> writes knowledge/hypotheses/H-YYYY-NNN.md skeleton and the hypotheses row (status open)
     -> refuses if the year's budget is exhausted (override requires --by human:<name> --reason)
2  implement quant/factors/deliv_pct_60.py with FactorSpec.preregistration_id = that H id
3  python -m quant factors sync
     -> factor_registry row (status 'registered'); refuses if inputs are not all Panel-provided
        names; refuses if code hash changed for an existing (factor_id, version)
4  python -m quant factors test DELIV_PCT_60
     -> unit tests: NaN policy, financials_na, lookback, sign orientation; planted-signal test
        (7.5 b) passes through this factor's slot; runs on the backfill track if price-only
5  next as_of: status -> 'shadow' automatically on first live computation
6  months pass; every monthly report shows its shadow IC table
7  review month (registered_on + P1 months): the rules fire a proposal or record 'not yet'
8  approve -> 'active' effective next as_of; the composite's model_versions row is bumped
   (weights recomputed hierarchically); an ADR is written
Contamination guard: historical scores rows are never recomputed under the new model; if the
owner wants "what if it had been active since 2026", that is an experiment (kind='rescoring')
with its own experiment_id and a track label 'counterfactual' in evaluations, never 'live'.
```

### 9.7 Approval protocol

```
auto-applied by the run (no approval)      : ingest, gates, scoring with the current champion
                                              and challengers, labels, evaluations, learning
                                              curve, KB report/ledger writes, proposals, UI export,
                                              git commit (push only with --push)
LLM may approve (--by llm:<model>)          : register_hypothesis (budget permitting),
                                              data_fix that only adds corporate_actions rows or
                                              flags, report wording, moving a factor
                                              shadow -> probation-of-shadow (i.e. flagging)
human required (--by human:<name>)          : promote_factor, retire, promote_model,
                                              refit_weights being APPLIED (a refit is computed
                                              automatically but applied only via approval),
                                              threshold_change, universe rule, cost model,
                                              sector_group_def version, schema migration,
                                              any budget override
```

The LLM's role in step 6 is *first reader*: it produces a written recommendation per proposal (a checklist: evidence sufficient? multiple-testing threshold met? correlation constraint? cost effect? what could be wrong?) appended to the proposal file. The `approve` command records `decided_by`; the report shows the share of decisions taken by LLM vs human so rubber-stamping is visible. An LLM approval of a human-required class is refused by the CLI, not by convention.

---

## 10. Architecture

### 10.1 Package layout

```
quant/
  __init__.py
  __main__.py                  python -m quant  -> cli.main()
  cli.py                       argparse subcommands (10.3); every command takes --as-of, --db, --config
  config.py                    loads quant.toml; REPO_DIR-relative paths; env overrides QUANT_DB_PATH, QUANT_DATA_DIR
  calendar.py                  NSE trading days, last_trading_day_of_month(), results-season helpers
  db/
    schema.sql                 all DDL (3.3, 4.3, 4.4, 7.7, 9.2, 10.2) - single source of truth
    migrate.py                 schema_version table; forward-only migrations 001_...sql
    legacy.py                  10.5 migration of the V1 tables
    io.py                      connect(row_factory=sqlite3.Row), upsert helpers, zlib blob helpers
  data/
    universe.py                niftyindices CSV fetch/parse/store; ISIN keying; sector_map updates
    prices.py                  yf.download batching, Parquet store, incremental + readjustment detection
    bhav.py                    nsearchives fetch (per trading day), gzip store, liquidity_daily
    fundamentals.py            yfinance statements -> fundamentals_pit with known_at rules
    holdings.py                info holdings + get_shares_full -> holdings_pit
    market.py                  info -> market_snapshot (units normalised ONCE, here)
    corporate_actions.py       splits/dividends store; bhav reconciliation; quarantine
    panel.py                   Panel: the only read API for factors (enforces known_at <= as_of)
    gates.py                   G1..G9; row flags
    manifest.py                data/MANIFEST.json write/verify; monthly_total_return.parquet export
  sectors/
    groups.py                  sector_group_def v1 table + merge fallbacks; yahoo crosswalk
    features.py                sector_features (3.7)
  factors/
    base.py  transform.py  registry.py (discovery + sync + tests)
    mom_12_1.py trend_200.py high_52w.py rev_1m.py lowvol_252.py roce_ttm.py cash_conv_3y.py
    leverage.py accruals.py roe_stab_3y.py eps_g_3y.py earn_mom_q.py rev_g_3y.py fcf_yield.py
    ey_ebit.py pb_inv.py div_yield.py inst_chg_q.py deliv_pct_60.py sect_mom_6.py sect_breadth.py
    controls.py                SIZE, LIQ_TURNOVER, BETA_252
  model/
    composite.py               6.1 hierarchical composite, eligibility
    models.py                  EW_HIER, EW_FLAT, INDIA_PRIOR, MOM_ONLY definitions
    learn.py                   IC_SHRUNK refit (6.3), bounds (6.4), evidence gates (6.5)
  evaluation/
    labels.py                  L_h construction (2.2), maturity indexing (7.3)
    stats.py                   spearman_ic, newey_west, block_bootstrap, t_crit
    evaluate.py                7.1 loop -> evaluations
    benchmarks.py              7.4 constructions
    leakage.py                 7.5 tests
    learning_curve.py          7.7
  portfolio/
    paper.py                   8.1 construction, buffer rule 8.4, trades ledger
    costs.py                   8.3 cost stack, tiers 8.2, calibration proxies
    scoreboard.py              8.5
  knowledge/
    hypotheses.py  proposals.py  decisions.py  report.py  lessons.py
    templates/                 markdown templates (5.5 hypothesis, 9.3 ADR, monthly report)
  ui/
    export.py                  writes ui/data.js (10.6)
tests/
  test_calendar.py test_units.py test_known_at.py test_transforms.py test_factors_*.py
  test_composite.py test_learn.py test_stats.py test_labels.py test_leakage.py test_costs.py
  test_paper.py test_gates.py test_legacy_migration.py test_cli_smoke.py
  fixtures/                    tiny synthetic price/fundamental panels; a 5-stock, 30-month toy DB
knowledge/                     9.3
data/                          4.7 (MANIFEST.json + two small parquet files committed; rest ignored)
quant.toml                     10.4
quant_engine.db                same file as today; new tables added; legacy tables untouched
ui/                            index.html app.js style.css data.js (vanilla)
legacy/                        harness_v16_learning.py, weight_optimizer.py, quant_math.py, ... moved here
                               unchanged, with a README saying they are frozen and how to run them
                               against a copy of the DB. (Move, do not delete: AGENTS.md rule.)
```

### 10.2 Remaining core DDL (tables not shown earlier)

```sql
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS liquidity_daily (           -- from bhavcopy, EQ series only
  trade_date      TEXT NOT NULL,
  nse_symbol      TEXT NOT NULL,
  isin            TEXT,
  close_raw       REAL, avg_price REAL, high REAL, low REAL,
  qty             REAL, turnover_lacs REAL, trades INTEGER,
  deliv_qty       REAL, deliv_per REAL,
  PRIMARY KEY (trade_date, nse_symbol)
);

CREATE TABLE IF NOT EXISTS factor_values (
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  factor_id       TEXT NOT NULL,
  version         INTEGER NOT NULL,
  raw             REAL,                                -- NULL allowed
  z               REAL,                                -- group gauss rank; 0 when imputed
  imputed         INTEGER NOT NULL DEFAULT 0,
  flags           TEXT,                                -- comma list (4.6 row flags)
  inputs_asof_json TEXT,                               -- {'ebit_a': {'period_end':..,'known_at':..}, ...} provenance
  PRIMARY KEY (as_of, isin, factor_id, version)
);
CREATE INDEX IF NOT EXISTS ix_fv_factor_asof ON factor_values(factor_id, as_of);

CREATE TABLE IF NOT EXISTS scores (
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  model_version   INTEGER NOT NULL,
  sector_group    TEXT NOT NULL,
  group_def_ver   INTEGER NOT NULL,
  family_scores_json TEXT NOT NULL,                    -- {'MOMENTUM': 0.42, ...}
  composite       REAL NOT NULL,
  composite_neutral REAL NOT NULL,
  sector_tilt     REAL,
  final           REAL NOT NULL,
  rank_all        INTEGER NOT NULL,
  rank            INTEGER,                             -- NULL if not eligible
  decile          INTEGER, quintile INTEGER,
  eligible        INTEGER NOT NULL,
  exclusion_reason TEXT,                               -- 'liquidity' | 'data' | 'sector' | NULL
  liq_tier        TEXT,                                -- 'A'..'D'
  n_imputed       INTEGER NOT NULL,
  track           TEXT NOT NULL,                       -- 'live' | 'backfill' | 'legacy' | 'counterfactual'
  PRIMARY KEY (as_of, isin, model_id, model_version, track)
);
CREATE INDEX IF NOT EXISTS ix_scores_model_asof ON scores(model_id, as_of);

CREATE TABLE IF NOT EXISTS labels (
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  horizon_m       INTEGER NOT NULL,
  end_date        TEXT NOT NULL,
  r_log           REAL,                                -- ln TR
  r_arith         REAL,
  r_group_median  REAL,
  l_rel           REAL,                                -- r_log - r_group_median (the label)
  sector_group    TEXT NOT NULL,
  label_status    TEXT NOT NULL,                       -- 'ok' | 'terminated_corporate_event' | 'suspended' | 'missing' | 'quarantined_ca'
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (as_of, isin, horizon_m)
);

CREATE TABLE IF NOT EXISTS benchmarks_monthly (
  month_end       TEXT NOT NULL,
  benchmark_id    TEXT NOT NULL,                       -- 7.4 ids
  tr_index        REAL NOT NULL,                       -- total-return index level, base 100
  source          TEXT NOT NULL,
  PRIMARY KEY (month_end, benchmark_id)
);

CREATE TABLE IF NOT EXISTS portfolio_paper (
  model_id        TEXT NOT NULL,
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  weight          REAL NOT NULL,
  entry_as_of     TEXT NOT NULL,
  entry_price     REAL NOT NULL,                       -- next-day AVG_PRICE proxy
  action          TEXT NOT NULL,                       -- 'hold' | 'buy' | 'sell' | 'rebalance'
  liq_tier        TEXT NOT NULL,
  cost_bps        REAL NOT NULL,                       -- applied this month
  PRIMARY KEY (model_id, as_of, isin)
);

CREATE TABLE IF NOT EXISTS portfolio_nav (
  model_id        TEXT NOT NULL,
  month_end       TEXT NOT NULL,
  nav_gross       REAL NOT NULL, nav_net REAL NOT NULL,
  turnover_1m     REAL NOT NULL, cost_1m_bps REAL NOT NULL,
  n_holdings      INTEGER NOT NULL,
  PRIMARY KEY (model_id, month_end)
);

CREATE TABLE IF NOT EXISTS legacy_snapshot_map (        -- 10.5
  legacy_date     TEXT NOT NULL,                       -- daily_predictions.date
  as_of           TEXT NOT NULL,                       -- mapped last trading day
  is_full         INTEGER NOT NULL,
  superseded_by   TEXT,
  defects         TEXT NOT NULL,                       -- JSON list
  migrated_at     TEXT NOT NULL,
  PRIMARY KEY (legacy_date)
);
```

### 10.3 CLI commands and expected outputs

```
python -m quant init                         create/migrate schema; write quant.toml if missing
   -> "schema at version 3; db /.../quant_engine.db; data dir /.../data"

python -m quant migrate-legacy [--dry-run]   10.5
   -> table: legacy_date -> as_of, rows, defects; "6 snapshots mapped, 4 full, 2 partial/superseded;
      12 weight rows -> model_versions role=legacy; performance_tracking NOT migrated (recomputed)"

python -m quant universe --as-of 2026-09-30  fetch 8 CSVs, update membership/sector_map/security_master
   -> "NIFTY500 500 rows sha 3f2a...; 0 sector reclassifications; 1 new ISIN (TENNIND); groups: G01 31 ..."

python -m quant prices [--rebuild] --as-of   incremental (or full) download; readjustment detection
   -> "553 tickers, 23 batches, 0 x 429; 2 readjustments (ISINs...), MANIFEST updated"

python -m quant bhav --as-of                 fetch missing trading days of the month
   -> "22 files fetched, 21 parsed (1 holiday 404 expected), liquidity_daily +11,004 rows"

python -m quant fundamentals --as-of [--full]   info always; statements if --full or results month
   -> "553 tickers; known_at sources: earnings_dates 312, lodr_45d 198, lodr_60d 43; 0 unit_fix"

python -m quant ingest --as-of               universe + prices + bhav + fundamentals + CA reconcile + gates
   -> gate table G1..G9 with PASS/FAIL and the number; exit code 2 if blocked

python -m quant score --as-of                factor_values + scores + paper trades for all models
   -> "10 active, 9 shadow factors; eligible 471/500 (liquidity 9, data 17, sector 3);
       champion top 5: ...; buffer trades: 3 sells, 3 buys"

python -m quant evaluate --as-of             labels matured this month; evaluations; leakage; learning curve
   -> "labels: h=1 for 2026-08 (498 ok, 1 quarantined), h=3 for 2026-06 ...;
       composite IC_1 (2026-08) +0.031; cumulative IC_1 n=3 mean +0.024 HAC t 1.1;
       leakage: shuffle PASS (|mean| 0.002), planted PASS (0.094), shift PASS, CA PASS, boundary PASS"

python -m quant kb update --as-of            hypotheses checks, report, lessons
   -> "knowledge/reports/2026-09.md written (14 sections); 0 hypotheses closed"

python -m quant propose --as-of              rule-based proposals
   -> "2 proposals: P-2026-09-01 data_fix ca_mismatch ITCHOTELS (adj_factor), P-2026-09-02 register BULK_BLOCK_DEALS"

python -m quant approve P-2026-09-01 --by human:saurabh --reason "..."
python -m quant reject  P-2026-09-02 --by llm:claude --reason "..."
   -> decisions row + ADR path; refuses llm for human-required kinds with a one-line message

python -m quant experiment run --config experiments/x_2026_014.toml
   -> experiments row; markdown under knowledge/experiments/

python -m quant learning-curve [--png]       prints the 7.7 table; optional PNG via matplotlib if installed
python -m quant scoreboard                   prints 8.5 rows for every model
python -m quant ui-export                    writes ui/data.js
python -m quant verify                       re-derives every reported number in the latest report
                                             from the DB + committed parquet, offline; diff must be empty
python -m quant run-month --as-of [--push]   ingest -> score -> evaluate -> kb -> propose -> ui-export -> git commit
```

Exit codes: 0 ok, 1 error, 2 blocked by gates, 3 refused (approval class / budget).

### 10.4 Configuration (`quant.toml`)

```toml
schema_version = 3

[paths]
db          = "quant_engine.db"          # relative to repo; env QUANT_DB_PATH overrides
data_dir    = "data"
knowledge   = "knowledge"
ui_dir      = "ui"

[calendar]
timezone            = "Asia/Kolkata"
holiday_fallback    = ["2026-10-02", "2026-10-20", "2026-11-05", "2026-12-25"]   # extended yearly

[sources]
nifty_csv_base   = "https://niftyindices.com/IndexConstituent/"
nifty_lists      = ["ind_nifty500list.csv","ind_niftytotalmarket_list.csv","ind_nifty200momentum30_list.csv",
                    "ind_nifty500quality50_list.csv","ind_nifty100quality30list.csv","ind_niftymidcap150list.csv",
                    "ind_niftysmallcap250list.csv","ind_nifty100list.csv"]
bhav_url         = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv"
equity_l_url     = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
user_agent       = "Mozilla/5.0"
yf_sleep_s       = 0.5
yf_batch_size    = 25
yf_batch_sleep_s = 1.0
yf_max_retries   = 5
statements_months = [2, 5, 8, 11]          # months in which quarterly/annual statements are refetched for all

[storage]
format = "parquet"                          # or "csv.gz"
compress_info_json = true

[universe]
scored_index      = "NIFTY500"
min_rows_gate     = 480
min_group_size    = 15
group_def_version = 1

[labels]
horizons_m        = [1, 3, 6, 12, 24, 36]
primary_h         = 12
group_adjust      = "median"

[factors]
winsor            = [0.01, 0.99]
max_missing_share = 0.40
max_active        = 14
max_shadow        = 12
corr_limit_new    = 0.70
corr_limit_redund = 0.85

[model]
champion          = "EW_HIER_v1"
sleeve_cap        = 0.20
learn_n0          = 6
learn_kappa       = 0.5
learn_bounds      = [0.5, 2.0]
min_live_months_for_promotion = 36

[evaluation]
hac_kernel        = "bartlett"
bootstrap_n       = 2000
shuffle_perms     = 200
planted_rho       = 0.10
n500_div_yield    = 0.012

[portfolio]
notional_inr      = 5000000
n_holdings        = 30
max_per_group     = 6
sell_rank         = 60
tiers_turnover_cr = { A = 100, B = 25, C = 3 }
impact_bps        = { A = 10, B = 25, C = 50 }
stt_bps           = 10
stamp_bps_buy     = 1.5
exchange_bps      = 0.297
sebi_bps          = 0.01
brokerage_bps     = 0
dp_charge_inr     = 15

[knowledge]
hypothesis_budget_per_year = 6
human_required = ["promote_factor","retire","promote_model","refit_weights","threshold_change",
                  "universe_rule","cost_model","sector_group_def","schema_migration","budget_override"]
```

### 10.5 Migration of the existing `quant_engine.db` and its 2026 snapshots

Principle: nothing in the legacy tables is altered; V2 tables are added beside them; the legacy snapshots become the first rows of the `legacy` track with defects recorded.

```
step 1  legacy_snapshot_map
   2026-06-04 (47 rows)   -> as_of 2026-06-04, is_full 0, defects ['partial']
   2026-06-12 (499)       -> as_of 2026-06-12, is_full 1, superseded_by 2026-06-14
                             (two full snapshots 2 days apart; the later one was the documented run)
   2026-06-14 (499, Sun)  -> as_of 2026-06-12 (last trading day <= date)   ** same as_of as above **
                             resolution: keep 06-14 as the migrated snapshot for as_of 2026-06-12,
                             mark 06-12 superseded. Record this explicitly in the map.
   2026-07-11 (499, Sat)  -> as_of 2026-07-10
   2026-08-14 (499, Fri)  -> as_of 2026-08-14
   2026-09-03 (500, Thu)  -> as_of 2026-09-03
   defects on every full snapshot: ['div_yield_x100','roe_none_as_zero','growth_imputed_15pct',
      'bucketed_scores','no_sector_neutral','price_unadjusted_quote','sentiment_in_score',
      'yahoo_sector_only','no_known_at']  (+ 'no_data_flags' for all; 'no_base_score' for pre-Sep)

step 2  security_master / symbol_history from the current CSV + EQUITY_L; legacy tickers matched
        by symbol (ticker minus '.NS'); unmatched (renamed/delisted since) logged, mapped by hand
        in a committed CSV legacy/ticker_isin_overrides.csv

step 3  scores rows, track='legacy', model_id='LEGACY_V18', model_version = the active_weights.id
        in force at the date (weights_in_force logic from weight_optimizer.py):
          composite = base_score if present else the weighted sum recomputed from the 8 stored
                      factor columns and the in-force weights; final = final_score;
          rank_all from final; eligible = 1 for all (there was no screen); sector_group from the
          CURRENT sector_map (source 'carry_back', flagged) -- there is no PIT sector for 2026-06
        The 8 legacy factor columns are also written to factor_values with factor_id
        'LEGACY_<name>' version 0, so their ICs can be compared with the V2 replacements.

step 4  labels for the four legacy as_of dates computed from the V2 adjusted price store (so the
        ZFCVINDIA split and dividends are handled), with sector-relative adjustment. This is the
        first place the legacy months become comparable with the live track. The
        performance_tracking table is NOT migrated: its returns are unadjusted quotes.

step 5  model_versions: 12 active_weights rows -> role 'legacy', weights_json as stored,
        note copied; decisions row D-2026-09-00 'legacy import' with the red-team review as rationale.

step 6  backfill track: V2 price factors (MOM_12_1, TREND_200, LOWVOL_252, HIGH_52W, REV_1M) are
        computed for month-ends 2016-01 .. 2026-08 for the current Total Market list and scored
        with EW_HIER over the momentum family only and with MOM_ONLY_v1; labels and evaluations
        follow. Marked track='backfill' everywhere.

step 7  python -m quant verify prints, for each legacy as_of, the composite IC_1 against the
        adjusted, sector-relative label next to the red-team's unadjusted numbers
        (-0.063, +0.092, +0.117) so the effect of the corrections is documented in the first
        monthly report.
```

Acceptance for the migration: `SELECT COUNT(*) FROM daily_predictions` unchanged; `legacy_snapshot_map` has 6 rows; `scores WHERE track='legacy'` has 1,997 rows (499+499+499+500); every one of them has a `labels` row for h=1 or a `label_status` explaining why not; `model_versions WHERE role='legacy'` has 12 rows.

### 10.6 UI changes (vanilla HTML/JS/CSS; Chart.js stays as the one CDN script, stated plainly in the README)

`ui/data.js` gains these constants, all written by `quant/ui/export.py`:

```
snapshotMeta      { as_of, run_status, gates: [{id, pass, value}], universe, eligible, champion }
rankedStocks      top 50 + all holdings: { isin, symbol, name, sector_group, rank, decile, final,
                                          family_scores, factor_z: {factor_id: z}, flags, liq_tier,
                                          eligible, exclusion_reason, explain: {plain-English per family} }
excludedCohorts   { liquidity: [...], data: [...], sector: [...] } with their trailing cohort returns
learningCurve     rows of 7.7 for h in (1,3,12), composite + families + MOM_ONLY, live/backfill/legacy
scoreboard        8.5 rows per model, plus NAV series for the chart
factorRegistry    5.3 table with status, months in shadow, cumulative IC by horizon, next review month
sectorView        sector_features per group + group weights of the paper portfolio vs universe
decisions         last 24 ADRs (id, date, kind, subject, one-line rationale, by)
dataQuality       gate history (24 months), flag counts, ca_mismatch list, hypothesis budget ledger
```

Tabs in `index.html`: Ranking (replaces Accepted/Rejected; the "Turnaround" tab becomes a filter chip "negative FCF, high growth" rather than a separate pipeline), Portfolio & Scoreboard, Learning Curve, Factors, Sectors, Data Quality, Decisions. `app.js` renders each from the constants; no framework, no build step. The legacy "FATAL MULTIPLIER" language is removed; a stock below its 200-day average shows a neutral badge with its TREND_200 z.

### 10.7 Acceptance tests (pytest, no network; fixtures under tests/fixtures)

```
test_units.py             dividend_rate/close in [0, 0.25]; debtToEquity 357 -> 3.57; dividendYield never read
test_known_at.py          quarterly period_end 2026-06-30 -> known_at 2026-08-14 absent earnings_dates;
                          value with known_at 2026-08-14 invisible at as_of 2026-07-31, visible at 2026-08-31
test_labels.py            synthetic 6:1 split mid-month -> r_log unchanged; Rs 75 dividend -> TR includes it;
                          group median adjustment sums to ~0 within group; universe fixed at as_of
test_transforms.py        gauss rank of a 21-stock group has mean 0 +- 1e-9, sd in [0.95, 1.05];
                          NaN -> z 0 and imputed 1; winsor limits respected
test_factors_*.py         each factor: NaN policy (no imputation), financials_na, lookback -> NaN for IPO,
                          orientation (a monotone synthetic input yields a monotone raw)
test_composite.py         hierarchical weights sum to 1 per family and across; adding a 3rd momentum factor
                          leaves family weight 0.25; > 40% missing -> eligible 0
test_learn.py             n_months 13 -> lambda 0.153; bounds clip; wrong-sign factor never rewarded;
                          identical ICs -> weights identical to EW (idempotent on no information)
test_stats.py             Newey-West lag 11 on an AR series matches statsmodels within 1e-6 (statsmodels
                          used only in the test if installed; else a stored reference value);
                          t_crit(6) == 2.394
test_leakage.py           shuffle |mean IC| < 0.005 on the toy DB; planted rho 0.10 recovered in [0.07, 0.13]
test_costs.py             tier A round trip 42 bp; B 72; C 122; DP charge scales with notional
test_paper.py             buffer rule: rank 45 held, rank 61 sold; sector cap 6 enforced; cash 0 return
test_gates.py             G2 fails at 97.9% coverage; G6 flags but passes with 5 violators, fails at 6
test_legacy_migration.py  the 10.5 acceptance counts on a copy of quant_engine.db committed under
                          tests/fixtures/legacy_sample.db (a 20-ticker extract, not the full DB)
test_cli_smoke.py         run-month --as-of on the toy DB completes with exit 0 and writes a report
```

---

## 11. Phased roadmap

Implementation starts 2026-09-07. The first V2 live snapshot is as_of **2026-09-30** if the data layer passes its gates by the run window (3–4 October); otherwise **2026-10-30**. The legacy 2026-09-03 snapshot bridges either way.

### 11.1 Month 1 (by 2026-10-05): the data layer and the first honest snapshot

```
ships
  quant/ package skeleton, schema v3, quant.toml, python -m quant init|migrate-legacy|universe|
    prices|bhav|fundamentals|ingest|score|evaluate|kb|propose|ui-export|verify|run-month
  data layer: universe (8 CSVs, ISIN keyed), 10-year adjusted price backfill + MANIFEST,
    bhavcopy for Sep-Oct, fundamentals_pit with known_at rules, corporate_actions + reconcile,
    gates G1-G9
  factors: 10 active + 3 controls + HIGH_52W/REV_1M/LOWVOL backfill; registry synced;
    10 hypotheses H-2026-001..010 pre-registered; legacy factors retired by ADR-2026-09-01..03
  models: EW_HIER_v1 champion, EW_FLAT_v1, INDIA_PRIOR_v1, MOM_ONLY_v1 (IC_SHRUNK deferred)
  evaluation: labels + evaluations + leakage tests on the backfill and legacy tracks;
    learning_curve table populated for backfill; live track has its first scores, no labels yet
  knowledge: tables, first monthly report, lessons.md seeded from red_team_review.md
  UI: Ranking, Data Quality, Learning Curve (backfill dashed + legacy points), Decisions
  tests: >= 60 pytest tests green offline
month-1 report must contain
  actual sector-group counts; tier distribution; fundamentals coverage per factor; the legacy
  IC_1 recomputed on adjusted sector-relative labels next to the red-team numbers; call budget
  and wall-clock; the list of ca_mismatch names
```

### 11.2 Month 3 (by 2026-12-05)

```
  paper portfolios for every model with the cost stack; entry at next-day AVG_PRICE; NAV series
  cost calibration proxies from bhavcopy (8.3) in the report
  first live IC_1 (Oct, Nov) and IC_3 (none yet) on the chart; leakage tests monthly
  shadow factors computing: INST_CHG_Q (first delta at month 4), DELIV_PCT_60, ACCRUALS,
    ROE_STAB_3Y, REV_G_3Y, PB_INV, DIV_YIELD, HIGH_52W, REV_1M, sector sleeve features
  BULK_BLOCK_DEALS and SECT_FLOW_PROXY registered (budget 2/6 for 2026; 2027 budget opens Jan)
  UI: Portfolio & Scoreboard, Factors, Sectors tabs
  cron: monthly run on the first Saturday after month end, 06:30 IST, with --push
```

### 11.3 Month 6 (by 2027-03-05)

```
  IC_SHRUNK_v1 implemented; first quarterly refit (Mar 2027 as_of) computed and STORED, not applied
  first 6-month live labels (Sep/Oct 2026 cohorts); IC_3 series has 3-4 points
  python -m quant verify proven from a fresh clone with network disabled
  AGENTS.md / README rewritten for V2; legacy/ folder frozen with its own README
  first data-quality retrospective ADR: which gates fired, which flags dominate, unit_fix must be 0
  decision point (human): widen scored universe to Total Market? (needs tier-D share and runtime)
```

### 11.4 Month 12 (by 2027-09-05)

```
  first review month for h <= 3 factors (TREND_200, EARN_MOM_Q active; REV_1M, DELIV_PCT_60,
    SECT_BREADTH shadow): P1-P6 evaluated, proposals generated, decisions recorded
  year-1 report: learning-curve chart with h = 1 (n = 11) and h = 3 (n = 9) live; h = 12 backfill
    only; scoreboard for all models (12 months, "excess return, not yet distinguishable from zero")
  budget ledger: hypotheses tested in 2027 <= 6; k and t_crit printed
  first 12-month live labels arrive the following month (Oct 2027 as_of)
```

### 11.5 The learning-curve chart the owner will look at, and what it should look like when

Expected width of the 90% band around the cumulative mean IC, assuming month-to-month IC standard deviations of 0.08 (h=1), 0.10 (h=3) and 0.12 (h=12), and n_eff = n / h:

```
months live   h=1: n, +-1.645 SE      h=3: n, n_eff, +-SE      h=12: n, n_eff, +-SE
  6           5,   +-0.059            3,  1.0,  +-0.165        0
 12           11,  +-0.040            9,  3.0,  +-0.095        0
 24           23,  +-0.027            21, 7.0,  +-0.062        12, 1.0,  +-0.20
 36           35,  +-0.022            33, 11.0, +-0.050        24, 2.0,  +-0.14
 60           59,  +-0.017            57, 19.0, +-0.038        48, 4.0,  +-0.10
```

Reading it honestly: at month 36 the 1-month panel can distinguish an IC of +0.03 from zero; the 3-month panel can distinguish +0.06; the 12-month panel cannot distinguish anything below +0.15. That is why the 12-month panel carries the backfill track (dashed) for price factors, and why the primary *decision* evidence in years 1–3 comes from h = 1 and h = 3 while the *objective* stays at h = 12. The chart's caption says exactly this.

### 11.6 If only four weeks of implementation were available

Keep the parts that make every later month an honest observation; cut everything that consumes observations or presentation time.

```
KEEP (weeks 1-4)
  schema + migrate-legacy (10.5 steps 1-5 only; skip step 6 backfill scoring)
  universe (Nifty 500 + Total Market CSVs only), prices backfill, corporate_actions + reconcile,
    fundamentals_pit with known_at, gates G1-G7 and G9 (skip G8 smoke; the full shuffle test runs
    in evaluate)
  bhavcopy from go-live only (no 24-month backfill); tiers from the first month's data
  6 active factors: MOM_12_1, TREND_200, LOWVOL_252, ROCE_TTM, EARN_MOM_Q, FCF_YIELD
    (2 momentum, 2 quality, 1 growth, 1 value) + SIZE/LIQ controls; no shadow factors
  EW_HIER_v1 champion and MOM_ONLY_v1 reference only
  labels for h in {1, 3, 12}; evaluations; Newey-West; shuffle + planted + boundary leakage tests;
    learning_curve table; a plain-text learning-curve print (no chart)
  paper portfolio with a FLAT 70 bp round-trip cost and the buffer rule (no tiers, no calibration)
  knowledge tables: hypotheses, decisions, data_quality_events, run_log; monthly report; ADRs
    written by hand from a template (no proposals engine)
  UI: Ranking + Data Quality tabs only; learning curve as a table
  tests for units, known_at, labels, transforms, composite, stats, gates
CUT (added later without contaminating history, because factor_values/scores/labels are
     append-only and every row carries model_id, version and track)
  sector sleeve, sector_features, Yahoo-industry financial split (use one FIN group of 101)
  IC_SHRUNK learning rule, INDIA_PRIOR challenger, quarterly refits
  proposals engine and approval CLI (decisions are hand-written ADRs + a decisions row)
  benchmark reconstructions (keep ^CRSLDX proxy and EW_UNIVERSE only)
  cost tiers and calibration, bhav backfill, bulk-deal ingestion
  backfill-track scoring (keep the raw price backfill; score it later)
  Sectors / Factors / Scoreboard / Decisions UI tabs
```

The four-week version still produces, every month, exactly the rows the full version needs; nothing has to be re-scored when the cut features arrive.

---

## 12. Risks, failure modes and open questions

Each risk names the mechanism, then what the design does about it, then what it cannot do.

```
R01  Survivorship in the backfill track
     mechanism   : yfinance has no history for delisted names; today's list overstates momentum
                   and quality ICs. Survivorship = selecting on the outcome.
     design      : separate track, dashed line, 0.5 prior discount, never used for promotion.
     residual    : the discount is a guess; the true bias is unknown until a survivorship-free
                   source (purchased, or a bhavcopy-reconstructed universe, section 4.5) exists.

R02  Fundamental known_at is a rule, not a fact
     mechanism   : LODR deadlines (45/60 days) are upper bounds; most Nifty 500 companies report
                   2-5 weeks earlier. Our fundamental factors are therefore STALER than a real
                   investor's, which understates their IC (conservative) but also means the
                   EARN_MOM_Q signal is used ~3 weeks late, when part of the drift has happened.
     design      : earnings_dates used when present (large caps mostly); source recorded.
     residual    : a free, reliable results-date source for all 500 does not exist; NSE's
                   corporate announcements feed needs a session. Accept the lag; document it.

R03  yfinance breaks or changes units again
     mechanism   : a third-party scraper with no contract; dividendYield already changed units.
     design      : units normalised in one module (market.py) from unambiguous fields
                   (dividend_rate, not dividendYield); G6 unit gate; info_json kept for forensics;
                   MANIFEST + committed monthly returns keep evaluation reproducible offline.
     residual    : a month can be blocked; the loop tolerates a missed month (labels are
                   computed from the price store, not from run presence).

R04  NSE archive URLs or formats change; niftyindices CSV moves
     design      : URLs in quant.toml; parsers assert column names; a failure blocks only the
                   dependent features (liquidity tiers fall back to yfinance averageVolume x price,
                   flagged 'liq_from_yahoo').

R05  Demergers and schemes mis-adjusted by Yahoo
     design      : bhav reconciliation flags; quarantine, manual adj_factor; report lists them.
     residual    : requires a human minute per event; 2-6 a year.

R06  The first live years fall in an unrepresentative regime
     mechanism   : 2023-24 small-cap boom, 2025 correction; three years of live data may be one
                   regime. A factor can look dead or alive because of the regime, not its merit.
     design      : sector-relative labels remove the sector component of regimes; backfill track
                   shows the price factors across 2016-2026 regimes; retirement needs 24 months
                   AND HAC t <= -1.0, not just a bad year.
     residual    : real. State it in every annual report.

R07  Multiple-testing creep through "informal" experiments
     mechanism   : someone runs a notebook, sees a good IC, then pre-registers it.
     design      : budget, ledger, review months fixed at registration, proposals fired by rules.
     residual    : cannot be enforced against a determined owner; the lessons ledger is the
                   cultural control.

R08  LLM approver rubber-stamps
     design      : human-required classes enforced by the CLI; share of LLM decisions reported.

R09  Cost model wrong by 2x
     mechanism   : impact assumptions are educated guesses; a Rs 50 lakh notional in tier C names
                   may cost more.
     design      : calibration proxies from bhavcopy from month 3; notional is a config; the
                   report prints the largest notional at 2% of ADV.
     residual    : the paper P&L is not a live P&L; slippage is measured against next-day
                   AVG_PRICE, which is itself optimistic for a market order.

R10  Owner attention
     mechanism   : the design needs ~30 minutes of human review a month and a decision at review
                   months; unreviewed proposals expire (status 'expired' after 2 months) and the
                   champion simply continues.
     design      : nothing auto-applies that changes the model; the system degrades to "a clean
                   monthly record" if ignored, which is still the main asset.

R11  Small n forever (the honest one)
     mechanism   : section 2.1. Twelve-month skill may never be provable with monthly live data.
     design      : the objective is stated at 12 m but the decision instruments are at 1-3 m; the
                   falsifiers (1.4) are written now.
     residual    : if F1-F3 trigger at month 36, the answer is "buy point-in-time data or stop",
                   not "add factors".

R12  Index-inclusion effects contaminate labels
     mechanism   : Nifty 500 entrants get passive buying around review dates; a stock scored the
                   month before inclusion shows a return that is inclusion, not factor.
     design      : membership stored monthly; the report shows returns of entrants/exits as a
                   cohort; a future control factor INCLUSION_EVENT is a candidate.
```

### 12.1 Open questions for the owner (decisions the spec cannot take alone)

```
Q1  Notional for the paper portfolio: Rs 50 lakh (proposed) or Rs 5 crore? It changes tier D's
    threshold and the impact assumptions.
Q2  Should financials be scored at all in v2.0? Three groups, no EBIT/FCF, 20% of the universe.
    Proposal: yes, on MOM_12_1, TREND_200, LOWVOL_252, EPS_G_3Y, EARN_MOM_Q, PB_INV (shadow);
    their composite is a 5-factor composite and is flagged as such.
Q3  Run date: last trading day (prices final, Yahoo info a weekend stale) vs the 1st weekend
    (proposed). Any consistent choice is fine; it must never change silently.
Q4  Is a purchased point-in-time fundamentals dataset acceptable as an OPTIONAL adapter in year 2
    if R02/R11 bite? The schema is ready for it; the brief forbids it as a dependency, not as an option.
Q5  Total Market extension in month 6: yes if tier D <= 5% of Total Market and the runtime stays
    under 60 minutes; the owner decides on the cost of 250 more thin names vs their information.
Q6  Who is 'human:<name>' when the owner is away? An expired proposal is safe; a delegated approver
    should be named in quant.toml.
```

### 12.2 The whole argument in one picture

```
   retail-fetchable data                 discipline                       what accumulates
 ┌──────────────────────┐   ┌──────────────────────────────────┐   ┌──────────────────────────┐
 │ niftyindices CSVs    │   │ ISIN identity, NSE sector groups │   │ one point-in-time,       │
 │ (sector = 20 values) │──►│ (15), monthly membership         │──►│ sector-relative, total-  │
 │ yfinance adj prices  │   │ known_at <= as_of via Panel only │   │ return, cost-aware       │
 │ + statements + info  │──►│ continuous sector-neutral z      │──►│ observation per month    │
 │ nsearchives bhavcopy │   │ hierarchical equal weight        │   │ per stock per factor     │
 │ (turnover, delivery) │──►│ no hard kills, cohorts reported  │──►│ + labels at 1..36 m      │
 └──────────────────────┘   │ HAC stats, n_eff, leakage tests  │   └────────────┬─────────────┘
                            │ pre-registration, budget k,      │                │
                            │ human approval for model changes │                ▼
                            └──────────────────────────────────┘   learning curve: band narrows,
                                                                    centre above 0 => skill;
                                                                    around 0 => clean negative;
                                                                    weights move only when n_eff says so
```

**One sentence for leadership:** For the next three years this engine is a disciplined recording instrument for the Indian market — every month it ranks the Nifty 500 inside NSE sectors with a fixed, evidence-backed equal-weight composite and writes down, point-in-time and net of realistic NSE costs, whether that ranking worked — and only once that record is long enough to be statistically legible will it be allowed to change its own weights; anyone who promises alpha before then is reading the wrong chart.

### 12.3 Confidence, split in two

```
That this design is the right interpretation of the goals and constraints for a retail-data,
Indian-market implementation (sector source, horizons, factor set, guard rails, storage):   80%

That, implemented as written, the live sector-neutral composite will show a 12-month IC >= +0.04
with HAC t >= 1.5 by month 36 (target in 1.3):                                                 40%
   - the 1-month and 3-month ICs being positive with t >= 2 by month 36:                       55%
   - the learning curve's band narrowing as specified (a property of the plumbing, not of alpha): 90%
```

The gap between the first number and the second is the whole point of the design: it is built so that the second number can be measured, whatever it turns out to be.
