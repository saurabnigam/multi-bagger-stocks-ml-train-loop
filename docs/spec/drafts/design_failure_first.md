**V2 Engine Design — architect lens: "Failure-First Skeptic"**
Written 2026-09-05 against branch `red-team-review-sep-2026`. Inputs: `docs/spec/00_context_brief.md`, `docs/analysis/red_team_review.md`, `AGENTS.md`, the code in the repo root, read-only inspection of `quant_engine.db`, and live checks of `niftyindices.com` and yfinance 1.4.1 on 2026-09-05.

> **The answer up front.** Build the next engine as a point-in-time evidence ledger first and a stock ranker second. Every table, factor and rule below exists to block one of the nine things that kill retail quant projects: data snooping, survivorship, unadjusted corporate actions, look-ahead in fundamentals, costs eating the spread, regime change, factor crowding, operator abandonment, and silent data drift. The champion model is a hierarchical equal-weight, sector-neutral composite of six pre-registered factors; learned weights run as a shadow challenger that is a pure function of the matured-label evidence table (so it can never drift or double-count), and it cannot replace the champion before roughly month 36. The honest learning curve is not "weights got better"; it is "out-of-sample IC, measured with overlap-aware statistics on an expanding clean window, rises and its confidence band narrows". The design says exactly what would falsify it at months 12, 24 and 36.

Conventions in this document: every design element carries a `Guards:` line naming the failure mode it blocks. Numbers live in fenced blocks. Identifiers are decoded on first use. Nothing here is a measured performance claim; the only measured numbers are the ones already in the red-team review.

---

# 1. Objective & success metrics

## 1.1 Objective (one paragraph, no adjectives)

Every month, on the last trading day, produce a ranking of the Nifty 500 in which each stock is ranked *within its sector*, such that the top decile beats the bottom decile over the following 12 months after realistic trading costs; record what was known at that moment so that the claim can be tested later by anyone; and show, on one chart, whether that predictive power is real and whether it is rising as clean months accumulate.

The multi-bagger goal is expressed as a slow, lagging KPI (section 2.4), not as the training target. A model trained directly on "2x in 36 months" would have its first label in September 2029 and its first meaningful statistic around 2032. That is not a learning loop; that is a bet.

## 1.2 Success metrics with numeric targets and dates

All dates assume the first clean V2 snapshot is **2026-10-31** (month 1). "Clean" is defined in section 7.7. "IC" means Rank IC: the Spearman rank correlation, across stocks on one date, between the score and the subsequent return. "HAC t" is the t-statistic of the mean IC computed with a Newey–West (heteroskedasticity-and-autocorrelation-consistent) standard error that accounts for overlapping 12-month windows (section 7.3). "n_eff" is the number of non-overlapping 12-month observations: months of matured labels divided by 12.

```
Metric                                              Month 12      Month 24        Month 36        Month 60
------------------------------------------------------------------------------------------------------------
12m sector-neutral IC of champion (mean)            first label   >= +0.03        >= +0.04        >= +0.05
  HAC t-stat                                        n/a           >= 1.5          >= 2.0          >= 2.5
  n_eff (independent 12m observations)              0             1               2               4
Net-of-cost D10-D1 12m spread, annualised           n/a           > 0             >= +4%          >= +6%
Learning-curve slope (IC vs clean months), block-
  bootstrap 90% CI                                  n/a           reported        excludes 0      excludes 0
3m IC ladder (early-warning only, not a target)     >= +0.02      >= +0.02        >= +0.02        >= +0.02
Data-quality gate pass rate (rolling 12)            >= 10/12      >= 11/12        >= 11/12        >= 11/12
Share of imputed inputs among active factors        <= 15%        <= 10%          <= 10%          <= 10%
Hypotheses registered (cumulative cap)              <= 6          <= 12           <= 18           <= 30
Monthly loop wall-clock on a laptop                 < 60 min      < 60 min        < 60 min        < 60 min
Human time per month (read report + decide)         < 30 min      < 30 min        < 30 min        < 30 min
MB36 lift (section 2.4)                             n/a           n/a             first cohort    >= 1.5
```

Targets are targets. The numbers +0.03 / +0.04 / +0.05 were chosen because +0.05 sustained at 12 months is what published long-only factor composites in developed and Indian markets typically achieve out of sample; +0.10 would be exceptional and should be treated as a bug until proven otherwise.

## 1.3 What would falsify the approach

Falsification conditions are pre-committed here so nobody moves the goalposts later.

```
Checkpoint   Condition (all must hold)                                                  Consequence
--------------------------------------------------------------------------------------------------------------------
Month 12     shuffle test fails (|IC| > 2 SE on shuffled labels) in >= 2 of last 6      STOP. Leakage. Fix before
             months, OR the PIT reproducibility test (7.5) fails for any month           any further evaluation.
Month 24     champion 3m IC mean <= 0 AND 6m IC mean <= 0 over all clean months          Freeze factor set; run 12
                                                                                         more months; no new factors.
Month 36     champion 12m IC HAC t < 1.0  AND  net D10-D1 spread <= 0  AND  no active    Approach falsified for this
             price factor has HAC t >= 2.0                                               operator and data source.
                                                                                         Retire the fundamental
                                                                                         composite; keep only what
                                                                                         has t >= 2, or shut down.
Month 48     challenger (learned weights) has never beaten champion (12m IC diff HAC     Weight learning falsified.
             t < 1.0 over >= 24 months of paired record)                                 Weights stay equal forever;
                                                                                         "learning" = factor admission
                                                                                         and retirement only.
```

`Guards:` data snooping (targets and kill conditions are written before the data exist); operator abandonment (the owner knows in advance what "it is not working" looks like, so the project ends by decision rather than by neglect).

## 1.4 Why these are the right metrics and not the old ones

The current repository reports a single-period composite IC and an "IR" from three observations. Section 10 of the red-team review shows the standard error of a mean IC over three months is about 0.05, larger than every difference discussed. The metrics above all carry an explicit sample-size term (n_eff, HAC t, bootstrap CI), so a number cannot be quoted without its uncertainty. The dashboard (section 10.7) refuses to render an IC without its confidence band.

---

# 2. Prediction target & horizons

## 2.1 The label

For stock `i`, snapshot date `t` (last trading day of a month), horizon `h` in months:

```
TR_i(t, h)        = total-return index of i at end(t+h)  /  total-return index of i at t
                    (dividends reinvested on ex-date; splits and bonuses neutralised; section 4.3)
y_i(t, h)         = ln TR_i(t, h)  -  median_{j in bucket(i, t)} ln TR_j(t, h)
```

`bucket(i, t)` is the sector-neutralisation bucket the stock belonged to *at date t* (section 3.4). The label is the stock's log total return relative to the median of its own sector over the same window. Log returns so that a 2x and a 0.5x are symmetric; median rather than mean so one 10x name does not shift the whole sector's label.

Primary horizon: `h = 12`. Tracked horizons: `h in {1, 3, 6, 12, 24, 36}`. The primary horizon drives factor promotion, the challenger's weights and the headline chart. The 1/3/6-month rungs are diagnostics and early warnings. The 24/36-month rungs feed the multi-bagger KPI.

`Guards:` unadjusted corporate actions (total-return index, not quoted price); sector luck being booked as stock selection (sector-relative label); the mismatch the red team found between a 1-month objective and a multi-year goal.

## 2.2 Why 12 months, and why not 1 or 36

```
Horizon   Signal-to-noise for fundamentals   Cost drag at natural turnover   Independent obs after 3 years   Verdict
------------------------------------------------------------------------------------------------------------------------
1 month   low; dominated by trend/reversal    ~200%+ turnover/yr -> spread     36                              diagnostic only
          and microstructure                  mostly eaten (section 8.3)
3 months  moderate                            ~100-150%/yr                     12                              early warning
12 months good for quality/growth/value;      ~60-100%/yr with a hold band     3                               PRIMARY
          momentum still positive
36 months best match to "compounder"          low                              1                               slow KPI only
```

The three months already logged prove the 1-month problem: the composite's +0.117 in Aug→Sep was almost entirely a three-valued moving-average flag. A 12-month horizon is the shortest horizon at which a fundamentals-driven ranking has a fair chance to show up, while still producing three independent observations by month 48. The seed hypothesis (12 months primary) is correct; this design keeps it and makes the cost explicit: the first 12-month label matures at month 13, so the primary chart is empty for a year. The 3-month ladder fills that gap honestly, labelled as such.

## 2.3 Rules for labels

1. A label row is written only after `end(t+h)` has passed and the price checkpoint for that month exists. Until then the label does not exist (no "partial" returns are ever used in a statistic).
2. Once a stock has been scored at `t`, it is followed to maturity of every tracked horizon even if it leaves the Nifty 500, is suspended, or is delisted. Its price history keeps being ingested (section 4.5). `Guards:` survivorship in labels, which is the most common way a retail backtest overstates itself.
3. Delisting or a permanent halt: the label uses the last available total-return level and sets `delisted_flag = 1`. The evaluation reports every statistic twice: as-is, and with delisted names forced to `-50%` (a conservative involuntary-delisting assumption). If the two disagree materially, the report says so.
4. A suspected unrecorded corporate action in the window (section 4.4) sets `ca_flag = 1`; flagged rows are excluded from means and spreads but kept in the table with the flag, so nothing silently disappears.
5. Labels are never winsorised. Rank IC does not need it; decile means are reported as mean, median and 5%-trimmed mean side by side.

## 2.4 How "multi-bagger" becomes a measurable target

```
MB36_i(t)        = 1  if TR_i(t, 36) >= 2.0  else 0
base_rate(t)     = mean_i MB36_i(t)                          over the universe at t
hit_rate(t)      = mean_{i in top decile at t} MB36_i(t)
MB36_lift(t)     = hit_rate(t) / base_rate(t)                 (1.0 = no skill)
MB36_recall(t)   = #(top decile AND MB36) / #(MB36)
```

The base rate in the Nifty 500 varies enormously with the starting year (roughly 5% of names doubled in the 36 months from early 2008; over 40% did from March 2020), so recall alone is meaningless. Lift is the number the dashboard shows, with the base rate next to it. The first cohort matures in month 37; twelve monthly cohorts by month 48 are still about one independent observation. This KPI is reported, never optimised.

Intermediate proxy that matures faster and is mechanically linked to compounding: `Q1_12m_hit` = share of the top decile that lands in the top quartile of its sector's 12-month relative return. A stock that is top-quartile in two of three consecutive years is, in most windows, a 2x-plus name.

`Guards:` the target being unmeasurable during the owner's patience window; recall being quoted without base rate (survivorship of the "winners" list).

---

# 3. Universe & sector taxonomy

## 3.1 Universe source and point-in-time membership

Source: `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv`, verified live on 2026-09-05:

```
rows: 500
columns: Company Name, Industry, Symbol, Series, ISIN Code
distinct Industry values: 20
```

Every monthly run stores the file verbatim at `data/universe/nifty500_<asof>.csv` (about 40 KB, committed) and writes one `universe_membership` row per security. From October 2026 onwards, membership is therefore point-in-time by construction. Before that, only the current list exists; every backfilled month is marked `membership_source = 'survivor_backfill'` and any statistic computed on that period carries the label "survivorship-biased, descriptive only" in the report and the UI.

Fallback chain for the CSV: (1) live download, must have >= 480 rows and the five expected columns; (2) the most recent committed file, with `data_quality_event code='UNIVERSE_STALE'`; (3) BLOCK the run if the most recent file is older than 62 days. The old code's fallback to Nifty 50 is removed.

`Guards:` survivorship (membership recorded monthly; dropped names tracked to label maturity); silent universe shrinkage (the 50-stock fallback that used to exist).

## 3.2 Security identity: ISIN, not ticker

```
securities(security_id INTEGER PK, isin TEXT UNIQUE, name TEXT, first_seen TEXT, last_seen TEXT, status TEXT)
symbol_history(security_id, nse_symbol, yahoo_ticker, valid_from, valid_to)
```

Tickers change (ZOMATO became ETERNAL in 2025); ISINs almost never do. All internal tables key on `security_id`. The Yahoo ticker is `nse_symbol + '.NS'` unless overridden in `config/manual_ticker_overrides.csv`. When the CSV shows a new symbol against a known ISIN, the run closes the old `symbol_history` row and opens a new one; price history is stitched by ISIN.

`Guards:` silent identity drift (a renamed ticker looking like a delisting plus a new listing, which breaks momentum and label continuity).

## 3.3 Canonical sector source: NSE sector level from the constituent file

The seed hypothesis proposes the four-level NSE/AMFI classification (Macro-Economic Sector → Sector → Industry → Basic Industry) as canonical. From this lens that is half right. The constituent CSV's `Industry` column *is* the NSE level-2 "Sector" (20 values: Financial Services, Capital Goods, Healthcare, ...). It arrives free, monthly, with ISINs, and can be snapshotted point-in-time from today. The full four-level AMFI file is a spreadsheet published irregularly with no history; depending on it for neutralisation would make the canonical mapping non-reproducible. So:

- **Canonical for neutralisation:** NSE level-2 sector from the monthly CSV (`sector_l2`).
- **Optional finer level:** AMFI Basic Industry, loaded from a manually dropped file `data/external/amfi_classification_<date>.xlsx` when present, stored as `industry_l4`, used only for descriptive features, never for neutralisation.
- **Fallback only:** Yahoo `sector`, mapped through a versioned crosswalk.

Why Yahoo cannot be canonical, measured on the 2026-09-03 snapshot against today's CSV:

```
NSE 'Capital Goods'      (63 names) -> Yahoo: Industrials 49, Basic Materials 7, Technology 5, Consumer Cyclical 1, Utilities 1
NSE 'Consumer Durables'  (16 names) -> Yahoo: Consumer Cyclical 7, Industrials 3, Technology 3, Basic Materials 2, UNKNOWN 1
NSE 'Financial Services' (101)      -> Yahoo: Financial Services 97, Technology 4
NSE 'Telecommunication'  (10)       -> Yahoo: Communication Services 7, Technology 3
```

The two taxonomies disagree on roughly one name in five outside financials. Mixing them across months would create fake reclassifications.

`Guards:` silent data drift in sector labels; irreproducible neutralisation.

## 3.4 Neutralisation buckets (versioned config)

Within-sector ranking needs enough names per bucket. Three NSE sectors are too small. `config/sector_buckets_v1.yaml` merges them:

```
neutral_bucket                          nse_sector_l2 members                                  n (2026-09)
--------------------------------------------------------------------------------------------------------
Financial Services                      Financial Services                                     101
Capital Goods                           Capital Goods                                          63
Healthcare                              Healthcare                                             48
Automobile & Components                 Automobile and Auto Components                         38
Consumer Services                       Consumer Services                                      29
FMCG                                    Fast Moving Consumer Goods                             28
Information Technology                  Information Technology                                 27
Chemicals                               Chemicals                                              26
Consumer Durables & Textiles            Consumer Durables, Textiles                            21
Metals & Mining                         Metals & Mining                                        18
Power                                   Power                                                  17
Oil, Gas & Consumable Fuels             Oil Gas & Consumable Fuels                             17
Services & Diversified                  Services, Diversified                                  17
Telecom & Media                         Telecommunication, Media Entertainment & Publication    15
Construction                            Construction                                           13
Construction Materials                  Construction Materials                                 11
Realty                                  Realty                                                 11
                                                                                         total 500, 17 buckets, min 11
```

Rule: a bucket must hold >= 8 members at every snapshot; if a future constituent change breaks that, the run raises a proposal to revise the bucket file (a new `taxonomy_version`), it does not silently merge. Financial Services at 101 names is one bucket in v1; splitting banks / NBFCs / capital-market firms is a registered open question (section 12) because financial-statement fields differ and the bucket is large enough to hide within-bucket sector bets.

## 3.5 The `sector_map` table and reclassification

```sql
CREATE TABLE sector_map (
  security_id      INTEGER NOT NULL,
  sector_l2        TEXT NOT NULL,          -- NSE level-2 sector text as published
  neutral_bucket   TEXT NOT NULL,          -- from config/sector_buckets_v<N>.yaml
  industry_l4      TEXT,                   -- AMFI basic industry when available
  source           TEXT NOT NULL,          -- 'nse_csv' | 'nse_csv_prior' | 'yahoo_crosswalk' | 'manual' | 'backfilled_current'
  confidence       REAL NOT NULL,          -- 1.0 nse_csv; 0.9 prior; crosswalk majority share; 0.5 manual
  taxonomy_version INTEGER NOT NULL,
  valid_from       TEXT NOT NULL,          -- ISO date, inclusive
  valid_to         TEXT,                   -- ISO date, exclusive; NULL = current
  PRIMARY KEY (security_id, valid_from)
);
```

Scoring at date `t` uses the row with `valid_from <= t AND (valid_to IS NULL OR t < valid_to)`. When the CSV shows a different `sector_l2` for an ISIN than the open row, the run closes the open row at `t` and opens a new one; a `data_quality_event code='SECTOR_RECLASS'` is written; the label for snapshots before `t` keeps the old bucket (labels are computed with `bucket(i, t)` frozen at snapshot time). Nothing is rewritten.

Fallback chain when a security is missing from the current CSV (it dropped out of the index but is still tracked to label maturity):

```
1. nse_csv          current month's file                               confidence 1.0
2. nse_csv_prior    last NSE mapping for this ISIN, <= 24 months old     confidence 0.9
3. yahoo_crosswalk  Yahoo sector -> NSE sector via                       confidence = majority share
                    config/yahoo_to_nse_crosswalk_v1.csv                 (e.g. Industrials->Capital Goods 0.53)
4. Unclassified     ranked against the whole universe, flagged           confidence 0.0
```

Only sources 1 and 2 count as "clean" for the learning-curve series.

## 3.6 Sector-level features

Registered as factors (section 5) with their own status, computed from the engine's own price store so they never depend on an external feed:

```
factor_key            formula                                                             direction  status at launch
--------------------------------------------------------------------------------------------------------------------
sector_mom_6m@1       equal-weight bucket log TR over months t-6..t-1 (skip last month)   +          shadow
sector_breadth@1      share of bucket members with close > 200-day SMA at t               +          shadow
sector_rs_12m@1       bucket 12m TR minus universe 12m TR                                 +          candidate
sector_fpi_flow@1     FPI (foreign portfolio investor) net sector flow, last fortnight,   +          candidate,
                      from the NSDL sector-wise report, as a manually dropped CSV in                 external adapter,
                      data/external/fpi_sector_flows.csv with a published_on column                 never a dependency
```

Sector features are *excluded* from the sector-neutral composite by construction (they are constant within a bucket). They can only enter a separately reported "tilted" composite, and only after promotion. This keeps the primary claim (stock selection within sector) uncontaminated.

`Guards:` factor crowding / regime change (sector tilt is a separate, measurable, switchable bet, not baked into the ranking).

---

# 4. Data layer

## 4.1 Data flow

```
                 monthly, last trading day T (run on first weekday after, 20:00 IST)
                 ─────────────────────────────────────────────────────────────────────
 niftyindices CSV ──► data/universe/nifty500_T.csv ──► universe_membership, securities, symbol_history, sector_map
                                                                     │
 yfinance.download (batched, 13 months daily, 50 tickers/batch, 1 s between batches)
        │                                                            │
        ▼                                                            ▼
 data/prices.sqlite (local, gitignored)  ──reconcile overlap──►  corporate_actions, data_quality_events
   prices_daily(security_id, date, o,h,l,c, volume, dividend, split_ratio, download_asof)
        │
        ├──► TR index per security (4.3) ──► price_checkpoints(month_end, close, tr_index, adv20, adv60, mcap)  [committed]
        │
 yfinance.Ticker per stock (0.5 s sleep; info + 3 annual statements + quarterly income)
        │
        ▼
 fundamentals_pit(security_id, field, period_end, value, unit, observed_at = T)                                [committed]
        │
        ▼
 field_contracts check ──► data_quality_events ──► GATE (BLOCK / WARN / INFO)
        │
        ▼  (only if not BLOCKED)
 factors.compute(asof=T) reads ONLY rows with observed_at <= T  ──► factor_values                             [committed]
        │
        ▼
 model.score ──► scores (champion, challenger, shadows)  ──► portfolio.rebalance ──► portfolio_* , scoreboard  [committed]
        │
        ▼
 labels.mature(asof=T): for every earlier snapshot s with s+h <= T  ──► labels                                 [committed]
        │
        ▼
 evaluate ──► evaluations, learning_curve ──► knowledge.report + proposals ──► ui/data.js ──► git commit
```

## 4.2 Point-in-time storage: the one rule

Every fact that can change after first publication is stored as an observation, never as a value:

```sql
CREATE TABLE fundamentals_pit (
  security_id          INTEGER NOT NULL,
  field                TEXT NOT NULL,        -- canonical field name from config/field_contracts_v1.yaml
  period_end           TEXT NOT NULL,        -- fiscal period the value describes (ISO date); '' for point fields like mcap
  value                REAL,
  unit                 TEXT NOT NULL,        -- 'inr' | 'frac' | 'pct' | 'x' | 'shares' | 'days'
  source               TEXT NOT NULL,        -- 'yf.info' | 'yf.financials' | 'yf.balance_sheet' | 'yf.cashflow' | 'yf.quarterly_financials' | 'yf.major_holders' | 'manual'
  observed_at          TEXT NOT NULL,        -- date this engine first saw the value (= run asof for live runs)
  observed_at_imputed  INTEGER NOT NULL DEFAULT 0,  -- 1 when observed_at was derived by the lag rule (backfill)
  run_id               INTEGER NOT NULL,
  PRIMARY KEY (security_id, field, period_end, observed_at)
);
```

A factor computing at `asof = t` may read a value only if `observed_at <= t`; among several observations of the same `(security_id, field, period_end)` it takes the one with the greatest `observed_at <= t`. Restatements therefore appear as new rows and the old value stays available for the months in which it was the truth.

Lag rule for backfilled statements (no observed_at known): Indian listed companies must publish audited annual results within 60 days of fiscal year end and quarterly results within 45 days. Yahoo lags further. So `observed_at = period_end + 75 days` (annual) or `+ 60 days` (quarterly), `observed_at_imputed = 1`. Any factor whose inputs include an imputed observation is counted in the "imputed share" hygiene metric.

`Guards:` look-ahead in fundamentals (the current harness reads `stock.financials` at run time, which is fine live but would leak if used to backfill scores); silent restatement.

## 4.3 Prices, corporate actions and total returns

What Yahoo actually returns (verified 2026-09-05 on HEROMOTOCO.NS):

```
history(period='max', auto_adjust=False, actions=True)  -> 6006 rows from 2002-07-01
columns: Open, High, Low, Close, Adj Close, Volume, Dividends, Stock Splits
Close      : split-adjusted by Yahoo (bonus issues appear as splits), NOT dividend-adjusted
Adj Close  : split- and dividend-adjusted, re-computed by Yahoo on every new dividend (retroactive)
```

Design:

1. Store `Close`, `Volume`, `Dividends`, `Stock Splits` per day in `prices_daily`, plus `download_asof`. Do **not** store Yahoo's `Adj Close` as truth; store it in a side column `yahoo_adj_close` purely for cross-checking.
2. Build the total-return index deterministically in-engine:

```
TR_0 = 1
TR_t = TR_{t-1} * (Close_t + Dividend_t) / Close_{t-1}
```

   Since Yahoo's `Close` is already split-consistent within one download, splits need no extra term. Dividends are added on the ex-date.
3. Reconciliation on every monthly ingest: the download covers the last 13 months for every tracked security (cheap: about 10 batched calls). For the 12 overlapping months, compare the newly downloaded `Close` with the stored `Close`:
   - identical within 0.05%: fine;
   - differs by a constant ratio that matches a newly reported `Stock Splits` event: apply that ratio to the stored history for that security, write `corporate_actions` and a `data_quality_event code='SPLIT_RESTATED'` (logged, deterministic, reversible);
   - differs otherwise: `code='UNEXPLAINED_PRICE_REVISION'`, severity WARN per security, BLOCK if more than 2% of the universe is affected in one run; the stored values are kept and the new ones quarantined in `prices_daily_quarantine` until a human accepts them with `quant data accept-revision`.
4. Cross-check: monthly in-engine TR vs Yahoo `Adj Close` return for the same month; tolerance 30 bp; violations logged as `code='TR_MISMATCH'`.

`Guards:` unadjusted corporate actions (the ZFCVINDIA 6:1 split that produced a "-84% return"); retroactive rewriting of history by the vendor; silent drift between two downloads of "the same" series.

## 4.4 Unrecorded corporate actions (demergers, rights)

Yahoo does not encode demergers or rights issues as actions; the price simply drops. Detector, run per security per ingest:

```
if |1-day log return| > ln(1.25) and no Dividends/Stock Splits on that date:
    write data_quality_event(code='SUSPECTED_UNRECORDED_CA', severity=WARN, security_id, date, ret)
    set ca_flag = 1 on every label whose window contains that date
```

The monthly report lists these names. A human either confirms it was a genuine move (`quant data clear-ca-flag`) or enters the action manually in `corporate_actions` (`source='manual'`), after which the TR index is rebuilt for that security.

## 4.5 Ingest scope and rate limits

```
Per monthly run
  universe CSV                                   1 request        ~1 s
  yf.download, 13 months daily, all tracked      ~11 batches x 50  ~1-2 min   (1.0 s sleep between batches, threads=False)
    tracked = current 500 + names still inside an open label window (grows to ~550 over time)
  per-security fundamentals                      500 x 5 calls     ~25-30 min (0.5 s sleep per security retained; ~2.5 s of calls)
    calls: info, financials, balance_sheet, cashflow, quarterly_financials  (news is dropped)
  factors + scores + labels + evaluation + report                 < 3 min
  TOTAL                                                           ~35 min, under the 60-min budget
```

The 0.5 s per-security throttle from `AGENTS.md` is kept for the per-ticker path. If Yahoo returns HTTP 429, the run sleeps 120 s and resumes from the last completed security; a run that cannot finish fundamentals for >= 90% of the universe is marked `status='partial'` and does not write `scores` (prices and checkpoints are still written, because they are needed for labels).

## 4.6 Backfill plan for price history

```
quant data backfill --years 10 --batch 50 --sleep 1.5
  500 current constituents x period='max'  -> ~1.24 M daily rows (500 x 10 y x ~247 days)
  local size: data/prices.sqlite ~90 MB (gitignored)
  wall-clock: ~10 batches x ~15 s + sleeps  -> ~5 min; re-runnable; resumes by security
  output: data/manifest/prices_manifest.json  (per security: first_date, last_date, n_rows, sha256 of the series, download_asof, yfinance_version)  [committed]
```

What the backfill is for: computing price-based factors that need history (12-1 momentum, 252-day volatility, 200-day SMA, ADV) from the first live snapshot, and producing a *descriptive*, survivorship-biased pre-registration prior for those factors. What it is **not** for: the learning curve, the scoreboard, or any promotion decision. Fundamental factors are not backfilled at all beyond the 5 annual / 6 quarterly periods Yahoo exposes, and those are used only to compute the first live snapshot's trailing values with the lag rule.

`Guards:` survivorship (the backfill contains only today's survivors, so it is quarantined from inference); data snooping (no factor is admitted because it "backtested well" on the survivor set).

## 4.7 Field contracts and drift detection

`config/field_contracts_v1.yaml` mirrored into `field_contracts`; each ingest checks every field:

```
field                  unit   min      max      max_null_rate  source          note
------------------------------------------------------------------------------------------------------------
close_inr              inr    0.5      1e6      0.00           yf.download
volume_shares          shares 0        1e10     0.00           yf.download
dividend_inr           inr    0        5e4      -              yf.download
market_cap_inr         inr    1e9      1e14     0.02           yf.info         floor = Rs 100 crore
shares_outstanding     shares 1e6      1e11     0.05           yf.info
dividend_rate_inr      inr    0        5e4      0.40           yf.info         preferred over dividendYield
dividend_yield_frac    frac   0        0.15     0.40           derived         = dividend_rate_inr / close; the 349% bug lived here
debt_to_equity_x       x      0        15       0.15           derived         total_debt / total_equity from balance sheet; yf 'debtToEquity' is PERCENT (3.57 == 3.57%)
inst_held_frac         frac   0        1        0.10           yf.major_holders
insider_held_frac      frac   0        1        0.10           yf.major_holders  promoter proxy
roe_frac               frac   -2       2        0.30           derived         net_income / avg equity; yf 'returnOnEquity' is often None
roce_frac              frac   -2       3        0.20           derived         EBIT / (total assets - current liabilities)
revenue_inr            inr    0        1e14     0.05           yf.financials   per period_end
net_income_inr         inr    -1e13    1e13     0.05           yf.financials
ebit_inr               inr    -1e13    1e13     0.10           yf.financials
ocf_inr                inr    -1e13    1e13     0.10           yf.cashflow
capex_inr              inr    -1e13    1e13     0.15           yf.cashflow
total_assets_inr       inr    1e8      1e15     0.05           yf.balance_sheet
current_liab_inr       inr    0        1e15     0.15           yf.balance_sheet
total_debt_inr         inr    0        1e15     0.15           yf.balance_sheet
total_equity_inr       inr    -1e13    1e15     0.05           yf.balance_sheet
```

Drift check: for each numeric field, the run compares this month's cross-sectional distribution with the pooled previous three months using the population stability index (PSI; a divergence measure that is roughly 0 for identical distributions and above 0.25 for a materially shifted one). `PSI > 0.25` on any field used by an active factor writes `code='FIELD_DRIFT'` (WARN); on three or more fields, BLOCK. `yfinance.__version__` is pinned in `requirements.txt` and recorded on every `runs` row; a version change without a matching decision record is a WARN.

Gate levels:

```
BLOCK (no scores written; prices/checkpoints still written; report says why)
  priced securities < 450  |  universe file < 480 rows or missing columns  |  UNEXPLAINED_PRICE_REVISION > 2% of universe
  any active-factor input field null rate > its max_null_rate + 0.10  |  FIELD_DRIFT on >= 3 active-factor fields
  median |close_new / close_stored - 1| over the overlap window > 0.1%  |  PIT reproducibility test (7.5) failed last month and unresolved
WARN  (scores written, month flagged not-clean if the warning touches an active factor)
  any single contract violation above 5% of universe  |  SUSPECTED_UNRECORDED_CA on > 5 names  |  yfinance version changed
INFO  everything else, kept for the monthly report
```

`Guards:` silent data drift (the dividend-yield x100 and ROE-None bugs would have been BLOCKs on day one under these contracts).

## 4.8 Storage format and what is committed to git

Constraints: the working Python has **no pyarrow, duckdb or fastparquet** (checked 2026-09-05), the owner wants history in git, and GitHub LFS quotas and binary-file merges are two classic ways a hobby repo dies. Decision:

```
Artifact                                   Where                          Committed?   Size / growth
--------------------------------------------------------------------------------------------------------------
Daily bars, 10 y x 500 names               data/prices.sqlite             NO           ~90 MB; regenerable; manifest committed
Prices manifest                            data/manifest/*.json           YES          ~150 KB, rewritten monthly
Universe CSV per month                     data/universe/*.csv            YES          40 KB / month
Main state DB (schema v2)                  quant_engine.db                YES          starts ~3 MB after raw_json is moved
                                                                                        out; grows ~0.6 MB / month after VACUUM
Monthly ledger (text export of every       data/ledger/YYYY-MM/*.csv      YES          ~400 KB text / month, delta-friendly
  row written that month: checkpoints,
  fundamentals_pit, factor_values, scores,
  labels, evaluations, events, decisions)
Knowledge base                             knowledge/**                   YES          text
UI payload                                 ui/data.js                     YES          ~300 KB (was 1.3 MB; trimmed in 10.7)
```

The **ledger CSVs are the canonical record**; `quant db rebuild` reconstructs `quant_engine.db` from `data/ledger/` and `data/universe/`, and the CI test asserts the rebuilt DB matches the committed one row-for-row. The binary DB stays committed because the owner asked for it and it is convenient, but if two branches ever conflict on it, the rule is: delete, rebuild from the ledger, recommit.

Parquet is permitted as an *optional* adapter (`quant data export-parquet`) if `pyarrow` is present; it is not a dependency. Over ten years the committed footprint is roughly 100 MB of mostly text, which plain git handles.

`Guards:` operator abandonment via repo bloat or LFS billing surprises; unmergeable binary state; vendor disappearance (the ledger keeps every label and factor value that was ever used, so the learning curve survives even if Yahoo Finance does not).

---

# 5. Factor library

## 5.1 Plugin contract

File: `quant/factors/base.py`

```python
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol
import pandas as pd

Status = Literal['candidate', 'shadow', 'active', 'retired', 'quarantined']

@dataclass(frozen=True)
class FactorSpec:
    name: str                       # 'mom_12_1'
    version: int                    # formula change => version bump => new factor_key 'mom_12_1@2'
    family: str                     # 'trend' | 'risk' | 'quality' | 'growth' | 'value' | 'flow' | 'sector'
    hypothesis: str                 # one sentence, falsifiable
    expected_sign: int              # +1 or -1 : sign of the 12m sector-neutral IC the hypothesis predicts
    horizons_m: tuple[int, ...]     # horizons at which the hypothesis is claimed, e.g. (6, 12)
    inputs: tuple[str, ...]         # field names from field_contracts, or 'prices_daily'
    min_history_days: int           # e.g. 273 for 12-1 momentum
    neutralise: bool = True         # rank within neutral_bucket
    winsor: tuple[float, float] = (0.01, 0.99)
    registered_on: date = None
    registered_by: str = ''
    evidence: str = ''              # prior evidence: papers, Indian index track records, or 'none'
    status: Status = 'candidate'

    @property
    def factor_key(self) -> str:
        return f'{self.name}@{self.version}'

@dataclass
class FactorContext:
    asof: date
    universe: pd.Index                     # security_id of names to score at asof
    prices: 'PriceStore'                   # .close(security_ids, start, end), .tr_index(...), .adv(...); refuses dates > asof
    fundamentals: 'PITStore'               # .latest(field, asof, security_ids) -> Series; enforces observed_at <= asof
    sectors: pd.Series                     # security_id -> neutral_bucket at asof

class Factor(Protocol):
    spec: FactorSpec
    def compute(self, ctx: FactorContext) -> pd.Series:
        """Raw factor value indexed by security_id. NaN where not computable. Must be deterministic."""
```

Registry: `knowledge/registry/factors.yaml` is the single source of truth; `quant/factors/registry.py` loads it, imports the plugin module named in the entry, and refuses to compute any factor whose `registered_on` is after `ctx.asof` (test: `test_registry_refuses_precommit_compute`). The SHA-256 of the YAML is written to every `runs` row.

Post-processing pipeline (shared, in `quant/model/neutralise.py`):

```
raw  ->  drop if coverage < 60% of universe (factor marked 'insufficient' this month)
     ->  winsorise at (1%, 99%) across the whole universe
     ->  within neutral_bucket: z = (x - mean) / std, clipped to [-3, 3]; and pct = rank / n
     ->  store raw_value, winsor_value, neutral_z, neutral_pct, bucket, coverage_flag  in factor_values
```

`Guards:` silent redefinition (version bump is mandatory and history is never recomputed); data snooping (registration precedes computation, enforced in code); bucketed scores (continuous values replace the 5-level buckets that made three legacy factors near-constant).

## 5.2 Initial factor list (pre-registered in the first commit)

Sign: the sign of the 12-month sector-neutral IC the hypothesis predicts. Evidence grades: A = replicated in India and in the international literature and has a live Indian index product; B = international literature, weak or mixed Indian evidence; C = plausible, no formal evidence. Status is the launch status.

```
factor_key            family   formula (at asof t)                                                    sign  horizons   evid  status
--------------------------------------------------------------------------------------------------------------------------------------
mom_12_1@1            trend    ln TR(t-12m -> t-1m)                                                     +    6,12      A     active
trend_200@1           trend    close_t / SMA_200(t) - 1                                                 +    3,6,12    A     active   (replaces the death-cross kill)
lowvol_252@1          risk     - std(daily log returns, last 252 trading days) * sqrt(252)               +    12        A     active
quality_roce@1        quality  EBIT_ttm / (total_assets - current_liab), latest PIT annual                +    12        A     active
accruals@1            quality  - (net_income_ttm - ocf_ttm) / total_assets                               +    12        B     active   (Sloan accruals; cash-conversion done properly)
growth_rev_3y@1       growth   ln(revenue_fy0 / revenue_fy-3) / 3, PIT annual, needs 4 fiscal years       +    12        B     active   (the owner's core thesis; weak prior, kept honest)
earnings_yield@1      value    EBIT_ttm / EV  (non-financials); net_income_ttm / mcap (financials)        +    12        B     shadow   (value has been weak in India 2012-2024; needs its own evidence)
pb_sector@1           value    - ln(mcap / total_equity)                                                  +    12        B     shadow
leverage@1            risk     - (total_debt - cash) / EBITDA_ttm, non-financials only                    +    12        B     shadow   (continuous replacement of the near-constant balance-sheet factor)
inst_hold_chg@1       flow     inst_held_frac(t) - inst_held_frac(t-3m), from this engine's own PIT rows  +    3,6,12    C     shadow   (proxy for Yahoo's update timing; declared as such)
str_1m@1              trend    - ln TR(t-1m -> t)                                                        +    1,3       B     shadow   (short-term reversal; diagnostic for the 1m ladder)
illiq@1               risk     - ln ADV_60(t)                                                             +    12        B     candidate (also the liquidity screen input; do not double-use before evidence)
sector_mom_6m@1       sector   see 3.6                                                                    +    6,12      B     shadow
sector_breadth@1      sector   see 3.6                                                                    +    3,6       C     shadow
legacy_death_cross@0  legacy   the stored 0/0.8/1.0 multiplier, migration only                            +    1         -     retired  (kept so the 2026 history can be reproduced)
```

Retired at migration, with decision records (`ADR-202610-01..05`): `moat` (a hand-typed list including names already known to have compounded: look-ahead written into the factor), `strategic_risk` (hand-typed), `headline_sentiment` (bypassed the weight budget; input unreliable), `dcf_margin_of_safety` (zero for 63% of the universe; five free parameters), `trap_score` as a multiplier (its usable components, leverage and accruals, become continuous factors). The DCF may still be shown on the stock page as a descriptive number; it is not a factor.

Why six active factors and not twelve: every extra factor is a hypothesis, and section 9.6 charges each one against a multiple-testing budget. Six is enough to cover four families. The initial actives are the ones with grade A or a direct mandate from the owner (growth).

## 5.3 Families and the anti-stuffing rule

The champion is equal-weight **by family, then by factor within family** (section 6.1). Registering three momentum variants therefore cannot triple the momentum weight. Families at launch: `trend` (mom_12_1, trend_200), `risk` (lowvol_252), `quality` (quality_roce, accruals), `growth` (growth_rev_3y). `value` and `flow` families exist in the registry but have no active member, so the champion has no value or flow exposure until one is promoted.

`Guards:` gaming the "equal-weight baseline" by adding correlated copies; factor crowding via accidental over-concentration in one style.

## 5.4 Status lifecycle

```
                 register (YAML + ADR, before any computation)
   candidate ─────────────────────────────────────────────► shadow  (computed and stored monthly, weight 0, paper-tracked)
                                                               │
                             promotion criteria met (9.5) and approved
                                                               ▼
                                                            active  (in champion; family-equal weight)
                                                               │
                  retirement criteria met (9.5) and approved   │        data contract broken
                                                               ▼        ▼
                                                            retired  quarantined (auto, reversible; excluded from stats until cleared)
                                                               │
                       still computed for 12 more months for the post-mortem, then frozen
```

Every transition writes a `decisions` row and an ADR file. `quarantined` is the only automatic transition (a safety action taken by the gate), and it is reversible by a decision.

## 5.5 Pre-registration record (what must exist before month 1 of shadow)

`knowledge/registry/factors.yaml` entry (example):

```yaml
- name: mom_12_1
  version: 1
  family: trend
  module: quant.factors.price.momentum:Mom12_1
  hypothesis: "Stocks with higher 12-month return, skipping the latest month, continue to outperform sector peers over the next 6-12 months."
  expected_sign: +1
  horizons_m: [6, 12]
  inputs: [prices_daily]
  min_history_days: 273
  registered_on: 2026-10-01
  registered_by: owner
  evidence: "A. Jegadeesh-Titman (1993); Indian replications; Nifty 200 Momentum 30 live since 2020."
  status: active
  hypothesis_id: H-2026-001
```

The matching ADR states the predicted sign and the horizon in prose, and the *decision rule that will retire it*. If a factor is later found to have a sign opposite to its registration, it is retired, not flipped (flipping after seeing the data is snooping; a flipped version may be registered as a new hypothesis and must earn its way through shadow).

---

# 6. Scoring model & weight learning

## 6.1 Champion: hierarchical equal weight, sector-neutral

```
for each active factor f:      z_f(i)     = neutral_z from factor_values (already within-bucket)
for each family F:             fam_F(i)   = mean over active f in F with non-NaN z_f(i)       (NaN if none)
composite:                     comp(i)    = mean over families F with non-NaN fam_F(i)
coverage rule:                 require >= 3 of the 4 launch families present, else eligible = 0, reason 'coverage'
final:                         composite_pct(i) = rank of comp within bucket(i) / n_bucket   (0..1; UI shows 0..100)
```

Equal weight is the permanent baseline because, with fewer than about five independent years of evidence, no learned weight vector can be shown to beat it, and the red team already measured learned weights losing to it on the legacy data. `Guards:` regime change (equal weight is the minimum-regret allocation when the regime is unknown); data snooping.

## 6.2 Hard filters that remain, and how filtered names are evaluated

The seed hypothesis is right that the 0.0x death-cross kill must go, and this design goes further: **no score multiplier of any kind survives**. `final = base x trap x momentum` is replaced by `composite_pct` alone. Trend is a factor; leverage is a factor; nothing zeroes a rank.

Two eligibility filters remain, both applied *after* scoring and both recorded per stock in `scores.eligible / eligibility_reason` so that every filtered cohort is evaluated separately every month:

```
filter       rule                                                           effect
-------------------------------------------------------------------------------------------------------------
liquidity    ADV_60 (60-day average daily traded value, INR) < Rs 2 crore   scored, not portfolio-eligible
             OR traded on < 90% of the last 60 sessions
coverage     fewer than 3 of 4 families computable, or no price checkpoint  not scored ('unscored' cohort)
```

The monthly report contains a table "excluded cohorts": mean 12m return and IC of `illiquid` and `unscored` names versus the eligible set. If the excluded names systematically outperform, the filter is costing alpha and a proposal is raised.

`Guards:` a filter destroying rank information for a third of the universe (the red team's central finding); a filter's cost going unmeasured.

## 6.3 Challenger: shrunk, sign-constrained IC weights as a pure function of evidence

The current optimizer is a stateful multiplicative update (`w_new = w_old * exp(...)`), which is exactly why it double-applied gradients and pinned Growth at 30%. The challenger's weights are instead recomputed from scratch each month from the evidence table; there is no state to corrupt and re-running is trivially idempotent.

```
Inputs at month T, for each active factor f:
  IC_f,s        = 12m sector-neutral Rank IC of f at snapshot s, for every s with s + 12m <= T (matured only)
  ICbar_f       = mean_s IC_f,s
  se_f          = HAC standard error of ICbar_f with Bartlett lag 11        (section 7.3)
  n_eff         = (number of matured snapshots) / 12
Shrinkage:
  lambda        = n_eff / (n_eff + k),      k = 3     (prior strength = three independent years)
  ev_f          = max(0, sign_f * ICbar_f)            (evidence against the registered sign counts as zero, never negative weight)
  w_ic_f        = ev_f / sum_g ev_g                   (if sum is 0, w_ic = w_eq)
  w_f           = (1 - lambda) * w_eq_f  +  lambda * w_ic_f
Bounds and normalisation (N = number of active factors):
  floor = 0.4 / N,  ceiling = 2.4 / N                 (for N = 8 this reproduces the legacy 5% / 30%)
  clip, renormalise to sum 1, round to 3 dp, add the rounding residue to the largest weight (sum == 1.000 exactly)
Composite:
  comp_chal(i)  = sum_f w_f * z_f(i), then re-ranked within bucket like the champion
```

Behaviour of `lambda` over time, so nobody is surprised:

```
month   matured 12m snapshots   n_eff   lambda
 13         1                   0.08     0.03
 24        12                   1.0      0.25
 36        24                   2.0      0.40
 48        36                   3.0      0.50
 84        72                   6.0      0.67
```

The challenger never fully trusts the data. Weights per family are also reported so the owner can see whether the challenger is quietly becoming a momentum fund.

Weights are stored in `weight_sets` every month with `method='shrunk_ic_v1'`, `n_eff`, `lambda` and the evidence JSON, so any month's challenger can be recomputed bit-for-bit from the ledger (test `test_challenger_weights_reproducible`).

Not in v1, by decision: regime-conditional weights, horizon mixing (the challenger learns only from 12m labels; the 1m and 3m rungs are diagnostics), and any gradient or optimiser with a learning rate. `Guards:` regime change and data snooping (regime labels are assigned after the fact, as the red team noted); the non-idempotent optimizer; the 1-month trend flag steering a 12-month model.

## 6.4 Minimum evidence before deviating, and champion/challenger

- The challenger is scored, stored and paper-traded from month 1 (its weights equal the champion's until month 13 because `lambda ~ 0`).
- It cannot become the champion before the criteria in section 9.5 are met: at least 36 months of paired live record, 12m IC difference HAC t >= 2.0, and a positive net paper IR difference. Realistically that is month 36-48 at the earliest.
- If promoted, the old champion continues as a challenger so the swap is reversible with the same evidence standard.
- The seed hypothesis' "12 non-overlapping periods before deviating" is the right instinct but is undefined at a 12-month horizon (it would mean 12 years). The design replaces it with the explicit `lambda` schedule (deviation is allowed but shrunk) plus a hard promotion gate.

## 6.5 What happens to today's death cross

Retired as a filter and as a multiplier at migration (`ADR-202610-06`). Its information content is carried by `trend_200@1` (continuous, sector-neutral). The legacy multiplier is preserved as `legacy_death_cross@0` in `factor_values` for the four 2026 snapshots so that the attribution table in the red-team review can be regenerated from the new schema (acceptance test in section 10.6).

Consequence to state plainly: the new champion will hold names in downtrends if their other factors are strong. The trend family carries 25% of the champion weight (one of four families). If the owner wants a heavier trend tilt, the route is a registered proposal, not a multiplier.

---

# 7. Evaluation protocol

## 7.1 What is evaluated every month

`quant evaluate --asof T` computes, for every snapshot `s` and horizon `h` with `s + h <= T` that has not been evaluated yet (plus a full recomputation of rolling aggregates):

```
subject_kind   subject_key                 metrics per (s, h)
------------------------------------------------------------------------------------------------------------
factor         each factor_key (all       rank IC (sector-neutral, pooled within-bucket ranks); rank IC (raw);
               statuses except candidate)  decile mean/median/trimmed-mean sector-relative log return; D10-D1 gross;
                                           coverage; n
score_set      champion_v1, challenger_v1  same, plus D10-D1 net of cost (using decile turnover measured s-1 -> s),
                                           hit rate (share of top decile with positive sector-relative return),
                                           Q1_12m_hit, MB36 lift when h = 36
benchmark      ew_universe, cw_universe,   total return over (s, h); used by the scoreboard, not IC
               ew_sector_matched, mom30_inhouse, quality50_inhouse, ext_nifty500_price
cohort         illiquid, unscored,         mean return and IC of the excluded cohorts vs eligible
               delisted
```

Sector-neutral IC, precisely: within each bucket rank both the score and the label to (0,1]; pool across buckets; Spearman correlation on the pooled ranks. Also reported: the bucket-size-weighted mean of within-bucket ICs. The pooled number is primary.

## 7.2 Walk-forward with embargo

The challenger at month `T` uses only labels with `end(s + 12m) <= T`. That is an embargo of exactly the horizon: the most recent twelve snapshots have unmatured labels and contribute nothing. The scores it produces at `T` are then labelled at `T + 12m`. So every challenger IC in `evaluations` is strictly out of sample by construction; there is no separate "backtest" mode that could be misconfigured.

For the descriptive, survivorship-biased backfill of price factors (section 4.6), the same code path is used with `asof` stepping monthly through history; the report prints it in a separate block titled "Backfill (biased) — not evidence".

```
    s          s+12m                T
    |-----------|------------------->|
    label of s matures here          challenger at T may use s (and everything older)
                 |<--- 12 months --->|   snapshots in here: unmatured, excluded (embargo)
```

## 7.3 Overlap-aware statistics (implemented in numpy; statsmodels is not available)

Monthly snapshots with 12-month labels give an IC series whose consecutive values share 11 of 12 months of returns; naive standard errors are too small by roughly `sqrt(12)`. Implementation in `quant/evaluation/hac.py`:

```
given x_1..x_T (monthly IC series), lag L = h_months - 1  (11 for 12m, 5 for 6m, 2 for 3m, 0 for 1m)
xbar    = mean(x)
gamma_j = (1/T) * sum_{t=j+1..T} (x_t - xbar)(x_{t-j} - xbar)          j = 0..L
S       = gamma_0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gamma_j            Bartlett kernel
se_hac  = sqrt(S / T)
t_hac   = xbar / se_hac
n_eff   = T / h_months
```

Also reported: (a) a stationary block bootstrap (expected block length 12) 90% CI of the mean IC, 2,000 resamples; (b) the twelve non-overlapping sub-series (every 12th month, phases 0..11) with their plain means, as a robustness row. Unit tests: on i.i.d. data `se_hac ~ se_naive` within 15%; on a constructed 12-month moving sum of i.i.d. noise, `se_hac / se_naive` is between 2.5 and 4.

`Guards:` overstated significance from overlapping windows (the same mechanism that turned three months into an "IR of 1.335").

## 7.4 Benchmarks and the exact definition of "alpha"

All benchmarks are built in-engine from the same `price_checkpoints`, so there is no basis mismatch (dividends, timing, adjustments) between portfolio and benchmark:

```
benchmark_key         construction                                                                      why
-------------------------------------------------------------------------------------------------------------------------
ew_universe           equal-weight all eligible names at s, held h months                                what "no skill" earns
ew_sector_matched     equal-weight within each bucket, buckets weighted as in the paper portfolio        isolates stock selection from sector tilt
cw_universe           weighted by mcap at s (shares_outstanding x close, PIT)                            what an index fund earns
mom30_inhouse         top 30 by mom_12_1 among the 200 largest by mcap, rebalanced with the portfolio    a free proxy for Nifty 200 Momentum 30
quality50_inhouse     top 50 by the quality family score, rebalanced with the portfolio                  a free proxy for Nifty 500 Quality 50
ext_nifty500_price    Yahoo ^CRSLDX (Nifty 500 price index), informational only                           reality check; price return only
ext_nifty500_tri      manual CSV from niftyindices.com "Total Returns Index" if the owner downloads it    optional, never a dependency
```

The scoreboard's headline **alpha** is defined as:

```
alpha_12m(s) = net paper-portfolio TR over (s, 12m)  -  ew_sector_matched TR over (s, 12m)
```

because a sector-neutral ranking claims to pick stocks within sectors and nothing else. The second row is active return versus `cw_universe` (what the owner would otherwise buy). Both are reported with the number of live months and a HAC t-stat; the UI labels anything with `n_eff < 2` as "not yet evidence".

## 7.5 Leakage tests (run every month; also in CI on synthetic data)

```
test                       procedure                                                              pass condition
---------------------------------------------------------------------------------------------------------------------------
shuffle                    permute labels within each snapshot date; recompute champion IC          |mean IC| < 2 * se_hac  (else code='LEAKAGE_SHUFFLE', BLOCK next run)
planted signal             add synthetic factor = 0.10 * standardised future label + noise;         recovered IC in [0.08, 0.12]
                           run through the full factor -> neutralise -> IC path
as_of audit (SQL)          for every factor_values row at asof, join the inputs it declares;        zero rows with observed_at > asof
                           assert max(observed_at) <= asof
PIT reproducibility        pick 3 random past months; rebuild a temp DB containing only rows with   recomputed factor_values and scores equal the
                           observed_at <= that month; recompute factors and scores                  stored ones bit-for-bit
survivorship               set of securities evaluated at s equals universe_membership(s),          zero missing; delisted names present with flag
                           including later-delisted names
look-back mirror           IC of each factor vs the PAST 12m return (t-12m -> t)                    reported next to forward IC; a factor whose
                                                                                                    backward IC >> forward IC is flagged 'momentum proxy'
```

`Guards:` look-ahead and leakage of every kind the author of the pipeline cannot see from inside it.

## 7.6 Cost-adjusted spreads

For each score set and horizon, decile portfolios are formed at `s` and the turnover of each decile from `s-1` to `s` is measured. Net spread:

```
D10-D1_net(s, h) = D10-D1_gross(s, h) - [turnover_D10(s) + turnover_D1(s)] * cost_one_way(bucket-weighted, section 8.3) * 2
```

reported at cost multipliers 0.5x, 1x and 2x. A spread that is positive only at 0.5x is reported as "not robust to costs".

## 7.7 Learning-curve measurement (the chart the owner will look at)

Definitions:

- A month `s` is **clean** iff the gate did not BLOCK, no WARN touched an active factor, every active factor had coverage >= 80%, the sector source for >= 95% of names was `nse_csv` or `nse_csv_prior`, and no active factor computed at `s` was later quarantined for a defect present at `s`. The four legacy 2026 snapshots are never clean (section 10.6).
- `clean_months(T)` = number of clean snapshots with matured 12m labels at `T`.

The `learning_curve` table stores, at every `T`, for each subject (each factor, champion, challenger) and horizon:

```
learning_curve(computed_at, subject_kind, subject_key, horizon_m, clean_months, months_matured, n_eff,
               ic_mean, ic_hac_se, ic_hac_t, ic_boot_lo90, ic_boot_hi90, cusum_ic, spread_net_annualised)
```

Three views are drawn from it:

1. **Expanding-window IC vs clean months** — x = `clean_months`, y = `ic_mean` with the bootstrap band. Rising y with a narrowing band is the learning curve. A flat y with a narrowing band is "we now know precisely that it does not work", which is also progress.
2. **CUSUM of monthly IC** — cumulative sum of the 12m IC series. A rising straight line is stable skill; a hump then decline is regime dependence or decay. Drawn per factor and for both score sets; the challenger-minus-champion CUSUM is the "did learning help" line.
3. **Slope test** — regress `ic_mean` on `clean_months` over the last 24 points with a block-bootstrap CI on the slope; the number the section 1.2 target refers to.

The "learning" being measured is therefore of the whole process (cleaner data, better factor set, shrunk weights), not of a weight vector. That is the honest reading of "predictability must increase over time".

`Guards:` a learning curve that is really a look-ahead artefact (only clean, matured, embargoed observations enter it).

---

# 8. Portfolio & cost model

## 8.1 Paper portfolios

One paper portfolio per score set and cadence, all with identical rules so differences are attributable to the ranking:

```
portfolio_id                  score_set        cadence     rule_version
---------------------------------------------------------------------------
champ_m                       champion_v1      monthly     pf_v1
champ_q                       champion_v1      quarterly   pf_v1
chal_m                        challenger_v1    monthly     pf_v1
ew_bench                      (none)           monthly     ew_sector_matched benchmark as a portfolio, same costs applied
```

Rules `pf_v1` (`config/portfolio_v1.yaml`):

```
eligible universe   scores.eligible = 1 at rebalance date
selection           within each bucket, the top 10% by composite_pct (n_b = round(0.10 * bucket size), min 1)  -> ~50 names
hold band           an existing holding is kept while it remains in the top 20% of its bucket (buy at D10, sell below D8)
weights             equal weight across holdings; single-name cap 4%; bucket weight = bucket share of eligible universe
                    (automatically capped by proportional selection; max any bucket 25%)
cash                residual from caps sits in cash at 0% (conservative)
rebalance timing    trades assumed at the close of the first trading day after the snapshot (T+1), not at the snapshot close
dividends           reinvested in the paying stock on ex-date (consistent with the TR index)
delisting           position marked at last price, then cash, delisted_flag on the trade
```

Trading at T+1 close rather than at the snapshot close removes the small look-ahead of trading on information that is only computable after the close. `Guards:` implementation shortfall hidden by same-bar execution.

## 8.2 Turnover and liquidity

Turnover is measured, not assumed: one-way turnover per rebalance = sum of |weight changes| / 2. Expected with the hold band at monthly cadence: roughly 60-100% per year one-way; without the band it would exceed 200%, which is why the band exists. Quarterly cadence is run in parallel to measure the cost/decay trade-off rather than argue about it.

Liquidity buckets from `price_checkpoints.adv60_inr` (60-day average daily traded value):

```
bucket   ADV_60 (INR)             portfolio eligibility
L1       >= Rs 50 crore           yes
L2       Rs 10 - 50 crore         yes
L3       Rs 2 - 10 crore          yes, flagged; position cap 2%
L4       < Rs 2 crore             no  (scored; evaluated as the 'illiquid' cohort)
```

## 8.3 Cost assumptions (stated as assumptions, `config/costs_v1.yaml`)

Indian cash-equity delivery costs as of 2026, per side, in basis points of traded value:

```
component                         bp (one way)   note
-------------------------------------------------------------------------------------------
STT (securities transaction tax)  10.0           delivery, both buy and sell
stamp duty                         1.5           buy side only (averaged to 0.75 each way)
exchange + SEBI + GST              0.4
brokerage                          0 - 3         discount broker; assume 2
statutory + brokerage subtotal    ~13
market impact by liquidity bucket  L1 10 | L2 25 | L3 50
TOTAL one way                      L1 23 | L2 38 | L3 63   (round trip 46 / 76 / 126 bp)
```

These are assumptions; the yaml carries a `source` and `as_of` per line and any change is a proposal. Sensitivity at 0.5x / 1x / 2x is always reported (section 7.6).

`Guards:` costs eating the spread (net figures are primary everywhere; a 1-month strategy with 200% turnover in L2 names loses ~1.5% a year to costs before any alpha).

## 8.4 Crowding and concentration monitors

Monthly, in the report and `scoreboard.extra_json`:

- overlap (share of names) between `champ_m` holdings and the current Nifty 200 Momentum 30 and Nifty 500 Quality 50 constituent files (`ind_nifty200momentum30list.csv`, `ind_nifty500quality50list.csv` from niftyindices, stored under `data/universe/`);
- valuation spread of the top decile vs the universe (median earnings yield), to see whether the ranking is buying what everyone else already bought;
- bucket weights vs universe shares; top-10 concentration.

None of these trigger automatic action; they are inputs to the human's monthly read. `Guards:` factor crowding.

## 8.5 Alpha scoreboard definition

`scoreboard(portfolio_id, asof, window, net_tr, bench_ew_sector_tr, bench_cw_tr, active_return_ews, active_return_cw, te, ir, hac_t, max_dd, turnover_1y, cost_drag_1y, months_live)` with `window in {'since_inception', 'rolling_12m', 'rolling_36m'}`.

Reading rule printed on the dashboard: an information ratio needs `IR * sqrt(years_live) >= 2` before it is distinguishable from zero. At `IR = 0.5` that is 16 years; at `IR = 1.0`, four years. The scoreboard therefore shows a "years to significance at current IR" column so nobody sizes a real position on 18 months of paper.

---

# 9. Feedback loop & knowledge base

## 9.1 The monthly loop

```
 ┌────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │  MONTH-END T (last NSE trading day)                                                                │
 │                                                                                                    │
 │  Day T+1, 20:00 IST  (cron / launchd; or by hand)       python -m quant run monthly --asof T       │
 │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌───────────┐  ┌──────────┐  ┌──────────┐   │
 │  │ 1 ingest │─►│ 2 gates  │─►│ 3 factors│─►│ 4 score│─►│ 5 rebal.  │─►│ 6 mature │─►│ 7 evalu- │   │
 │  │ universe │  │ BLOCK?   │  │ compute  │  │ champ  │  │ paper     │  │ labels   │  │ ate +    │   │
 │  │ prices   │  │ WARN?    │  │ neutral- │  │ chall. │  │ portfolios│  │ s+h <= T │  │ learning │   │
 │  │ fundam.  │  │          │  │ ise      │  │ shadows│  │ costs     │  │          │  │ curve    │   │
 │  └──────────┘  └────┬─────┘  └──────────┘  └────────┘  └───────────┘  └──────────┘  └────┬─────┘   │
 │                     │ BLOCK: steps 3-5 skipped; 6-7 still run; report says why               │       │
 │                     ▼                                                                        ▼       │
 │  ┌─────────────────────────────────────────────────────────────────────────────────────────────┐    │
 │  │ 8 knowledge: write knowledge/reports/T.md (one page), proposals/T.json, update lessons,       │    │
 │  │   export ledger CSVs, ui/data.js; git commit -m "loop T" on branch loop/T                     │    │
 │  └───────────────────────────────────────────────┬─────────────────────────────────────────────┘    │
 │                                                  │  automatic part ends here (~35 min)              │
 │  ════════════════════════════════════════════════╪════════════════════════════════════════════════  │
 │                                                  ▼  human / LLM seat (< 30 min)                     │
 │  ┌─────────────────────────────────────────────────────────────────────────────────────────────┐    │
 │  │ 9 review: read report; for each proposal: quant approve <id> --by <name> --note "..."          │    │
 │  │           or quant reject <id> ...  -> decisions row + ADR file                                │    │
 │  │ 10 merge loop/T into main; push. Approved registry changes take effect at T+1 month.           │    │
 │  └─────────────────────────────────────────────────────────────────────────────────────────────┘    │
 └────────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲                                                                                     │
        └──────────────────────── next month uses the updated registry ───────────────────────┘
```

Skipped or late months: `quant run monthly --asof <past T>` is legal. Price-based factors, labels, checkpoints and benchmarks are rebuilt point-in-time from `prices.sqlite`. Fundamental factors cannot be recovered for a month that was never snapshotted; the run writes them from the latest earlier observation with `stale_days` recorded, marks the month not-clean, and the report says so. Nothing else degrades; the system is safe to leave idle. `Guards:` operator abandonment.

## 9.2 What is auto-applied versus proposed

```
automatic (logged in runs / data_quality_events)              proposed (needs an approval; effective next month)
----------------------------------------------------------------------------------------------------------------
ingest, checkpoints, TR index, split restatement               any factor status change (shadow->active, active->retired)
factor computation with the current registry                    registration of a new factor or hypothesis
champion and challenger scoring; challenger weight recompute    champion <-> challenger swap
paper rebalances, label maturation, evaluation, report          change to costs, liquidity thresholds, bucket file, gates
quarantine of a factor whose input contract broke (reversible)  accepting an UNEXPLAINED_PRICE_REVISION or manual corporate action
```

## 9.3 Approval protocol

- Roles: `owner` (human) and `assistant` (an LLM run with the repo checked out). Both act through the same CLI.
- The assistant may approve or reject proposals of kind `clear_ca_flag`, `accept_revision`, `quarantine_release` and `shadow_promotion_candidate_note`. It may **not** approve `activate_factor`, `retire_factor`, `swap_champion`, `register_factor`, `change_costs`, `change_gates`; for those it writes a recommendation into the proposal (`proposals.assistant_review`) and the owner decides.
- Every approval writes `approvals(proposal_id, decided_by, role, decided_at, git_sha, note)` and generates the ADR skeleton; an approval without an ADR file fails the CI check `test_every_decision_has_adr`.
- No approval may cite evidence that is not in `evaluations` (the CLI refuses free-text numbers in `--note` that do not appear in the evidence JSON; a soft check that prints a warning).

`Guards:` data snooping by the reviewer (decisions must point at pre-computed, stored evidence); an LLM quietly rewriting the model.

## 9.4 Knowledge base: tables and files

DDL (SQLite; all in `quant_engine.db`, exported to `data/ledger/`):

```sql
CREATE TABLE runs (
  run_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,           -- 'monthly' | 'backfill' | 'migrate' | 'evaluate' | 'adhoc'
  asof TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  git_sha TEXT NOT NULL, registry_sha256 TEXT NOT NULL, yfinance_version TEXT NOT NULL, python_version TEXT NOT NULL,
  status TEXT NOT NULL,                                     -- 'ok' | 'partial' | 'blocked' | 'failed'
  gate_status TEXT NOT NULL,                                -- 'pass' | 'warn' | 'block'
  is_clean INTEGER,                                         -- filled by evaluate once labels mature; NULL until then
  notes_json TEXT
);

CREATE TABLE hypotheses (
  hypothesis_id TEXT PRIMARY KEY,                           -- 'H-2026-001'
  family TEXT NOT NULL, statement TEXT NOT NULL, expected_sign INTEGER NOT NULL, horizon_m INTEGER NOT NULL,
  registered_on TEXT NOT NULL, registered_by TEXT NOT NULL, factor_key TEXT,
  sequence_in_family INTEGER NOT NULL,                      -- 1, 2, 3 ... used for the deflated threshold
  status TEXT NOT NULL                                      -- 'open' | 'supported' | 'rejected' | 'withdrawn'
);

CREATE TABLE experiments (
  experiment_id INTEGER PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES hypotheses,
  run_id INTEGER NOT NULL REFERENCES runs, design_json TEXT NOT NULL,   -- horizon, window, universe filter, test statistic
  preregistered_at TEXT NOT NULL, executed_at TEXT NOT NULL,
  data_window_start TEXT NOT NULL, data_window_end TEXT NOT NULL,
  result_json TEXT NOT NULL,                                -- ic_mean, se_hac, t_hac, n_eff, sign_hit_rate, deflated_t_crit
  verdict TEXT NOT NULL                                     -- 'pass' | 'fail' | 'inconclusive'
);

CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY,                             -- 'P-2027-03-02'
  created_run_id INTEGER NOT NULL REFERENCES runs, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, evidence_json TEXT NOT NULL, assistant_review TEXT,
  status TEXT NOT NULL,                                     -- 'open' | 'approved' | 'rejected' | 'expired'
  decided_by TEXT, decided_at TEXT, decision_id TEXT
);

CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY,                             -- 'ADR-202703-02'
  proposal_id TEXT REFERENCES proposals, kind TEXT NOT NULL, summary TEXT NOT NULL,
  adr_path TEXT NOT NULL, decided_by TEXT NOT NULL, role TEXT NOT NULL, decided_at TEXT NOT NULL,
  effective_from TEXT NOT NULL, git_sha TEXT NOT NULL
);

CREATE TABLE approvals (
  approval_id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals,
  decided_by TEXT NOT NULL, role TEXT NOT NULL, decision TEXT NOT NULL, decided_at TEXT NOT NULL, git_sha TEXT NOT NULL, note TEXT
);

CREATE TABLE data_quality_events (
  event_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs, asof TEXT NOT NULL,
  severity TEXT NOT NULL,                                   -- 'INFO' | 'WARN' | 'BLOCK'
  code TEXT NOT NULL, security_id INTEGER, field TEXT, detail_json TEXT, resolved_run_id INTEGER
);

CREATE TABLE evaluations (
  eval_id INTEGER PRIMARY KEY, computed_run_id INTEGER NOT NULL, asof TEXT NOT NULL, horizon_m INTEGER NOT NULL,
  subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL, metric TEXT NOT NULL, value REAL, n INTEGER, extra_json TEXT,
  UNIQUE (asof, horizon_m, subject_kind, subject_key, metric)
);
```

Files:

```
knowledge/
  registry/factors.yaml            single source of truth for factor specs (hash recorded per run)
  registry/hypotheses.yaml         mirrors the hypotheses table for human editing; loader reconciles
  decisions/ADR-YYYYMM-NN-<slug>.md   one per decision; template below
  reports/YYYY-MM.md               auto-generated one-page monthly report (never hand-edited)
  proposals/YYYY-MM.json           machine-readable proposals for that month
  lessons.md                       append-only ledger: date, what was learned, link to evidence, link to ADR
  README.md                        how to read this folder in five minutes
```

ADR template:

```
Title: ADR-YYYYMM-NN: <title>
Status: proposed | accepted | rejected | superseded by ADR-...
Date: YYYY-MM-DD      Decided by: <name> (<role>)      Proposal: P-...      Git: <sha>
## Context        what the evidence table showed (paste the rows, with n_eff and HAC t)
## Decision       one sentence
## Pre-registered consequence   what we now expect to observe, by when, and what would reverse this decision
## Alternatives rejected
## Effective from  YYYY-MM (always the next monthly run)
```

## 9.5 Promotion and retirement criteria (numeric)

```
transition                 criteria (all)                                                                            who
--------------------------------------------------------------------------------------------------------------------------------
candidate -> shadow        YAML entry + ADR committed; plugin has unit tests incl. a synthetic PIT test; coverage on the      assistant may
                           last snapshot >= 60%                                                                              propose, owner approves
shadow -> active           >= 24 clean months in shadow (12m labels matured for >= 12 of them)                               owner
  (standard track)         sign correct: ICbar has the registered sign and sign hit rate >= 60% of matured months
                           t_hac >= deflated threshold t_crit(K_family) (9.6)
                           |corr| with every active factor in the same family < 0.7 (on neutral_z, pooled)
                           net D10-D1 at 12m > 0 at 1x cost
shadow -> active           >= 12 clean months; evidence grade A documented in the ADR; 6m-horizon labels used;                owner
  (evidence fast track)    t_hac(6m) >= 1.5 with correct sign; hit rate >= 60%; correlation rule as above
active -> retired          >= 36 months active AND ICbar(12m) has the wrong sign with t_hac <= -1.5                          owner
                           OR coverage < 60% for 3 consecutive months (quarantine first, retire if unresolved in 6 months)
                           OR input field definition/unit changed at source (quarantine, then re-register as @version+1)
challenger -> champion     >= 36 months of paired live record; mean(IC_chal - IC_champ) at 12m > 0 with paired t_hac >= 2.0;   owner
                           net paper IR(chal) - IR(champ) > 0 over the same window; family weights all within [0.10, 0.50]
any -> quarantined         automatic on BLOCK-level contract failure of an input; released by decision                       system / assistant
```

## 9.6 Multiple-testing control

- **Budget:** at most 6 new hypotheses registered per calendar year, at most 3 per family per year. The count is on the dashboard ("hypotheses tested YTD / cap").
- **Deflated threshold:** for the K-th hypothesis tested in a family (`sequence_in_family = K`), the promotion t-statistic must exceed the two-sided Bonferroni value for K tests at 5%:

```
K        1      2      3      4      5      6
t_crit   1.96   2.24   2.39   2.50   2.58   2.64
```

  plus a **haircut**: the expected best-of-K IC under the null, `se_hac * sqrt(2 ln K)`, is subtracted from `ICbar` before comparing to zero in the report ("IC after multiple-testing haircut").
- **Registration before data:** the sequence number is assigned at registration, so a hypothesis cannot be quietly withdrawn and re-registered to reset K (withdrawn hypotheses keep their number and appear in the count).
- Practical consequence stated plainly: with 36 months of 12-month labels (`n_eff = 2`), an IC of +0.05 with a typical monthly IC standard deviation of 0.12 gives `t_hac ~ 0.05 / (0.12 * sqrt(12/36)) ~ 0.7`. Almost nothing will pass the standard track before year four. That is why the fast track requires external grade-A evidence and why the initial six are admitted on evidence rather than on this repository's data.

`Guards:` data snooping / the "factor zoo".

## 9.7 How a new parameter is added safely (step list)

```
1. Write the hypothesis (sign, horizon, family, formula, inputs) -> knowledge/registry/hypotheses.yaml + factors.yaml (status: candidate)
2. Write ADR-...: prediction, retirement rule.  Commit.  (registered_on = this date)
3. Implement quant/factors/<family>/<name>.py + tests:
     test_<name>_matches_hand_computed_example
     test_<name>_uses_only_observed_at_lte_asof   (synthetic PIT store with a "future" row that must be ignored)
     test_<name>_coverage_on_last_snapshot >= 0.60
4. quant factors register --key <name>@1            -> status shadow; first computed at the NEXT monthly run, never earlier
5. (price-based only) quant factors backfill --key <name>@1 --descriptive   -> prints the biased backfill block; writes nothing to evaluations
6. Wait: 12 (fast) or 24 (standard) clean months. The report shows its shadow record every month.
7. The run auto-generates a proposal 'activate_factor' the first month all criteria in 9.5 hold; owner approves/rejects.
8. On approval, the champion at the next run includes it (family-equal). Historical scores are not recomputed; the champion's score_set id
   is versioned: champion_v1 -> champion_v2. Both are kept and evaluated; the learning curve shows the version boundary as a vertical line.
```

Scores are immutable per `score_set` version, so the historical record can never be contaminated by a later factor. `Guards:` contaminating the historical record; retro-fitting.

## 9.8 The monthly report (auto-generated, one page)

`knowledge/reports/YYYY-MM.md` sections, in order: (1) gate status and any BLOCK/WARN with counts; (2) "what you need to do" — at most three items (open proposals, CA flags to confirm); (3) headline table: champion and challenger 12m / 6m / 3m IC with `n_eff` and HAC t, months live; (4) learning-curve numbers (slope, CI) and a link to the UI chart; (5) scoreboard rows; (6) factor table (status, coverage, IC, sign hit rate, deflated threshold, months in status); (7) excluded-cohort table; (8) data-quality summary with drift flags; (9) top-10 and bottom-10 names by bucket (for curiosity, explicitly labelled as not investment advice); (10) hypotheses tested YTD / cap. Every number carries its n.

---

# 10. Architecture

## 10.1 Package layout

```
quant/
  __init__.py
  cli.py                       python -m quant <command>; argparse; every command takes --asof and --db
  config.py                    repo-relative paths; env overrides QUANT_DB_PATH, QUANT_PRICES_PATH, QUANT_UI_DIR; loads config/*.yaml
  data/
    universe.py                fetch_nifty500(asof) -> DataFrame; store_universe_file(); membership rows; ISIN reconciliation
    identity.py                securities / symbol_history upsert; resolve_security_id(isin | symbol)
    prices.py                  PriceStore over data/prices.sqlite: download_batches(), reconcile_overlap(), tr_index(), checkpoints()
    actions.py                 corporate_actions detection (splits from reconciliation, unrecorded-CA detector), manual entry
    fundamentals.py            per-security yfinance pull -> fundamentals_pit rows; derived fields (roce, de, yield); lag rule for backfill
    contracts.py               field_contracts load + check; PSI drift; gate decision
    quality.py                 data_quality_events writer; gate summary
  sectors/
    taxonomy.py                sector_map maintenance from the universe file; bucket file; reclassification handling
    crosswalk.py               yahoo -> nse fallback with confidence
  factors/
    base.py                    FactorSpec, FactorContext, Factor protocol
    registry.py                load factors.yaml; import plugins; enforce registered_on <= asof; sha256
    price/momentum.py          Mom12_1, Str1m
    price/trend.py             Trend200
    price/volatility.py        LowVol252
    price/liquidity.py         Illiq
    fundamental/quality.py     QualityRoce, Accruals
    fundamental/growth.py      GrowthRev3y
    fundamental/value.py       EarningsYield, PbSector
    fundamental/leverage.py    Leverage
    flow/institutional.py      InstHoldChg
    sector/sector_features.py  SectorMom6m, SectorBreadth
    legacy/death_cross.py      LegacyDeathCross (migration only)
  model/
    neutralise.py              winsorise, within-bucket z and pct, coverage rules
    composite.py               champion (family-equal) and weighted composites; eligibility filters
    weights.py                 challenger: shrunk sign-constrained IC weights (pure function of evaluations)
  evaluation/
    labels.py                  mature labels; delisting & CA flags; sector-relative log returns
    ic.py                      pooled within-bucket rank IC; decile stats; cohort stats
    hac.py                     newey_west_mean_se(x, lag); block_bootstrap_ci(x, block, n)
    leakage.py                 shuffle, planted signal, as_of audit, PIT reproducibility, survivorship, look-back mirror
    benchmarks.py              ew / cw / sector-matched / in-house mom30 & quality50 / external index rows
    learning_curve.py          expanding-window stats, CUSUM, slope test -> learning_curve table
  portfolio/
    construct.py               pf_v1 rules; hold band; caps; T+1 execution
    costs.py                   costs_v1.yaml; per-trade cost by liquidity bucket
    ledger.py                  positions, trades, NAV series
    scoreboard.py              windows, IR, HAC t, years-to-significance
  knowledge/
    proposals.py               generate proposals from criteria; approve/reject; approvals rows
    adr.py                     ADR skeleton writer; CI check every decision has an ADR
    report.py                  monthly markdown report
    lessons.py                 append to lessons.md
  db/
    schema.sql                 full v2 DDL (below)
    migrate.py                 schema versioning; migrate_legacy()
    ledger_export.py           export month's rows to data/ledger/YYYY-MM/*.csv; rebuild_from_ledger()
  ui_export.py                 writes ui/data.js
tests/
  unit/        one module per quant module; no network; synthetic fixtures
  integration/ test_monthly_loop_synthetic.py  (full loop on a 40-stock synthetic universe with planted signal)
  fixtures/    synthetic prices, statements, universe files
config/
  engine.yaml  costs_v1.yaml  portfolio_v1.yaml  sector_buckets_v1.yaml  field_contracts_v1.yaml
  yahoo_to_nse_crosswalk_v1.csv  manual_ticker_overrides.csv  manual_isin.csv
data/
  universe/  manifest/  ledger/  external/  prices.sqlite (gitignored)
knowledge/   (section 9.4)
ui/          index.html app.js style.css data.js vendor/chart.umd.js
```

Legacy scripts (`harness_v16_learning.py`, `weight_optimizer.py`, `eval_portfolio_health.py`, `update_ui_v16.py`, `concall_analyzer.py`, `harness_v15_*`, `update_ui_v15.py`) move to `legacy/` untouched, importable for the migration acceptance test, and are removed from the cron path. `quant_math.py` stays importable from `legacy/` because the migration reuses `trap_penalty_multiplier` to reproduce legacy final scores.

## 10.2 Full DDL (schema version 2)

In addition to `fundamentals_pit`, `sector_map`, `runs`, `hypotheses`, `experiments`, `proposals`, `decisions`, `approvals`, `data_quality_events`, `evaluations` given above:

```sql
PRAGMA user_version = 2;

CREATE TABLE securities (
  security_id INTEGER PRIMARY KEY, isin TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, status TEXT NOT NULL      -- 'listed' | 'delisted' | 'suspended' | 'unknown'
);
CREATE TABLE symbol_history (
  security_id INTEGER NOT NULL REFERENCES securities, nse_symbol TEXT NOT NULL, yahoo_ticker TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, PRIMARY KEY (security_id, valid_from)
);
CREATE TABLE universe_membership (
  security_id INTEGER NOT NULL REFERENCES securities, as_of TEXT NOT NULL, in_index INTEGER NOT NULL,
  source TEXT NOT NULL,                      -- 'nse_csv' | 'survivor_backfill' | 'legacy_snapshot'
  source_sha256 TEXT, PRIMARY KEY (security_id, as_of)
);
CREATE TABLE price_checkpoints (
  security_id INTEGER NOT NULL REFERENCES securities, month_end TEXT NOT NULL,
  close REAL NOT NULL, tr_index REAL NOT NULL, adv20_inr REAL, adv60_inr REAL, days_traded_60 INTEGER,
  shares_out REAL, mcap_inr REAL, source_asof TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs,
  PRIMARY KEY (security_id, month_end)
);
CREATE TABLE corporate_actions (
  ca_id INTEGER PRIMARY KEY, security_id INTEGER NOT NULL REFERENCES securities, ex_date TEXT NOT NULL,
  kind TEXT NOT NULL,                        -- 'split' | 'bonus' | 'dividend' | 'demerger' | 'rights' | 'suspected'
  ratio REAL, amount_inr REAL, source TEXT NOT NULL, observed_at TEXT NOT NULL, run_id INTEGER NOT NULL,
  UNIQUE (security_id, ex_date, kind)
);
CREATE TABLE field_contracts (
  field TEXT PRIMARY KEY, unit TEXT NOT NULL, min_value REAL, max_value REAL, max_null_rate REAL NOT NULL,
  source TEXT NOT NULL, notes TEXT, contract_version INTEGER NOT NULL
);
CREATE TABLE factor_registry (
  factor_key TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, family TEXT NOT NULL,
  hypothesis TEXT NOT NULL, expected_sign INTEGER NOT NULL, horizons_json TEXT NOT NULL, inputs_json TEXT NOT NULL,
  status TEXT NOT NULL, registered_on TEXT NOT NULL, registered_by TEXT NOT NULL, evidence TEXT,
  activated_on TEXT, retired_on TEXT, spec_sha256 TEXT NOT NULL, hypothesis_id TEXT REFERENCES hypotheses
);
CREATE TABLE factor_values (
  asof TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities, factor_key TEXT NOT NULL REFERENCES factor_registry,
  raw_value REAL, winsor_value REAL, neutral_z REAL, neutral_pct REAL, bucket TEXT NOT NULL,
  coverage_flag TEXT NOT NULL,               -- 'ok' | 'imputed_input' | 'stale_input' | 'missing'
  run_id INTEGER NOT NULL REFERENCES runs, PRIMARY KEY (asof, security_id, factor_key)
);
CREATE INDEX ix_factor_values_key_asof ON factor_values (factor_key, asof);
CREATE TABLE weight_sets (
  weight_set_id INTEGER PRIMARY KEY, asof TEXT NOT NULL, score_set TEXT NOT NULL, factor_key TEXT NOT NULL,
  weight REAL NOT NULL, method TEXT NOT NULL, n_eff REAL, lambda REAL, evidence_json TEXT, run_id INTEGER NOT NULL,
  UNIQUE (asof, score_set, factor_key)
);
CREATE TABLE scores (
  asof TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities, score_set TEXT NOT NULL,
  composite_z REAL, composite_pct REAL, bucket TEXT NOT NULL, n_factors_used INTEGER NOT NULL, n_families_used INTEGER NOT NULL,
  eligible INTEGER NOT NULL, eligibility_reason TEXT, liquidity_bucket TEXT, run_id INTEGER NOT NULL REFERENCES runs,
  PRIMARY KEY (asof, security_id, score_set)
);
CREATE TABLE labels (
  asof TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities, horizon_m INTEGER NOT NULL,
  end_date TEXT NOT NULL, tr_return REAL, sector_rel_logret REAL, bucket_at_asof TEXT NOT NULL,
  status TEXT NOT NULL,                      -- 'matured' | 'delisted' | 'flagged'
  delisted_flag INTEGER NOT NULL DEFAULT 0, ca_flag INTEGER NOT NULL DEFAULT 0, computed_run_id INTEGER NOT NULL,
  PRIMARY KEY (asof, security_id, horizon_m)
);
CREATE TABLE learning_curve (
  computed_at TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL, horizon_m INTEGER NOT NULL,
  clean_months INTEGER NOT NULL, months_matured INTEGER NOT NULL, n_eff REAL NOT NULL,
  ic_mean REAL, ic_hac_se REAL, ic_hac_t REAL, ic_boot_lo90 REAL, ic_boot_hi90 REAL, cusum_ic REAL, spread_net_annualised REAL,
  PRIMARY KEY (computed_at, subject_kind, subject_key, horizon_m)
);
CREATE TABLE portfolios (
  portfolio_id TEXT PRIMARY KEY, score_set TEXT, cadence TEXT NOT NULL, rule_version TEXT NOT NULL, inception TEXT NOT NULL
);
CREATE TABLE portfolio_positions (
  portfolio_id TEXT NOT NULL REFERENCES portfolios, asof TEXT NOT NULL, security_id INTEGER NOT NULL,
  weight REAL NOT NULL, entry_asof TEXT NOT NULL, PRIMARY KEY (portfolio_id, asof, security_id)
);
CREATE TABLE portfolio_trades (
  trade_id INTEGER PRIMARY KEY, portfolio_id TEXT NOT NULL, asof TEXT NOT NULL, exec_date TEXT NOT NULL,
  security_id INTEGER NOT NULL, side TEXT NOT NULL, weight_delta REAL NOT NULL, cost_bp REAL NOT NULL, liquidity_bucket TEXT NOT NULL
);
CREATE TABLE portfolio_nav (
  portfolio_id TEXT NOT NULL, month_end TEXT NOT NULL, nav REAL NOT NULL, gross_nav REAL NOT NULL, turnover_1w REAL, cash_w REAL,
  PRIMARY KEY (portfolio_id, month_end)
);
CREATE TABLE scoreboard (
  portfolio_id TEXT NOT NULL, asof TEXT NOT NULL, window TEXT NOT NULL,
  net_tr REAL, bench_ew_sector_tr REAL, bench_cw_tr REAL, active_return_ews REAL, active_return_cw REAL,
  te REAL, ir REAL, hac_t REAL, max_dd REAL, turnover_1y REAL, cost_drag_1y REAL, months_live INTEGER, extra_json TEXT,
  PRIMARY KEY (portfolio_id, asof, window)
);
-- legacy (migration; never written by the loop)
CREATE TABLE legacy_predictions      AS SELECT * FROM daily_predictions;      -- executed by migrate, then daily_predictions dropped
CREATE TABLE legacy_active_weights   AS SELECT * FROM active_weights;
CREATE TABLE legacy_performance_tracking AS SELECT * FROM performance_tracking;
CREATE TABLE legacy_defects (
  snapshot_date TEXT NOT NULL, scope TEXT NOT NULL,        -- 'snapshot' | 'field' | 'ticker'
  field TEXT, ticker TEXT, defect_code TEXT NOT NULL, detail TEXT NOT NULL
);
```

## 10.3 CLI

```
python -m quant db init                              create schema v2 in an empty DB
python -m quant db migrate-legacy                    section 10.6; idempotent; refuses to run twice
python -m quant db rebuild --from data/ledger        rebuild quant_engine.db from the ledger; prints row-count diff vs existing
python -m quant db export-ledger --asof 2026-10-31   write data/ledger/2026-10/*.csv for that run

python -m quant data backfill --years 10             section 4.6; resumable
python -m quant data ingest --asof 2026-10-31        universe + prices + fundamentals + contracts + gate (no scoring)
python -m quant data gates --asof 2026-10-31         re-print the gate decision for a run
python -m quant data accept-revision --security-id 123 --run-id 45
python -m quant data add-ca --isin INE... --ex-date 2026-11-03 --kind demerger --ratio 0.82
python -m quant data clear-ca-flag --isin INE... --date 2026-11-03

python -m quant sectors build --asof 2026-10-31      sector_map maintenance; prints reclassifications
python -m quant factors compute --asof 2026-10-31 [--only mom_12_1@1]
python -m quant factors register --key illiq@1       candidate -> shadow (writes proposal if not owner)
python -m quant factors backfill --key mom_12_1@1 --descriptive
python -m quant score --asof 2026-10-31              champion + challenger + shadow composites; eligibility
python -m quant labels mature --asof 2026-10-31
python -m quant evaluate --asof 2026-10-31           evaluations + learning_curve + leakage tests + cohorts
python -m quant portfolio rebalance --asof 2026-10-31
python -m quant knowledge report --asof 2026-10-31
python -m quant knowledge propose --asof 2026-10-31
python -m quant approve P-2026-11-01 --by saurabh --role owner --note "..."
python -m quant reject  P-2026-11-01 --by saurabh --role owner --note "..."
python -m quant ui export
python -m quant verify leakage --asof 2026-10-31
python -m quant verify pit --months 3
python -m quant status                               last run, gate, open proposals, months to next maturity

python -m quant run monthly --asof 2026-10-31        the whole loop, steps 1-8 of 9.1, in order, stopping on BLOCK after step 2
                                                     and resuming at step 6
```

Expected output of `run monthly` on success (abridged, so an implementer knows what "done" looks like):

```
[1/8] ingest     universe 500 (nse_csv sha 9f3a...)  prices 549 securities, 13 months, 0 unexplained revisions, 1 split restated (ZFCVINDIA 6:1)
                 fundamentals 500/500 (28m 41s)  imputed inputs among active-factor fields: 6.2%
[2/8] gates      PASS  (WARN 0, INFO 7)
[3/8] factors    14 computed; coverage: mom_12_1 99.4% trend_200 99.4% lowvol_252 99.2% quality_roce 91.0% accruals 90.6% growth_rev_3y 84.2% ...
[4/8] score      champion_v1 eligible 471 (illiquid 21, unscored 8); challenger_v1 lambda=0.000 (n_eff 0.0)
[5/8] portfolio  champ_m: 49 holdings, one-way turnover 7.8%, est. cost 3.1 bp of NAV; chal_m identical (weights equal)
[6/8] labels     matured: h=1 for 2026-09-30 (500), h=3 for 2026-07-31 (0: legacy month, price dates only)
[7/8] evaluate   3m ladder: champion IC n/a (first V2 label at h=3 matures 2027-01); leakage: shuffle PASS, planted PASS (0.097), PIT PASS
[8/8] knowledge  report knowledge/reports/2026-10.md; proposals: 0 open; ledger data/ledger/2026-10 (9 files, 412 KB); ui/data.js written
run_id 17 status ok gate pass  35m 12s
```

## 10.4 Configuration (`config/engine.yaml`, excerpt)

```yaml
schema_version: 2
timezone: Asia/Kolkata
horizons_m: [1, 3, 6, 12, 24, 36]
primary_horizon_m: 12
neutralisation:
  bucket_file: config/sector_buckets_v1.yaml
  min_bucket_size: 8
  winsor: [0.01, 0.99]
  z_clip: 3.0
  min_families_for_score: 3
champion:
  score_set: champion_v1
  method: family_equal
challenger:
  score_set: challenger_v1
  method: shrunk_ic_v1
  prior_strength_k: 3
  bounds_floor_over_n: 0.4
  bounds_ceiling_over_n: 2.4
liquidity:
  min_adv60_inr: 20000000        # Rs 2 crore
  min_days_traded_60: 54
gates:
  min_priced_securities: 450
  min_universe_rows: 480
  max_unexplained_revision_share: 0.02
  max_overlap_median_abs_dev: 0.001
  max_drift_fields_before_block: 3
  psi_warn: 0.25
ingest:
  batch_size: 50
  batch_sleep_s: 1.0
  per_security_sleep_s: 0.5
  lookback_months: 13
  on_429_sleep_s: 120
multiple_testing:
  max_new_hypotheses_per_year: 6
  max_per_family_per_year: 3
```

## 10.5 Cron / launchd

```
  (crontab -e; laptop; 20:00 IST on the 1st..3rd of each month; the engine exits immediately if the month is already run)
0 20 1-3 * *  cd /path/to/repo && /path/to/venv/bin/python -m quant run monthly --asof last-trading-day-of-previous-month >> logs/loop.log 2>&1
```

`--asof last-trading-day-of-previous-month` is resolved in-engine from the NSE holiday list shipped in `config/nse_holidays.yaml` (updated yearly; a missing year is a WARN, not a BLOCK). GitHub Actions is deliberately not the default runner: Yahoo rate-limits shared runner IPs unpredictably, which would turn a data-vendor problem into a gate failure every month.

## 10.6 Migration of the existing `quant_engine.db`

Facts about the legacy data, verified 2026-09-05 read-only:

```
daily_predictions rows by date (weekday):  2026-06-04 Thu 47 | 2026-06-12 Fri 499 | 2026-06-14 Sun 499 | 2026-07-11 Sat 499 | 2026-08-14 Fri 499 | 2026-09-03 Thu 500
   total 2,543 rows; 501 distinct tickers (one left and one joined between 07-11 and 08-14; one joined for 09-03)
   06-12 and 06-14 have identical prices for all 499 names (weekend run: same Friday close), different final scores (weights changed)
   no snapshot carries Data_Flags, Industry or Market_Cap_Cr in raw_json (all six predate the fixed harness)
   dividend yield > 25% (the x100 bug):  06-04: 0 | 06-12: 0 | 06-14: 322 | 07-11: 324 | 08-14: 324 | 09-03: 327
   distinct values per score column (09-03): quality 12, growth 5, valuation 6, risk 6, moat 7, bs 4, cap_alloc 7, smart_money 10
active_weights: 12 rows; only row 12 has trained_through (2026-09-03, backfilled)
performance_tracking: 4,773 rows over 2,043 prediction ids (after the red-team dedupe), forward dates 2026-06-11 .. 2026-09-03
```

`quant db migrate-legacy` does, in one transaction, and prints a reconciliation table:

```
step  action                                                                                     expected result
------------------------------------------------------------------------------------------------------------------------------
 1    rename daily_predictions -> legacy_predictions, active_weights -> legacy_active_weights,     3 legacy tables, row counts unchanged
      performance_tracking -> legacy_performance_tracking (never modified again)
 2    securities: resolve the 501 legacy tickers to ISINs via today's CSV (500) + config/manual_isin.csv   501 securities; 0 unresolved
      for the one ticker no longer in the index (query: SELECT ticker FROM legacy_predictions
      GROUP BY ticker HAVING MAX(date) < '2026-09-03')
 3    runs: one row per legacy snapshot, kind='migrate', status per table below                   6 runs rows
 4    universe_membership: source='legacy_snapshot' for the 5 full snapshots at their price dates  ~2,496 rows
 5    sector_map: neutral_bucket from today's CSV for all 501, source='backfilled_current',         501 rows, valid_from='2026-06-01'
      confidence 0.8 (sector labels rarely move in 3 months; flagged anyway)
 6    price_checkpoints: NOT from legacy `price` (unadjusted quote). Built from the price backfill  5 price dates x ~500 = ~2,500 rows
      (section 4.6) at the legacy PRICE dates; legacy quote stored in legacy tables only
 7    factor_values: legacy_quality@0 .. legacy_smart_money@0 (8), legacy_trap@0, legacy_death_cross@0,   ~2,496 x 11 rows
      legacy_final@0, raw_value = stored score, neutral_z/pct computed within bucket, coverage_flag='legacy'
 8    weight_sets: 12 rows -> score_set='legacy_v18', method='legacy_eg', one weight row per factor        96 rows
 9    labels: recomputed from price_checkpoints (total return, split-safe) for h in {1} between consecutive   ~1,500 rows h=1-equivalent,
      legacy price dates, stored with horizon_m = actual calendar months rounded; ZFCVINDIA window ca_flag=0      status 'matured'
      because the split is now handled; legacy price-only returns kept in legacy_performance_tracking
10    legacy_defects: rows for every known defect (table below)                                              >= 9 rows
11    data_quality_events: one BLOCK-severity event per defect per snapshot, run_id = migration run           ~30 rows
12    factor_registry: the legacy_* keys with status='retired', registered_on='2026-06-01', evidence='legacy'  11 rows
13    decisions: ADR-202610-01..06 (retire moat, strategic risk, sentiment, DCF factor, trap multiplier,      6 rows + 6 files
      death-cross kill) with the red-team review as context
```

Legacy snapshot classification and price dates:

```
snapshot     is_full  price_asof   role                                   why
----------------------------------------------------------------------------------------------------------------
2026-06-04   0        2026-06-04   partial warm-up (47 names)              below FULL_UNIVERSE_MIN; excluded from all statistics
2026-06-12   1        2026-06-12   superseded_by 2026-06-14                same prices as 06-14; keeping both would double-count
2026-06-14   1        2026-06-12   legacy period 1 start                   Sunday run; quote = Friday 06-12 close
2026-07-11   1        2026-07-10   legacy period 1 end / 2 start           Saturday run; quote = Friday 07-10 close
2026-08-14   1        2026-08-14   legacy period 2 end / 3 start
2026-09-03   1        2026-09-03   legacy period 3 end / 4 start
```

Known-defect flags written to `legacy_defects` (every legacy statistic in the UI carries them):

```
defect_code                  scope     applies to                     detail
--------------------------------------------------------------------------------------------------------------------------
DIV_YIELD_X100               field     06-14, 07-11, 08-14, 09-03     Div_Yield_% multiplied by 100; CapAlloc_Score inflated for ~65% of names
ROE_NONE_AS_ZERO             field     all                            missing ROE scored as ROE < 5%: +20 trap for ~58% of names
SENTIMENT_OUTSIDE_BUDGET     field     all                            up to +10 points added to base from headline keywords
GROWTH_IMPUTED_15PCT         field     all                            missing growth history imputed as +15%
DCF_SINGLE_YEAR_FCF          field     all                            intrinsic value from one year of FCF
NEAR_CONSTANT_FACTOR         field     all                            risk 85%, moat 96%, bs 84% of names on one value
NO_DATA_FLAGS                snapshot  all                            imputed inputs indistinguishable from real ones
UNADJUSTED_QUOTE             snapshot  all                            price = quote at run time; not split/dividend adjusted; TR rebuilt in step 9
WEEKEND_RUN                  snapshot  06-14, 07-11                   snapshot date differs from price date
HINDSIGHT_TICKER_LISTS       field     moat, risk                     factor definitions contain names chosen after the fact
```

Acceptance tests for the migration (`tests/integration/test_migrate_legacy.py`):

```
test_row_counts_preserved            legacy_* row counts == pre-migration counts (2,543 / 12 / 4,773)
test_all_tickers_resolved            501 securities, none with isin LIKE 'UNRESOLVED%'
test_attribution_table_reproduced    recompute the red-team table "final / momentum alone / fundamental composite" from
                                     factor_values(legacy_*) + legacy price-only returns: matches red_team_review.md to 3 dp
                                     (-0.063/+0.092/+0.117 ; -0.033/+0.030/+0.125 ; +0.045/+0.058/+0.050)
test_split_no_longer_a_return        ZFCVINDIA 06-12 -> 07-10 TR return is between -30% and +30% (was -84.1%)
test_superseded_snapshot_excluded    evaluations contain no row with asof = '2026-06-12'
test_legacy_months_never_clean       runs.is_clean = 0 for all six legacy runs
test_migrate_is_idempotent           running migrate-legacy twice raises and changes nothing
```

The four 2026 snapshots therefore become the first (flagged, never-clean) points on every chart, exactly as the brief requires, without pretending they are comparable to V2 months.

## 10.7 UI changes (vanilla HTML/JS/CSS, no build step)

- `ui/data.js` shrinks: per-stock HTML strings and `raw_json` blobs are replaced by typed fields; the page composes text. Target size under 300 KB.
- Chart.js is **vendored** to `ui/vendor/chart.umd.js` (a static file, no package manager) so the dashboard works offline and the "zero-dependency" claim in the README becomes true; the Google Fonts link is removed.
- New tabs, in this order: **Evidence** (learning-curve chart: expanding IC with band + CUSUM; the IC table with n_eff and HAC t; the falsification checklist from 1.3 with live status), **Ranking** (per bucket; champion and challenger side by side; eligibility badges; nothing called "accepted/rejected"), **Portfolio** (scoreboard, NAV vs benchmarks, turnover, cost drag, years-to-significance), **Data health** (gate status, drift flags, imputed share, CA flags awaiting confirmation), **Knowledge** (factor registry with status and months-in-status, open proposals, last 12 ADR titles, hypotheses YTD / cap), **Legacy** (the four 2026 snapshots with their defect badges).
- Rendering rule enforced in `app.js`: any IC or return shown without an accompanying `n` and band is a bug; the exporter refuses to emit such a record (`ui_export.py` validation).
- The "Turnaround" interceptor (`Growth_Score >= 80 AND FCF < 0`) becomes a plain filter chip on the Ranking tab ("high growth, negative FCF") rather than a separate scoring path.

---

# 11. Phased roadmap

## 11.1 What ships when

```
Month 1  (by 2026-10-31, first V2 snapshot)
  quant/ package skeleton; schema v2; migrate-legacy with its 7 acceptance tests; price backfill + manifest; TR index + reconciliation;
  universe/ISIN/sector_map; field contracts + gates; 6 active + 6 shadow factors; champion + challenger scoring; labels; paper portfolios;
  evaluate with HAC + bootstrap + leakage tests; monthly report; ledger export; UI Evidence/Ranking/Data health tabs; cron.
  Evidence available: none for V2 (first 1m label at month 2, first 3m at month 4, first 12m at month 13). Legacy points flagged.

Month 3  (2026-12-31)
  First 1m ladder points (2); first shuffle/planted/PIT tests on real data; descriptive backfill block for price factors;
  Portfolio and Knowledge tabs; proposal generator; ADR tooling; lessons.md seeded from the red-team review.

Month 6  (2027-03-31)
  3m ladder has 3 points; 6m has 1; excluded-cohort report stabilises; crowding monitors; quarterly-cadence portfolio comparison;
  first drift statistics with 6 months of PSI history; first candidate registration allowed (budget starts counting).

Month 12 (2027-09-30)
  6m ladder has 7 points; 3m has 10; first fast-track promotion review possible for a grade-A shadow factor; falsification checkpoint 1 (leakage).
  Learning-curve chart shows the 3m/6m rungs with bands; the 12m rung is still empty and the chart says so in words.

Month 13-24
  12m rung begins (month 13); challenger lambda leaves zero; first honest "champion vs challenger" line; checkpoint 2 at month 24.

Month 36
  n_eff = 2 at 12m; first MB36 cohort; checkpoint 3 (the falsification test that matters).
```

## 11.2 The learning-curve chart the owner will look at

One panel, two lines and a band, x-axis = clean months with matured 12m labels (legacy months drawn as hollow markers at x < 0, never joined to the line):

```
 IC (12m, sector-neutral)
  +0.10 |                                                     bootstrap 90% band narrows as x grows
        |                                  ............:::::::::::::::::::::
  +0.05 |                      ......::::::::::::::::::::::::::::: champion ────────
        |             ....:::::::::::::::::::::::::::::::::::::::: challenger - - - -
   0.00 |----o----o----:::::::::::::::::::::::::::::::::::::::::::::::::::::::::::: target lines from 1.2 drawn faint
        |  o    o     ::::
  -0.05 |  legacy      first 12m
        |  (flagged)   label at month 13
        +---------------------------------------------------------------------------► clean months (12m labels matured)
           -4  -3  -2  -1   0        6        12        18        24        30        36
```

Below it, the CUSUM panel, and a one-line verdict computed from the slope test: "rising / flat / falling; CI [..]; months to checkpoint N: k".

## 11.3 If only 4 weeks of implementation were available

Keep (in this order; each item is worthless without the ones above it):

```
week 1   schema v2 + migrate-legacy + ISIN identity + universe snapshot + price backfill + TR index + split reconciliation
week 2   fundamentals_pit ingest with contracts and gates; sector_map from the CSV; 4 factors only: mom_12_1, trend_200, quality_roce, growth_rev_3y
week 3   champion (family-equal) scoring; labels; evaluate with HAC + shuffle + PIT test; ledger export; one-page report
week 4   run monthly wiring + cron; UI Evidence tab (chart + IC table with bands); ADR template and the six retirement ADRs
```

Cut (explicitly, with the reason it is safe to defer):

- the challenger and `weight_sets` (its weights equal the champion's for 13 months anyway);
- paper portfolios, costs and the scoreboard (decile spreads net of a flat 80 bp round trip are printed instead; the portfolio layer is added before month 13);
- sector-level features, `inst_hold_chg`, value and leverage shadows (nothing is lost: they are registered later with a later `registered_on`, which is honest);
- crowding monitors, PSI drift (contracts with min/max/null-rate still run), quarterly cadence, block bootstrap (HAC alone);
- UI tabs other than Evidence and Data health; the legacy scripts keep serving the stock pages until then.

What must not be cut even under pressure: point-in-time storage with `observed_at`, the total-return index with split reconciliation, ISIN identity, the gates, pre-registration in the registry, and the shuffle + PIT tests. Those are the guards; everything else is furniture.

---

# 12. Risks, failure modes and open questions

Each risk names its mechanism in one line so a reader who has not met the term can judge it.

## 12.1 Failure modes this design guards against, and the residual risk

```
failure mode                 mechanism (one line)                                        guard in this design                      residual risk
----------------------------------------------------------------------------------------------------------------------------------------------------------
data snooping                testing many ideas and keeping the winners inflates         pre-registration, hypothesis budget,      the owner reads the shadow table
                             apparent skill                                              deflated thresholds, immutable score sets  monthly and forms opinions before
                                                                                                                                  registration; unfixable, disclosed
survivorship                 losers vanish from the sample, winners stay                 PIT membership, track-to-maturity,        the price backfill is survivor-only;
                                                                                         delisting flags, cohort reports           quarantined to descriptive use
unadjusted corporate actions a split looks like a crash                                  in-engine TR index, overlap                demergers/rights still need a human
                                                                                         reconciliation, unrecorded-CA detector     confirmation each month
look-ahead in fundamentals   using a number before it was published                      observed_at on every fact, lag rule,       Yahoo's own lag is unknown and
                                                                                         PIT reproducibility test                   variable; live snapshots are safe,
                                                                                                                                  backfilled statements are not used
costs eating the spread      turnover x cost exceeds gross alpha                          net-first reporting, hold band, T+1 fills, impact assumptions are guesses;
                                                                                         liquidity screen, 0.5x/1x/2x sensitivity   sensitivity bands disclose that
regime change                what worked in one regime fails in the next                 equal-weight champion, shrinkage k=3,       a 3-year bull market will make every
                                                                                         no regime labels, CUSUM view              momentum-heavy ranking look brilliant
factor crowding              everyone owns the same names; the premium is arbitraged     overlap with index products, valuation     no automatic response; monitor only
                             or crashes together                                         spread of the top decile, family caps
operator abandonment         the monthly ritual stops; state rots                         one command, < 60 min, one-page report,    the owner's attention is the single
                                                                                         re-runnable past months, ledger rebuild    point of failure; cron mitigates
silent data drift            the vendor changes units or coverage without notice          field contracts, PSI, version pin,          new fields can still arrive wrong;
                                                                                         BLOCK gates                                the first month's contract is a guess
```

## 12.2 Top risks in this design itself

1. **Evidence starvation.** A 12-month primary horizon at monthly cadence means `n_eff = 2` at month 36. The design accepts this rather than lie about it, and uses the 3m/6m ladder as early warning. Mechanism name: *small sample* — the standard error of a mean shrinks with the square root of independent observations, and overlapping windows do not add independent observations.
2. **Yahoo Finance as a single free source.** Coverage of Indian statements is uneven (`returnOnEquity` is None for many names; `freeCashflow` None even for Hero MotoCorp), fields change encoding between versions, and rate limits are opaque. Guard: everything derived from statements rather than `info` ratios where possible; contracts; the ledger keeps what was observed. Residual: if Yahoo removes Indian fundamentals, the fundamental families stop accruing clean months; the price families continue. An optional adapter interface (`quant/data/sources/`) is specified in the handoff so a paid or scraped source can be added without touching factors.
3. **The financials bucket.** 101 names in one neutralisation bucket, with statement fields that mean different things for banks, NBFCs and exchanges. `quality_roce` and `accruals` are close to meaningless for banks; v1 computes them where the fields exist and marks the rest missing, so the bucket often scores on trend, risk and growth only. Open question 12.3-1.
4. **Backfilled sector labels.** The four legacy months and any price backfill use today's sector mapping. Sector reclassification in three months is rare; over ten years it is not. Disclosed by `source='backfilled_current'`.
5. **Approval seat fatigue.** If the monthly report ever exceeds one page or asks for more than three decisions, the owner will stop reading it. Enforced by the report generator (it truncates and says so).
6. **A brilliant first year.** If the champion posts +0.10 IC at 3 months for a year, the temptation to size real money before month 36 will be enormous. The dashboard's "years to significance" column and the checkpoint table exist for that moment.

## 12.3 Open questions to settle with evidence, not opinion

1. Split Financial Services into banks / NBFC / capital markets & insurance at `taxonomy_version 2`? Decide at month 6 using within-bucket dispersion of 12m returns; register as a proposal.
2. Should `trend_200` and `mom_12_1` share a family (as designed) or should trend be its own family? Correlation on live data at month 6 decides; the rule is `|corr| >= 0.7` means same family.
3. Fast-track vs standard track for `earnings_yield`: value has an Indian index product but weak recent evidence; grade B as registered. Revisit at month 12.
4. `inst_hold_chg`: is Yahoo's `institutionsPercentHeld` updated on filing dates (quarterly, ~21 days after quarter end) or continuously? Log the observation dates for six months, then decide whether the factor is flow or noise.
5. Delisting treatment: last price vs -50% assumption. Report both until a delisting actually occurs in the tracked set; then look at what happened.
6. Quarterly versus monthly rebalancing as the default paper portfolio: decided by the paired scoreboard at month 18, not before.
7. Whether an LLM approver may ever activate a factor. Default no; revisit after 12 months of the assistant's recorded recommendations can be compared with outcomes.
8. NSE holiday calendar maintenance and the exact "last trading day" rule when the month ends on a holiday: shipped as a yaml, needs a yearly update; a missed update degrades to a WARN.

## 12.4 Confidence, split in two

```
That the failure modes named here are the ones that will otherwise sink this project, and that the guards
as specified block them:                                                                                     85%
That a sector-neutral six-factor equal-weight ranking built from free Yahoo data will show a 12m IC >= +0.04
with HAC t >= 2 by month 36 (the checkpoint-3 pass condition):                                                35%
```

The second number is low on purpose. It is the number the whole design is built to measure honestly rather than to assume.

## 12.5 The whole argument in one diagram

```
   free data ──► point-in-time ledger ──► pre-registered factors ──► equal-weight champion ──► paper portfolio, net of cost
      │          (observed_at, TR index,   (sign, horizon, family,     (no multipliers,          (T+1 fills, hold band,
      │           ISIN, PIT membership)     budgeted, versioned)        no filters that zero)     liquidity screen)
      │                   │                          │                          │                          │
      ▼                   ▼                          ▼                          ▼                          ▼
   contracts +        reproducibility           shadow record            challenger = f(evidence)    scoreboard with
   gates BLOCK        test each month           12-24 months             shrunk, sign-constrained,   years-to-significance
   bad months                                                            promoted only at t >= 2
                                                                                  │
                                 ┌────────────────────────────────────────────────┘
                                 ▼
                learning curve = expanding-window OOS IC with HAC band, clean months only, CUSUM, slope test
                                 │
                                 ▼
                checkpoints at months 12 / 24 / 36 / 48 decide: continue, freeze, retire, or stop
```

**One sentence for leadership:** the new engine is built so that after three years it can prove, with statistics that account for its own small sample, whether ranking Indian stocks within their sectors on six pre-registered factors predicts twelve-month returns after costs — and if it cannot, the same machinery will say so plainly and stop.
