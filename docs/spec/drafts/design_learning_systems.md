**V2 Engine Design — Learning Systems & MLOps lens**
Draft 1, written 2026-09-05 on branch `red-team-review-sep-2026`. Author role: architect "Learning Systems & MLOps". Inputs: `docs/spec/00_context_brief.md`, `docs/analysis/red_team_review.md`, `AGENTS.md`, the code and `quant_engine.db` as of commit `3676a0b`, plus three live read-only checks (NSE constituent CSV header, `yf.download` adjusted-history behaviour, per-ticker split/dividend/quarterly fields).

> **Answer up front.** Build the V2 engine as an *experiment registry that happens to score stocks*, not as a scorer that happens to keep notes. The champion model at launch is a sector-neutral equal-weight composite of ten to eleven pre-registered factors; it is deliberately not allowed to learn weights until it has twelve independent evaluation periods at a 3-month horizon, and even then it only moves by shrinkage toward the evidence. Everything the owner wants to see rise over time — out-of-sample rank IC, net-of-cost spread, multi-bagger recall — is produced by a fixed, pre-registered evaluation protocol that re-fits from scratch at every point on the curve, so a rising line means the system learned, not that noise averaged out. Speed of visible progress comes from *challengers* (aggressive variants and a price-only track backfilled to 2016) that compete on paper and can only replace the champion by passing numeric, pre-registered promotion criteria under a yearly hypothesis budget. The four 2026 snapshots migrate in as flagged, hollow points; the first clean point is the 2026-10-03 run.

Everything in this document is a target or a design decision. No performance is claimed.

---

# 1. Objective & success metrics

## 1.1 What the system is optimising

The system's job is a **ranking** of Nifty 500 stocks each month such that, after sector effects are removed and trading costs are paid, higher-ranked stocks earn higher forward returns — and the *measured* skill of that ranking rises as clean months accumulate.

Three objects must be kept apart, because the legacy engine conflated them:

```
learning target     what the weight-learning rule sees        3-month sector-relative log total return
evaluation target   what the scoreboard reports as primary    12-month sector-relative log total return
slow KPI            what the owner ultimately cares about     36-month multi-bagger recall (2x total return)
```

Section 2 justifies the horizons. This section fixes what "success" means numerically and, more importantly, what would falsify the approach.

## 1.2 Success metrics (targets, not claims)

All metrics are computed by `python -m quant evaluate` from the `evaluations` table (section 10) and shown on the dashboard's Scoreboard tab. "Clean month" means a monthly run that passed every data-quality gate in section 4.6 with no override.

```
Metric                                   Definition (see §7 for formulas)                 Target by      Target value
M1  OOS rank IC, H=3, champion            rolling 12-run mean of monthly Spearman(score,   month 12       > 0.02 with HAC t > 1.0
                                          3-run-ahead sector-relative return)              month 24       > 0.03 with HAC t > 2.0
                                                                                            month 36       > 0.03 with HAC t > 2.5
M2  OOS rank IC, H=12, champion           same at 12-run horizon (first reading month 13)  month 24       > 0.04 (12 overlapping points, HAC lag 11)
M3  Net top-minus-bottom decile spread,   annualised, cost model §8, H=12                  month 24       > 0
    H=12                                                                                    month 36       lower 90% bootstrap bound > 0
M4  Learning-curve slope                  OLS slope of (champion OOS IC − EW OOS IC) on     month 36       > 0 with HAC t > 1.5
                                          months-of-clean-data k, over the last 24 points
M5  Multi-bagger recall@decile            share of names with 36-month total return ≥ 2x    month 39       > 1.5 × base rate (10% for a random decile)
                                          that were top-decile at t0                                       first live reading Oct 2029
M6  Hypothesis discipline                 hypotheses tested YTD ≤ budget; 0 unregistered    every month    hard invariant
                                          factors in any model version
M7  Gate pass rate                        clean runs / total runs, trailing 12              every month    ≥ 10/12
M8  Reproducibility                       re-scoring any past month from stored inputs      every month    100% (hash-identical)
                                          reproduces stored scores
```

Two horizons of the same thing are deliberately in the list. M1 is the *learning* metric and will move first; M2 is the *goal* metric and will lag by a year. The owner should expect M1 to be readable before M2 exists.

## 1.3 The timeline of what can be known when

This is the single most important expectation-setting table in the document. The live fundamental track starts from zero in October 2026; nothing can shortcut that. The price-only track can be backfilled.

```
                                   live fundamental track       price-only track (backfilled)
first clean scoring run            2026-10-03                   2016-01-29 (monthly grid, survivorship-biased)
first H=1 OOS point                2026-11 run                  available immediately (~128 points)
first H=3 OOS point                2027-01 run                  available immediately (~126 points)
12 independent H=3 periods         2029-10 run (36 runs)        available immediately (~42 non-overlapping)
first H=12 OOS point               2027-10 run                  available immediately (~117 overlapping)
first 36-month multi-bagger read   2029-10 run                  available immediately (~93 overlapping)
weights may deviate from EW        2029-10 (champion rule §6)   challenger C2 from day 1 (§6.6)
```

## 1.4 What would falsify the approach

State this now so it cannot be re-argued later:

```
F1  After 36 clean monthly runs, M1 (H=3 champion OOS IC) has HAC t < 1.0            → the factor set has no measurable
                                                                                         short-horizon skill in this universe
F2  After 36 clean runs, M4 (learning-curve slope of champion − EW) ≤ 0               → weight learning adds nothing; freeze
                                                                                         champion at EW permanently, keep only
                                                                                         factor add/retire as the learning mechanism
F3  After 48 clean runs, M3 lower bootstrap bound < 0 while M1 is positive            → skill exists but does not survive costs;
                                                                                         switch to quarterly rebalance and re-test 12 more months
F4  The price-only backfilled track shows M1 < 0.02 over 2016-2026                    → momentum/low-vol do not work in this universe
                                                                                         at this horizon; remove them from the baseline
                                                                                         before the live track wastes 3 years on them
```

F4 can be checked in month 1. It is the only falsifier available early, and it is cheap.

## 1.5 Where the seed hypotheses are wrong from this lens

- **Seed 1 (12-month primary target).** Right as the *evaluation* target, wrong as the *learning* target. A 12-month target evaluated monthly gives one independent observation per year; a learning rule fed that would have three independent data points in 2029. Learning uses H=3; evaluation reports H=12 alongside (section 2).
- **Seed 4 (weights in [5%, 30%]).** A 5% floor makes retirement impossible and a 30% ceiling is where the legacy optimiser got stuck. Shrinkage toward equal weight plus a single 25% cap replaces both (section 6).
- **Seed 4 ("only allowed to deviate once ≥ 12 non-overlapping periods exist").** Correct, but under-specified: 12 periods *at which horizon*? At H=3 that is 36 months. This document accepts the 36-month wait for the champion and gives the owner earlier movement through challengers, not by loosening the gate.
- **Seed 6 (learning curve = OOS IC vs months of data).** A cumulative-mean IC rises even for a model that never learns, because the estimate's noise shrinks. The chart the owner looks at must be built by *re-fitting at each k* and must be plotted against the equal-weight baseline's curve at the same k. Section 7.7 defines it; the difference between "evidence curve" and "learning curve" is enforced in the schema (`learning_curve_points.ew_oos_ic` is mandatory).

---

# 2. Prediction target & horizons

## 2.1 The measurable target

For stock i, scoring date t (a run date, section 7.1), and horizon H measured in runs:

```
TRI_i(d)             total-return index built from prices_daily (split- and dividend-adjusted by us, §4.3)
r_tot(i,t,H)     =   ln( TRI_i(t+H) / TRI_i(t) )
r_sec(i,t,H)     =   r_tot(i,t,H) − mean_{j ∈ group(i,t)} r_tot(j,t,H)        group = sector_group as of t (§3)
r_mkt(i,t,H)     =   r_tot(i,t,H) − mean_{j ∈ universe(t)} r_tot(j,t,H)
r_sec_w          =   r_sec winsorised at the 1st/99th percentile within (t,H)
```

Stored in `forward_returns` (section 10) with all four columns, so the choice of winsorisation or neutralisation is reversible. Equal-weight group means are used, not cap-weighted, because the composite ranks stocks, not rupees.

Delisted or suspended names keep their last traded price to the end of the horizon and receive `partial_horizon=1`. They are never dropped; dropping them is how survivorship bias enters.

## 2.2 Why H=3 for learning and H=12 for evaluation

```
Horizon   independent obs / year   noise per obs (IC SE at n≈450, ρ≈0.3 cross-corr)   fit to "compounder" goal
H=1       12                       ≈ 0.05                                              poor (noise, microstructure)
H=3        4                       ≈ 0.05 but 3× larger signal for slow factors        acceptable
H=6        2                       same                                                good
H=12       1                       same                                                good, but 1 obs/yr
```

The IC standard error hardly changes with horizon (it is a cross-sectional statistic), but the *signal* of slow factors (quality, value, flows) accumulates roughly with √H to H, while momentum's signal peaks around 3–12 months. H=3 is the shortest horizon at which the slow factors are not at a structural disadvantage against trend, and it still yields four independent periods a year. It is a compromise; it is written down as one.

H=12 is the evaluation target because the owner's stated goal is multi-year compounders, and a factor that ranks 12-month returns is the closest measurable thing to that goal with a usable sample size.

H=1 is kept purely as a diagnostic (it is what the four legacy snapshots can support) and as the learning horizon of challenger C1 (section 6.6).

## 2.3 The multi-bagger KPI

```
multibagger(i,t)      = 1  if  max_{h ≤ 36 runs} TRI_i(t+h)/TRI_i(t) ≥ 2.0    else 0
                        (path max, not end-point: a stock that doubled and gave it back still "was" a multi-bagger)
recall@decile(t)      = P( top-decile at t | multibagger(i,t)=1 )
precision@decile(t)   = P( multibagger(i,t)=1 | top-decile at t )
base rate(t)          = mean_i multibagger(i,t)
lift(t)               = precision@decile / base rate
```

Historical base rates in Nifty 500 over rolling 3-year windows vary between roughly 5% and 30% depending on the starting year (the backfill will measure this; do not trust the range until it has). Because the positives are few and clustered in time, this is a *slow KPI reported with a 90% Wilson interval*, never a learning target. Using it as an objective would overfit to the two or three bull years that generate most positives.

## 2.4 Horizons tracked

```
H (runs)   role                          stored in forward_returns   reported on dashboard
1          diagnostic, legacy comparable  yes                         IC only
3          learning target                yes                         IC, spread, learning curve
6          secondary                      yes                         IC
12         primary evaluation             yes                         IC, net spread, alpha scoreboard
24         secondary slow                 yes                         IC
36         multi-bagger KPI               yes (as multibagger flag)   recall/precision/lift
```

---

# 3. Universe & sector taxonomy

## 3.1 Canonical source (verified live 2026-09-05)

```
URL      https://niftyindices.com/IndexConstituent/ind_nifty500list.csv
Header   Company Name,Industry,Symbol,Series,ISIN Code
Rows     500 constituents
Industry 20 distinct values:
         Financial Services 101 | Capital Goods 63 | Healthcare 48 | Automobile and Auto Components 38
         Consumer Services 29 | Fast Moving Consumer Goods 28 | Information Technology 27 | Chemicals 26
         Metals & Mining 18 | Power 17 | Oil Gas & Consumable Fuels 17 | Consumer Durables 16 | Services 14
         Construction 13 | Realty 11 | Construction Materials 11 | Telecommunication 10 | Textiles 5
         Media Entertainment & Publication 5 | Diversified 3
```

The seed hypothesis assumed the four-level NSE/AMFI hierarchy (Macro-Economic Sector → Sector → Industry → Basic Industry). That hierarchy is not in this file; the `Industry` column is the *Sector* level of NSE's scheme. Decision: **the `Industry` column of this CSV, renamed `nse_sector`, is canonical.** Twenty groups are enough for neutralisation (finer groups would have too few members per group for rank-based neutralisation in a 500-stock universe). The four-level file is an optional enrichment behind an adapter interface (`quant/sectors/taxonomy.py::TaxonomyAdapter`) and an open question (section 12); nothing depends on it.

The `ISIN Code` column is stored and used as the stable identity key across symbol changes (`Symbol` changes on renames; ISIN does not).

## 3.2 Fallback chain

```
1. nse_sector from the constituent CSV pulled this run                   source='nse_csv'
2. nse_sector from the most recent stored sector_map row for the ISIN    source='nse_csv_stale'   (CSV fetch failed or symbol missing)
3. Yahoo `sector` mapped to the nearest nse_sector via a fixed table      source='yahoo_mapped'    (new listing not yet in a stored CSV)
4. 'Unclassified'                                                        source='none'            (counts against gate G5)
```

The Yahoo→NSE mapping table lives in `quant/sectors/yahoo_to_nse.py` and is versioned like any other code (its hash is part of the run's `code_hash`). Yahoo `sector`/`industry` are also stored per ticker per run as secondary attributes; they are useful for display and for the WACC table the legacy DCF used, but they never drive neutralisation.

## 3.3 Point-in-time mapping

```sql
CREATE TABLE sector_map (
  isin            TEXT NOT NULL,
  ticker          TEXT NOT NULL,            -- e.g. HEROMOTOCO.NS at valid_from
  nse_sector      TEXT NOT NULL,
  sector_group    TEXT NOT NULL,            -- after the small-group merge, §3.4
  yahoo_sector    TEXT,
  yahoo_industry  TEXT,
  source          TEXT NOT NULL CHECK (source IN ('nse_csv','nse_csv_stale','yahoo_mapped','none','legacy_migration')),
  source_hash     TEXT,                     -- sha256 of the CSV that produced this row
  valid_from      TEXT NOT NULL,            -- ISO date, inclusive
  valid_to        TEXT,                     -- NULL = current
  PRIMARY KEY (isin, valid_from)
);
CREATE TABLE universe_membership (
  as_of           TEXT NOT NULL,
  isin            TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  in_index        INTEGER NOT NULL CHECK (in_index IN (0,1)),
  source          TEXT NOT NULL,            -- 'nse_csv' | 'legacy_snapshot' | 'backfill_current_list'
  source_hash     TEXT,
  PRIMARY KEY (as_of, isin)
);
```

Rules:
- Every run fetches the CSV, hashes it, and writes a new `sector_map` row for an ISIN **only if** its `nse_sector` changed; the previous row gets `valid_to = as_of − 1 day`. Reclassifications therefore take effect from the run that observed them, never retroactively.
- `group(i,t)` in section 2.1 is looked up with `valid_from ≤ t < valid_to`. Backfilled months before 2026-06 use the earliest known row (`source='backfill_current_list'`) and every evaluation on that track carries the flag `sector_pit='current_proxy'`. This is a known, labelled bias (section 12).
- Membership history for the live track is exact from 2026-10 onward (monthly CSV pulls). For 2026-06 to 2026-09 the four legacy ticker lists are loaded as `source='legacy_snapshot'`. For the backfill, membership is the *current* list — survivorship-biased, labelled as such everywhere it appears.

## 3.4 Neutralisation groups and the small-sector rule

Rank-based neutralisation within a group of 3 stocks is meaningless (ranks are 1, 2, 3). Rule:

```
sector_group = nse_sector                                   if members at t ≥ 8
             = merge target from sector_groups table        otherwise
Merge table v1 (owner may edit; changes are versioned and take effect from the next run):
  Diversified (3)                          → Capital Goods
  Textiles (5)                             → Consumer Durables
  Media Entertainment & Publication (5)    → Consumer Services
```

This gives 17 groups with the smallest at ~10 members. The table is `sector_groups(version, nse_sector, sector_group, valid_from, decision_id)`.

Stock-level factors are neutralised within `sector_group` (section 5.3). The composite therefore carries **no** sector bet unless a sector-level factor is explicitly included — which is the only sanctioned way to express one.

## 3.5 Sector-level features

These are factors in the library (section 5) whose value is the same for every member of a group. They are computed from our own price and holdings store, so they are point-in-time by construction.

```
sector_mom_12_1      equal-weight mean of members' mom_12_1 raw values           direction +   H 3–12
sector_breadth_200   share of members with close > SMA200                        direction +   H 1–3
sector_inst_flow_3m  equal-weight mean of members' inst_hold_chg_3m              direction +   H 3–6
sector_dispersion    cross-sectional std of members' 3-month returns             not a factor; stored as a regime feature
```

"Sector flows" in the owner's sense (FII/DII net buying by sector) exists only as monthly PDFs from NSDL/CDSL; a parser is an optional adapter (`quant/data/adapters/nsdl_flows.py`, status: not built). Until then, `sector_inst_flow_3m` from Yahoo's `heldPercentInstitutions` is the proxy, and its status is `candidate` (section 5.5) because the input's quality is unproven.

Sector-level factors are z-scored **across groups** (17 values) and capped in total composite weight at 20% (section 6.4), so a bad sector call cannot dominate.

---

# 4. Data layer

## 4.1 Data flow

```
                    monthly run (first Saturday, IST)
                    ─────────────────────────────────
 niftyindices CSV ─► universe_membership, sector_map ─────────────────────────────┐
                                                                                  │
 yf.download (batched, unadjusted, actions=True) ─► data/prices/year=YYYY/*.parquet
                                                    corporate_actions (sqlite)     │
                                                    prices manifest (sha256)      │
                                                                                  ▼
 yf.Ticker(t).info / .financials / .balance_sheet ─► data/raw/fundamentals/YYYY-MM.jsonl.gz
   / .cashflow / .quarterly_financials  (0.5 s)      fundamentals_pit (sqlite, as_of = pull date)
                                                                                  │
                                              ┌───────────────────────────────────┘
                                              ▼
                                   quality gates G1..G8  ──fail──► runs.status='blocked', data_quality_events
                                              │ pass
                                              ▼
                                   factor_exposures (per factor_version) ─► scores (per model_version)
                                              │
                                              ▼
                          forward_returns for older as_of now realised ─► evaluations, learning_curve_points
                                              │
                                              ▼
                          paper_positions / paper_performance ─► knowledge/reports/YYYY-MM.md ─► ui/*.js
```

## 4.2 Point-in-time storage

Every non-price input is stored with two dates:

```sql
CREATE TABLE fundamentals_pit (
  isin         TEXT NOT NULL,
  ticker       TEXT NOT NULL,
  field        TEXT NOT NULL,        -- canonical field name, e.g. 'net_income', 'ocf', 'capex', 'total_assets',
                                     --   'current_liabilities', 'ebit', 'revenue', 'diluted_eps', 'total_debt',
                                     --   'shareholders_equity', 'shares_outstanding', 'dividend_rate',
                                     --   'held_pct_institutions', 'market_cap', 'trailing_pe', 'book_value_ps'
  period_end   TEXT,                 -- fiscal period the value describes (NULL for point values like market_cap)
  freq         TEXT CHECK (freq IN ('A','Q','P')),   -- annual, quarterly, point-in-time snapshot value
  value        REAL,
  unit         TEXT NOT NULL,        -- 'INR', 'INR_cr', 'fraction', 'percent', 'count', 'ratio'
  as_of        TEXT NOT NULL,        -- date WE observed it (pull date). This is the only honest availability date.
  source       TEXT NOT NULL,        -- 'yahoo_info' | 'yahoo_financials' | 'yahoo_quarterly' | 'legacy_raw_json' | 'lag_proxy'
  pit_quality  TEXT NOT NULL CHECK (pit_quality IN ('observed','lagged_proxy','legacy')),
  run_id       INTEGER,
  PRIMARY KEY (isin, field, period_end, freq, as_of)
);
```

Usage rule, enforced in `quant/data/store.py::load_fundamentals(as_of)`: a row is usable for scoring date t only if `as_of ≤ t`. For the live track every value is `observed` and the rule is exact. Yahoo does not tell us when a statement was first published, so **fundamentals cannot be backfilled point-in-time**. The backfill track therefore either excludes fundamental factors (the default; it is a price-only track) or, for exploratory work only, uses `pit_quality='lagged_proxy'` rows with `as_of = period_end + 120 days` for annual and `+ 60 days` for quarterly statements. Evaluations on lagged-proxy inputs are stored with `data_track='backfill_lagged'` and are never shown on the primary Scoreboard.

Units are normalised at ingest with the rules the red team verified:

```
dividend yield   := dividend_rate / close           (never Yahoo's dividendYield)
debt_to_equity   := Yahoo debtToEquity / 100         (Yahoo gives percent)
roe              := NULL when Yahoo returns None     (never 0)
market_cap       := Yahoo marketCap in INR           (converted to crores only for display)
statements       := INR as delivered; crore conversion only in the UI export
```

Every transformation has a unit test with a real observed value (e.g. `HEROMOTOCO dividendYield=3.48 → 0.0348`).

## 4.3 Prices, corporate actions and total returns

**Decision: store unadjusted closes and our own adjustment factors; never store Yahoo's adjusted series.** Verified this session: `yf.download(auto_adjust=True)` returns ZFCVINDIA's June 2026 closes around ₹2,600 today, while the DB stores the unadjusted quote of ₹14,714 for 2026-06-14. Yahoo rewrites history at each corporate action, so any adjusted series we store today will disagree with one we pull next year. Unadjusted closes plus a ledger of actions are stable and auditable.

```
Parquet: data/prices/year=YYYY/part-0.parquet      (one file per calendar year, zstd)
columns: date(date32), ticker(str), isin(str), open, high, low, close(float64, unadjusted),
         volume(int64), split_ratio(float64, 1.0 when none), dividend(float64, 0.0 when none),
         source(str), pulled_at(date32)
```

```sql
CREATE TABLE corporate_actions (
  isin TEXT NOT NULL, ticker TEXT NOT NULL, ex_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('split','bonus','dividend','rights','demerger','symbol_change','other')),
  ratio REAL,            -- split/bonus factor, e.g. 6.0 for a 6:1 split (Yahoo 'Stock Splits' value)
  amount REAL,           -- dividend per share in INR
  source TEXT NOT NULL,  -- 'yahoo_actions' | 'manual' | 'inferred'
  observed_at TEXT NOT NULL,
  note TEXT,
  PRIMARY KEY (isin, ex_date, kind)
);
```

Total-return index, computed in `quant/evaluation/returns.py::build_tri(prices, actions)`:

```
daily gross return  g_d = (close_d + dividend_d) / (close_{d−1} / split_ratio_d)
TRI_d               = TRI_{d−1} × g_d,   TRI_0 = 100
sanity              any |g_d − 1| > 0.40 with split_ratio_d == 1.0 and no 'inferred' action → data_quality_event
                    code='UNEXPLAINED_JUMP', ticker excluded from forward_returns for windows containing d,
                    and listed in the monthly report for manual confirmation
```

Rights issues and demergers are not fully captured by Yahoo's action feed. They surface as `UNEXPLAINED_JUMP` events and are resolved manually by inserting a `corporate_actions` row with `source='manual'` and a `decision_id`. This is the honest state of free Indian data; the events table makes the gap visible rather than silently biasing returns.

## 4.4 Backfill plan

```
python -m quant data backfill-prices --start 2016-01-01 --batch-size 25 --sleep 1.0
  ~500 tickers / 25 per batch = 20 yf.download calls, auto_adjust=False, actions=True, threads=False
  expected wall time: 3–6 minutes; expected rows: ~1.3 M; expected Parquet size: 18–30 MB total (zstd)
  writes: data/prices/year=2016..2026, corporate_actions, prices_manifest
  survivorship: universe = today's constituents (no free historical membership); every backfill artefact is tagged
                data_track='backfill_price_only' and universe_source='current_list'
python -m quant data backfill-prices --tickers-from legacy      # adds tickers present in 2026 snapshots but not in today's list
```

Benchmarks pulled with the same command: `^CRSLDX` (Nifty 500 price index; verify the symbol on first run), `^NSEI`. Factor-index proxies are constructed, not downloaded (section 7.4).

Incremental monthly update: `python -m quant data update-prices` pulls from `max(date) − 10 days` to today per ticker (the 10-day overlap catches late corrections) and rewrites only the current year's Parquet file.

## 4.5 Storage format and what is committed to git

```
artefact                              format                  committed?   growth
quant_engine.db (state, registry,     SQLite                  yes          ~1–2 MB / month
  exposures, scores, evaluations)
data/prices/year=YYYY/*.parquet       Parquet zstd            yes          ~2–3 MB / year; only current-year file changes monthly
data/raw/fundamentals/YYYY-MM.jsonl.gz raw Yahoo payloads     yes          ~0.5–0.8 MB / month
data/MANIFEST.json                    sha256, rows, ranges    yes          tiny
knowledge/**/*.md                     markdown                yes          tiny
ui/*.js exports                       JS                      yes          ~1.5 MB, rewritten monthly
derived factor panels (cache)         Parquet under .cache/   no           regenerable from the above
```

Decision: **plain git, no LFS.** Projected five-year repository growth is roughly 150–250 MB, inside GitHub's comfort zone; LFS adds a dependency and bandwidth quota for no benefit at this size. Trigger to revisit: any single file > 50 MB or repository > 800 MB (`python -m quant data check` prints both). The raw fundamentals payloads are committed because they are the *only* record of what Yahoo said on that date — the point-in-time claim rests on them.

## 4.6 Data-quality flags and gates

Per-ticker flags are stored in `data_quality_events`; run-level gates decide whether scoring proceeds.

```sql
CREATE TABLE data_quality_events (
  event_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, as_of TEXT NOT NULL,
  isin TEXT, ticker TEXT, field TEXT,
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  code TEXT NOT NULL,          -- see list below
  message TEXT, blocked INTEGER NOT NULL DEFAULT 0
);
```

```
Gate  Check                                                                  Threshold                 On failure
G1    constituents with a close at as_of                                     ≥ 480 / 500               BLOCK
G2    stale prices (volume 0 and unchanged close ≥ 5 consecutive sessions)   ≤ 2% warn, > 5% block     WARN / BLOCK
G3    UNEXPLAINED_JUMP events this month                                     ≤ 10                      BLOCK above 10; each event excludes the ticker's returns
G4    unit sanity: div yield ≤ 25%, D/E ≤ 50, ROCE ∈ [−100%, 200%],           per-field violations       field set NULL + flag; > 5% of universe → BLOCK
      inst holding ∈ [0,1], market_cap > 0
G5    sector_map coverage with source ≠ 'none'                               ≥ 98%                     BLOCK
G6    active-factor coverage (non-missing raw value)                          price factors ≥ 95%,       factor excluded from this month's composite
                                                                              others ≥ 70%              (recorded in scores.excluded_factors); < 50% on
                                                                                                        ≥ 3 factors → BLOCK
G7    reproducibility: re-score previous as_of from stored inputs             hash identical            BLOCK
G8    leakage: shuffle test |IC| < 2·SE; planted-signal test recovers IC      both pass                 BLOCK
      within ±0.02 of expectation (§7.5)
```

A blocked run writes `runs.status='blocked'`, keeps all ingested data, produces no `scores` rows, and writes the report anyway. `python -m quant run monthly --override-gate G2 --decision-id 17` proceeds past a named gate only with an existing Tier-2 decision record (section 9.7). Overrides are counted in M7.

---

# 5. Factor library

## 5.1 Plugin contract

```python
"""quant/factors/base.py — the plugin contract every factor implements."""
from dataclasses import dataclass, field
from typing import Protocol, Literal
import pandas as pd

Family = Literal['momentum','trend','risk','quality','value','growth','flows','leverage','payout','sector','control']
Status = Literal['proposed','registered','candidate','active','probation','retired','archived']

@dataclass(frozen=True)
class FactorMeta:
    name: str                         # snake_case, unique, never reused
    version: int                      # bump on ANY formula/input change; old versions stay computable
    family: Family
    direction: int                    # +1: higher raw value → higher expected return; −1: the reverse
    horizon_months: tuple[int, int]   # (min, max) horizon over which the hypothesis is expected to hold
    hypothesis: str                   # one paragraph, plain language, written BEFORE evaluation
    formula: str                      # human-readable formula, must match compute()
    inputs: tuple[str, ...]           # fundamentals_pit field names and/or 'prices'
    level: Literal['stock','sector']  # sector-level factors take the same value for a whole sector_group
    min_history_days: int = 0         # for price factors
    min_coverage: float = 0.70        # gate G6 threshold for this factor
    neutralise: bool = True           # False only for sector-level factors and controls
    evidence: str = ''                # citations / prior results, with the caveat that none are from this system
    preregistration_id: str | None = None   # hypotheses.hypothesis_id; required before status ≥ 'registered'

class Factor(Protocol):
    meta: FactorMeta
    def compute(self, inputs: 'FactorInputs', as_of: str) -> pd.Series:
        """Raw value indexed by isin for every universe member at as_of. NaN where not computable.
        Must only touch data with as_of ≤ scoring as_of — FactorInputs enforces this and raises on violation."""
```

`FactorInputs` (in `quant/factors/inputs.py`) is a read-only view built by the framework for one `as_of`; it exposes `prices(window_days)`, `fundamental(field, freq, lag_periods=0)`, `holdings()`, `sector_group()`, and raises `LookaheadError` if anything newer than `as_of` is requested. This is the mechanical enforcement of the point-in-time rule; a factor cannot leak by accident.

Registration: `python -m quant factors register mom_12_1` imports the plugin, validates the metadata, computes `code_hash = sha256(inspect.getsource(module))`, and inserts rows into `factor_registry` and `factor_versions`. A factor whose code hash changes without a version bump fails gate G7 (re-scoring the previous month would produce a different hash), so silent edits are impossible.

## 5.2 Registry tables

```sql
CREATE TABLE factor_registry (
  factor_id TEXT PRIMARY KEY,           -- = meta.name
  family TEXT NOT NULL, direction INTEGER NOT NULL CHECK (direction IN (-1,1)),
  horizon_min INTEGER NOT NULL, horizon_max INTEGER NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('stock','sector')),
  hypothesis TEXT NOT NULL, formula TEXT NOT NULL, inputs_json TEXT NOT NULL, evidence TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed','registered','candidate','active','probation','retired','archived')),
  preregistration_id TEXT REFERENCES hypotheses(hypothesis_id),
  registered_on TEXT, first_oos_as_of TEXT, activated_on TEXT, retired_on TEXT,
  status_decision_id INTEGER REFERENCES decisions(decision_id)
);
CREATE TABLE factor_versions (
  factor_id TEXT NOT NULL REFERENCES factor_registry(factor_id),
  version INTEGER NOT NULL, code_hash TEXT NOT NULL, module_path TEXT NOT NULL,
  valid_from TEXT NOT NULL, change_note TEXT, decision_id INTEGER,
  PRIMARY KEY (factor_id, version)
);
CREATE TABLE factor_exposures (
  as_of TEXT NOT NULL, isin TEXT NOT NULL, factor_id TEXT NOT NULL, version INTEGER NOT NULL,
  raw_value REAL, z_value REAL,               -- z_value NULL when raw missing; composite treats NULL as 0 with imputed=1
  imputed INTEGER NOT NULL DEFAULT 0, sector_group TEXT NOT NULL,
  data_track TEXT NOT NULL DEFAULT 'live',    -- 'live' | 'backfill_price_only' | 'backfill_lagged' | 'legacy'
  run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, isin, factor_id, version, data_track)
);
```

Exposures are never overwritten. Recomputing a month (after a bug fix) writes rows under the new `version`; the old rows stay, and `scores.factor_versions_json` says which version each historical score used.

## 5.3 Standardisation pipeline (framework, not plugin)

```
raw value per isin
  → drop if universe coverage < meta.min_coverage (gate G6; factor excluded this month, recorded)
  → winsorise at 1st / 99th percentile of the cross-section at as_of
  → if meta.neutralise: rank within sector_group, r ∈ (0,1) using (rank − 0.5)/n
    else:               rank across the cross-section (stock) or across groups (sector-level)
  → z = Φ⁻¹(r)   (inverse normal; gives a continuous, symmetric exposure with no bucket steps)
  → multiply by meta.direction so that +z always means "expected to outperform"
  → clip to [−3, +3]
  → missing → z = 0, imputed = 1   (neutral, not penalised; the imputed share is reported per factor)
```

Why inverse-normal ranks and not raw z-scores: ranks are robust to the fat tails and unit accidents that corrupted the legacy inputs; the inverse-normal transform keeps the composite's arithmetic well behaved; sector-neutral by construction. The legacy five-level buckets are gone.

## 5.4 Initial factor list

Status at launch, formula, direction, horizon, and the evidence basis. "Evidence" here means external literature or index track records, **not** results from this system; the four legacy periods are too few to count as evidence for anything. India-specific caveats are stated where known.

```
id                    family    dir  H(m)   level   status@launch  formula (inputs)
mom_12_1              momentum   +   3–12   stock   active         ln(close[t−21]/close[t−252]) on TRI; needs ≥ 200 valid days
mom_6_1               momentum   +   3–6    stock   candidate      ln(TRI[t−21]/TRI[t−126])
vol_252               risk       −   3–12   stock   active         std(daily ln returns, 252d)·√252
trend_200             trend      +   1–3    stock   active         close/SMA200 − 1  (replaces the death-cross kill, §6.7)
max_ret_21            risk       −   1–3    stock   candidate      max daily return over 21d ("lottery" effect)
roce                  quality    +   6–12   stock   active         EBIT_A / (total_assets_A − current_liabilities_A), latest annual with as_of ≤ t
cash_conversion_3y    quality    +   6–12   stock   active         Σ ocf(3 latest annual) / Σ net_income(3 latest annual), only if Σ NI > 0 else NaN
accruals              quality    −   6–12   stock   active         (net_income_A − ocf_A) / total_assets_A
leverage              leverage   −   3–12   stock   active         total_debt / shareholders_equity; NaN for sector_group 'Financial Services'
earnings_yield        value      +   6–12   stock   active         net_income_TTM / market_cap  (TTM from quarterly if ≥ 4 quarters, else annual)
book_to_price         value      +   6–12   stock   active         book_value_ps / close
shareholder_yield     payout     +   6–12   stock   candidate      dividend_rate / close  (buybacks unavailable from Yahoo)
rev_growth_3y         growth     +   6–12   stock   candidate      CAGR(revenue_A, 3y); NaN if either endpoint ≤ 0
asset_growth          growth     −   6–12   stock   candidate      total_assets_A / total_assets_{A−1} − 1
inst_hold_chg_3m      flows      +   3–6    stock   active*        held_pct_institutions(t) − held_pct_institutions(t − 3 runs)  from OUR history
sector_mom_12_1       sector     +   3–12   sector  active         EW mean of members' mom_12_1 raw
sector_breadth_200    sector     +   1–3    sector  candidate      share of members with close > SMA200
sector_inst_flow_3m   sector     +   3–6    sector  candidate      EW mean of members' inst_hold_chg_3m
size_log_mcap         control    n/a n/a    stock   control        ln(market_cap); used as a regression control in evaluation, weight 0 in all models
adv_63                control    n/a n/a    stock   control        mean(close × volume, 63d) in INR; drives the liquidity screen and cost bucket, weight 0
```

`*` `inst_hold_chg_3m` needs three runs of our own holdings history; it is `active` in the registry but is excluded by gate G6 until coverage exists (2026-12 run for the live track; the four legacy snapshots provide partial earlier coverage, flagged).

Evidence notes (short; the full text goes into each `factor_registry.evidence` field):
- `mom_12_1`, `mom_6_1`: Jegadeesh & Titman (1993); Asness, Moskowitz & Pedersen (2013) "Value and Momentum Everywhere" includes India-adjacent emerging-market evidence; NSE publishes the Nifty 200 Momentum 30 index (back-calculated to 2005, live since 2020). Caveat: crash risk in sharp reversals (2009, 2020).
- `vol_252`: Ang, Hodrick, Xing & Zhang (2006); NSE Nifty 100 Low Volatility 30 track record. Caveat: interacts with the rate cycle.
- `trend_200`: Moskowitz, Ooi & Pedersen (2012) time-series momentum; the legacy engine's own three periods are consistent with a positive sign (+0.041 mean IC) but are not evidence.
- `roce`, `cash_conversion_3y`, `accruals`: Novy-Marx (2013) profitability; Sloan (1996) accruals; NSE Nifty 500 Quality 50 uses ROE, D/E and EPS variability.
- `earnings_yield`, `book_to_price`: Fama & French (1992); Indian evidence mixed since 2018 (value underperformed 2018–2020, recovered 2021–2024). Kept in the baseline because the baseline should hold the canonical families; learning will down-weight if warranted.
- `leverage`: weak standalone premium in the literature; included as a *risk* control the owner explicitly wants, direction fixed at −.
- `rev_growth_3y`, `asset_growth`: past sales growth has weak predictive evidence; asset growth has robust *negative* evidence (Cooper, Gulen & Schill 2008). Both are candidates, not baseline, despite the owner's intuition that multi-baggers are growth stories. That intuition is a hypothesis; the registry is where it gets tested.
- `inst_hold_chg_3m`: institutional flow persistence (Gompers & Metrick 2001); the input here is Yahoo's `heldPercentInstitutions`, whose update cadence in India is quarterly shareholding patterns with a lag. Active because it is the only flow signal available; watched.

Legacy factor disposition:

```
legacy factor        V2 disposition
quality              → roce + cash_conversion_3y + accruals (continuous)
growth               → rev_growth_3y (candidate); composite growth formula retired
valuation (DCF MoS)  → earnings_yield + book_to_price; DCF kept ONLY as a UI diagnostic, not a factor
risk (strategic)     → retired: hand-typed ticker lists = look-ahead; 85% constant
moat                 → retired: same reason; 96% constant
balance sheet        → leverage (continuous)
cap alloc            → shareholder_yield (candidate)
smart money          → inst_hold_chg_3m (level term dropped; the change is the signal)
trap score           → decomposed into accruals / leverage / (profit decline = candidate later); no multiplier
momentum multiplier  → trend_200 continuous factor; dc_flag stored for diagnostics
headline sentiment   → archived (input unreliable; concall_analyzer not carried forward)
```

## 5.5 Pre-registration

No factor may see a forward return before a `hypotheses` row exists for it:

```sql
CREATE TABLE hypotheses (
  hypothesis_id TEXT PRIMARY KEY,            -- 'H-2026-001'
  kind TEXT NOT NULL CHECK (kind IN ('factor','model','rule','data')),
  title TEXT NOT NULL,
  statement TEXT NOT NULL,                   -- "Stocks with higher X earn higher 3–12m sector-relative returns because ..."
  expected_sign INTEGER CHECK (expected_sign IN (-1,1)),
  horizon_min INTEGER, horizon_max INTEGER,
  primary_metric TEXT NOT NULL,              -- 'oos_ic_h3' | 'net_spread_h12' | ...
  success_criterion TEXT NOT NULL,           -- numeric, e.g. "HAC t ≥ 2.5 on ≥ 24 monthly OOS ICs; positive in ≥ 60% of months"
  failure_criterion TEXT NOT NULL,
  preregistered_on TEXT NOT NULL,
  first_oos_as_of TEXT NOT NULL,             -- first scoring date whose returns may count; must be ≥ preregistered_on
  code_hash TEXT,                            -- of the plugin at pre-registration
  proposer TEXT NOT NULL,                    -- 'human:<name>' | 'llm:<model-id>' | 'system:monthly_report'
  budget_year INTEGER NOT NULL,              -- counts against §9.5 budget
  status TEXT NOT NULL CHECK (status IN ('open','supported','rejected','withdrawn','inconclusive')),
  outcome_summary TEXT, decided_on TEXT, decision_id INTEGER
);
```

The pre-registration document is also written as `knowledge/hypotheses/H-2026-001.md` (generated from the row, then hand-edited if needed; the row is canonical). The eleven launch factors get hypotheses `H-2026-001..011` dated the day of the first V2 run; their `first_oos_as_of = 2026-10-03`. Legacy periods are explicitly excluded from counting toward any launch factor's evidence.

## 5.6 Status lifecycle

```
proposed ──register──► registered ──first compute──► candidate ──promotion criteria──► active
                                                        │                                │
                                                        │ failure criteria                │ probation criteria
                                                        ▼                                ▼
                                                     rejected                        probation ──24m──► retired ──12m──► archived
                                                                                          │
                                                                                          └─recovery criteria─► active
```

```
transition                 criteria (all numeric, evaluated by `python -m quant factors review`)
registered → candidate     first month with coverage ≥ min_coverage; computed and stored monthly; weight 0 in champion
candidate → active         ≥ 24 monthly OOS ICs at H=3 (n_eff ≥ 8) with HAC t ≥ 2.5 in the pre-registered direction,
                           positive in ≥ 60% of months, and net-of-cost decile spread at H=3 > 0; AND within budget (§9.5);
                           Tier-1 decision (§9.7)
candidate → rejected       ≥ 24 monthly OOS ICs with HAC t ≤ 0.5, or 36 months without meeting activation; hypothesis.status='rejected'
active → probation         trailing 36-run OOS mean IC HAC t < 0.5 for 6 consecutive reviews, or < 0 at any review
probation → active         trailing 36-run HAC t ≥ 1.5 for 3 consecutive reviews
probation → retired        24 months in probation without recovery; Tier-1 decision; weight 0 from the next model version
retired → archived         12 months after retirement; no longer computed; exposures kept
```

A retired factor keeps being computed for 12 months so that the retirement decision itself can be audited ("did it start working the month we dropped it?"). Nothing is ever deleted.

---

# 6. Scoring model & weight learning

## 6.1 The model

```
score_m(i,t) = Σ_f  w_{m,f}(t) · z_f(i,t)          over factors f with status ∈ {active} in model version m at t
rank / decile across the universe (screened-in names)
```

There are no multipliers, no hard kills, no trap penalties. Anything that used to be a multiplier is now either a factor (with a learnable, bounded weight) or a *screen* (section 6.7), and screened names are still scored and evaluated.

## 6.2 Baseline = champion at launch

```
model_id 'EW'        equal weight 1/N over active stock factors + sector factors, subject to the 20% sector cap (§6.4)
                     N = 10 at launch (11 once inst_hold_chg_3m has coverage) → w_f ≈ 0.10
```

The red team showed learned weights (+0.031 mean IC) did not beat equal weights (+0.046) on the only data that exists. Equal weight is therefore the null model *and* the champion until evidence says otherwise. `EW` is computed and stored every month forever, whatever the champion is; it is the reference line on every chart.

## 6.3 The learning rule (champion family `SHRINK`)

Bayesian-flavoured shrinkage from equal weights toward IC-proportional weights, with the amount of shrinkage set by the amount of independent evidence.

```
for each active factor f, using ONLY scoring dates t with realised H-run returns and t ≤ train_end − H:
    IC_f(t)   = Spearman( z_f(·,t), r_sec_w(·,t,H) )                   H = 3 for the champion
    mean_f    = mean_t IC_f(t)
    n_months  = count of t
    n_eff     = n_months / H                                            independent periods
    raw_f     = max(mean_f, 0)                                          negative evidence → target weight 0; never a sign flip
    target_f  = raw_f / Σ_g raw_g          (if Σ raw = 0 → target = EW)
    λ         = 0                          if n_eff < 12                 ← minimum-evidence gate
              = n_eff / (n_eff + K)        otherwise, K = 24
    w_f       = (1 − λ)/N + λ · target_f
    then: sector-level factors' total weight capped at 0.20; any single w_f capped at 0.25; renormalise to Σ = 1;
          round to 4 dp; residue added to the largest weight (invariant: Σ = 1.0000 exactly)
```

What this does in numbers:

```
n_eff (independent 3m periods)    months of data    λ      max deviation from EW for a factor with target 0.30 and N=10
< 12                              < 36              0.00   0
12                                36                0.33   +0.067   (0.10 → 0.167)
24                                72                0.50   +0.100
48                                144               0.67   +0.133
```

An everyday analogy for K: it is the number of "phantom" independent periods that vote for equal weight. With K=24, the data needs 24 independent periods (six years at H=3) to have an equal say with the prior. That is deliberately conservative; the owner should not expect the champion's weights to look interesting before 2030. Movement earlier than that comes from challengers.

The rule is idempotent by construction: it is a pure function of `(train_end, factor set, K, H)`; running it twice writes nothing new unless `train_end` advanced. `model_versions.trained_through` records the last scoring date whose returns were used.

## 6.4 Bounds and invariants

```
Σ_f w_f = 1.0000 exactly (4 dp, residue to the largest weight)
0 ≤ w_f ≤ 0.25
Σ_{f sector-level} w_f ≤ 0.20
w_f = 0 for any factor not 'active' in this model version
direction is fixed by the registry; the learning rule cannot flip a sign (it can only shrink toward 0)
```

The legacy [5%, 30%] band is dropped. The floor prevented retirement and the ceiling is where the old optimiser saturated. The health suite's checks move to `python -m quant model check`, which asserts the four invariants above on every stored model version.

## 6.5 Minimum evidence before deviating

Stated once more because it is the rule most likely to be argued with: **the champion's weights are exactly equal until 12 independent periods at the learning horizon exist (36 monthly runs at H=3), and then move only by the shrinkage formula.** No manual weight edits. A human who wants different weights registers a challenger.

## 6.6 Champion / challenger

```sql
CREATE TABLE model_versions (
  model_version_id INTEGER PRIMARY KEY,
  model_id TEXT NOT NULL,                       -- 'EW' | 'SHRINK_H3' | 'C1_SHRINK_H1' | 'C2_PRICE_ONLY' | 'C3_GROWTH_TILT' | 'LEGACY_V16'
  role TEXT NOT NULL CHECK (role IN ('baseline','champion','challenger','legacy','retired')),
  learning_rule TEXT NOT NULL,                  -- 'equal' | 'shrink' | 'legacy_eg'
  rule_params_json TEXT NOT NULL,               -- {"H":3,"K":24,"min_n_eff":12,"cap":0.25,"sector_cap":0.20}
  factor_set_json TEXT NOT NULL,                -- [{"factor_id":"mom_12_1","version":1}, ...]
  weights_json TEXT NOT NULL,
  trained_through TEXT,                         -- last scoring date whose returns were used (NULL for 'equal')
  valid_from TEXT NOT NULL, valid_to TEXT,
  hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id),
  decision_id INTEGER REFERENCES decisions(decision_id),
  created_at TEXT NOT NULL, note TEXT
);
```

Every registered model version is scored every month (it costs milliseconds), gets its own paper portfolio, and its own evaluation rows. Launch set:

```
EW               baseline, permanent
SHRINK_H3        champion at launch (identical to EW until 2029-10 by construction; exists so the rule is exercised and audited)
C1_SHRINK_H1     challenger: same rule, H=1, K=12, min_n_eff=12 (12 months). Tests "does short-horizon learning help long-horizon skill?"
C2_PRICE_ONLY    challenger: factors {mom_12_1, mom_6_1, vol_252, trend_200, max_ret_21, sector_mom_12_1}, weights learned by SHRINK on the
                 2016–2026 backfill (n_eff ≈ 42 → λ ≈ 0.64 on day 1). Tests "does the backfilled price track transfer to live data?"
C3_GROWTH_TILT   challenger: EW over active ∪ {rev_growth_3y, shareholder_yield}. Tests the owner's growth intuition on paper.
```

Cap: at most **3 live challengers** at any time (C1–C3 fill it). A new challenger needs a retired one and a hypothesis within budget.

Promotion (challenger → champion), Tier-2 human decision, criteria evaluated by `python -m quant model review`:

```
P1  ≥ 24 monthly OOS evaluations at H=3 for both champion and challenger over the same dates
P2  paired difference in monthly OOS IC (challenger − champion): HAC t ≥ 2.0, deflated for the number of challengers ever
    registered (§9.5: threshold t ≥ 2.0 + 0.25·ln(m), m = challengers to date)
P3  net-of-cost H=12 spread of challenger ≥ champion over the overlapping window (no significance test; direction only)
P4  challenger turnover ≤ 1.5 × champion turnover
P5  no gate overrides in the challenger's evaluation window
```

Demotion: a champion that loses to `EW` under P1–P2 (with EW as "challenger") reverts to `EW`. This makes the equal-weight baseline a permanent floor on the champion's quality.

## 6.7 What happens to the death-cross hard kill

It is removed as a filter and re-expressed three ways:

```
trend_200            continuous factor, active, weight ≈ 0.10 at launch (learnable, bounded)
dc_flag              stored diagnostic per (as_of, isin): 1 if close < SMA50 < SMA200; reported IC monthly; weight 0
screen_report        each month: mean forward return of dc_flag=1 vs 0, so the old filter's effect is measured, not assumed
```

Hard screens that remain are about *tradability and data*, not prediction, and screened names are still scored and evaluated with `scores.screened_out=1` so the screens' own effect is visible:

```
S1  liquidity     adv_63 < ₹3 crore                → screened_out, reason 'illiquid'
S2  data          no close at as_of or > 10 missing sessions in 63d  → 'no_price'
S3  fundamentals  ≥ 6 of the active factors imputed for this name   → 'thin_data'   (scored anyway; excluded from paper portfolio only)
```

The "Turnaround interceptor" (`Growth ≥ 80 and FCF < 0`) in `update_ui_v16.py` becomes a saved *view* in the UI (`ui/views/turnaround.js`: `rev_growth_3y` top quintile ∩ `cash_conversion_3y` < 0), not a scoring path. Its members' forward returns are reported as a cohort every month, which is how the hyper-capex hypothesis gets tested instead of narrated.

---

# 7. Evaluation protocol

## 7.1 The run grid

```
live grid      as_of = last NSE close on or before the run start; runs scheduled first Saturday of each month 08:00 IST
               cron: 0 8 1-7 * 6  (first Saturday)          run window may slip to any day 1–7; as_of follows the actual pull
               first V2 run: 2026-10-03 (Saturday) → as_of = 2026-10-01 close (Oct 2 is Gandhi Jayanti, market closed) — verify holiday calendar
legacy grid    2026-06-14, 2026-07-11, 2026-08-14, 2026-09-03 (irregular; kept as-is, flagged)
backfill grid  last trading day of each month, 2016-01 … 2026-08; data_track='backfill_price_only'
horizons       counted in runs (H=3 = three runs ahead); actual calendar days stored in forward_returns.days
```

Spacing on the live grid is 28–35 days. HAC statistics treat the series as evenly spaced monthly; the error from this is second-order and is noted in the report footer.

## 7.2 Walk-forward with embargo

```
for train_end k in grid (starting when ≥ 12 runs exist):
    training set   = { (t, realised r(t,H)) : t + H ≤ k }          ← embargo: no return window may end after k
    fit            = learning rule on training set  → weights w(k)
    test point     = score at as_of = k with w(k); realised later at k + H
    store          = learning_curve_points(k, model_id, H, oos_ic(k), ew_oos_ic(k))
```

Because the test point at `k` is only realised at `k+H`, the learning curve is always `H` runs behind the present. That is correct and the chart says so.

`quant/evaluation/walkforward.py::purged_walk_forward(dates, H) -> Iterator[(train_dates, test_date)]` is a pure function with a property test: no training return window may overlap the test date.

## 7.3 Statistics

```
per (as_of, model or factor, H):  IC = Spearman(score, r_sec_w) over screened-in universe (also stored: all-universe IC)
series statistics over T months:
    mean IC, std IC
    HAC t-stat: Newey–West with lag = H − 1  (quant/evaluation/stats.py::hac_tstat)
    block bootstrap 90% CI: 1,000 resamples, block length = H, circular
    hit rate: share of months with IC > 0
    naive t (independence) is ALSO stored, labelled 'naive', so the gap is visible
cross-sectional dependence: IC standard error is reported per month as se_ic = sqrt((1 + (n−1)·ρ̄) / n) with ρ̄ = mean pairwise
    return correlation in the month (typically 0.2–0.4 in India); this widens the per-month bands honestly
regression variant: Fama–MacBeth slope of r_sec_w on z with size_log_mcap control, monthly; reported alongside IC
```

## 7.4 Benchmarks

```
B0  EW universe      equal-weight, screened-in names, rebalanced each run with the §8 cost model     PRIMARY comparator
B1  CW universe      market-cap-weighted, same names (cap from fundamentals_pit 'market_cap' at as_of)  proxy for Nifty 500 TR
B2  ^CRSLDX          Nifty 500 price index from Yahoo, plus 1.2%/yr dividend adjustment                 external sanity check only
B3  MOM30 proxy      top 30 of the largest 200 names by (0.5·mom_6_1/vol + 0.5·mom_12_1/vol), semi-annual rebalance   constructed
B4  QUAL50 proxy     top 50 by mean rank of (roce, −leverage, −EPS variability 5y), semi-annual rebalance                constructed
```

B3/B4 are labelled "proxy" everywhere; NSE's actual methodologies differ in detail and the real indices are not available as free daily total-return series. If an ETF with adequate history is confirmed (candidates to verify: `MOMOMENTUM.NS`, `MOM30IETF.NS`, `QUAL30IETF.NS`), it is added as B3x/B4x, never substituted silently.

## 7.5 Leakage tests (run every month, gate G8)

```
T1 shuffle         permute isin labels of the score within each as_of → |mean IC| must be < 2·SE over the last 24 months
T2 planted signal  z_plant = 0.05·standardised future r_sec + noise; run through the full pipeline → recovered IC within ±0.02 of 0.05
T3 time shift      re-score month k using fundamentals with as_of ≤ k + 90 days (deliberate leak) → IC must RISE by > 0.01
                   (if it does not, the pipeline is not actually using fundamentals, which is a different bug)
T4 future file     re-score month k−1 from stored inputs → hash equals scores.input_hash (gate G7)
T5 sector leak     compute IC of sector_group dummies alone → must be ≈ 0 on r_sec (sanity for neutralisation)
```

## 7.6 Cost-adjusted spreads

```
decile portfolios D1..D10 by score each as_of, equal weight within decile, screened-in names only
gross spread(H)  = mean r_tot(D10) − mean r_tot(D1)
net spread(H)    = gross − (turnover_D10 + turnover_D1) × cost_bps(bucket)   with turnover measured against the prior as_of's decile membership
long-only net    = D10 net return − B0 net return       ← the number the alpha scoreboard leads with
```

## 7.7 Learning-curve measurement

The chart the owner will look at is defined exactly, because it is the deliverable of the whole exercise:

```
x-axis   k = number of clean monthly runs of the live fundamental track since 2026-10 (0 = first run)
         (a second panel uses the backfill grid, x = months since 2016-01, drawn in a muted colour)
y-axis   OOS IC at H=3 of the model fitted with data through k, evaluated at k, realised at k+3, smoothed by a trailing 12-point mean
lines    champion (SHRINK_H3), EW baseline, each challenger; shaded 90% block-bootstrap band on the champion
marks    vertical lines at every decisions row of kind ∈ {promotion, retirement, factor_activation}
inset    (champion − EW) trailing-12 mean with HAC t; this inset IS the learning claim
rule     a point is plotted only when its return is realised; hollow markers for legacy and backfill tracks
```

Two curves that must not be confused, and are stored in different tables so they cannot be:
- **Evidence curve** (`evaluations`): trailing mean IC of a fixed factor vs months. Rises as noise shrinks. Says "we know more about this factor".
- **Learning curve** (`learning_curve_points`): OOS IC of the re-fitted model vs k, against EW at the same k. Rises only if learning helps. Says "the system got better".

`python -m quant learning-curve --model SHRINK_H3 --h 3 --out ui/learning_curve.js` writes both.

---

# 8. Portfolio & cost model

## 8.1 Paper portfolio

```
PF_TOP10   long-only, names ranked in the top decile at as_of, equal weight, screened-in only
           hold band: enter at rank ≤ 10%, hold while rank ≤ 20%, exit above 20%   (cuts turnover roughly in half)
           sector cap: no sector_group > 25% of weight (rarely binds because scores are sector-neutral)
           cash: uninvested residue from the sector cap sits in cash at 0%
PF_TOP10_Q same, rebalanced every 3rd run (quarterly variant; primary if F3 in §1.4 triggers)
PF_LS      D10 − D1 spread portfolio, analytics only (not investable for a retail owner in India without SLB)
one PF_* set per model version; B0..B4 use the same engine
```

```sql
CREATE TABLE paper_positions (
  as_of TEXT NOT NULL, model_version_id INTEGER NOT NULL, portfolio TEXT NOT NULL,
  isin TEXT NOT NULL, weight REAL NOT NULL, entered_on TEXT NOT NULL, rank_pct REAL, cost_bucket TEXT,
  PRIMARY KEY (as_of, model_version_id, portfolio, isin)
);
CREATE TABLE paper_performance (
  as_of TEXT NOT NULL, model_version_id INTEGER NOT NULL, portfolio TEXT NOT NULL,
  gross_return REAL, turnover_one_way REAL, cost REAL, net_return REAL,
  b0_net_return REAL, excess_vs_b0 REAL, n_positions INTEGER, days INTEGER,
  PRIMARY KEY (as_of, model_version_id, portfolio)
);
```

## 8.2 Liquidity screen and cost buckets

Indian delivery-trade fixed costs (STT 0.1% each side since the 2024 budget, stamp duty 0.015% on buys, exchange and SEBI charges, GST on those; zero brokerage assumed) come to roughly 12 bps one-way. Impact and half-spread are assumptions by liquidity bucket:

```
bucket   adv_63 (INR)              fixed   impact+spread   one-way bps   round-trip bps
A        ≥ 50 crore                12      10              22            44
B        10 – 50 crore             12      25              37            74
C        3 – 10 crore              12      55              67            134
D        < 3 crore                 —       —               screened out (S1)
stress   all buckets × 1.5, reported alongside as 'net_return_stress'
```

These numbers are the biggest unmeasured assumption in the portfolio layer (section 12). They are stored in `config/costs.yaml` with a version, and every `paper_performance` row records `cost_model_version`.

## 8.3 Expected turnover and cost drag (estimate, to be measured)

```
top-decile membership churn without hold band, monthly     ~35–45%   (typical for a composite with momentum)
with 10/20 hold band                                       ~15–25%
cost at 20% one-way turnover, bucket-B average             0.20 × 2 × 37 bps ≈ 15 bps / month ≈ 1.8% / year
```

If measured turnover exceeds 30% one-way for three consecutive months, the report raises a `rule` hypothesis to test the quarterly variant as champion portfolio.

## 8.4 Alpha scoreboard definition

```
ALPHA (headline)  = annualised net return of PF_TOP10(champion) − annualised net return of B0, both from paper_performance,
                    over the trailing 12 and 36 runs, with a block-bootstrap 90% CI (block 3)
secondary         = the same vs B1 (cap-weighted), B3, B4
beta-adjusted     = intercept of monthly excess-vs-B1 regressed on B1 excess (CAPM-style), reported but not headline
honesty row       = EW model's PF_TOP10 on the same basis, always shown next to the champion
```

"Alpha" on the dashboard means the headline row and nothing else. It is a paper number after assumed costs, not a live P&L.

---

# 9. Feedback loop & knowledge base

## 9.1 The monthly loop

```
   ┌──────────────────────────────────────────────────────────────────────────────────────────┐
   │  first Saturday, 08:00 IST        python -m quant run monthly                             │
   └──────────────────────────────────────────────────────────────────────────────────────────┘
        │
   (1)  │ ingest      universe + sector CSV → prices update → fundamentals pull (≈30 min) → raw payloads to data/raw
        ▼
   (2)  │ gates       G1–G8  ──fail──► runs.status='blocked' → report → STOP (needs Tier-2 override to continue)
        ▼ pass
   (3)  │ expose      compute every factor with status ≥ candidate (all versions in use) → factor_exposures
        ▼
   (4)  │ score       every model_version with valid_to IS NULL → scores (+ screens, dc_flag)
        ▼
   (5)  │ realise     forward_returns for as_of ≤ today − H, all H → evaluations (IC, spreads, FM slopes), learning_curve_points
        ▼
   (6)  │ paper       rebalance PF_* for every model + B0..B4 → paper_performance
        ▼
   (7)  │ learn       champion rule: recompute weights with trained_through advanced (no-op until §6.5 gate) → new model_version if changed
        ▼
   (8)  │ review      factors review + model review → list of criteria met (candidate→active, probation, promotion, budget status)
        ▼
   (9)  │ propose     system drafts decisions (status='proposed') + hypotheses it recommends (e.g. "turnover > 30% → test quarterly")
        ▼
  (10)  │ record      knowledge/reports/YYYY-MM.md (auto), ui/*.js, git commit "Monthly run YYYY-MM (as_of …)"
        ▼
  (11)  │ approve     human or LLM works the proposed queue: python -m quant kb approve <id> / reject <id>  (Tier rules §9.7)
        ▼
  (12)  │ apply       approved decisions take effect at the NEXT run (never mid-run); ADR file written; git commit
```

Steps 1–10 are unattended and take about 35 minutes on a laptop (the fundamentals pull dominates; the 0.5 s throttle is kept). Steps 11–12 are the only human-touch points and can be done any time before the next run; unreviewed proposals simply carry over and are listed as "pending N months".

## 9.2 Experiment registry DDL

In addition to `hypotheses` (section 5.5), `factor_registry`, `model_versions`, `evaluations`:

```sql
CREATE TABLE runs (
  run_id INTEGER PRIMARY KEY, as_of TEXT NOT NULL UNIQUE, data_track TEXT NOT NULL DEFAULT 'live',
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','blocked','failed')),
  git_sha TEXT NOT NULL, code_hash TEXT NOT NULL,      -- code_hash = sha256 over quant/** source
  gates_json TEXT, override_decision_id INTEGER, n_universe INTEGER, n_scored INTEGER, note TEXT
);
CREATE TABLE evaluations (
  eval_id INTEGER PRIMARY KEY, computed_at_run INTEGER NOT NULL REFERENCES runs(run_id),
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('factor','model','portfolio','screen','benchmark')),
  subject_id TEXT NOT NULL,                 -- factor_id | model_version_id | 'PF_TOP10:<mvid>' | 'dc_flag' | 'B0'
  as_of TEXT NOT NULL, horizon INTEGER NOT NULL, data_track TEXT NOT NULL,
  metric TEXT NOT NULL,                     -- 'ic' | 'ic_all' | 'fm_slope' | 'gross_spread' | 'net_spread' | 'hit' | 'turnover' | ...
  value REAL, n INTEGER, se REAL, method TEXT,   -- method: 'spearman' | 'hac_l2' | 'bootstrap_b3' | 'naive'
  window_start TEXT, window_end TEXT,       -- for aggregated (rolling) rows; NULL for per-month rows
  UNIQUE (subject_kind, subject_id, as_of, horizon, data_track, metric, method, window_start, window_end)
);
CREATE TABLE learning_curve_points (
  model_id TEXT NOT NULL, horizon INTEGER NOT NULL, data_track TEXT NOT NULL,
  k INTEGER NOT NULL,                       -- clean runs of training data used
  train_end TEXT NOT NULL, test_as_of TEXT NOT NULL, realised_as_of TEXT NOT NULL,
  weights_json TEXT NOT NULL, oos_ic REAL NOT NULL, ew_oos_ic REAL NOT NULL, n INTEGER NOT NULL,
  computed_at_run INTEGER NOT NULL,
  PRIMARY KEY (model_id, horizon, data_track, k)
);
CREATE TABLE experiments (
  experiment_id TEXT PRIMARY KEY,           -- 'X-2027-004'
  hypothesis_id TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
  design_json TEXT NOT NULL,                -- {"subject":"factor:rev_growth_3y","H":3,"train":"...","test":"...","metric":"oos_ic"}
  status TEXT NOT NULL CHECK (status IN ('planned','running','done','aborted')),
  result_json TEXT, started_on TEXT, finished_on TEXT, run_id INTEGER, note TEXT
);
CREATE TABLE decisions (
  decision_id INTEGER PRIMARY KEY, made_on TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('factor_status','model_promotion','model_retirement','rule_change','gate_override',
                                     'cost_model','taxonomy','data_fix','hypothesis_registration','other')),
  tier INTEGER NOT NULL CHECK (tier IN (0,1,2)),
  title TEXT NOT NULL, context TEXT NOT NULL, options_json TEXT NOT NULL, decision TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL,         -- eval_ids, experiment_ids, hypothesis_ids the decision rests on
  criteria_check_json TEXT,                 -- machine output of the numeric criteria at decision time
  status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','provisional','superseded','reverted')),
  proposer TEXT NOT NULL, approver TEXT, approver_kind TEXT CHECK (approver_kind IN ('human','llm','system')),
  ratified_by TEXT, ratified_on TEXT,       -- human co-signature for LLM-approved Tier-1 items
  effective_from TEXT,                      -- always a future as_of
  adr_path TEXT, supersedes INTEGER REFERENCES decisions(decision_id)
);
CREATE TABLE lessons (
  lesson_id INTEGER PRIMARY KEY, recorded_on TEXT NOT NULL, text TEXT NOT NULL,
  evidence_refs_json TEXT, decision_id INTEGER, tags TEXT
);
```

## 9.3 Human-readable records

```
knowledge/
  decisions/ADR-2026-10-03-001-launch-factor-set.md      one file per approved decision (template below)
  hypotheses/H-2026-001.md …                              generated from the row, hand-editable narrative
  reports/2026-10.md …                                    auto-generated monthly report (§9.4)
  lessons.md                                              append-only ledger, one bullet per lessons row
  README.md                                               how to read this folder; links to the registry queries
```

ADR template (fields mirror the `decisions` row; the row is canonical, the file is for humans and for git blame):

```
Title: ADR-2026-10-03-001 — Launch factor set (11 factors, equal weight)
Status: approved | Tier: 2 | Proposer: human:<owner> | Approver: human:<owner>
Effective from: 2026-10-03 | Supersedes: —
## Context
## Options considered
## Decision
## Evidence (eval_ids / experiment_ids / hypothesis_ids)
## Criteria check (verbatim output of `quant factors review` / `quant model review`)
## Consequences and what would reverse this
```

## 9.4 Monthly report contents (auto-generated)

```
1. Run summary: as_of, status, gates table, n_universe, n_scored, overrides
2. Data quality: flags by code, imputed share per factor, UNEXPLAINED_JUMP list for confirmation
3. Scoreboard: M1–M8 with CIs; champion vs EW vs challengers; alpha scoreboard rows
4. Learning curve: the chart data + slope/t of (champion − EW)
5. Factor evidence table: per factor OOS IC (H=1,3,12) mean, HAC t, hit rate, months, status, months-to-review
6. Screens and cohorts: dc_flag effect, turnaround view cohort return, liquidity-screened names' return
7. Review output: criteria met/unmet for every candidate/active factor and every challenger
8. Proposed decisions and hypotheses (the approval queue), with budget status (used / remaining this year)
9. Footer: code_hash, git_sha, cost_model_version, taxonomy version, statistics caveats
```

## 9.5 Multiple-testing control

Every hypothesis tested against out-of-sample data consumes budget, whether it succeeds or not:

```
budget per calendar year        8 hypotheses total:  ≤ 5 kind='factor', ≤ 3 kind∈{'model','rule'}
                                (data-fix hypotheses are unlimited; they do not touch returns)
live challengers                ≤ 3 at any time
activation threshold            HAC t ≥ 2.5 (≈ p 0.006 one-sided) — a deliberate haircut over the textbook 2.0,
                                following the "t > 3 for new factors" argument of Harvey, Liu & Zhu (2016), softened
                                because our tests are pre-registered and directional
promotion threshold             t ≥ 2.0 + 0.25·ln(m), m = challengers registered to date (m=3 → 2.27; m=10 → 2.58)
yearly review                   Benjamini–Hochberg at FDR 10% across the year's cohort of hypothesis p-values;
                                anything activated that fails BH goes to probation
dashboard                       "hypotheses tested YTD: n / 8" is always visible (M6)
```

Expected false activations over five years at 8 hypotheses/year: `40 × 0.006 ≈ 0.24`. The budget is what keeps that arithmetic true; without it the threshold is decorative.

## 9.6 How a new parameter is added safely

Worked example: the monthly report notes that top-decile names with rising promoter pledges underperform, and someone wants a `promoter_pledge_chg` factor.

```
step  command / action                                                     effect on history
1     python -m quant kb propose --kind hypothesis --template factor        creates hypotheses row status='open', budget check
      (fill: statement, sign −, H 3–12, metric oos_ic_h3, success/failure criteria)
2     write quant/factors/flows/promoter_pledge_chg.py with FactorMeta       nothing computed yet
      (preregistration_id = the new H-id)
3     python -m quant factors register promoter_pledge_chg                  factor_registry status='registered', code_hash pinned,
                                                                            first_oos_as_of = next run date
4     next monthly run                                                      exposures computed & stored; weight 0 everywhere;
                                                                            status → 'candidate' once coverage ≥ min_coverage
5     months pass; evaluations accumulate under data_track='live'           champion untouched; challenger may include it only via
                                                                            a separate model hypothesis (budget)
6     python -m quant factors review  (every run)                           prints criteria; when met, drafts a Tier-1 decision
7     approve → status 'active' effective next run                          new model_version for every model that includes 'active'
                                                                            factors by rule (EW, SHRINK_H3); old versions get valid_to
8     historical scores                                                     unchanged forever; the new factor appears in scores from
                                                                            effective_from onward; learning_curve re-fits include it
                                                                            only for k ≥ its first_oos_as_of
```

"Without contaminating the historical record" is guaranteed by three mechanical facts: exposures/scores are keyed by version and never updated; `first_oos_as_of` is enforced in `evaluations` (rows before it are stored with `data_track='pre_registration'` and excluded from every review query); and the pipeline code hash is part of every run row.

## 9.7 Approval protocol

```
Tier 0  automatic, logged as decisions(kind, tier=0, approver_kind='system')
        ingest, gates, exposure & score computation for registered models, evaluations, paper portfolios, reports, UI export,
        the champion's scheduled shrinkage refresh (§6.3 — a pure function of stored data, not a judgment)
Tier 1  proposed by system → may be approved by an LLM (approver_kind='llm', status='provisional') → human ratifies within 60 days
        or the decision auto-reverts at the next run
        registering a hypothesis within budget; candidate→active / active→probation / probation→retired per §5.6 criteria;
        creating a challenger for an approved model hypothesis; cost-model recalibration within ±25% of current values
Tier 2  human only (approver_kind='human'); LLM may draft the ADR but cannot approve
        promotion or demotion of the champion; any change to a pre-registered rule or threshold (K, H, gates, budget, cost buckets
        beyond ±25%, taxonomy merge table); overriding a blocked run; activating a factor that fails any criterion;
        anything that edits or deletes stored exposures, scores, returns or evaluations (which the CLI does not expose at all)
```

An LLM approver must attach `criteria_check_json` produced by the CLI and cite `evidence_refs`; `python -m quant kb approve` refuses a Tier-1 approval whose criteria check contains any `false`. The next run refuses to instantiate a champion whose creating decision is `provisional` past its ratification deadline. These are code paths, not conventions.

---

# 10. Architecture

## 10.1 Package layout

```
quant/
  __init__.py  __main__.py  cli.py  config.py  errors.py
  data/       yahoo.py (throttled client; batch download; info/statement pulls)   prices.py (parquet store, TRI)
              fundamentals.py (normalise units → fundamentals_pit)  universe.py (CSV fetch, membership)
              quality.py (flags, gates G1–G8)  store.py (SQLite access, LookaheadError-enforcing views)
              adapters/ (optional: nsdl_flows.py, nse_taxonomy.py, shareholding.py — interfaces + stubs)
  sectors/    taxonomy.py (nse_sector, fallback chain, yahoo_to_nse map)  groups.py (merge rule)  features.py (sector-level inputs)
  factors/    base.py  inputs.py  registry.py  standardize.py
              momentum/  risk/  trend/  quality/  value/  growth/  flows/  leverage/  payout/  sector/  controls/
  model/      composite.py (score = Σ w·z)  learning.py (shrink rule, pure)  champion.py (versions, invariants, review)
  evaluation/ returns.py (TRI, forward_returns)  ic.py  stats.py (HAC, bootstrap, se_ic)  walkforward.py
              leakage.py (T1–T5)  learning_curve.py  benchmarks.py (B0–B4)  fm.py (Fama–MacBeth)
  portfolio/  paper.py  costs.py  screens.py
  knowledge/  registry.py (hypotheses/experiments/decisions CRUD)  review.py (criteria engine)  budget.py
              reports.py (monthly markdown)  adr.py
  migrate/    legacy.py  schema.py (DDL + migrations, versioned)
  ui_export.py
tests/        unit (pure functions, fixtures with real observed Yahoo values), integration (tmp DB, synthetic prices),
              property (walkforward embargo, weight invariants), leakage (T1–T5 on synthetic data)
config/       engine.yaml (H, K, gates, screens, budget)  costs.yaml  taxonomy_merge.yaml
data/         prices/  raw/fundamentals/  MANIFEST.json
knowledge/    decisions/  hypotheses/  reports/  lessons.md  README.md
ui/           index.html  app.js  style.css  data.js  scoreboard.js  learning_curve.js  kb.js  vendor/chart.umd.js  views/turnaround.js
legacy/       harness_v16_learning.py, weight_optimizer.py, quant_math.py, eval_portfolio_health.py, update_ui_v16.py, tests
              (moved, import paths fixed, kept runnable for audit; not part of the monthly loop)
```

## 10.2 Database schema (SQLite, `quant_engine.db`) — remaining tables

Tables already defined above: `sector_map`, `universe_membership`, `fundamentals_pit`, `corporate_actions`, `data_quality_events`, `factor_registry`, `factor_versions`, `factor_exposures`, `hypotheses`, `model_versions`, `paper_positions`, `paper_performance`, `runs`, `evaluations`, `learning_curve_points`, `experiments`, `decisions`, `lessons`. Remaining:

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT);

CREATE TABLE prices_manifest (
  file TEXT PRIMARY KEY, sha256 TEXT NOT NULL, rows INTEGER NOT NULL, min_date TEXT, max_date TEXT, pulled_at TEXT NOT NULL
);

CREATE TABLE scores (
  as_of TEXT NOT NULL, isin TEXT NOT NULL, ticker TEXT NOT NULL,
  model_version_id INTEGER NOT NULL REFERENCES model_versions(model_version_id),
  score REAL NOT NULL, rank INTEGER NOT NULL, rank_pct REAL NOT NULL, decile INTEGER NOT NULL,
  sector_group TEXT NOT NULL,
  screened_out INTEGER NOT NULL DEFAULT 0, screen_reasons TEXT,        -- 'illiquid|thin_data'
  dc_flag INTEGER, excluded_factors TEXT,                              -- factors dropped by G6 this month
  factor_versions_json TEXT NOT NULL, input_hash TEXT NOT NULL,        -- sha256 of the exposure vector used (gate G7)
  data_track TEXT NOT NULL DEFAULT 'live', run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, isin, model_version_id, data_track)
);

CREATE TABLE forward_returns (
  as_of TEXT NOT NULL, isin TEXT NOT NULL, horizon INTEGER NOT NULL,   -- horizon in runs
  end_as_of TEXT NOT NULL, days INTEGER NOT NULL,
  r_tot REAL, r_sec REAL, r_mkt REAL, r_sec_w REAL,
  partial_horizon INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0, exclude_reason TEXT,
  multibagger_2x INTEGER,                                              -- populated for horizon = 36 only
  data_track TEXT NOT NULL DEFAULT 'live', price_manifest_sha TEXT NOT NULL, computed_at_run INTEGER NOT NULL,
  PRIMARY KEY (as_of, isin, horizon, data_track)
);

CREATE TABLE sector_groups (
  version INTEGER NOT NULL, nse_sector TEXT NOT NULL, sector_group TEXT NOT NULL,
  valid_from TEXT NOT NULL, decision_id INTEGER, PRIMARY KEY (version, nse_sector)
);

CREATE TABLE holdings_history (                                        -- our own PIT record of institutional holding
  as_of TEXT NOT NULL, isin TEXT NOT NULL, held_pct_institutions REAL, source TEXT NOT NULL, PRIMARY KEY (as_of, isin)
);

-- legacy, migrated verbatim, read-only
CREATE TABLE legacy_daily_predictions AS SELECT * FROM daily_predictions;      -- executed by migrate, then daily_predictions dropped
CREATE TABLE legacy_active_weights   AS SELECT * FROM active_weights;
CREATE TABLE legacy_performance_tracking AS SELECT * FROM performance_tracking;

CREATE INDEX ix_exposures_asof_factor ON factor_exposures(as_of, factor_id);
CREATE INDEX ix_scores_asof_model ON scores(as_of, model_version_id);
CREATE INDEX ix_fwd_asof_h ON forward_returns(as_of, horizon);
CREATE INDEX ix_eval_subject ON evaluations(subject_kind, subject_id, horizon, metric);
CREATE INDEX ix_fund_isin_field_asof ON fundamentals_pit(isin, field, as_of);
```

Size estimate: `factor_exposures` ≈ 500 × 20 factors × 12 = 120 k rows/year; `scores` ≈ 500 × 5 models × 12 = 30 k rows/year; `forward_returns` ≈ 500 × 6 horizons × 12 = 36 k rows/year. The backfill adds ≈ 128 months × 500 × 7 price factors ≈ 450 k exposure rows once. SQLite handles this comfortably (< 100 MB after five years).

## 10.3 CLI

```
python -m quant init                                  create/upgrade schema (schema_version), write config defaults
python -m quant migrate legacy [--dry-run]            §10.5
python -m quant data backfill-prices --start 2016-01-01 [--tickers-from current|legacy|both]
python -m quant data update-prices
python -m quant data ingest-fundamentals [--as-of DATE] [--limit N]      throttled pull, writes raw + fundamentals_pit
python -m quant data check                            gates report without scoring; repo size report
python -m quant universe refresh                      CSV → universe_membership + sector_map (PIT rows)
python -m quant sectors show [--as-of DATE]           group sizes, merges in force, coverage
python -m quant factors register <name> | list | compute --as-of DATE [--track live|backfill] | review
python -m quant score --as-of DATE [--model ID]       all live model versions by default
python -m quant evaluate [--through DATE]             realise returns, write evaluations + learning_curve_points
python -m quant learning-curve --model SHRINK_H3 --h 3 [--out ui/learning_curve.js]
python -m quant portfolio rebalance --as-of DATE      all models + benchmarks
python -m quant model review | check | list
python -m quant kb propose --kind hypothesis|decision ... | approve <id> --as human:<name>|llm:<model> | reject <id> | queue | report --month YYYY-MM
python -m quant test leakage [--as-of DATE]           T1–T5
python -m quant ui export
python -m quant run monthly [--as-of DATE] [--override-gate Gx --decision-id N] [--no-ingest]   steps 1–10 of §9.1
python -m quant run backfill-track                    one-off: exposures/scores/evaluations for the 2016–2026 price-only grid
```

Expected output of a healthy `run monthly` (abridged):

```
[quant] run 2026-10-03  as_of=2026-10-01  track=live  git=ab12cd3  code=9f3e…
[ingest]   universe 500 (csv sha 7c1a…)  sector_map: 0 changes  prices: +23 sessions × 500  fundamentals: 500 ok, 0 failed (31m 12s)
[gates]    G1 500/500 ok | G2 stale 0.4% ok | G3 jumps 2 (ZFCVINDIA? no: BAJAJHFL rights) ok | G4 0 violations | G5 100% |
           G6 inst_hold_chg_3m coverage 0% → excluded (expected until 2026-12) | G7 hash ok | G8 T1 |IC|=0.004 T2 0.049 ok
[expose]   19 factors × 500 = 9,500 rows (imputed: leverage 20.2% [financials], cash_conversion_3y 11.6%, roce 4.0%)
[score]    EW#3 SHRINK_H3#3 C1#2 C2#1 C3#1  → 2,500 rows; screened_out 27 (illiquid 19, thin_data 8)
[realise]  H=1 for 2026-09-03 (legacy, flagged): n=497, EW IC +0.021 | no H≥3 realised yet on live track
[paper]    PF_TOP10 EW: 48 names, turnover n/a (first rebalance) ; B0 500 names
[learn]    SHRINK_H3: n_eff=0.0 < 12 → weights = EW (no new version)
[review]   0 factors meet activation; 0 in probation; challengers: none reviewable (< 24 points); budget 2026: 11/8 used?
           → NOTE launch factors are exempt (registered under ADR-…-001); 0/8 used
[propose]  0 decisions, 1 hypothesis draft: 'rights-issue detection for BAJAJHFL' (kind=data)
[record]   knowledge/reports/2026-10.md  ui/data.js ui/scoreboard.js ui/learning_curve.js ui/kb.js  committed 5e9d…
```

## 10.4 Configuration (`config/engine.yaml`, all values referenced above)

```yaml
grid: {schedule: "first_saturday", tz: "Asia/Kolkata"}
horizons: {learning: 3, evaluation: 12, tracked: [1, 3, 6, 12, 24, 36]}
learning: {rule: shrink, K: 24, min_n_eff: 12, cap: 0.25, sector_cap: 0.20}
challengers: {max_live: 3}
gates: {G1_min_priced: 480, G2_stale_warn: 0.02, G2_stale_block: 0.05, G3_max_jumps: 10, G4_max_share: 0.05, G5_min_cov: 0.98,
        G6_price_cov: 0.95, G6_other_cov: 0.70, G6_block_factors: 3}
screens: {adv_min_inr: 30000000, max_missing_sessions_63d: 10, thin_data_imputed: 6}
standardise: {winsor: [0.01, 0.99], clip_z: 3.0, min_group_size: 8}
budget: {per_year_total: 8, factor: 5, model_or_rule: 3, activation_t: 2.5, promotion_t_base: 2.0, promotion_t_log_coef: 0.25, fdr: 0.10}
returns: {jump_threshold: 0.40, winsor: [0.01, 0.99]}
yahoo: {request_sleep_s: 0.5, batch_size: 25, batch_sleep_s: 1.0}
```

Changing any value under `learning`, `gates`, `budget`, `returns` or `standardise` requires a Tier-2 decision id in the commit message; `quant init` diffs the file against the last applied config and refuses to run without one.

## 10.5 Migration of the existing `quant_engine.db`

`python -m quant migrate legacy` (idempotent; writes a `decisions` row kind='data_fix' tier=0 and an ADR):

```
1. Copy daily_predictions / active_weights / performance_tracking → legacy_* tables verbatim; drop originals only after row-count check.
2. runs: one row per legacy snapshot date, data_track='legacy', status='ok', note with defects:
     2026-06-04  47 rows   is_full=0 → excluded from every grid (note 'partial_nifty50_run')
     2026-06-12  499 rows  duplicate of 06-14 two days earlier → excluded from grid (note 'duplicate_pre_launch')
     2026-06-14, 2026-07-11, 2026-08-14, 2026-09-03 → legacy grid points 1–4
   flags on all four: 'no_data_flags','price_unadjusted_quote','div_yield_units_bug','roe_none_as_zero_bug','sentiment_in_base',
                      'moat_risk_lookahead_lists','bucketed_scores','no_industry_field'
3. universe_membership: tickers of each full snapshot, source='legacy_snapshot'. ISIN filled from the current CSV by symbol; unmatched
   symbols (renamed/delisted) get isin='LEGACY:<symbol>' and a data_quality_event.
4. sector_map: for every legacy ticker, nse_sector from the current CSV with source='legacy_migration', valid_from='2026-06-14'.
5. fundamentals_pit from raw_json (pit_quality='legacy', as_of = snapshot date), only fields that are recoverable and unit-safe:
     market_cap (from Market_Cap_Cr where present — absent in Sep-03 payloads; else NULL), trailing_pe, book_value? (absent → NULL),
     held_pct_institutions (Inst_Holdings_%/100), total_debt/equity ratio (Debt_to_Equity as 'ratio'), roce_pct, sma50, sma200,
     ocf_array / fcf_array (4 annual values, INR_cr; the period_end for each slot is unknown → freq='A', period_end = 'FY-k'
     relative labels; usable for cash_conversion_3y only, flagged)
   NOT migrated as values: Div_Yield_% (corrupt ×100 in all four snapshots; recomputable only if dividend_rate existed, it does not),
     FCF_Yield_% (unit bug), Composite_Growth_% (absent in payloads), sentiment.
6. holdings_history from Inst_Holdings_% for the four dates (gives inst_hold_chg_3m partial coverage from 2026-09).
7. factor_exposures for data_track='legacy': computed by the V2 standardiser from the recoverable raw inputs above
   (trend_200 from Price/SMA_200, leverage, roce, cash_conversion_3y, earnings_yield from 1/PE, inst_hold_chg_3m where possible);
   price factors (mom_12_1, vol_252) computed from the backfilled prices_daily at those dates (these ARE clean).
8. legacy scores: model_version 'LEGACY_V16' role='legacy' with weights_json from the active_weights row in force at each date;
   scores.score = legacy final_score, plus a second version 'LEGACY_V16_BASE' = base_score (or reconstructed composite where NULL).
9. forward_returns for the legacy grid recomputed from prices_daily (adjusted, dividends included) — NOT from legacy price quotes.
   The ZFCVINDIA −84% disappears; the report prints old-vs-new return for the 20 largest discrepancies.
10. legacy active_weights → model_versions rows (role='legacy'), one per row, with note copied; trained_through kept.
11. evaluations for legacy grid at H=1 (the only realisable horizon), data_track='legacy', marked hollow on charts.
Acceptance: python -m quant evaluate --track legacy reproduces the red-team's attribution table within ±0.01 IC for the
'final_score as stored' and 'fundamental composite' rows when price_unadjusted quotes are used (a --legacy-prices switch exists
for exactly this reconciliation), and shows the adjusted-return version alongside.
```

## 10.6 UI changes (vanilla, no build step)

```
ui/index.html      add tabs: Scoreboard | Factors | Knowledge | Turnaround (view) alongside Accepted/Rejected (renamed 'Top decile' / 'Rest')
ui/scoreboard.js   M1–M8 tiles with CI text; champion/EW/challenger table; alpha scoreboard rows; gate history strip
ui/learning_curve.js  data for the §7.7 chart (two panels + inset), rendered with Chart.js
ui/kb.js           decisions list (status, tier, approver), hypothesis budget bar, pending approval queue, factor registry table
ui/vendor/chart.umd.js  vendored copy of Chart.js (pinned version) so the page works offline; the README's "zero-dependency" claim becomes true
ui/data.js         per-stock detail as today, plus: sector_group, rank_pct per model, z per factor (with imputed markers), screen reasons,
                   dc_flag, cost bucket; the DCF/MoS text stays as a diagnostic paragraph labelled "not used in ranking"
```

Sidebar header shows: as_of, champion model id and version, "weights: equal (n_eff 0/12)" until the gate opens, hypotheses YTD.

---

# 11. Phased roadmap

## 11.1 Month 1 — ships by the 2026-10-03 run

```
package skeleton, schema v1, config, `quant init`
migrate legacy (§10.5) with reconciliation report
price backfill 2016→, TRI, corporate_actions, UNEXPLAINED_JUMP events
universe + sector_map PIT, merge rule, sectors show
factors: mom_12_1, mom_6_1, vol_252, trend_200, max_ret_21, roce, cash_conversion_3y, accruals, leverage, earnings_yield,
         book_to_price, inst_hold_chg_3m, sector_mom_12_1, size_log_mcap, adv_63 (registered; hypotheses H-2026-001..011 for the active set)
models: EW, SHRINK_H3 (gated → identical), C2_PRICE_ONLY (weights from backfill)
gates G1–G7 (G8 in month 2), screens S1–S3
evaluate at H=1/3/12 on backfill + legacy tracks; first learning-curve chart (backfill panel populated, live panel empty)
monthly report v1, ADR-001 (launch set), UI Scoreboard tab with the chart
falsifier F4 checked and written into the report
```

Acceptance for month 1: `pytest -q` green (target ≥ 120 tests); `python -m quant run monthly --as-of 2026-10-01` completes in < 45 min with status `ok`; `run backfill-track` produces ≥ 120 H=3 OOS points for C2 and EW with the chart rendering; the legacy reconciliation matches the red-team numbers.

## 11.2 Month 3 — by the 2026-12 run

```
G8 leakage tests T1–T5 in the gate set
paper portfolios PF_TOP10 / _Q / LS for all models; B0–B4; cost model v1; alpha scoreboard
challengers C1_SHRINK_H1 and C3_GROWTH_TILT registered (2 model hypotheses of the 2027 budget pre-booked, or 2026's if approved in Dec)
kb propose/approve/queue with tiers; ADR generation; lessons ledger
UI Factors + Knowledge tabs; vendored Chart.js
first inst_hold_chg_3m coverage on the live track
```

## 11.3 Month 6 — by the 2027-03 run

```
first live H=3 OOS points (as_of 2026-10, -11, -12 realised) → the live panel of the learning curve gets its first 3 hollow-to-solid points
sector_breadth_200, sector_inst_flow_3m, shareholder_yield, rev_growth_3y, asset_growth computing as candidates
first `factors review` with real criteria output (nothing will pass yet; that is expected and the report says so)
yearly-review template (BH-FDR) implemented and dry-run on the backfill track
optional adapter interfaces stubbed: NSE 4-level taxonomy, NSDL flows, shareholding pattern (pledge)
```

## 11.4 Month 12 — by the 2027-09 run

```
12 clean live runs; ~9 realised H=3 points; first H=12 point realised at the 2027-10 run
first formal champion/challenger review (P1 needs 24 points → "not reviewable", printed as such)
first yearly hypothesis-budget close-out and BH review (on whatever candidates have ≥ 12 points; most will be 'inconclusive')
multi-bagger flag pipeline exercised on the backfill track (2016–2023 starts) with base-rate table in the report
cost model recalibration hypothesis if turnover data warrants (Tier-1)
```

## 11.5 The chart the owner will look at, by phase

```
month 1    backfill panel: EW vs C2_PRICE_ONLY, ~120 points, 2016–2026; live panel: empty axes with the first as_of marked
month 6    live panel: 3 points, wide bands, EW and champion coincide (by design); inset (champion − EW) flat at 0
month 12   live panel: ~9 points; C1 diverges from EW (it may learn from month 12); C3 visible; still no claim possible
month 24   live panel: ~21 points; first meaningful HAC t on M1; C1 vs EW paired test first readable at 24 points
month 36   champion weights move for the first time (n_eff = 12); the inset starts to mean something; F1/F2 decided
```

## 11.6 If only 4 weeks of implementation were available

Keep the parts that make the record trustworthy later; cut everything that can be added without rewriting history.

```
KEEP (weeks 1–4)                                                CUT / DEFER
schema v1 with versioned exposures/scores/returns (§10.2)       challengers C1, C3 (C2 too — it needs the backfill scoring path)
PIT fundamentals with as_of; raw payload archive                paper portfolios, cost model, alpha scoreboard
price store with our own TRI + corporate_actions                benchmarks B1–B4 (keep only B0 = EW universe mean, trivial)
universe + sector_map PIT + merge rule                          sector_breadth / sector_inst_flow / all candidates except registered stubs
6 factors: mom_12_1, vol_252, trend_200, roce, leverage,        Fama–MacBeth, bootstrap CIs (keep HAC t only)
           earnings_yield  (+ sector_mom_12_1 if time)          UI Factors/Knowledge tabs (keep Scoreboard with the chart only)
EW model only; SHRINK rule as a pure function with tests        the LLM approval path (human-only Tier 2 for now)
  but no model_versions churn                                   NSE/NSDL adapters
gates G1, G4, G5, G6, G7; screens S1, S2                        G2, G3 automation (print jump list manually), G8 (run T1 by hand)
forward_returns for H=1,3,12; evaluations; learning_curve_points with ew_oos_ic      H=6/24/36, multibagger flag
migrate legacy (all of §10.5 — this is not optional)             backfill of price *scoring* (do backfill *prices* — it is 5 minutes)
hypotheses table + pre-registration of the 6 launch factors     experiments table, lessons table (add later; nothing depends on them)
decisions table + ADR-001                                        monthly report automation (write it by hand from CLI output)
```

The rule for the cut: anything that changes *what is stored about the past* must ship in week 1–4; anything that only *reads* the past can come later.

---

# 12. Risks, failure modes and open questions

## 12.1 Risks and failure modes (each with the mechanism and the mitigation in the design)

1. **Yahoo Finance changes or breaks.** Mechanism: undocumented unit changes (already happened with `dividendYield`), field removals, throttling. Mitigation: raw payloads archived monthly so a change is diffable; unit tests pinned to observed values; gate G4 blocks on unit drift; adapter interface so a second source can be added without touching factors. Residual: a multi-month outage leaves a hole in the live track that nothing can backfill point-in-time.

2. **Survivorship bias in the backfilled track.** Mechanism: today's constituents are, by selection, the survivors; momentum and low-vol look better on survivors. Mitigation: every backfill artefact carries `universe_source='current_list'`; the track is used for F4 (a *negative* check) and for C2's prior, never for the headline; C2 must earn promotion on live data. Residual: C2's initial weights may be wrong in a way that takes two years to show.

3. **Small n for years (the central risk).** Mechanism: at H=3 there are four independent periods a year; nothing about the fundamental track is statistically readable before 2029. Mitigation: the design refuses to move champion weights before then, states the timeline in §1.3, and gives the owner honest early movement through challengers and the backfill panel. Residual: owner impatience → pressure to loosen K or min_n_eff. Both changes are Tier-2 and require an ADR; the ADR template forces writing down what evidence justified it.

4. **Overfitting through challenger proliferation.** Mechanism: each challenger is a hypothesis test; ten challengers and the best one "wins" by chance. Mitigation: ≤ 3 live, hypothesis budget, promotion t deflated by ln(m), yearly BH review, and the rule that a champion which loses to EW is demoted. Residual: the budget can be gamed by bundling ("one hypothesis with five sub-variants"); the reviewer must reject bundles — a convention, not code.

5. **LLM approver rubber-stamping.** Mechanism: an LLM asked to approve will find reasons to approve. Mitigation: LLM approvals are Tier-1 only, provisional, require the CLI's criteria check with no `false`, and auto-revert without human ratification in 60 days. Residual: a human who ratifies without reading; the ADR file exists so that a later reader can see what was ratified on what evidence.

6. **Point-in-time ambiguity of fundamentals.** Mechanism: our `as_of` is the pull date, which is later than the true availability date (conservative) but earlier than a slow reader would have used; Yahoo may also revise past statements silently. Mitigation: pull date is the honest, reproducible choice; revisions are visible in the raw archive diff; restated values get a new `as_of` row rather than overwriting. Residual: factors built on quarterly TTM values will sometimes use a quarter that was published 2 days before the pull — fine — or miss one published 2 days after — also fine, and symmetric.

7. **Corporate actions Yahoo does not carry (rights, demergers).** Mechanism: a rights issue at a discount looks like a −20% day; a demerger like −40%. Mitigation: UNEXPLAINED_JUMP events, exclusion from returns, manual `corporate_actions` rows with decision ids. Residual: small biases in the months before a manual fix; the report lists them.

8. **Sector taxonomy look-ahead on the backfill.** Mechanism: using today's sector for 2016 neutralisation. Mitigation: labelled `sector_pit='current_proxy'`; live track is exact. Residual: none on the live track.

9. **Git repository growth.** Mechanism: SQLite binary deltas plus Parquet rewrites. Mitigation: yearly Parquet partitions, size printed by `data check`, LFS trigger thresholds. Residual: a mistaken commit of `.cache/` — `.gitignore` covers it.

10. **Cost model wrong by 2×.** Mechanism: impact assumptions for buckets B/C are guesses. Mitigation: stress row at 1.5×, cost model versioned, Tier-1 recalibration within ±25%. Residual: if true costs are much higher, F3 triggers and the quarterly variant is the fallback; the IC-based metrics are unaffected either way.

11. **The approach produces no net alpha in Nifty 500 at all.** This is not a risk to the design; it is the outcome the design is built to detect honestly (F1–F3). The knowledge base would then be the deliverable: a documented, pre-registered, out-of-sample negative result over three years, which is worth more than the legacy engine's positive claims.

12. **Holiday calendar and irregular grid.** Mechanism: run on a day after a market holiday; `as_of` drifts; HAC assumes even spacing. Mitigation: `as_of` follows the last close; `days` stored; footnote in the report. Residual: negligible.

## 12.2 Open questions (owner or implementer to settle; defaults stated)

```
Q1  Is there a stable free URL for NSE's four-level industry classification?  Default: no dependency; adapter stub only.
Q2  Benchmark ETF tickers with ≥ 3 years of history for Momentum 30 / Quality 50 proxies?  Default: constructed proxies B3/B4.
Q3  Source for promoter pledge and shareholding patterns (NSE/BSE quarterly PDFs/JSON)?  Default: candidate factor blocked on data.
Q4  Should the first V2 run be 2026-10-03 or should a one-off run on 2026-09-30 close the September gap?  Default: 2026-10-03;
    the 2026-09-03 legacy snapshot already covers September.
Q5  Which holiday calendar source (NSE publishes a yearly PDF; a hard-coded yearly list is acceptable)?  Default: config/holidays_YYYY.yaml.
Q6  Does the owner want the LLM approval path at all in year 1?  Default: implement the tier logic; enable llm approver in month 3.
Q7  Should `leverage` be net debt (needs cash) or gross debt/equity?  Default: gross D/E from Yahoo; net debt as a version-2 hypothesis.
Q8  Keep any DCF/intrinsic-value computation?  Default: UI diagnostic only, computed in ui_export from stored inputs, not in factors.
Q9  Does the price-only challenger C2 count against the 2026 budget?  Default: yes, as the first of the year's model hypotheses
    (registered with ADR-001), leaving 2 model + 5 factor slots.
```

## 12.3 The whole argument in one diagram

```
                     legacy (V16/V18)                              V2 (this design)
              ┌──────────────────────────┐                ┌───────────────────────────────────────┐
 what learns  │ 8 weights, every run,    │                │ factor set (add/retire by pre-registered │
              │ from 1-month IC, no gate │     ───►       │ criteria); weights only by shrinkage    │
              │                          │                │ after 12 independent H=3 periods         │
              ├──────────────────────────┤                ├───────────────────────────────────────┤
 what is      │ final_score IC, 3 months,│                │ OOS IC vs EW at the same k, HAC t, CI,  │
 measured     │ split in the returns     │     ───►       │ our own TRI, embargoed walk-forward      │
              ├──────────────────────────┤                ├───────────────────────────────────────┤
 how history  │ overwritten weights,     │                │ versioned exposures/scores/returns,      │
 is kept      │ no as_of, raw_json blob  │     ───►       │ as_of everywhere, raw payload archive,   │
              │                          │                │ decisions + hypotheses tables + ADRs      │
              ├──────────────────────────┤                ├───────────────────────────────────────┤
 how change   │ edit code, rerun         │                │ propose → pre-register → shadow →        │
 happens      │                          │     ───►       │ criteria → tiered approval → next run    │
              └──────────────────────────┘                └───────────────────────────────────────┘
```

**One sentence for the owner:** for the first three years this engine will mostly be proving, month by month and with numbers it cannot fudge, whether its factors work at all in the Nifty 500 — and that record, not any weight it learns, is what will make the chart go up later if anything can.

**Confidence, split in two.** That this design correctly measures whatever skill exists and cannot flatter itself: 85%. That the live fundamental track will show a positive, cost-surviving H=12 skill by 2029: 35–40% — the same number the red team gave, because nothing measured since has changed it.
