# V2 Quant Engine — Master Specification

Status: approved for implementation. Written 2026-09-06 on branch `red-team-review-sep-2026`.
Synthesised from five independent design drafts (`docs/spec/drafts/`), the context brief (`docs/spec/00_context_brief.md`) and the red-team review (`docs/analysis/red_team_review.md`). Where the drafts disagreed, section 13 records the decision and why.

Audience: a coding LLM implementing this repository from documents alone. Every table, function and command named here is a contract. `subagents/README.md` splits this document into workstreams; each workstream doc is self-contained but this document wins on any conflict.

Nothing in this document is a measured result. Every performance number is a **target** and is labelled as such.

---

## 0. How to read this document

```
Section   What it fixes                                  Who implements (subagents/)
1         objective, targets, falsification               everyone reads; WS11 wires the checklist
2         label and horizons                              WS07
3         universe, identity, sector taxonomy             WS01
4         data layer: prices, fundamentals, PIT, gates    WS02, WS03, WS04
5         factor library and plugin contract              WS05
6         scoring model, screens, weight learning         WS06
7         evaluation protocol and statistics              WS07
8         paper portfolio, costs, alpha scoreboard        WS08
9         monthly loop, knowledge base, governance        WS09
10        architecture: package, DDL index, CLI, config   WS00 (skeleton), all
10.7      legacy migration                                WS10
10.8      UI                                              WS11
11        roadmap                                         WS11 (sign-off)
12        risks                                           everyone
13        decisions log (why, alternatives, reversal)     everyone
14        glossary
15        open questions with defaults                    implementer takes the default
```

Conventions: dates are ISO `YYYY-MM-DD` text; money is rupees (crores only in the UI); fractions are fractions (0.0348, never 3.48); `as_of` is always an NSE trading day; identifiers in `snake_case`; SQL is SQLite.

---

## 1. Objective & success metrics

### 1.1 Objective

Every month, on the last NSE trading day, rank every Nifty 500 stock **within its sector group** by a composite of pre-registered, continuous, sector-neutral factors; record every input point-in-time so the ranking can be audited later by anyone; measure the ranking's out-of-sample predictive power with statistics that respect overlapping returns; and change the model only when accumulated evidence clears a pre-declared bar. The purpose of the ranking is to find stocks that compound over years; the purpose of the loop is to find out whether it can, and to say so plainly if it cannot.

Three things are kept apart because the legacy engine conflated them:

```
learning target      what may change weights or factor status   3-month sector-relative log total return
thesis target        what the scoreboard headlines               12-month sector-relative log total return
slow KPI             what the owner ultimately cares about       36-month "doubled" lift, observed never optimised
```

### 1.2 Targets (not claims) and when each first becomes judgeable

"Clean month" = a monthly run that passed every blocking gate with no override (section 4.6). n_eff = labelled months / horizon months (independent observations). "HAC t" = Newey–West t-statistic (section 7.3). First V2 live snapshot: `as_of = 2026-09-30` if the data layer is ready by 2026-10-05, else `2026-10-30`.

```
Metric (live track, champion)                              Month 12        Month 24          Month 36          Falsified if
------------------------------------------------------------------------------------------------------------------------------------
Data-quality gate pass rate (trailing 12)                  >= 10/12        >= 11/12          >= 11/12          --
Share of (stock x active factor) inputs missing            <= 15%          <= 10%            <= 10%            --
3M sector-neutral Rank IC, cumulative mean                 measured        >= +0.02          >= +0.03, t >= 2  90% HAC CI entirely below +0.01 at m36
12M sector-neutral Rank IC, cumulative mean                n/a             measured (n_eff 1) >= +0.04, t >= 1.5 --
Net-of-cost top-30 vs EW universe, annualised              n/a             measured          > 0               <= 0 over 24 labelled months at m48
Learned (challenger) minus EW composite, 3M IC             gate closed     paired t reported > 0, t >= 1.5     learned <= EW over 24 paired months -> rule shelved
Composite vs 1,000 random-weight composites                n/a             >= 60th pct        >= 75th pct       below median at m36
36M multi-bagger lift (precision / base rate)              n/a             n/a               first cohort m39  < 1.2x on first two annual cohorts
Hypotheses registered per calendar year                    <= 6            <= 6              <= 6              hard invariant
Monthly loop wall-clock on a laptop                        < 60 min        < 45 min          < 45 min          --
```

Why +0.03 / +0.04 and not +0.10: sustained sector-neutral ICs of published long-only composites in India and other emerging markets sit in 0.03–0.08 at 3–12 months. +0.10 would be exceptional and should be treated as a bug until proven otherwise.

### 1.3 Falsification checkpoints (pre-committed so the goalposts cannot move)

```
Month 12   any leakage test (7.5) fails on real data in >= 2 of the last 6 months     -> STOP; fix; nothing else counts
Month 24   3M and 6M cumulative IC both <= 0 for the champion                          -> freeze factor set 12 months; no new registrations
Month 36   3M IC 90% CI below +0.01 AND net top-30 <= EW universe                       -> fundamental composite falsified for this data source;
                                                                                          keep only families with t >= 2 or stop
Month 48   challenger never beats champion (paired 3M IC t < 1.0 over >= 24 months)    -> weight learning falsified; weights stay equal forever;
                                                                                          "learning" = factor admission and retirement only
```

### 1.4 What "predictability increases over time" means, measurably

Two different curves, stored in two different tables so they cannot be confused (section 7.7):

- **Evidence curve** — cumulative mean IC of a fixed model or factor against clean months, with its confidence band. It converges; a narrowing band around a positive value is skill, a narrowing band around zero is a clean negative result. Both are progress.
- **Learning curve** — out-of-sample IC of the model *re-fitted with data through month k*, evaluated at k, plotted next to the equal-weight baseline at the same k. It rises only if the system actually learned (better factor set, retirements, shrunk weights).

Every decision (promotion, retirement, weight rule change) is a labelled vertical line on both charts.

---

## 2. Prediction target & horizons

### 2.1 Label definition

For security *i*, snapshot `as_of = t` (last NSE trading day of the month), horizon *h* in months, `d_h` = last NSE trading day of month `t + h`:

```
TRI(i, d)             total-return index built in-engine from unadjusted closes + actions (section 4.3)
r_log(i,t,h)      =   ln( TRI(i, d_h) / TRI(i, t) )
G(i,t)                sector_group of i valid on t (section 3.4), frozen at t
r_grp(g,t,h)      =   median over j in G=g of r_log(j,t,h)          (same stocks that were scored at t)
L_h(i,t)          =   r_log(i,t,h) - r_grp(G(i,t),t,h)               THE LABEL (sector-relative log total return)
r_uni(i,t,h)      =   r_log(i,t,h) - median over universe(t)          stored, secondary
```

Rules:
1. A label row exists only after `d_h` has passed and a close exists. No partial returns ever enter a statistic.
2. Every stock scored at `t` is followed to maturity of every horizon even if it leaves the index, is suspended or delisted (`labels.status`). Dropping them is survivorship bias.
3. Delisting or permanent halt: last available TRI, `status='delisted_partial'`, included in IC and spreads; the report counts them and also shows statistics with delisted names forced to −50% as a sensitivity row.
4. An unresolved suspected corporate action inside the window (4.4) sets `status='excluded_ca'`; the row is kept and excluded from means. Nothing silently disappears.
5. Labels are never winsorised for IC. Decile and quintile means are reported as mean, median and 5%-trimmed mean side by side; portfolio arithmetic uses arithmetic returns.
6. Median, not mean, for the group adjustment so one demerger or 5x name does not move a group's baseline.

### 2.2 Horizons and their roles

```
h (months)  independent obs / year   role                                          statistics
1           12                       noise / data-quality diagnostic; legacy comparable  IC, HAC lag 0
3            4                       LEARNING horizon: weight learning, factor promotion IC, HAC lag 2, learning curve primary panel
6            2                       diagnostic                                    IC, HAC lag 5
12           1                       THESIS horizon: scoreboard headline; 12M sign check gates every promotion   IC, HAC lag 11, net spread
24           0.5                     diagnostic                                    IC
36           0.33                    multi-bagger KPI                              lift, base rate, Wilson CI
```

Why 3 months for learning (decision D01): with monthly snapshots, a 12-month label yields one independent observation per year; a learning rule fed that would have three data points in 2029. Three months is the shortest horizon at which quality, value and growth signals show up in published cross-sectional work rather than pure trend autocorrelation, and it gives four independent observations a year. One month is what the red team showed to be mostly a moving-average filter. Twelve months remains the thesis horizon because it is the shortest horizon that is defensibly "about compounding".

### 2.3 The multi-bagger KPI

```
mb36(i,t)        = 1 if TRI(i, d_36) / TRI(i, t) >= 2.0 else 0            (end-point; unambiguous)
mb36_touch(i,t)  = 1 if max over month-ends m in (t, d_36] of TRI(i,m)/TRI(i,t) >= 2.0   (diagnostic)
base_rate(t)     = mean_i mb36(i,t)
precision@D(t)   = share of decile-10 names (champion, at t) with mb36 = 1
recall@D(t)      = share of mb36 = 1 names that were decile 10 at t
lift(t)          = precision@D(t) / base_rate(t)                            (1.0 = no skill)  HEADLINE
```

Base rates in the Nifty 500 swing from single digits to over 40% by starting year, so recall alone is regime, not skill. Reported with a 90% Wilson interval. First live cohort matures 36 months after the first V2 snapshot. Never a learning target: positives are few and clustered in bull years.

Faster proxy, mechanically linked to compounding: `q_top_12m_hit` = share of decile 10 landing in the top quartile of its group's 12-month label.

### 2.4 Timing conventions

```
as_of          last completed NSE trading day on or before month-end, derived from the ^CRSLDX close series (a day with a close is a trading day);
               config/holidays fallback list only if the index series is missing
run window     first 1–7 calendar days of the next month, IST; as_of follows the data, never the calendar
prices         usable at as_of: date <= as_of
fundamentals   usable at as_of: available_from <= as_of  (section 4.4)
holdings       usable at as_of: captured_at <= as_of, i.e. THIS run's capture (stamped run date > as_of) is not usable this month; last month's is
membership     latest universe snapshot with as_of_snapshot <= as_of
paper trades   executed at the close of as_of + 1 trading day
```

---

## 3. Universe, identity & sector taxonomy

### 3.1 Universe

Source: `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv` (verified 2026-09-05: 500 rows; columns `Company Name, Industry, Symbol, Series, ISIN Code`; all `Series = EQ`; no duplicate Symbol or ISIN). Every monthly run saves the file verbatim to `data/universe/nifty500_<as_of>.csv` (~40 KB, committed) and writes `universe_membership` rows. Membership is therefore point-in-time from the first V2 run. Also fetched and saved monthly, same layout, for benchmark replication (7.4): `ind_nifty200Momentum30_list.csv`, `ind_nifty200Quality30_list.csv` (→ `data/universe/idx_mom30_<as_of>.csv`, `idx_qual30_<as_of>.csv`). Optional: `ind_niftytotalmarket_list.csv` for the extended (unscored) universe.

Fallback chain if the fetch fails or returns < 480 rows: the most recent committed file, with `data_quality_events code='UNIVERSE_STALE'` (WARN); BLOCK if that file is older than 62 days. The legacy Nifty 50 fallback is removed.

Symbols containing `&` or `-` (`ARE&M`, `BAJAJ-AUTO`, `GVT&D`, `J&KBANK`, `M&MFIN`, `M&M`, `NAM-INDIA`) are accepted by Yahoo verbatim with `.NS`; file names use ISIN, never symbol.

### 3.2 Identity: ISIN, not ticker

```sql
CREATE TABLE securities (
  security_id   INTEGER PRIMARY KEY,
  isin          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  listing_date  TEXT,                       -- optional, from EQUITY_L.csv adapter
  first_seen    TEXT NOT NULL, last_seen TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('listed','delisted','suspended','unknown'))
);
CREATE TABLE symbol_history (
  security_id INTEGER NOT NULL REFERENCES securities,
  nse_symbol  TEXT NOT NULL, yahoo_ticker TEXT NOT NULL,     -- yahoo_ticker = nse_symbol || '.NS' unless overridden
  valid_from  TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL,
  PRIMARY KEY (security_id, valid_from)
);
CREATE TABLE universe_membership (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  index_name TEXT NOT NULL,                                     -- 'NIFTY500' | 'NIFTY200MOM30' | 'NIFTY200QUAL30' | 'NIFTYTOTALMARKET'
  nse_symbol TEXT NOT NULL, nse_sector TEXT, series TEXT,
  source TEXT NOT NULL CHECK (source IN ('nse_csv','nse_csv_stale','legacy_snapshot','current_backfill')),
  source_sha256 TEXT, PRIMARY KEY (as_of, index_name, security_id)
);
```

A symbol rename (same ISIN, new Symbol) closes the open `symbol_history` row and opens a new one; price history is stitched by ISIN. Manual overrides: `config/manual_ticker_overrides.csv` (isin, yahoo_ticker, note).

### 3.3 Canonical sector = NSE sector level from the constituent CSV

The CSV column named `Industry` **is** NSE's level-2 *Sector* (20 values). The four-level NSE/AMFI hierarchy is not retail-fetchable by script (the NSE quote API returns 403 to scripts; the AMFI file is an irregular spreadsheet). Decision D04: `nse_sector` = that column, verbatim; Yahoo `sector`/`industry` stored as attributes; Yahoo industry used for exactly one structural purpose (splitting Financial Services). Measured 2026-09-05 (counts will drift):

```
Financial Services 101 | Capital Goods 63 | Healthcare 48 | Automobile and Auto Components 38 | Consumer Services 29
Fast Moving Consumer Goods 28 | Information Technology 27 | Chemicals 26 | Metals & Mining 18 | Power 17
Oil Gas & Consumable Fuels 17 | Consumer Durables 16 | Services 14 | Construction 13 | Construction Materials 11
Realty 11 | Telecommunication 10 | Textiles 5 | Media Entertainment & Publication 5 | Diversified 3
```

Why not Yahoo as canonical: on the 2026-09-03 snapshot, NSE "Capital Goods" (63) maps to Yahoo Industrials 49 / Basic Materials 7 / Technology 5 / other 2; the taxonomies disagree on roughly one name in five outside financials. Mixing them across months would fabricate reclassifications.

### 3.4 Neutralisation groups (`sector_group`), versioned

Rule set `sector_group_def` version 1, applied in order at each `as_of`:

```
R1  Financial Services split by Yahoo industry (ENABLED FROM MONTH 3 by decision; month 1 ships FS as one group):
      FS_BANKS     yahoo_industry matches /Bank/
      FS_LENDERS   /Credit Services|Mortgage|Financial Conglomerates|Insurance/
      FS_MARKETS   everything else in Financial Services (Capital Markets, Asset Management, Exchanges & Data, fintech, unknown)
R2  Merge table for small NSE sectors:
      Textiles                          -> Consumer Durables & Textiles   (with Consumer Durables)
      Media Entertainment & Publication -> Consumer Services & Media      (with Consumer Services)
      Diversified                       -> Services & Diversified         (with Services)
      Telecommunication                 -> stays 'Telecommunication' while >= 8 members; below that merges into Services & Diversified
R3  Any group with < min_group_size (8) members at as_of -> merged into its `merge_into` target from the def table;
    if none, into 'OTHER'. A merge writes data_quality_events code='GROUP_MERGED' (INFO). It never silently changes history.
```

Expected at launch: 17–19 groups, smallest ≥ 8. The implementer prints the actual group table in the month-1 report and records it in ADR-001.

```sql
CREATE TABLE sector_group_def (
  version INTEGER NOT NULL, nse_sector TEXT NOT NULL, yahoo_industry_pattern TEXT,   -- NULL = any; regex otherwise
  sector_group TEXT NOT NULL, macro_sector TEXT NOT NULL, merge_into TEXT, min_group_size INTEGER NOT NULL DEFAULT 8,
  registered_on TEXT NOT NULL, decision_id TEXT, PRIMARY KEY (version, nse_sector, yahoo_industry_pattern)
);
CREATE TABLE sector_map (
  security_id INTEGER NOT NULL REFERENCES securities,
  valid_from TEXT NOT NULL, valid_to TEXT,                 -- inclusive / exclusive; NULL = current
  nse_sector TEXT, yahoo_sector TEXT, yahoo_industry TEXT,
  sector_group TEXT NOT NULL, group_def_version INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('nse_csv','nse_csv_prior','yahoo_crosswalk','manual','legacy_backfill','current_backfill')),
  confidence REAL NOT NULL,                                -- 1.0 nse_csv; 0.9 prior; crosswalk share; 0.5 manual/backfill
  PRIMARY KEY (security_id, valid_from)
);
```

Scoring at `t` joins `valid_from <= t AND (valid_to IS NULL OR t < valid_to)`. A changed classification closes the old row at `t` and opens a new one; `data_quality_events code='SECTOR_RECLASS'` (INFO). Labels keep `G(i,t)` frozen at snapshot time; nothing is rewritten. Changing `sector_group_def` is a new version (Tier-2 decision); old scores keep their `group_def_version`.

Fallback chain when a security is missing from the current CSV (dropped out but still tracked to label maturity):

```
1. nse_csv          this month's file                                   confidence 1.0
2. nse_csv_prior    last NSE mapping for the ISIN, <= 24 months old      confidence 0.9
3. yahoo_crosswalk  config/yahoo_to_nse_crosswalk_v1.csv (modal NSE      confidence = modal share
                    sector observed for that Yahoo sector+industry)
4. UNCLASSIFIED     ranked against the whole universe; eligible = 0      confidence 0.0   (gate: > 1% of members -> BLOCK)
```

Only sources 1–2 count as clean for the learning curve.

### 3.5 Sector-level features and the sector sleeve

Sector-neutral ranking removes sector bets from the stock composite by construction. Sector bets, if wanted, are a **separate, capped, measurable** decision. Features per `(as_of, sector_group)` from the engine's own stores (never an external feed):

```
feature_id            formula                                                             status at launch
sector_mom_6m         EW mean of members' ln TR over (t-6m, t-1m]                        shadow
sector_breadth_200    share of members with close > SMA200                               shadow
sector_flow_proxy     median 3-month change in members' inst_held_frac (own PIT history)  candidate (needs 3 runs)
sector_val_spread     median earnings yield of group minus universe median                shadow
sector_dispersion     cross-sectional std of members' 3M returns (regime feature, not a factor)
```

They enter only the `sector_overlay_v1` challenger (6.6) with total sleeve weight capped at 0.20 and zero at launch. NSDL FPI sector flows exist only as fortnightly PDFs; a parser is an optional adapter (`quant/data/adapters/nsdl_flows.py`, stub only).

---

## 4. Data layer

### 4.1 Data flow

```
 niftyindices CSVs (3) ─► data/universe/*.csv ─► universe_membership, securities, symbol_history, sector_map
                                                                     │
 yf.download (batches of 25, threads=False, 1.0 s between batches, auto_adjust=False, actions=True)
        ▼
 data/prices_daily.sqlite  (git-ignored; rebuildable)        ──reconcile overlap──► corporate_actions, data_quality_events
   prices_daily(security_id, date, open, high, low, close_raw, yahoo_adj_close, volume, dividend, split_ratio, pulled_at)
        │
        ├─► TRI per security (4.3) ─► prices_monthly(as_of, close_raw, tri, adv_63_inr, n_days_63, mcap_inr, shares_out)   [committed via ledger]
        │
 yf.Ticker per security (0.5 s sleep): info, financials, balance_sheet, cashflow, quarterly_financials, quarterly_balance_sheet, earnings_dates
        ▼
 data/raw/fundamentals/<as_of>.jsonl.gz  (committed: the point-in-time proof)
        ▼
 fundamentals(security_id, statement, freq, period_end, field, value, available_from, available_from_basis, fetched_at)   [ledger]
 holdings(security_id, captured_at, inst_pct, insider_pct, shares_out)                                                     [ledger]
 security_attributes(as_of, security_id, mcap_inr, shares_out, adv_63_inr, ev_inr, trailing_pe, price_to_book, dividend_rate, beta, yahoo_sector, yahoo_industry)
        ▼
 gates G1..G10 ─► data_quality_events ─► BLOCK stops before factors (prices/labels still update)
        ▼
 FactorInputs(as_of)  (the ONLY read path for factors; physically cannot return anything after as_of)
        ▼
 factor_values ─► scores (every model) ─► paper_* ─► labels (matured) ─► evaluations, learning_curve ─► proposals ─► report, ui, ledger export, commit
```

### 4.2 Storage and git policy (decision D07)

The working Python has no `pyarrow`, `duckdb`, `statsmodels` or `git-lfs`. Month-1 adds **no** dependency.

```
Artifact                                       Where                                Committed   Growth
--------------------------------------------------------------------------------------------------------------
Daily bars (10 y x ~550 names, ~1.3 M rows)    data/prices_daily.sqlite             NO          ~150 MB; rebuilt by `quant prices backfill`
Prices manifest (per security sha256, ranges)  data/MANIFEST.json                   YES         ~150 KB, rewritten monthly
Universe / index constituent CSVs              data/universe/*.csv                  YES         ~45 KB / month
Raw Yahoo fundamentals payloads                data/raw/fundamentals/<as_of>.jsonl.gz  YES      ~0.6 MB / month  (the PIT proof)
Monthly ledger (every row written that month)  data/ledger/<YYYY-MM>/<table>.csv    YES         ~2.5 MB text / month; delta-friendly
V2 state database                              quant.db                             YES         ~1–2 MB / month after VACUUM; committed ONCE per month
Legacy database (frozen, read-only)            quant_engine.db                      YES         unchanged forever
Knowledge base                                 knowledge/**                         YES         text
UI payloads                                    ui/data*.js                          YES         < 1.5 MB, rewritten monthly
```

The **ledger CSVs are canonical**. `python -m quant db rebuild` reconstructs `quant.db` from `data/ledger/` + `data/universe/`; `python -m quant db verify` asserts row-count and checksum equality per table and runs in CI. If two branches ever conflict on `quant.db`: delete, rebuild from the ledger, recommit. Parquet is an optional export adapter if `pyarrow` is ever installed; nothing depends on it. Trigger to revisit: any file > 50 MB or repo > 800 MB (`quant db size` prints both).

### 4.3 Prices, corporate actions and total returns (decision D05)

Verified 2026-09-05 with yfinance 1.4.1: `yf.download(..., auto_adjust=False, actions=True, group_by='ticker', threads=False)` returns `Open High Low Close Adj Close Volume Dividends Stock Splits`; the index is tz-aware `Asia/Kolkata` (strip once in `quant/data/yahoo.py`); `Close` is split-adjusted by Yahoo within one download, `Adj Close` additionally dividend-adjusted and **rewritten backward on every new dividend**; ZFCVINDIA's 6:1 split appears as `Stock Splits = 6.0` on 2026-06-24; bonus issues appear as splits; rights issues and demergers do **not** appear and produce a price gap.

Design: store facts, adjust locally.

```
prices_daily columns  close_raw (Yahoo 'Close' as delivered), yahoo_adj_close (side column, cross-check only), volume, dividend, split_ratio (1.0 when none)
TRI                   TRI_0 = 100 ;  TRI_d = TRI_{d-1} * (close_raw_d + dividend_d) / close_raw_{d-1}
                      (Yahoo's Close is already split-consistent inside one download, so no extra split term; dividends added on ex-date)
monthly reconciliation  each run downloads the last 13 months for every tracked security and compares the 12 overlapping months:
   identical within 0.05%                      fine
   constant ratio == a newly reported split    apply ratio to stored history; corporate_actions row; event 'SPLIT_RESTATED' (INFO)
   anything else                               event 'UNEXPLAINED_PRICE_REVISION' (WARN per security; BLOCK if > 2% of universe);
                                               new values quarantined in prices_daily_quarantine until `quant data accept-revision`
cross-check           monthly in-engine TR return vs Yahoo Adj Close return, tolerance 30 bp -> event 'TR_MISMATCH' (INFO)
unrecorded actions    |1-day log return| > ln(1.40) with no dividend/split that day -> event 'SUSPECTED_UNRECORDED_CA' (WARN);
                      labels whose window contains that day get status 'excluded_ca' until a human/LLM enters the action
                      (`quant data add-ca --isin .. --ex-date .. --kind demerger|rights --factor ..`) or clears the flag.
                      The legacy +-60% monthly filter is retired: Indian small caps do move 60% in a month legitimately.
```

```sql
CREATE TABLE corporate_actions (
  security_id INTEGER NOT NULL REFERENCES securities, ex_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('split','bonus','dividend','rights','demerger','scheme','manual_adj','suspected')),
  ratio REAL, amount_inr REAL, adj_factor REAL,           -- split/bonus: new per old; dividend: INR/share; manual: multiplicative price factor
  source TEXT NOT NULL CHECK (source IN ('yahoo_actions','reconcile','manual','inferred')),
  observed_at TEXT NOT NULL, decision_id TEXT, note TEXT,
  PRIMARY KEY (security_id, ex_date, kind)
);
CREATE TABLE prices_monthly (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  close_raw REAL, tri REAL, adv_63_inr REAL, n_days_63 INTEGER, mcap_inr REAL, shares_out REAL,
  quote_legacy REAL, source TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, security_id)
);
```

Backfill: `python -m quant prices backfill --start 2016-01-01 --batch 25 --sleep 1.0` (~22 batches, ~3–10 min, resumable per security; also pulls tickers present in legacy snapshots but not in today's list). Benchmarks pulled into the same daily table under their Yahoo symbols: `^CRSLDX`, `^NSEI`, `^CNX200`, `NIFTYBEES.NS`, `MID150BEES.NS`, `MOM30IETF.NS` (from 2024-06), `QUAL30IETF.NS` (from 2023-08), `LOWVOLIETF.NS`. Monthly update pulls the trailing 13 months per security (idempotent upsert keyed by date). The 0.5 s per-request throttle from AGENTS.md is kept for per-ticker calls; HTTP 429 → sleep 120 s, resume from the last completed security; a run that cannot finish ≥ 90% of fundamentals is `status='partial'` and writes no scores (prices and labels still update).

### 4.4 Fundamentals and holdings, point-in-time (decision D06)

Yahoo keys statements by *period end*, not publication date; using period end as the availability date is a 1–3 month look-ahead. Every statement value carries three dates:

```
period_end        the fiscal period the value describes
available_from    the first date the value could have been public:
                    1. earnings_dates row with a reported EPS within 60 days after period_end  -> that date + 1 trading day   basis 'earnings_date'
                    2. quarterly statement                                                    -> period_end + 45 calendar days + 1 trading day   'lodr_45d'  (SEBI LODR Reg 33)
                    3. annual / Q4                                                            -> period_end + 60 calendar days + 1 trading day   'lodr_60d'
                    4. value first seen by us AFTER the rule date (late restatement)          -> max(rule date, fetched_at)   'first_fetch'
fetched_at        when this engine captured it (provenance; a later fetch with a different value INSERTS a new row, never updates)
```

PIT query for date `d`: rows with `available_from <= d`; among versions of the same `(security, statement, period_end, field)` prefer the largest `fetched_at <= d` (what we knew then); for the backfill segment, the earliest version ever captured, flagged `pit_basis='backfilled_current_version'`.

```sql
CREATE TABLE fundamentals (
  security_id INTEGER NOT NULL REFERENCES securities,
  statement TEXT NOT NULL CHECK (statement IN ('income','balance','cashflow','info')),
  freq TEXT NOT NULL CHECK (freq IN ('A','Q','P')),         -- annual, quarterly, point value from info
  period_end TEXT NOT NULL,                                  -- '' for point values
  field TEXT NOT NULL,                                       -- canonical name from config/field_contracts_v1.yaml
  value REAL, unit TEXT NOT NULL CHECK (unit IN ('inr','frac','x','shares','count')),
  available_from TEXT NOT NULL,
  available_from_basis TEXT NOT NULL CHECK (available_from_basis IN ('earnings_date','lodr_45d','lodr_60d','first_fetch','run_date')),
  fetched_at TEXT NOT NULL, source TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY (security_id, statement, period_end, field, fetched_at)
);
CREATE INDEX ix_fund_pit ON fundamentals (security_id, field, available_from);
CREATE TABLE holdings (
  security_id INTEGER NOT NULL REFERENCES securities, captured_at TEXT NOT NULL,
  inst_pct REAL, insider_pct REAL, shares_out REAL, source TEXT NOT NULL,
  PRIMARY KEY (security_id, captured_at)
);
CREATE TABLE security_attributes (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  mcap_inr REAL, shares_out REAL, float_shares REAL, adv_63_inr REAL, ev_inr REAL, trailing_pe REAL, price_to_book REAL,
  dividend_rate_inr REAL, beta REAL, yahoo_sector TEXT, yahoo_industry TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (as_of, security_id)
);
```

Fields stored (one row per field, no JSON blobs for anything a factor reads):

```
income (A + Q):   Total Revenue, EBIT, EBITDA, Net Income, Diluted EPS, Basic EPS, Interest Expense
balance (A + Q):  Total Assets, Current Liabilities, Total Debt, Cash And Cash Equivalents, Stockholders Equity, Ordinary Shares Number
cashflow (A):     Operating Cash Flow, Capital Expenditure, Free Cash Flow (if present)
info (P):         marketCap, sharesOutstanding, floatShares, enterpriseValue, trailingPE, priceToBook, dividendRate, beta,
                  heldPercentInstitutions, heldPercentInsiders, sector, industry, industryKey
```

Unit normalisation happens in exactly one module (`quant/data/yahoo.py`), one function per field, each with a test pinned to a value observed 2026-09-05:

```
dividendRate 75.0 (INR/share) -> dividend yield = 75 / close      (NEVER read dividendYield; it changed units between yfinance versions)
debtToEquity 357               -> 3.57 (percent -> ratio)          (v1 factors compute leverage from statement rows instead)
heldPercentInstitutions 0.3905 -> unchanged (fraction)
returnOnEquity None            -> NaN, never 0
statements                     -> rupees as delivered; crores only in the UI
```

No factor may impute a growth rate, an ROE or a yield. Missing input → NaN → flagged. This rule is what prevents the +15% default growth and the None→0 ROE bugs from recurring.

HTTP budget per run: 7 per-ticker calls × 500 × 0.5 s ≈ 30 min plus batched prices ≈ 3 min. Annual statements refresh quarterly (months 2, 5, 8, 11 after year 1) to halve this. Institutional holdings are captured every run into `holdings` (`captured_at = run date`); Yahoo's `institutional_holders` frame is empty for Indian names, so `info.heldPercentInstitutions` is the input and its update cadence is unknown — logged for 12 months, then decided (15-Q4).

### 4.5 Backfill and evidence tracks

```
track = 'live'      inputs recorded at as_of by the monthly run; all factors; the only track that counts for promotion, weights, scoreboard
track = 'backfill'  price/volume-derived factors only, month-ends 2016-01 .. first live month, universe = today's constituents
                    (survivorship-biased) with today's sector map (labelled 'current_backfill'); used for (a) sanity (mom_12_1 should
                    show a positive 12M IC in India; if not, the pipeline is broken), (b) descriptive priors, (c) a chart to look at
                    while live data accumulates. Drawn dashed; the word "backfill" is mandatory next to any number from it.
track = 'legacy'    the four 2026 snapshots migrated from quant_engine.db (10.7), defects flagged; drawn as hollow points; never clean.
```

Fundamental factors have no backfill track: Yahoo exposes 5 annual / 6 quarterly periods with no restatement history. An optional experiment track `backfill_lagged` (annual values usable at period_end + 90 days) may be used for development only and is never shown on the scoreboard.

### 4.6 Data-quality flags and gates

Row flags travel with the value (`factor_values.flags`, `fundamentals` provenance, `labels.status`). Run gates decide whether the month's scores are written. Every gate result is a `data_quality_events` row and a `dq_runs` row; the report prints the table verbatim; a blocked month is a first-class outcome (recorded, appears as a gap on the learning curve, re-runnable after the fix; a blocked run cannot replace a passed run without `--force`).

```
Gate  Check                                                                     Threshold                         On failure
G1    universe rows parsed, 5 expected columns                                  >= 480                            BLOCK
G2    universe file not stale in an index-review month (Mar/Sep)               sha differs from previous          BLOCK
G3    members with a close_raw on as_of                                         >= 98%                            BLOCK
G4    members whose close_raw == previous cohort's close (the 06-12/06-14 bug)  < 5%                              BLOCK
G5    UNEXPLAINED_PRICE_REVISION share of universe                              <= 2%                             BLOCK
G6    unit sanity: dividend yield <= 0.25; D/E <= 50; |trailing_pe| < 1000;      violators per field <= 5          violators flagged & NULLed; BLOCK above
      0 <= inst_pct <= 1; mcap > 0; no negative Total Assets
G7    sector_group coverage (source != none)                                     >= 99%                            BLOCK
G8    active-factor coverage: price factors >= 95%, others >= 70% of eligible   per factor                        factor excluded this month (recorded); BLOCK if >= 3 excluded
G9    reproducibility: re-score previous as_of from stored inputs                hash identical                    BLOCK
G10   leakage smoke: shuffle test on this month's scores vs last month's matured 1M labels  |mean IC| < 0.01     BLOCK
W1    median statement staleness                                                 <= 15 months                      WARN
W2    imputed/missing share per active factor                                    <= 20%                            WARN
W3    near-constant factor (modal share of universe >= 0.80)                      0 active factors                  WARN (3 consecutive -> quarantine proposal)
W4    SUSPECTED_UNRECORDED_CA count                                              <= 5                              WARN
W5    yfinance version changed without a decision record                        --                                WARN
W6    PSI drift > 0.25 on an active-factor input field vs pooled prior 3 months  per field                         WARN; >= 3 fields -> BLOCK
```

`config/field_contracts_v1.yaml` mirrors into `field_contracts(field, unit, min_value, max_value, max_null_rate, source, contract_version)`; G6/W6 read it.

```sql
CREATE TABLE data_quality_events (
  event_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('INFO','WARN','BLOCK')),
  code TEXT NOT NULL, security_id INTEGER, field TEXT, detail_json TEXT, resolved_by TEXT
);
CREATE TABLE dq_runs (run_id INTEGER NOT NULL, gate TEXT NOT NULL, value REAL, threshold REAL, passed INTEGER NOT NULL, blocking INTEGER NOT NULL,
  PRIMARY KEY (run_id, gate));
```

---

## 5. Factor library

### 5.1 Plugin contract (`quant/factors/base.py`)

```python
from dataclasses import dataclass
from typing import Callable, Literal
import pandas as pd

Family = Literal['momentum', 'low_risk', 'quality', 'value', 'growth', 'flows', 'sector', 'control', 'legacy']
Status = Literal['registered', 'shadow', 'active', 'probation', 'retired', 'quarantined']   # status lives in factor_registry, NOT here

@dataclass(frozen=True)
class FactorSpec:
    name: str                       # 'mom_12_1' ; snake_case; stable forever (bump version instead of renaming)
    version: int                    # ANY formula/input change => version + 1 => new factor_id 'mom_12_1@2'; old keeps its history
    family: Family
    direction: int                  # +1: higher raw => higher expected sector-relative return; -1: the reverse
    horizon_m: int                  # horizon the hypothesis is stated for (3 or 12); promotion tests use it
    hypothesis: str                 # one falsifiable sentence, written BEFORE evaluation
    formula: str                    # human-readable; must match compute()
    inputs: tuple[str, ...]         # canonical field names / 'prices' / 'holdings' ; FactorInputs refuses anything else
    lookback_days: int = 0          # trading days of history needed; fewer => NaN, flag 'low_history'
    applies_to_financials: bool = True   # False => NaN for sector_group starting 'FS_' or nse_sector == 'Financial Services'
    level: Literal['stock', 'sector'] = 'stock'
    backfillable: bool = False      # True only for price/volume factors
    min_coverage: float = 0.70      # G8 threshold for this factor
    evidence: str = ''              # external evidence + honest caveat; never results from this repository
    hypothesis_id: str = ''         # 'H-2026-001'; required before status >= 'registered'

    @property
    def factor_id(self) -> str: return f"{self.name}@{self.version}"

@dataclass
class FactorInputs:
    """Read-only view for ONE as_of, built by quant/factors/inputs.py. It physically contains nothing dated after as_of,
    which is what the PIT-truncation test (7.5) checks. A factor has no other data access."""
    as_of: str
    members: pd.Index                                    # security_id of the universe at as_of
    sector_group: pd.Series                              # security_id -> group at as_of
    def tri(self, lookback_days: int) -> pd.DataFrame: ...          # dates x security_id, dates <= as_of
    def close_raw(self, lookback_days: int) -> pd.DataFrame: ...
    def volume(self, lookback_days: int) -> pd.DataFrame: ...
    def adv_inr(self) -> pd.Series: ...
    def mcap_inr(self) -> pd.Series: ...
    def attribute(self, field: str) -> pd.Series: ...                # security_attributes at as_of (trailing_pe, price_to_book, ev_inr, dividend_rate_inr, beta)
    def fundamental(self, statement: str, field: str, freq: str, n_periods: int) -> pd.DataFrame: ...
        # security_id x period rank (0 = latest with available_from <= as_of); PIT-filtered; NaN where absent
    def ttm(self, field: str) -> pd.Series: ...                     # sum of last 4 quarters all available; else latest annual, flag 'ttm_from_annual'
    def holdings(self, lag_runs: int = 0) -> pd.Series: ...         # inst_pct from the capture with captured_at <= as_of, lag_runs runs earlier
    def benchmark_tri(self, symbol: str, lookback_days: int) -> pd.Series: ...

class Factor:
    spec: FactorSpec
    def compute(self, x: FactorInputs) -> pd.Series:
        """Raw value indexed by security_id for every member (NaN allowed). Pure: no I/O, no network, no clipping, no ranking."""
```

Registry: one module per family under `quant/factors/`; `quant/factors/registry.py::REGISTRY` collects `Factor` objects; `python -m quant factors sync` mirrors specs into `factor_registry` (status `registered`) and pins `code_sha256 = sha256(inspect.getsource(module))`. A changed hash without a version bump fails G9 (re-scoring last month would differ). Status is a dated column in the table, never in code.

### 5.2 Standardisation (framework, identical for every factor, not overridable)

```
raw(i)  ->  if not applies_to_financials and i is financial: NaN
        ->  coverage check: non-NaN share of members >= min_coverage else factor excluded this month (G8), rows still stored with flag
        ->  winsorise at the 1st / 99th percentile of the WHOLE cross-section (defence in depth; ranks make it nearly a no-op)
        ->  within sector_group (or across groups for level='sector'): average-tie rank r in 1..n ; u = (r - 0.5) / n
        ->  z = Phi^-1(u) * direction              (van der Waerden scores: every group mean 0, sd ~1 whatever its size)
        ->  clip z to [-3, 3]
        ->  groups with n < 5 non-NaN: z = NaN, flag 'group_too_small'
        ->  missing raw stays NaN (NOT 0); flag 'missing'
        ->  store raw, winsor, z, flags in factor_values
```

Because z is rank-based within group, five-level buckets, hand-typed lists and "50 for 96% of the universe" cannot recur: a factor constant within a group produces NaN z and fails coverage visibly.

```sql
CREATE TABLE factor_registry (
  factor_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, family TEXT NOT NULL,
  direction INTEGER NOT NULL CHECK (direction IN (-1,1)), horizon_m INTEGER NOT NULL, level TEXT NOT NULL,
  hypothesis TEXT NOT NULL, formula TEXT NOT NULL, inputs_json TEXT NOT NULL, lookback_days INTEGER NOT NULL,
  applies_to_financials INTEGER NOT NULL, backfillable INTEGER NOT NULL, min_coverage REAL NOT NULL, evidence TEXT,
  hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id), code_sha256 TEXT NOT NULL, module_path TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('registered','shadow','active','probation','retired','quarantined')),
  registered_on TEXT NOT NULL, first_live_as_of TEXT, status_changed_on TEXT NOT NULL, status_decision_id TEXT
);
CREATE TABLE factor_values (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL, factor_id TEXT NOT NULL REFERENCES factor_registry,
  raw REAL, winsor REAL, z REAL, sector_group TEXT NOT NULL, flags TEXT,          -- comma list: missing|low_history|imputed_input|stale_input|group_too_small|ttm_from_annual
  track TEXT NOT NULL DEFAULT 'live' CHECK (track IN ('live','backfill','legacy')), run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, security_id, factor_id, track)
);
CREATE INDEX ix_fv_factor_asof ON factor_values (factor_id, as_of);
CREATE TABLE sector_features (as_of TEXT NOT NULL, sector_group TEXT NOT NULL, feature_id TEXT NOT NULL, value REAL, n_members INTEGER,
  PRIMARY KEY (as_of, sector_group, feature_id));
```

Exposures are never overwritten; a recomputation after a bug fix writes rows under a new version.

### 5.3 Launch factor set (decision D09)

`dir` = expected sign of the Spearman IC of raw vs `L_3`/`L_12`. `fin` = applies to Financial Services groups. Status at launch. Evidence grades: A = replicated internationally and in India with a live NSE index product; B = international evidence, mixed Indian; C = plausible, untested. All active factors are pre-registered as `H-2026-001..` on the day of the first V2 run.

```
factor            family    dir  h   fin  bkf  status    formula (at as_of t; all fundamentals PIT-filtered)                                  grade
----------------  --------  ---  --  ---  ---  --------  -------------------------------------------------------------------------------------  -----
mom_12_1          momentum   +   3   yes  yes  active    ln( TRI[t-21d] / TRI[t-252d] ) ; needs >= 230 obs                                       A
trend_200         momentum   +   3   yes  yes  active    close_raw[t] / SMA200(close_raw) - 1     (continuous replacement of the death cross)   A
vol_252           low_risk   -   12  yes  yes  active    std(daily ln TRI returns, 252 d) * sqrt(252) ; needs >= 200 obs                        A
roce              quality    +   12  no   no   active    ttm(EBIT) / (Total Assets - Current Liabilities), latest annual balance                 A
accruals          quality    -   12  no   no   active    (Net Income_A - Operating Cash Flow_A) / Total Assets_A                                 B
cash_conversion_3y quality   +   12  no   no   active    sum(OCF_A, 3 FY) / sum(Net Income_A, 3 FY) ; NaN if denominator <= 0                    B
earnings_yield    value      +   12  yes  no   active    non-fin: ttm(EBIT) / ev_inr ; fin: ttm(Net Income) / mcap_inr  (yield form: losses rank low) B
book_to_price     value      +   12  yes  no   active    Stockholders Equity_A / mcap_inr                                                        B
eps_growth_3y     growth     +   12  yes  no   active    ln(EPS_A0 / EPS_A3) / 3 ; NaN if either endpoint <= 0 (never imputed)                    C (owner thesis)
earn_mom          growth     +   3   yes  no   active    ( ttm(Net Income) - ttm(Net Income, 4 quarters earlier) ) / |ttm(Net Income, 4q earlier)| B
inst_hold_chg_3m  flows      +   3   yes  no   active*   holdings(lag 0) - holdings(lag 3 runs) ; NaN until 3 runs of OUR history exist            C
mom_6_1           momentum   +   3   yes  yes  shadow    ln( TRI[t-21d] / TRI[t-126d] )
dist_52w_high     momentum   +   3   yes  yes  shadow    close_raw[t] / max(close_raw, 252 d) - 1
rev_1m            momentum   -   1   yes  yes  shadow    ln( TRI[t] / TRI[t-21d] )      (short-term reversal; diagnostic for the 1M rung)
max_ret_21        low_risk   -   1   yes  yes  shadow    max daily return over 21 d     (lottery effect)
leverage          quality    -   12  no   no   shadow    (Total Debt - Cash) / ttm(EBITDA) ; NaN if EBITDA <= 0     (continuous replacement of bs_score)
roe_stability_3y  quality    +   12  yes  no   shadow    mean(ROE_A, 3 FY) / std(ROE_A, 3 FY) ; ROE_A = NI_A / Stockholders Equity_A
fcf_yield         value      +   12  no   no   shadow    mean(OCF_A + CapEx_A, 3 FY) / ev_inr     (CapEx is negative in yfinance)
div_yield         value      +   12  yes  no   shadow    dividend_rate_inr / close_raw
rev_growth_3y     growth     +   12  yes  no   shadow    ln(Revenue_A0 / Revenue_A3) / 3 ; NaN if either <= 0
size              control    n/a  -   yes  yes  control   ln(mcap_inr)         weight 0 in every model; used as a regression control and diagnostic
liq               control    n/a  -   yes  yes  control   ln(adv_63_inr)       weight 0; drives the liquidity screen and cost bucket
beta_252          control    n/a  -   yes  yes  control   OLS beta of daily TRI returns on ^CRSLDX, 252 d
dc_flag           legacy     n/a  1   yes  yes  diagnostic 1 if close < SMA50 < SMA200 ; evaluated monthly ("would-have-been-killed" cohort); never weighted
sector_mom_6m     sector     +   3   --   yes  shadow    section 3.5 (sleeve only)
sector_breadth_200 sector    +   3   --   yes  shadow    section 3.5 (sleeve only)
sector_flow_proxy sector     +   3   --   no   candidate section 3.5 (sleeve only)
```

`*` `inst_hold_chg_3m` is registered active but excluded by G8 until three runs of own holdings history exist (third live run); the four legacy snapshots supply partial earlier coverage, flagged.

Families in the champion at launch: momentum, low_risk, quality, value, growth (five); flows joins when covered (six). Why growth is active despite the weakest prior: it is the owner's stated thesis; the honest way to test a thesis is to pre-register it with retirement criteria and let the record decide, not to exclude it and argue.

Legacy factor disposition (all retired at migration with ADRs; values migrated as `legacy_*@0`, never recomputed):

```
quality_score (buckets)        -> roce + accruals + cash_conversion_3y
growth_score (+15% imputed)    -> eps_growth_3y + earn_mom (+ rev_growth_3y shadow); no imputation
valuation_score (DCF MoS, 63% zeros) -> earnings_yield + book_to_price (+ fcf_yield shadow); DCF survives only as a UI explainer "not used in ranking"
risk_score (hand-typed, 85% constant) -> RETIRED (hindsight lists)
moat_score (18 names, 96% constant)   -> RETIRED (hindsight lists); roe_stability_3y is the measurable moat proxy
bs_score (84% constant)        -> leverage (continuous)
cap_alloc_score (unit bug)     -> div_yield shadow
smart_money_score              -> inst_hold_chg_3m (change only; level is a size proxy)
trap_score multiplier          -> RETIRED; its components are continuous inputs above
momentum_multiplier 0/0.8/1.0  -> trend_200 (continuous, weighted) + dc_flag diagnostic
headline sentiment             -> RETIRED; no reliable free source
```

### 5.4 Pre-registration record

Nothing is evaluated against out-of-sample labels before a `hypotheses` row (9.2) and `knowledge/hypotheses/H-YYYY-NNN.md` exist. Template:

```
# H-2026-001  mom_12_1@1 : 12-1 month total-return momentum predicts 3-month sector-relative return
registered_on: 2026-09-30   registered_by: human:<owner>   budget_year: 2026   kind: factor
family: momentum   direction: +1   horizon_m: 3   formula: ln(TRI[t-21]/TRI[t-252]), sector-neutral gaussian rank
first_oos_as_of: 2026-09-30   (first live cohort that counts)
success_criterion: live 3M IC cumulative mean * direction >= +0.02 AND HAC t >= t_crit(m) after >= 12 labelled months; 12M sign check >= 0
failure_criterion: 24-month rolling HAC t <= -1.5, or coverage < 60% for 3 consecutive months
correlation constraint: |Spearman(z, z_k)| <= 0.70 vs every active factor in the same family
prior_evidence: (two honest lines)     backfill prior allowed: yes (price-only)   discount: 0.5
why it might fail in India: ...        what would make me retire it: ...
```

The row freezes formula, direction, horizon, `first_oos_as_of` and the module's git SHA. Changing any of them is a new version and a new hypothesis. A factor whose live sign turns out opposite to its registration is retired, not flipped.

### 5.5 Status lifecycle

```
registered ──first live compute──► shadow ──promotion criteria (9.5), Tier-1/2 decision──► active
   │                                  │                                                    │
   │ withdrawn                        │ failure criteria / 36 months without support        │ 24m rolling t < 0 or coverage < 60% x3
   ▼                                  ▼                                                    ▼
retired ◄─────────────────────────────┴──────────── probation (weight halved within family, 6 months) ──► retired
                                                          └── recovery criteria ──► active
any ──BLOCK-level input contract failure──► quarantined (automatic, reversible by decision; excluded from composites and statistics until released)
```

- `registered`: hypothesis row exists; computed on backfill only if backfillable; never on live cohorts.
- `shadow`: computed and stored every live month; evaluated like an active factor; weight 0 in every model. This is where evidence accumulates.
- `active`: in the champion, family-equal weight.
- `retired`: computed for 24 more months (so "we retired it and then it worked" is measurable), then frozen; values kept forever.
- Every transition is a `decisions` row + ADR. Only `quarantined` is automatic.
- Caps: active ≤ 14, shadow ≤ 12 (config). Beyond that, retire first.

---

## 6. Scoring model & weight learning

### 6.1 Composite (decision D10)

```
for each active stock-level factor f with z_f(i) from 5.2:
  family_score_k(i)   = nanmean over active f in family k of z_f(i)          NaN if every member NaN  (flag 'family_missing')
  composite(i)        = sum_k W_k * family_score_k(i) / sum_{k present} W_k   (renormalised over families present)
  composite_neutral   = gaussian rank of composite within sector_group        (output is again N(0,1) per group)
  final(i)            = (1 - w_sleeve) * composite_neutral(i) + w_sleeve * sector_tilt(G(i))      w_sleeve = 0 for the champion
  scored(i)           = at least 3 families present AND at least 60% of active factors non-NaN ; else score NULL, reason 'coverage'
  rank_all            = rank of final over scored names ; decile, quintile ; rank_group within sector_group
  eligible(i)         = scored AND passes screens (6.4) ; rank = rank over eligible names (NULL if ineligible)
```

No multipliers of any kind. Nothing zeroes a rank. `final = base × trap × momentum` is gone.

### 6.2 Champion: hierarchical equal weight, permanent baseline

```
model_id 'EW_HIER_v1'    W_k = 1 / (number of families with >= 1 active factor)   ; equal within family (implicit in nanmean)
at launch: momentum, low_risk, quality, value, growth -> 0.20 each ; flows joins at 1/6 when inst_hold_chg_3m clears G8 (new model version)
```

Hierarchical, not flat, so adding a third quality factor does not raise the quality share, and a later change in factor count does not silently re-weight families. `EW_HIER_v1` is computed, stored and paper-traded every month for the life of the project and is the reference line on every chart. Two more references are scored monthly, never promotable: `EW_FLAT_v1` (flat 1/|F|; the brief's literal baseline) and `MOM_ONLY_v1` (mom_12_1 + trend_200 only; "is the composite better than plain momentum?").

### 6.3 Challenger: family-level shrunk-IC weights, a pure function of matured evidence (decision D11)

The legacy optimizer was a stateful multiplicative update; that is why it double-applied gradients and pinned growth at 30%. The challenger's weights are recomputed from scratch every month from `evaluations`; there is no state to corrupt and re-running is trivially idempotent.

```python
# quant/model/learn.py
def fit_family_weights(ic_hist: pd.DataFrame,   # index = as_of (monthly), columns = families, values = 3M IC of each family score, ONLY rows with matured labels
                       horizon_m: int = 3, k_shrink: float = 24.0, floor_mult: float = 0.5, cap_mult: float = 2.0,
                       min_n_eff: float = 4.0) -> tuple[dict[str, float], dict]:
    """
    F        = number of families ; n_months = rows whose label is complete (as_of + horizon <= last complete month)
    n_eff    = n_months / horizon_m
    alpha    = n_eff / (n_eff + k_shrink)                # n_eff 4 -> 0.143 ; 12 -> 0.333 ; 24 -> 0.5 ; 48 -> 0.667
    ic_bar_k = mean of column k ; raw_k = max(ic_bar_k, 0) ; raw /= sum(raw)  (if sum == 0: raw = 1/F)   # negative evidence -> floor, never negative weight
    w_k      = (1 - alpha) / F + alpha * raw_k
    if n_eff < min_n_eff: return exact equal weights, diagnostics['gate'] = 'closed'
    project onto { sum == 1, floor_mult/F <= w <= cap_mult/F } (iterative clamp + renormalise, as legacy project_weights)
    round to 4 dp ; rounding residue to the largest weight so sum == 1.0000 exactly
    """
```

Worked example (the unit test pins it): F=6, n_months=12 → n_eff=4 → alpha=0.1429; ic_bar=[0.06,0.02,0.03,−0.01,0.00,0.04] → raw=[0.400,0.133,0.200,0,0,0.267] → w=[0.2000,0.1619,0.1714,0.1429,0.1429,0.1810], bounds [0.0833, 0.3333], none clamped, sum 1.0000.

Rules: the gate opens at `n_eff >= 4` at H=3 (12 labelled months; calendar month 15); until then `IC_SHRUNK_v1 == EW_HIER_v1` by construction and a test asserts it. No decay (with a dozen independent observations, discarding old ones chases regimes nobody can identify in advance). No sign flips. Weights per `as_of` are stored in `model_weights` with `n_eff` and `alpha` so any month's score is reproducible. The legacy `[0.05, 0.30]` band is retired (it was set for eight factors and pinned growth); the new bounds are `[0.5/F, 2/F]`.

### 6.4 Screens that remain, and how screened names are treated

All screens are about tradability and data, never about prediction. Screened names are **scored, stored, labelled and evaluated** as their own cohort every month, next to the eligible universe, so a screen's cost is measured.

```
screen        rule                                                               effect
liquidity     adv_63_inr < Rs 2 crore OR traded on < 54 of the last 63 sessions   eligible = 0, reason 'illiquid'
series        Series != 'EQ' in the constituent file (trade-to-trade segments)     eligible = 0, reason 'series'
coverage      fewer than 3 families present or < 60% of active factors non-NaN     score NULL, reason 'coverage' (unscored cohort)
sector        sector_group == 'UNCLASSIFIED'                                       eligible = 0, reason 'sector'
history       fewer than 200 trading days of prices                                momentum/low_risk NaN (flag); scored if coverage passes
```

The death cross: retired as a filter and as a multiplier (ADR at migration). Its information is carried by `trend_200` (continuous, weighted, learnable). `dc_flag` is stored and the "would-have-been-killed vs rest" cohort return is printed every month for 24 months so the June-2026 lesson (killed names were the best performers) can never go unnoticed. The "Turnaround interceptor" becomes a saved UI view (`growth top quintile ∩ negative FCF`), not a scoring path; its cohort return is reported monthly.

### 6.5 Models table, champion/challenger, promotion

```sql
CREATE TABLE models (
  model_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('equal','shrink','overlay','reference','legacy')),
  role TEXT NOT NULL CHECK (role IN ('champion','challenger','reference','legacy','retired')),
  description TEXT NOT NULL, params_json TEXT NOT NULL, hypothesis_id TEXT, registered_on TEXT NOT NULL, decision_id TEXT
);
CREATE TABLE model_versions (
  model_id TEXT NOT NULL REFERENCES models, version INTEGER NOT NULL,
  factor_set_json TEXT NOT NULL,              -- [{"factor_id":"mom_12_1@1","family":"momentum"}, ...] active at valid_from
  weights_json TEXT NOT NULL,                 -- {"family": {"momentum": 0.2, ...}, "sleeve": 0.0}
  valid_from TEXT NOT NULL, valid_to TEXT, decision_id TEXT, note TEXT, PRIMARY KEY (model_id, version)
);
CREATE TABLE model_weights (                  -- the vector actually used for each as_of (challenger refits monthly)
  model_id TEXT NOT NULL, as_of TEXT NOT NULL, family TEXT NOT NULL, weight REAL NOT NULL, n_eff REAL, alpha REAL, gate TEXT, run_id INTEGER NOT NULL,
  PRIMARY KEY (model_id, as_of, family)
);
CREATE TABLE scores (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL, model_id TEXT NOT NULL, model_version INTEGER NOT NULL,
  sector_group TEXT NOT NULL, group_def_version INTEGER NOT NULL,
  family_scores_json TEXT NOT NULL, composite REAL, composite_neutral REAL, sector_tilt REAL, final REAL,
  rank_all INTEGER, rank INTEGER, rank_group INTEGER, decile INTEGER, quintile INTEGER,
  scored INTEGER NOT NULL, eligible INTEGER NOT NULL, exclusion_reason TEXT, liquidity_bucket TEXT, n_factors_used INTEGER NOT NULL, dc_flag INTEGER,
  input_hash TEXT NOT NULL,                   -- sha256 of the z-vector used (gate G9)
  track TEXT NOT NULL DEFAULT 'live', run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, security_id, model_id, track)
);
```

Launch set:

```
EW_HIER_v1        champion    permanent baseline                                                        from M1
EW_FLAT_v1        reference   flat equal weight over active factors                                     from M1
MOM_ONLY_v1       reference   momentum family only                                                      from M1
IC_SHRUNK_v1      challenger  6.3, H=3, K=24 ; identical to champion until the gate opens                from M1 (scaffold), paper from M3
SECTOR_OVERLAY_v1 challenger  champion + sleeve (3.5) with w_sleeve = 0.10 ; separate hypothesis          from M6
LEGACY_V18        legacy      migrated final_score ; and LEGACY_V18_BASE = base/composite                migration only
```

Cap: at most 3 live challengers. Promotion of a challenger to champion (Tier-2 human decision; criteria evaluated by `quant model review`):

```
P1  >= 24 monthly paired OOS 3M ICs (challenger and champion on the same dates, live track)
P2  paired difference HAC t >= t_crit(m_models) where t_crit(m) = max(2.0, Phi^-1(1 - 0.05/m)), m = challengers ever registered
P3  net-of-cost paper return of challenger >= champion over the same window
P4  12M IC of challenger >= champion where >= 3 monthly 12M labels exist (sign check only)
P5  challenger one-way turnover <= 1.5 x champion ; no gate overrides in the window
```

After promotion the old champion keeps being scored and paper-traded forever; a champion that loses to `EW_HIER_v1` under P1–P2 (with EW as the challenger) reverts. Equal weight is a permanent floor.

### 6.6 Invariants (replace the legacy 5%–30% rule; asserted by `quant model check` on every stored version)

```
I1  family weights sum to 1.0000 exactly (4 dp; residue to the largest)
I2  within-family weights sum to 1 per family (implicit for nanmean; explicit for any future within-family learner)
I3  every family weight in [0.5/F, 2/F] ; sleeve weight in [0, 0.20]
I4  weight = 0 for any factor not 'active' in that model version ; direction fixed by the registry (no learned sign flips)
I5  challenger weights are written to model_weights every month but applied to the live ranking only via a promotion decision
I6  every scores row carries model_id, model_version, group_def_version, input_hash ; history is append-only
```

---

## 7. Evaluation protocol

### 7.1 What is evaluated every month

For every `as_of` whose horizon-*h* labels matured this month, for every subject (every factor with status ≠ registered, every family score, every model's `final`, `dc_flag`, sector features, screened cohorts), for scopes `eligible` and `all`, for each track:

```
ic              Spearman( subject value at as_of , L_h ) over the scope           (sector-neutral by construction of L_h and z)
ic_uni          same vs r_uni                                                     (stored; diagnostic)
quintiles       formed WITHIN sector_group then pooled: mean/median/trimmed arithmetic TR by quintile ; spread_q5_q1
deciles         decile 10 and 1 mean TR ; turnover of decile membership vs previous as_of
spread_net      long-only: mean TR(top quintile) - mean TR(EW eligible) - cost_drag   (7.6)
hit_rate        share of top-decile names with L_h > 0
cohorts         mean TR of eligible vs illiquid / unscored / sector / series cohorts ; dc_flag = 1 vs 0
partial_ic      for shadow factors: IC of z residualised on the active factors' z (does it add anything?)
corr_matrix     Spearman among active + shadow z at as_of (redundancy rule)
fm_slope        Fama–MacBeth slope of L_h on z with size control (regression variant; reported, not decisive)
```

```sql
CREATE TABLE labels (
  as_of TEXT NOT NULL, security_id INTEGER NOT NULL, horizon_m INTEGER NOT NULL, end_date TEXT NOT NULL,
  r_log REAL, r_arith REAL, r_group_median REAL, l_rel REAL, r_uni REAL, sector_group TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','delisted_partial','suspended','excluded_ca','missing')),
  mb36 INTEGER, mb36_touch INTEGER, price_manifest_sha TEXT, computed_run_id INTEGER NOT NULL,
  PRIMARY KEY (as_of, security_id, horizon_m)
);
CREATE TABLE evaluations (
  eval_id INTEGER PRIMARY KEY, computed_run_id INTEGER NOT NULL,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('factor','family','model','feature','cohort','flag','benchmark')),
  subject_id TEXT NOT NULL, as_of TEXT NOT NULL, horizon_m INTEGER NOT NULL, scope TEXT NOT NULL, track TEXT NOT NULL,
  metric TEXT NOT NULL, value REAL, n INTEGER, se REAL, method TEXT,                     -- method: 'spearman'|'hac_l2'|'bootstrap_b3'|'naive'
  window_start TEXT, window_end TEXT,                                                    -- NULL for per-month rows; set for rolling/cumulative rows
  UNIQUE (subject_kind, subject_id, as_of, horizon_m, scope, track, metric, method, window_start, window_end)
);
CREATE TABLE evaluations_log (log_id INTEGER PRIMARY KEY, eval_key TEXT NOT NULL, old_value REAL, new_value REAL, changed_at TEXT NOT NULL, reason TEXT);
```

Evaluations are idempotent (same key replaces) and every change of a stored number is logged in `evaluations_log` with a reason. Statistics are recomputed from scratch each month; incremental state is a bug factory.

### 7.2 Walk-forward with embargo

```
   training set for a decision at month T   = { (as_of s, L_h(s)) : s + h <= T }        embargo = h ; no return window may end after T
   test point                                = the score produced AT T with those weights ; realised at T + h
   quant/evaluation/walkforward.py::labels_available(T, h) -> list[as_of]   is the single implementation; every learner calls it
   property test: no training return window overlaps the test date
```

Because scores are stored monthly as produced, the walk-forward series of a learned model **is** its `evaluations` rows; there is no separate backtest code path that can drift from production. For the backfill track a replay function steps `as_of` monthly through 2016–2026 with the same code.

### 7.3 Overlap-aware statistics (`quant/evaluation/stats.py`, numpy only; statsmodels is not available)

```
monthly IC series x_1..x_N at horizon h months ; lag L = h - 1  (0 for 1M, 2 for 3M, 5 for 6M, 11 for 12M)
xbar    = mean(x)
gamma_j = (1/N) * sum_{t=j+1..N} (x_t - xbar)(x_{t-j} - xbar)
S       = gamma_0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gamma_j          Bartlett kernel
se_hac  = sqrt(S / N) ;  t_hac = xbar / se_hac ;  ci90 = xbar +- 1.645 * se_hac
n_eff   = N / h                                                       reported next to every mean
block bootstrap  90% CI, circular moving blocks of length h, 1,000 resamples (reported when N >= 3h)
icir    = xbar / sd(x)   displayed only when n_eff >= 6, else printed as 'n/a (n_eff = k)'
naive t = xbar / (sd(x)/sqrt(N))   stored labelled 'naive' so the gap is visible ; never quoted
t_crit(m) = max(2.0, Phi^-1(1 - 0.05 / m))     m = hypotheses registered against this horizon in the trailing 24 months (9.4)
```

The legacy statistic `ic * sqrt((n-2)/(1-ic^2))` with n = 500 stocks must not appear anywhere in V2 output: 500 correlated stocks are not 500 observations. Sector-demeaning the label removes the largest common component; the time-series HAC t over months is the statistic of record. Unit tests: i.i.d. noise → `se_hac ≈ se_naive` within 15%; an h-month moving sum of white noise → `se_hac/se_naive` within 25% of `sqrt(h)`; constant series → flagged, no division by zero; `t_crit(6) == 2.394`.

### 7.4 Benchmarks (all built from the engine's own TRI so the basis matches the paper portfolios)

```
BM_EW          equal-weight TR of the eligible universe at as_of, held h                 PRIMARY null for a rank composite
BM_EW_SECTOR   EW within each sector_group, groups weighted as in the paper portfolio      isolates stock selection from sector tilt
BM_CW          mcap-weighted TR of the universe                                            what an index fund earns
BM_N500PR      ^CRSLDX price index (+1.2%/yr dividend accrual as 'N500_TR_PROXY', labelled) external sanity check
BM_MOM30_C     EW TR of the current Nifty200 Momentum 30 constituents (monthly file), rebalanced when the list changes   PIT from month 1
BM_QUAL30_C    EW TR of the current Nifty200 Quality 30 constituents, same method
BM_MOM30_ETF / BM_QUAL30_ETF / BM_LOWVOL_ETF / BM_N50_ETF / BM_MID150_ETF   ETF adjusted closes (short histories; tracking error; cross-checks only)
NULL_RANDOM    1,000 Dirichlet(1) random family-weight composites ; the champion's IC percentile within them (target 1.2)
NULL_BEST      best single active factor OOS ; the composite should not be worse than its best member
```

"Nifty 500 Quality 50" is not an NSE index; the free constituent files are for Nifty200 Momentum 30 and Nifty200 Quality 30. Neither replicated benchmark has free historical constituents, so the backfill track compares against `BM_EW` and `^CRSLDX` only.

### 7.5 Leakage tests (pytest on synthetic data in CI; the same functions on real data every month as G10 and `quant verify leakage`)

```
T1 shuffle            permute L_h across securities within each as_of for every factor and model -> |mean IC| < 2 se_hac ; shuffled p-value reported
T2 planted signal     replace one shadow factor's z by rho*gauss_rank(L_3) + sqrt(1-rho^2)*noise, rho = 0.10, through the FULL pipeline -> recovered IC in [0.07, 0.13];
                      then assert production REFUSES it (its compute reads forward data; FactorInputs has none -> KeyError)
T3 PIT truncation     compute factor_values at as_of on a DB physically truncated to rows with date/available_from/captured_at <= as_of -> bit-identical to stored
T4 as_of boundary     SQL: no factor_values row at as_of has an input with available_from > as_of (provenance columns) ; no daily row > as_of in FactorInputs
T5 embargo            give the learner an ic_hist row whose label is incomplete -> ignored (labels_available test)
T6 forward shift      shift every fundamentals.available_from by -90 days (illegal earlier knowledge) and recompute 3M ICs of fundamental factors:
                      any factor whose IC RISES by > 0.02 was already leaking (or the lag rule is too conservative) -> investigate
T7 corporate action   inject a synthetic 6:1 split and a Rs 100 dividend into a copy of one security's raw series: TRI return unchanged to 1e-6;
                      the reconciler flags the un-adjusted copy
T8 holdings lag       scores at as_of never read a holdings capture with captured_at > as_of
T9 survivorship       securities evaluated at s == universe_membership(s) including later-delisted names
T10 sector leak       IC of sector_group dummies alone vs L_h ~ 0 (neutralisation sanity)
```

A failure on real data writes a BLOCK event and stops the run.

### 7.6 Cost-adjusted spreads

```
spread_net(h) = mean TR(top quintile, eligible) - mean TR(BM_EW) - cost_drag(h)
cost_drag(h)  = measured one-way turnover into the top quintile over h (from stored membership changes, NOT an assumed 100%) x round-trip cost by bucket (8.3)
reported at cost multipliers 0.5x / 1x / 2x ; a spread positive only at 0.5x is labelled "not robust to costs"
```

### 7.7 Learning-curve measurement (decision D24)

```sql
CREATE TABLE evidence_curve (            -- cumulative statistics of a FIXED subject vs clean months
  computed_at TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, horizon_m INTEGER NOT NULL, track TEXT NOT NULL,
  months_clean INTEGER NOT NULL, n_labelled INTEGER NOT NULL, n_eff REAL NOT NULL,
  ic_cum_mean REAL, ic_hac_se REAL, ic_hac_t REAL, ic_boot_lo90 REAL, ic_boot_hi90 REAL, cusum_ic REAL, spread_net_cum REAL, slope_24 REAL,
  PRIMARY KEY (computed_at, subject_kind, subject_id, horizon_m, track)
);
CREATE TABLE learning_curve_points (     -- OOS IC of the RE-FITTED model vs the EW baseline at the same k
  model_id TEXT NOT NULL, horizon_m INTEGER NOT NULL, track TEXT NOT NULL, k INTEGER NOT NULL,   -- k = clean months of training data
  train_end TEXT NOT NULL, test_as_of TEXT NOT NULL, realised_as_of TEXT NOT NULL,
  weights_json TEXT NOT NULL, oos_ic REAL NOT NULL, ew_oos_ic REAL NOT NULL, n INTEGER NOT NULL, computed_run_id INTEGER NOT NULL,
  PRIMARY KEY (model_id, horizon_m, track, k)
);
```

```
months_clean(as_of) = number of live runs with dq_status 'passed' and as_of' <= as_of   (a blocked month does not advance the x-axis)
Chart, panel A   x = months_clean ; y = ic_cum_mean at H=3 with 90% band : EW_HIER_v1 (thick), each family (thin), shadow factors (dashed),
                 backfill segment shaded at x < 0, legacy months as hollow points, vertical lines at every applied decision,
                 horizontal band = 25th–75th percentile of NULL_RANDOM
panel B          same at H=12 (starts month 13)
panel C          CI half-width vs months (precision curve) and CUSUM of monthly IC
inset            (IC_SHRUNK_v1 - EW_HIER_v1) trailing-12 mean with HAC t  <- this inset IS the learning claim
rule             a point is plotted only when its label is realised ; no IC is ever rendered without n_eff and a band (ui_export refuses)
```

Cumulative mean IC is a convergence plot; it cannot rise indefinitely. "Predictability increases" therefore means: the estimate converges to a positive value, its band excludes zero, and factor retirements / data fixes / weight shrinkage show as annotated step changes. A band narrowing around zero is a clean negative result, not a failure of the system.

---

## 8. Portfolio & cost model

### 8.1 Paper portfolios (one per model; identical rules so differences are attributable to the ranking)

```
portfolio_id         model            rule                                                                              rebalance
PF_<model>_TOP30     every model      top 30 by rank among eligible ; equal weight at entry ; sector cap 6 of 30 (20%) ;    monthly review,
                                      buffer: a holding is kept while rank <= 60, sold above 60 or when ineligible ;       buffer-limited trades
                                      vacancies filled by the highest-ranked non-holding
PF_<model>_DEC10     every model      top decile among eligible, equal weight, no buffer (pure signal)                     monthly
PF_<model>_TOP30_Q   champion only    three tranches, each rebalanced every third month (quarterly variant)                 quarterly per tranche
PF_BM_EW             benchmark        all eligible names, equal weight, same cost model                                     monthly
```

Execution: trades at the close of `as_of + 1` trading day (no same-bar execution); cash from sells redeployed the same day; dividends reinvested in the paying stock (consistent with TRI); between rebalances weights drift; residual cash earns 0. Delisting: position marked at last price then cash, flagged. Notional assumption `config: portfolio.notional_inr = 5,000,000` (drives cost tiers; the report also prints the largest notional at which every holding stays under 2% of its 60-day ADV).

```sql
CREATE TABLE portfolios (portfolio_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, rule TEXT NOT NULL, cadence TEXT NOT NULL, inception TEXT NOT NULL, rule_version TEXT NOT NULL);
CREATE TABLE portfolio_positions (portfolio_id TEXT NOT NULL, as_of TEXT NOT NULL, security_id INTEGER NOT NULL, weight REAL NOT NULL,
  entry_as_of TEXT NOT NULL, rank_at_entry INTEGER, liquidity_bucket TEXT, PRIMARY KEY (portfolio_id, as_of, security_id));
CREATE TABLE portfolio_trades (trade_id INTEGER PRIMARY KEY, portfolio_id TEXT NOT NULL, as_of TEXT NOT NULL, exec_date TEXT NOT NULL,
  security_id INTEGER NOT NULL, side TEXT NOT NULL, weight_delta REAL NOT NULL, cost_bps REAL NOT NULL, liquidity_bucket TEXT NOT NULL);
CREATE TABLE portfolio_returns (portfolio_id TEXT NOT NULL, month_end TEXT NOT NULL, ret_gross REAL, turnover_one_way REAL, cost REAL, ret_net REAL,
  ret_net_stress REAL, bm_ew REAL, bm_ew_sector REAL, bm_cw REAL, bm_index REAL, n_positions INTEGER, cost_model_version TEXT NOT NULL,
  PRIMARY KEY (portfolio_id, month_end));
```

### 8.2 Liquidity buckets (from `prices_monthly.adv_63_inr` = trailing 63-day mean of close_raw × volume)

```
bucket   ADV_63                 eligible   impact assumption (one-way bp)
A        >= Rs 50 crore         yes        10
B        Rs 10 – 50 crore       yes        25
C        Rs 2 – 10 crore        yes        50   (position cap 2% of portfolio)
D        <  Rs 2 crore          NO         --   (scored, evaluated as the 'illiquid' cohort, never held)
```

The report prints the actual bucket distribution monthly; thresholds are decision-gated. An optional bhavcopy adapter (`nsearchives.nseindia.com sec_bhavdata_full_*.csv`) can later replace Yahoo volume with exchange turnover and delivery percentage; not a month-1 dependency.

### 8.3 Cost stack (delivery equity, NSE, discount broker, September 2026; stated as assumptions in `config/costs_v1.toml`)

```
component                                buy (bp)   sell (bp)
STT (delivery)                           10.0       10.0
stamp duty                                1.5        --
exchange txn + SEBI + GST on charges      ~0.4       ~0.4
brokerage                                 0 (configurable)
statutory subtotal one-way               ~12
impact by bucket                          A 10 | B 25 | C 50
TOTAL one-way                             A 22 | B 37 | C 62      round trip  A 44 | B 74 | C 124
stress                                    all x 1.5, stored as ret_net_stress
```

`cost(month) = Σ over trades |Δw| × total_one_way(bucket at trade time)`. Every `portfolio_returns` row stores `cost_model_version`; changing the table is a Tier-1 decision within ±25%, Tier-2 beyond. Annual drag estimate at 80% one-way turnover in a B/C book ≈ 0.7%/yr: the model lives or dies on whether its 12-month spread exceeds that, which is why the cost model ships in month 3, not month 12.

### 8.4 Alpha scoreboard (the only rows allowed to use the word "alpha")

```
row                        definition (net of 8.3 costs, from portfolio_returns)                  isolates
excess_vs_ew               ret_net(PF_champion_TOP30) - ret(PF_BM_EW)                            PRIMARY: selection vs an EW holder of the same universe
excess_vs_ew_sector        ret_net - bm_ew_sector                                                 stock selection net of sector tilt (what a sector-neutral model is accountable for)
excess_vs_cw               ret_net - bm_cw                                                        "did it beat the market"
excess_vs_mom30 / qual30   ret_net - BM_MOM30_C / BM_QUAL30_C                                     "better than buying the factor index?"
tracking error, IR         sd(monthly excess) * sqrt(12) ; annualised mean excess / TE   (IR printed only with n >= 24 months)
hac_t                      of the monthly excess series (lag 0: monthly returns do not overlap)
max drawdown, hit rate, turnover_1y, cost_drag_1y
years_to_significance      (2 / IR)^2 years at the current IR  (IR 0.5 -> 16 years; 1.0 -> 4 years)  shown so nobody sizes real money on 18 months of paper
honesty row                PF_EW_HIER_v1 always shown next to whatever the champion is
verdict word               'insufficient' (< 24 months) | 'weak positive' | 'positive' (IR > 0.5 and t >= 2) | 'negative'
```

The report generator enforces the rule: a row may be called alpha only when n ≥ 24 months and HAC t ≥ 2.0; otherwise it prints "excess return (not yet distinguishable from zero)".

### 8.5 Crowding and concentration monitors (report only; no automatic action)

Overlap of champion holdings with the current Momentum 30 and Quality 30 constituent files; median earnings yield of the top decile vs universe (are we buying what everyone already bought); group weights vs universe shares; top-10 concentration; share of sells held < 12 months (STCG exposure; India taxes short-term equity gains at 20% vs 12.5% long-term, which points the same way as the 12-month thesis horizon: rank for 12 months, hold with a buffer, trade little).

---

## 9. Feedback loop, knowledge base & governance

### 9.1 The monthly loop (`python -m quant run monthly --as-of <D>`)

```
  first 1–7 days of the month, IST                                                        human / LLM in the approval seat
  ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
   1 universe      fetch 3 constituent CSVs -> data/universe/ ; securities, symbol_history, universe_membership, sector_map   auto
   2 prices        yf.download trailing 13 months (25/batch, 1 s) -> prices_daily ; reconcile ; TRI ; prices_monthly ; benchmarks  auto
   3 fundamentals  per-security pulls (0.5 s) -> raw archive ; fundamentals ; holdings ; security_attributes  [~30 min]         auto
   4 gates         G1–G10, W1–W6 -> dq_runs, data_quality_events ; BLOCKED -> runs.status='blocked', report written, exit 2      auto
   5 factors       FactorInputs(as_of) -> factor_values for every factor with status in (shadow, active, probation, retired<24m, control)  auto
   6 score         every model with role != retired -> scores, model_weights (challenger refit under embargo)                    auto
   7 labels        mature L_h for every earlier as_of whose end month now has prices ; mb36 for 36M                             auto
   8 evaluate      evaluations, evidence_curve, learning_curve_points, benchmark rows, cohorts ; leakage T1–T10 on real data      auto
   9 portfolio     rebalance every PF_* ; portfolio_returns ; scoreboard                                                        auto
  10 review        factors review + model review -> criteria_check_json per candidate                                           auto
  11 propose       rule-based proposals (status 'proposed') + hypothesis drafts the rules recommend                              auto
  12 record        knowledge/reports/YYYY-MM.md ; knowledge/proposals/YYYY-MM.md ; lessons ; ui/data*.js ; ledger export ;      auto
                   quant.db VACUUM ; git commit "quant: monthly run YYYY-MM (as_of D)"   (push only with --push)
  ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  13 approve       python -m quant kb approve|reject P-YYYY-MM-NN --by human:<name>|llm:<model> --note "..."                     manual
  14 apply         python -m quant kb apply  -> decisions applied EFFECTIVE FROM THE NEXT as_of (never retroactive) ;            manual
                   model_versions / factor_registry updated ; ADR written ; git commit "quant: decisions YYYY-MM"
```

Steps 1–12 change no factor status and no weight rule on their own; step 6 uses what was already approved; step 11 only writes proposals. **The loop may measure and suggest; only a decision row may change what the loop does next.** Time budget: step 3 ≈ 30 min, everything else < 5 min. `--skip-fundamentals` reuses the month's snapshot for re-runs; `--stop-after STEP` and idempotent upserts make the run resumable. A skipped month is legal: price factors, labels and benchmarks rebuild point-in-time from the daily store; fundamental factors for a never-snapshotted month are written from the latest earlier observation with `stale_days` recorded and the month marked not-clean.

### 9.2 Knowledge-base tables

```sql
CREATE TABLE runs (
  run_id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('monthly','backfill','migrate','evaluate','adhoc')),
  track TEXT NOT NULL DEFAULT 'live', started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','partial','blocked','failed')),
  dq_status TEXT CHECK (dq_status IN ('passed','passed_with_warnings','blocked','legacy_defects')),
  git_sha TEXT NOT NULL, code_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL, registry_sha256 TEXT NOT NULL,
  yfinance_version TEXT, python_version TEXT, n_universe INTEGER, n_scored INTEGER, n_eligible INTEGER,
  http_calls INTEGER, http_429s INTEGER, override_decision_id TEXT, is_clean INTEGER, notes_json TEXT
);
CREATE UNIQUE INDEX ux_runs_asof_kind_track ON runs (as_of, kind, track);

CREATE TABLE hypotheses (
  hypothesis_id TEXT PRIMARY KEY,                    -- 'H-2026-001'
  kind TEXT NOT NULL CHECK (kind IN ('factor','model','rule','sector_feature','data','cost_model')),
  subject_id TEXT NOT NULL, title TEXT NOT NULL, statement TEXT NOT NULL,
  expected_sign INTEGER CHECK (expected_sign IN (-1,1)), horizon_m INTEGER, primary_metric TEXT NOT NULL,
  success_criterion TEXT NOT NULL, failure_criterion TEXT NOT NULL,
  registered_on TEXT NOT NULL, registered_by TEXT NOT NULL,   -- 'human:<name>' | 'llm:<model>' | 'system'
  first_oos_as_of TEXT NOT NULL, code_sha TEXT, budget_year INTEGER NOT NULL, sequence_in_year INTEGER NOT NULL,
  counts_toward_budget INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('open','supported','rejected','withdrawn','inconclusive')),
  n_periods_at_eval INTEGER, t_hac_at_eval REAL, t_crit_at_eval REAL, m_tests_at_eval INTEGER,
  resolved_on TEXT, resolution TEXT, decision_id TEXT, md_path TEXT NOT NULL
);
CREATE TABLE experiments (
  experiment_id TEXT PRIMARY KEY,                    -- 'X-2027-004'
  hypothesis_id TEXT REFERENCES hypotheses, run_id INTEGER REFERENCES runs,
  kind TEXT NOT NULL CHECK (kind IN ('shadow_eval','walk_forward','ablation','leakage','cost_calib','rescoring','backfill_dev','backfill_holdout')),
  config_json TEXT NOT NULL, code_sha TEXT NOT NULL, track TEXT NOT NULL, window_start TEXT, window_end TEXT,
  started_on TEXT NOT NULL, finished_on TEXT, result_json TEXT, verdict TEXT CHECK (verdict IN ('pass','fail','inconclusive')),
  counts_toward_budget INTEGER NOT NULL DEFAULT 0, md_path TEXT
);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY,                      -- 'P-2026-11-01'
  created_run_id INTEGER NOT NULL REFERENCES runs, as_of TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('promote_factor','probation','retire_factor','quarantine','release_quarantine','promote_model','demote_model',
                                     'register_hypothesis','rule_change','cost_model','taxonomy','data_fix','clear_ca_flag','accept_revision','other')),
  subject_id TEXT NOT NULL, payload_json TEXT NOT NULL, evidence_json TEXT NOT NULL, rule_id TEXT NOT NULL,
  criteria_check_json TEXT, proposed_by TEXT NOT NULL, llm_review TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','expired','superseded')),
  decided_on TEXT, decided_by TEXT, decision_id TEXT, md_path TEXT NOT NULL
);
CREATE TABLE decisions (                             -- append-only
  decision_id TEXT PRIMARY KEY,                      -- 'D-2026-11-01'
  proposal_id TEXT REFERENCES proposals, kind TEXT NOT NULL, tier INTEGER NOT NULL CHECK (tier IN (0,1,2)),
  subject_id TEXT NOT NULL, title TEXT NOT NULL, context TEXT NOT NULL, options_json TEXT NOT NULL, decision TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL, criteria_check_json TEXT,
  decided_on TEXT NOT NULL, decided_by TEXT NOT NULL, approver_kind TEXT NOT NULL CHECK (approver_kind IN ('human','llm','system')),
  ratified_by TEXT, ratified_on TEXT,                -- human co-signature required for LLM Tier-1 decisions within 60 days
  status TEXT NOT NULL CHECK (status IN ('approved','rejected','provisional','applied','superseded','reverted')),
  effective_from TEXT, applied_on TEXT, adr_path TEXT NOT NULL, supersedes TEXT, reverted_by TEXT, git_sha TEXT NOT NULL
);
CREATE TABLE lessons (lesson_id INTEGER PRIMARY KEY, recorded_on TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, evidence_refs_json TEXT, decision_id TEXT, tags TEXT);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT);
```

### 9.3 Human-readable records (`knowledge/`; markdown is generated FROM the tables, never the other way round)

```
knowledge/
  README.md                          how to read this folder in five minutes; the lifecycle diagram; the budget rule
  hypotheses/H-YYYY-NNN.md           pre-registration records (template 5.4)
  decisions/D-YYYY-MM-NN-<slug>.md   one ADR per decision (template below)
  proposals/YYYY-MM.md               the month's proposals in prose with evidence tables and the LLM first-reader checklist
  reports/YYYY-MM.md                 the auto-generated monthly report (9.7)
  reports/learning_curve_YYYY-MM.json
  experiments/X-YYYY-NNN.md          experiment write-ups
  lessons.md                         append-only ledger: one dated line per lesson, linked to an ADR or event
  db/*.jsonl                         one line per row of every knowledge table (git-friendly mirror; rebuildable)
```

ADR template:

```
# D-2026-11-01 — <title>
Status: approved | rejected | provisional | superseded by D-...     Tier: 0|1|2
Proposal: P-2026-11-01     Decided by: human:<name>     Ratified by: --     Effective from: 2026-12-31 (always the NEXT as_of)     Git: <sha>
## Context            what the evidence showed (paste the evaluation rows with n_eff and HAC t)
## Options considered
## Decision           one sentence; exact status / parameter change
## Evidence           eval_ids / experiment_ids / hypothesis_ids
## Criteria check     verbatim output of `quant factors review` / `quant model review`
## Pre-registered consequence   what we now expect to observe, by when, and what would reverse this decision
## Revert             the one command that undoes it and what history it leaves
```

### 9.4 Multiple-testing control

```
budget            <= 6 hypotheses per calendar year that consume out-of-sample labels (counts_toward_budget = 1) ; <= 3 per family per year
                  ablations, leakage tests, cost calibrations, data fixes and re-evaluations after fixes do not count
                  the CLI refuses the 7th (override = Tier-2 decision, itself logged) ; withdrawn hypotheses keep their sequence number
threshold         t_crit(m) = max(2.0, Phi^-1(1 - 0.05 / m)), m = hypotheses registered against the same horizon in the trailing 24 months
                  m=1 2.00 | m=3 2.13 | m=6 2.39 | m=12 2.64 | m=24 2.87       (scipy.stats.norm.ppf; a test pins these)
recorded          hypotheses.m_tests_at_eval and t_crit_at_eval are stored at evaluation time so the bar cannot move later
haircut           the report prints the expected best-of-m IC under the null, se_hac * sqrt(2 ln m), next to the best shadow factor's IC
no peeking        proposals fire only at the pre-registered review month, generated by the rules, never by a human noticing a good month
annual review     Benjamini–Hochberg at FDR 10% across the year's hypothesis p-values; anything active that fails goes to probation
challengers       <= 3 live at any time ; promotion threshold uses m = challengers ever registered
```

Expected false activations over five years at 6 hypotheses/year with t ≥ 2.39: `30 × 0.008 ≈ 0.25`. The budget is what keeps that arithmetic true.

### 9.5 Promotion, probation, retirement (all on the live track; criteria evaluated by `quant factors review` every run)

```
shadow -> active         >= 12 monthly labelled 3M evaluations with as_of >= first_oos_as_of  (h=12 factors: >= 12 labelled 3M periods AND the 12M sign check)
                         mean IC_3 * direction >= +0.02 AND HAC t >= t_crit(m)
                         IC_3 has the expected sign in >= 60% of months
                         12M sign check: where >= 3 monthly 12M labels exist, mean IC_12 * direction >= 0
                         partial IC vs current active set HAC t >= 1.5 (adds something new)
                         |Spearman(z_new, z_k)| <= 0.70 vs every active factor in the same family (latest as_of)
                         coverage >= 80% of eligible (non-financial if applicable) universe over the last 3 runs
                         net top-quintile spread at 3M > 0 over the evaluation window
                         ablation: adding it at equal within-family weight does not reduce the champion's cumulative IC_3 by more than 0.005
active -> probation      24-month rolling mean IC_3 * direction < 0 with HAC t <= -1.0 ; OR coverage < 80% for 3 consecutive months ; OR |corr| > 0.85 with another active factor for 6 months
probation -> active      24-month rolling HAC t >= 0 for 6 consecutive reviews
probation -> retired     6 months in probation without recovery ; Tier-1 decision
shadow -> retired        36 months without reaching 'supported' ; or withdrawn
any -> quarantined       automatic on a BLOCK-level contract failure of an input or W3 near-constant for 3 months ; released by decision
version bump             formula/input change -> new factor_id as 'registered' ; old id runs alongside 6 more months ; paired difference reported before the old retires
sector sleeve            w_sleeve 0 -> 0.10 requires the sleeve's own P1–P3 at h=3 plus SECTOR_OVERLAY_v1 beating the champion net of cost over 24 months ; 0.10 -> 0.20 same again
model promotion          6.5 P1–P5
```

### 9.6 Approval protocol (tiers; enforced by the CLI, not by convention)

```
Tier 0  automatic, logged as decisions(tier=0, approver_kind='system')
        ingest, gates, factor and score computation with approved definitions, labels, evaluations, paper portfolios, reports, UI export,
        the challenger's monthly weight refit (a pure function of stored data, not a judgement), quarantine on contract failure
Tier 1  proposed by the rules -> an LLM may approve (approver_kind='llm', status='provisional') -> a human ratifies within 60 days or it auto-reverts at the next run
        register_hypothesis within budget ; shadow->active, active->probation, probation->retired when EVERY criterion is met (criteria_check_json contains no false) ;
        clear_ca_flag / accept_revision / data_fix with evidence attached ; release_quarantine ; cost-model recalibration within +-25%
Tier 2  human only ; an LLM may draft the ADR but cannot approve
        promote_model / demote_model ; any change to a pre-registered rule or threshold (K, H, gates, budget, cost buckets beyond +-25%, taxonomy version, screens) ;
        overriding a blocked run ; activating a factor that fails any criterion ; budget override ; schema migration ;
        anything that edits or deletes stored exposures, scores, labels or evaluations (which the CLI does not expose at all)
```

`quant kb approve` refuses an LLM approval of a Tier-2 kind, and refuses a Tier-1 approval whose `criteria_check_json` contains any `false`. The next run refuses to instantiate a model version whose creating decision is `provisional` past its ratification deadline. The LLM's designed role is *first reader*: it appends a checklist review to each proposal (evidence sufficient? threshold met? correlation? cost effect? what could be wrong?). The report prints the share of decisions taken by LLM vs human so rubber-stamping is visible.

### 9.7 Monthly report (`knowledge/reports/YYYY-MM.md`, auto-generated, at most one page of prose plus tables)

```
> Verdict line: dq passed|blocked ; months_clean = N ; champion 3M IC cum = x (HAC t y, CI) ; challenger - EW = z ; PF excess vs EW YTD = a% net ; proposals pending = k
1  What you need to do            at most three items (open proposals, CA flags to confirm, ratifications due)
2  Run & data quality             gates table verbatim ; flag shares ; universe changes ; sector reclassifications ; imputed share per factor ; yfinance version
3  Labels matured this month      which as_of x horizon completed ; delisted / excluded counts
4  Statistics (live)              per factor / family / model x horizon: mean IC, HAC se, t, n, n_eff, CI90, hit rate   (backfill and legacy in separate tables with caveat lines)
5  Nulls                          random-composite percentile ; best single factor
6  Quintiles & spreads            gross and net at 3M and 12M ; monotonicity ; cost sensitivity 0.5x/1x/2x
7  Learning curve                 the numbers behind panels A–C and the inset ; slope with CI
8  Portfolio & scoreboard         8.4 rows with the verdict word ; turnover ; cost drag ; years to significance ; crowding monitors
9  Cohorts                        illiquid / unscored / dc_flag / turnaround-view returns vs eligible
10 Review & proposals             criteria met/unmet per candidate ; proposals with rule ids ; hypotheses YTD k/6 and t_crit in force
11 Lessons                        anything the run flagged that a human should remember
12 Reproduce                      run_id, git sha, code/config/registry hashes, command line ; footer: "Statistics with n_eff < 6 are not evidence."
```

### 9.8 Adding a new factor or parameter safely (the CLI enforces each step)

```
1  python -m quant kb hypothesis new --kind factor --subject <name>@1 --family .. --direction .. --horizon .. --by human:<name>
     -> hypotheses row (status open, sequence_in_year assigned), knowledge/hypotheses/H-....md skeleton ; REFUSED if the year's budget is spent
2  implement quant/factors/<family>.py entry with FactorSpec(hypothesis_id=that H) + unit test on a synthetic 5-security fixture
3  python -m quant factors sync        -> factor_registry row status 'registered', code_sha256 pinned ; refuses unknown inputs
4  python -m quant factors test <name> -> NaN policy, financials, lookback, orientation, planted-signal pass-through ; backfill dev/holdout report if backfillable
5  next monthly run                    -> computed and stored, weight 0 ; status 'shadow' on first live computation ; first_oos_as_of set
6  months pass ; every report shows its shadow table ; nothing about the past is recomputed
7  review month                        -> the rules fire a proposal or record 'not yet' with the unmet criteria
8  approve (Tier 1 if all criteria true) -> 'active' effective next as_of ; new model_versions row for every model that includes active factors by rule ; ADR written
Contamination guard: scores/factor_values are keyed by version and track and never updated ; evaluations before first_oos_as_of are stored with
track 'pre_registration' and excluded from every review query ; "what if it had been active since 2026" is an experiment (kind 'rescoring', track 'counterfactual').
A PARAMETER (min_group_size, K, a cost, a gate) follows the same path with kind 'rule' and an effective_from as_of ; every run stores config_sha256.
```

---

## 10. Architecture

### 10.1 Package layout (no new dependencies at month 1: stdlib + pandas/numpy/scipy/yfinance/pytest already installed)

```
quant/
  __init__.py  __main__.py (python -m quant -> cli.main)  cli.py (argparse; no click/typer)  config.py (tomllib; env QUANT_DB_PATH, QUANT_DATA_DIR)  errors.py
  db/
    schema.sql               ALL DDL in this document, in dependency order ; single source of truth
    core.py                  connect(row_factory=sqlite3.Row, WAL), apply_schema(), upsert(df, table, keys), replace_as_of(table, as_of, df, track)
    migrate.py               schema_version ; forward-only migrations 001_*.sql
    ledger.py                export(as_of) -> data/ledger/YYYY-MM/<table>.csv ; rebuild() ; verify() ; size()
  data/
    calendar.py              NSE trading days from the ^CRSLDX daily series ; last_trading_day_on_or_before(d) ; month_ends() ; holidays fallback
    yahoo.py                 THE ONLY module that calls yfinance: throttled client (0.5 s per ticker; 25/batch; 1.0 s between batches; 429 back-off),
                             tz stripping, raw payload archive, one unit-normalisation function per field with pinned tests
    universe.py              fetch_nse_csv(name, as_of) ; save verbatim ; parse ; membership rows ; benchmark lists
    identity.py              securities / symbol_history upsert ; resolve_security_id(isin|symbol) ; rename detection ; manual overrides
    prices.py                PriceStore over data/prices_daily.sqlite: backfill(), update(), reconcile_overlap(), build_tri(), monthly_panel(), manifest()
    actions.py               corporate_actions from Yahoo columns ; unrecorded-CA detector ; add_ca() ; clear_flag()
    fundamentals.py          statements -> long rows ; available_from rules ; ttm() ; pit_frame(as_of, field, freq, n)
    holdings.py              capture() ; series(as_of, lag_runs)
    attributes.py            info -> security_attributes
    benchmarks.py            index/ETF pulls ; BM_EW / BM_EW_SECTOR / BM_CW / replicated MOM30 & QUAL30 ; NULL_RANDOM
    contracts.py             field_contracts load ; range/null-rate checks ; PSI drift
    gates.py                 G1–G10, W1–W6 -> dq_runs, data_quality_events ; Blocked exception
    adapters/                OPTIONAL, stubs with interfaces only: bhavcopy.py, nsdl_flows.py, amfi_taxonomy.py, equity_l.py
  sectors/
    taxonomy.py              sector_group_def ; group assignment ; FS split rule ; merge rule ; update_sector_map(as_of) ; lookup(as_of)
    crosswalk.py             yahoo -> nse fallback with confidence ; config/yahoo_to_nse_crosswalk_v1.csv
    features.py              sector_features (3.5)
  factors/
    base.py  inputs.py (FactorInputs builder: the ONLY code that reads tables for factors)  standardise.py  registry.py (REGISTRY, sync, test)
    momentum.py  low_risk.py  quality.py  value.py  growth.py  flows.py  controls.py  sector.py  legacy.py (legacy_* series, dc_flag)
  model/
    composite.py             family scores, composite, neutral re-rank, sleeve, scored/eligible
    learn.py                 fit_family_weights() (6.3) ; pure
    models.py                model definitions ; score_all(as_of) ; versions ; invariants check ; review (6.5 P1–P5)
    screens.py               6.4
  evaluation/
    labels.py                mature(as_of) ; L_h ; statuses ; mb36
    metrics.py               rank_ic ; within-group quintiles ; spreads ; partial_ic ; corr_matrix ; fm_slope
    stats.py                 hac_mean_test(x, lag) -> HacResult(mean, se, t, ci_lo, ci_hi, n, n_eff) ; block_bootstrap_ci ; t_crit(m) ; wilson
    walkforward.py           labels_available(T, h) ; replay_backfill()
    leakage.py               T1–T10
    curves.py                evidence_curve ; learning_curve_points
    evaluate.py              the 7.1 loop -> evaluations (idempotent; logs changes)
  portfolio/
    construct.py             top-30 buffer rule ; decile ; tranches ; sector cap ; T+1 execution
    costs.py                 buckets ; cost stack ; stress
    paper.py                 roll_forward(month) -> positions, trades, returns
    scoreboard.py            8.4 rows ; verdict word ; years_to_significance
  knowledge/
    registry.py              hypotheses / experiments CRUD ; budget ; sequence numbers
    review.py                criteria engine (9.5) -> criteria_check_json
    proposals.py             draft(as_of) ; approve() ; reject() ; apply() ; tier enforcement
    adr.py                   ADR writer ; every_decision_has_adr()
    report.py                monthly markdown ; alpha-word rule
    lessons.py
  migrate/
    legacy.py                10.7
  ui_export.py               writes ui/data.js, ui/data_learning.js, ui/data_scoreboard.js, ui/data_factors.js, ui/data_kb.js ; refuses an IC without n_eff and band
config/
  quant.toml  costs_v1.toml  field_contracts_v1.yaml  sector_group_def_v1.csv  yahoo_to_nse_crosswalk_v1.csv  manual_ticker_overrides.csv  holidays.yaml
data/          universe/  raw/fundamentals/  ledger/  MANIFEST.json  prices_daily.sqlite (git-ignored)
knowledge/     9.3
tests/         unit/ (pure functions, recorded Yahoo fixtures)  integration/ (tmp DB, synthetic universe)  property/ (embargo, invariants)  leakage/ (T1–T10 synthetic)
  conftest.py  synthetic universe: 60 securities x 6 groups x 48 months with planted signals, a 6:1 split, a Rs 100 dividend, one sector reclassification, one missing fundamental
  fixtures/    nifty500_2026-09-05.csv, idx_mom30/idx_qual30 samples, yahoo_info_samples.json (HEROMOTOCO / PNB / a small cap, with their None fields), legacy_sample.db (20-ticker extract)
ui/            index.html  app.js  style.css  data*.js  vendor/chart.umd.js (vendored, pinned)
legacy/        harness_v16_learning.py, weight_optimizer.py, quant_math.py, eval_portfolio_health.py, update_ui_v16.py, concall_analyzer.py, db_setup.py,
               v15 files, test_quant_math.py, test_optimizer.py  -> moved at month 3, unchanged, still passing their 58 tests, imported only by the migration test
monthly_cron.sh   replaces daily_cron.sh
quant_engine.db   legacy DB, frozen read-only after migration
quant.db          V2 state
```

Reuse from the existing code (import, do not rewrite): `quant_math.normalize_yield`, `quant_math.sector_tokens`, `weight_optimizer.project_weights` (bounds projection), `harness_v16_learning._row_series/_fcf_series` (statement parsing, copied with tests), `db_setup.ensure_schema` pattern, the near-constant detection from `eval_portfolio_health`, `weight_optimizer.weights_in_force` (for the legacy migration).

### 10.2 DDL index

All `CREATE TABLE` statements in this document live in `quant/db/schema.sql`, applied idempotently, in this order: `schema_version, securities, symbol_history, universe_membership, sector_group_def, sector_map, corporate_actions, prices_monthly, fundamentals, holdings, security_attributes, field_contracts, runs, dq_runs, data_quality_events, hypotheses, factor_registry, factor_values, sector_features, models, model_versions, model_weights, scores, labels, evaluations, evaluations_log, evidence_curve, learning_curve_points, portfolios, portfolio_positions, portfolio_trades, portfolio_returns, benchmarks_monthly, experiments, proposals, decisions, lessons, legacy_snapshot_map, legacy_defects`. Plus:

```sql
CREATE TABLE benchmarks_monthly (month_end TEXT NOT NULL, benchmark_id TEXT NOT NULL, tri REAL NOT NULL, source TEXT NOT NULL, PRIMARY KEY (month_end, benchmark_id));
CREATE TABLE field_contracts (field TEXT PRIMARY KEY, unit TEXT NOT NULL, min_value REAL, max_value REAL, max_null_rate REAL NOT NULL, source TEXT NOT NULL, notes TEXT, contract_version INTEGER NOT NULL);
CREATE TABLE legacy_snapshot_map (legacy_date TEXT PRIMARY KEY, as_of TEXT NOT NULL, is_full INTEGER NOT NULL, superseded_by TEXT, defects_json TEXT NOT NULL, migrated_at TEXT NOT NULL);
CREATE TABLE legacy_defects (snapshot_date TEXT NOT NULL, scope TEXT NOT NULL, field TEXT, ticker TEXT, defect_code TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX ix_scores_model_asof ON scores (model_id, as_of);
CREATE INDEX ix_labels_asof_h ON labels (as_of, horizon_m);
CREATE INDEX ix_eval_subject ON evaluations (subject_kind, subject_id, horizon_m, metric);
```

Size estimate: `factor_values` ≈ 500 × 27 × 12 ≈ 160 k rows/year; `scores` ≈ 500 × 6 × 12 ≈ 36 k; `labels` ≈ 500 × 6 horizons × 12 ≈ 36 k; the backfill adds ≈ 128 × 500 × 10 ≈ 640 k factor rows once. Well under 100 MB after five years; VACUUM before the monthly commit.

### 10.3 CLI (`python -m quant ...`; every command prints `run_id`, resolved `as_of`, git sha and the row counts it wrote; exit codes 0 ok, 1 error, 2 blocked by a gate or leakage test, 3 refused by governance)

```
db          init | rebuild [--from data/ledger] | export --as-of D | verify | size | migrate-legacy [--dry-run]
universe    fetch --as-of D                       3 CSVs -> data/universe ; membership ; identity ; sector_map ; prints group table + reclassifications
prices      backfill --start 2016-01-01 [--batch 25 --sleep 1.0] | update --as-of D | monthly --as-of D | manifest [--verify]
data        ca detect --as-of D | ca add --isin I --ex-date D --kind K --factor F | ca clear-flag --isin I --date D | accept-revision --isin I --run-id N
fundamentals fetch --as-of D [--statements] [--limit N] [--isins ..]      info + holdings every run ; statements every run in year 1, then months 2/5/8/11
gates       run --as-of D                          G1–G10, W1–W6 ; exit 2 if blocked
sectors     show [--as-of D]                       groups, sizes, merges in force, coverage, reclassifications
factors     sync | list | test NAME | compute --as-of D [--factor ID] [--track live|backfill] | backfill --factor ID | review
score       --as-of D [--model ID]                 all non-retired models ; model_weights ; screens
labels      mature --as-of D
evaluate    --as-of D [--track ..]                  labels -> evaluations ; curves ; benchmarks ; cohorts ; leakage T1–T10 on real data
portfolio   rebalance --as-of D | scoreboard
model       list | check | review
kb          hypothesis new .. | hypothesis list | propose --as-of D | queue | approve P --by A [--note] | reject P --by A --note | apply | report --as-of D | lesson add TEXT
verify      leakage --as-of D | pit --months 3 | report --as-of D (re-derives every number in the report offline; diff must be empty)
ui          export
status                                             last run, gate, open proposals, ratifications due, months to next label maturity
run         monthly --as-of D [--skip-fundamentals] [--stop-after STEP] [--override-gate Gx --decision-id D] [--push] [--dry-run] [--force]
run         backfill-track                          one-off: factor_values/scores/labels/evaluations for the 2016–2026 price-only grid
```

Expected output of a healthy `run monthly` (abridged; the first real run records the actual line in the report):

```
[quant] run 2026-11-02  as_of=2026-10-30  track=live  git=ab12cd3  code=9f3e…  config=7c1a…  registry=55ee…
[1 universe]     NIFTY500 500 (sha 3f2a…) ; MOM30 30 ; QUAL30 30 ; sector_map: 0 reclass ; groups 18 (min 9) ; 0 new ISINs
[2 prices]       553 securities x 13 months ; 0 unexplained revisions ; 1 split restated (ZFCVINDIA 6:1) ; TR_MISMATCH 0 ; benchmarks 9 ok
[3 fundamentals] 500/500 ok (29m 40s) ; available_from basis: earnings_date 311 lodr_45d 146 lodr_60d 43 ; holdings captured 500
[4 gates]        G1..G10 PASS ; W2 leverage missing 21% (financials excluded by design) ; W6 0 fields ; dq=passed_with_warnings
[5 factors]      27 computed ; excluded this month: inst_hold_chg_3m (coverage 0%, expected until run 3)
[6 score]        EW_HIER_v1#1 EW_FLAT_v1#1 MOM_ONLY_v1#1 IC_SHRUNK_v1#1 -> 2,000 rows ; scored 492 ; eligible 471 (illiquid 13, series 2, sector 6) ; gate=closed n_eff=0.0
[7 labels]       h=1 for 2026-09-30: 497 ok, 1 excluded_ca, 2 delisted_partial ; no h>=3 matured yet on live track
[8 evaluate]     live: 1M IC EW_HIER +0.021 (n=1) ; backfill: 3M IC mom_12_1 cum +0.041 n_eff 42 HAC t 2.6 (survivorship-biased) ; leakage T1..T10 PASS
[9 portfolio]    PF_EW_HIER_v1_TOP30: 30 names, turnover 6.7% one-way, cost 2.5 bp ; PF_BM_EW 471 names
[10 review]      0 candidates meet activation ; 0 in probation ; challengers: none reviewable (< 24 paired points) ; budget 2026: 0/6 used (launch set exempt via ADR-001)
[11 propose]     1 proposal: P-2026-11-01 clear_ca_flag ITCHOTELS (demerger ex 2026-10-14, factor 0.94 suggested)
[12 record]      knowledge/reports/2026-10.md ; ledger data/ledger/2026-10 (14 files, 2.4 MB) ; ui/data*.js ; quant.db VACUUM 41 MB ; commit 5e9d…
run_id 17  status ok  dq passed_with_warnings  36m 02s
```

### 10.4 Configuration (`config/quant.toml`; changing anything under `learning`, `gates`, `budget`, `standardise`, `screens`, `costs` requires a Tier-2 decision id in the commit message; `quant db init` diffs against the last applied config and refuses to run without one)

```toml
schema_version = 1
[paths]        db = "quant.db"  legacy_db = "quant_engine.db"  data_dir = "data"  knowledge_dir = "knowledge"  ui_dir = "ui"  prices_db = "data/prices_daily.sqlite"
[calendar]     timezone = "Asia/Kolkata"  index_symbol = "^CRSLDX"  holidays_file = "config/holidays.yaml"
[sources]      nifty_base = "https://niftyindices.com/IndexConstituent/"
               lists = { NIFTY500 = "ind_nifty500list.csv", NIFTY200MOM30 = "ind_nifty200Momentum30_list.csv", NIFTY200QUAL30 = "ind_nifty200Quality30_list.csv" }
               user_agent = "Mozilla/5.0"
[yahoo]        per_ticker_sleep_s = 0.5  batch_size = 25  batch_sleep_s = 1.0  on_429_sleep_s = 120  max_retries = 5  history_start = "2016-01-01"  lookback_months = 13
               statements_every_run_until = "2027-09-30"  statements_months = [2, 5, 8, 11]
               index_symbols = ["^CRSLDX", "^NSEI", "^CNX200", "NIFTYBEES.NS", "MID150BEES.NS", "MOM30IETF.NS", "QUAL30IETF.NS", "LOWVOLIETF.NS"]
[universe]     min_rows = 480  stale_block_days = 62  scored_index = "NIFTY500"
[sectors]      group_def_version = 1  min_group_size = 8  split_financials = false     # true from month 3 via decision
[horizons]     learning_m = 3  thesis_m = 12  tracked_m = [1, 3, 6, 12, 24, 36]  multibagger_m = 36  multibagger_mult = 2.0
[standardise]  winsor = [0.01, 0.99]  clip_z = 3.0  min_group_nonnull = 5  min_families_for_score = 3  min_factor_share_for_score = 0.60
[factors]      max_active = 14  max_shadow = 12  corr_limit_new = 0.70  corr_limit_redundant = 0.85  min_coverage_default = 0.70  price_factor_coverage = 0.95
[learning]     model_id = "IC_SHRUNK_v1"  k_shrink = 24  min_n_eff = 4  floor_mult = 0.5  cap_mult = 2.0  sleeve_cap = 0.20
[challengers]  max_live = 3
[gates]        G1_min_rows = 480  G3_price_cov = 0.98  G4_dup_price_share = 0.05  G5_revision_share = 0.02  G6_max_violators = 5  G7_sector_cov = 0.99
               G8_block_excluded_factors = 3  G10_shuffle_abs_ic = 0.01  W1_stale_months = 15  W2_missing_share = 0.20  W3_modal_share = 0.80  W4_ca_count = 5  W6_psi = 0.25  W6_block_fields = 3
[screens]      min_adv_inr = 20000000  min_days_traded_63 = 54  min_price_history_days = 200
[returns]      jump_log_threshold = 0.3365   # ln(1.40)
[evaluation]   bootstrap_n = 1000  shuffle_perms = 200  planted_rho = 0.10  random_composites = 1000  n500_div_yield = 0.012
[portfolio]    n_holdings = 30  buffer_rank = 60  sector_cap = 6  notional_inr = 5000000  exec_lag_days = 1
[costs]        version = "v1"  fixed_bps_one_way = 12  impact_bps = { A = 10, B = 25, C = 50 }  bucket_adv_inr = { A = 500000000, B = 100000000, C = 20000000 }  stress_mult = 1.5
[budget]       hypotheses_per_year = 6  per_family_per_year = 3  alpha = 0.05  t_floor = 2.0  fdr = 0.10  llm_ratification_days = 60
[approval]     llm_allowed_kinds = ["register_hypothesis", "promote_factor", "probation", "retire_factor", "clear_ca_flag", "accept_revision", "data_fix", "release_quarantine", "cost_model_within_25pct"]
```

### 10.5 Scheduling

```
monthly_cron.sh:  0 20 1-5 * *  cd <repo> && <python> -m quant run monthly --as-of last-trading-day-of-previous-month >> logs/loop.log 2>&1
```

The engine exits immediately if that `as_of` already has a passed run. GitHub Actions is deliberately not the runner: shared runner IPs are rate-limited by Yahoo unpredictably.

### 10.6 Field contracts (`config/field_contracts_v1.yaml`, excerpt; mirrored into `field_contracts`)

```
field                   unit     min      max      max_null_rate  source              note
close_inr               inr      0.5      1e6      0.00           yf.download
volume_shares           shares   0        1e10     0.00           yf.download
market_cap_inr          inr      1e9      1e14     0.02           yf.info
shares_outstanding      shares   1e6      1e11     0.05           yf.info
dividend_rate_inr       inr      0        5e4      0.40           yf.info             preferred over dividendYield
dividend_yield_frac     frac     0        0.25     0.40           derived             = dividend_rate_inr / close ; the 349% bug lived here
inst_held_frac          frac     0        1        0.10           yf.info
insider_held_frac       frac     0        1        0.10           yf.info
revenue_inr, ebit_inr, ebitda_inr, net_income_inr, ocf_inr, capex_inr, total_assets_inr, current_liab_inr, total_debt_inr, cash_inr, equity_inr   inr  +-1e15  0.05–0.15  yf.statements
roce_frac               frac     -2       3        0.20           derived
```

### 10.7 Legacy migration (`python -m quant db migrate-legacy`; idempotent; refuses to run twice; one transaction; prints a reconciliation table)

Facts established read-only on 2026-09-05:

```
daily_predictions by date (weekday):  2026-06-04 Thu 47 | 2026-06-12 Fri 499 | 2026-06-14 Sun 499 | 2026-07-11 Sat 499 | 2026-08-14 Fri 499 | 2026-09-03 Thu 500   (2,543 rows, 501 tickers)
06-12 and 06-14 carry IDENTICAL prices for all 499 names (weekend run = same Friday close) and different final scores (weights changed)
no snapshot has Data_Flags, Industry or Market_Cap_Cr in raw_json ; ROE_% absent ; Div_Yield_% > 25% for 322–327 names per snapshot ; FCF_Yield_% off by 1e7
active_weights: 12 rows ; only row 12 has trained_through ; performance_tracking: 4,773 rows over 8 forward dates incl. 4-day "bypass" periods
```

```
step  action                                                                                                            expected
 1    quant_engine.db is opened READ-ONLY ; nothing in it is ever modified                                                  row counts unchanged forever
 2    legacy_snapshot_map:  06-04 -> as_of 2026-06-04 is_full 0 ('partial_nifty50_run')
                            06-12 -> as_of 2026-06-12 is_full 1 superseded_by 2026-06-14 ('duplicate_pre_launch')
                            06-14 -> as_of 2026-06-12 (Sunday -> last trading day) legacy cohort L1
                            07-11 -> as_of 2026-07-10 (Saturday) L2 ; 08-14 -> 2026-08-14 L3 ; 09-03 -> 2026-09-03 L4
 3    securities / symbol_history: 501 legacy tickers -> ISIN via today's CSV (500) + config/manual_ticker_overrides.csv ; unresolved -> 'LEGACY:<symbol>' + event
 4    universe_membership: source 'legacy_snapshot' at the mapped as_of for L1–L4 ; sector_map from today's CSV, source 'legacy_backfill', confidence 0.5, valid_from 2026-06-01
 5    prices_monthly for the four as_of dates from the V2 daily store (tri, adv) ; legacy quote kept in quote_legacy ; requires `prices backfill` first (the command checks)
 6    fundamentals from raw_json where unit-safe: trailing_pe, roce_pct, debt_to_equity (ratio), inst_holdings (/100 -> frac), sma50, sma200, ocf/fcf arrays (crores; fiscal_period_end unknown ->
      period_end 'FY-k' relative labels, flag) ; available_from = snapshot date, basis 'run_date' ; NOT migrated as values: Div_Yield_% (x100), FCF_Yield_% (1e7), sentiment, Composite_Growth (absent)
 7    holdings from Inst_Holdings_% (captured_at = snapshot date) ; gives inst_hold_chg_3m partial coverage from L4
 8    factor_values track 'legacy': the 8 scores + trap_score + momentum_multiplier + dc_flag as legacy_<name>@0 (raw = stored value ; z = gaussian rank within CURRENT sector_group) ;
      plus V2 price factors (mom_12_1, trend_200, vol_252, mom_6_1, dist_52w_high, rev_1m, max_ret_21) computed from the daily store at those dates — these ARE clean
 9    scores: model LEGACY_V18 (final = final_score) and LEGACY_V18_BASE (base_score, or the in-force weighted sum with the sentiment term excluded, flagged 'reconstructed') ;
      model_version = active_weights.id in force (weights_in_force logic) ; eligible = final > 0 ; group_def_version 1
10    model_versions: 12 rows role 'legacy' from active_weights (weights_json, note, trained_through)
11    labels for L1–L4 recomputed from the V2 TRI store (split- and dividend-safe, sector-relative) for h in {1} between consecutive legacy dates and h in {1,3,6,12,...} as they mature ;
      performance_tracking is NOT imported (unadjusted quotes, 4-day periods) ; it is summarised in the migration ADR
12    legacy_defects rows + one WARN data_quality_events row per defect per snapshot:
      DIV_YIELD_X100 | ROE_NONE_AS_ZERO | SENTIMENT_OUTSIDE_BUDGET | GROWTH_IMPUTED_15PCT | DCF_SINGLE_YEAR_FCF | NEAR_CONSTANT_FACTOR | NO_DATA_FLAGS |
      UNADJUSTED_QUOTE | WEEKEND_RUN (L1, L2) | HINDSIGHT_TICKER_LISTS | BUCKETED_SCORES | NO_SECTOR_NEUTRAL | NO_KNOWN_AT | DUPLICATE_SNAPSHOT_0612
13    factor_registry rows for legacy_* status 'retired', registered_on 2026-06-01, evidence 'legacy' ; decisions D-2026-09-01..07 with ADRs
      (retire moat, strategic risk, headline sentiment, DCF factor, trap multiplier, death-cross kill, cap-alloc buckets) citing the red-team review
14    runs: one row per legacy snapshot, kind 'migrate', track 'legacy', dq_status 'legacy_defects', is_clean 0
```

Acceptance tests (`tests/integration/test_migrate_legacy.py`, on `tests/fixtures/legacy_sample.db` (20-ticker extract) in CI and on the real DB in the sign-off):

```
test_legacy_db_untouched                 SELECT COUNT(*) FROM daily_predictions/active_weights/performance_tracking unchanged (2,543 / 12 / 4,773)
test_snapshot_map                        6 rows ; 06-12 superseded ; weekend dates normalised (06-14 -> 06-12, 07-11 -> 07-10)
test_scores_counts                       scores(LEGACY_V18, track legacy) == 1,997 (499+499+499+500) ; factor_values legacy_* == 1,997 x 11
test_split_no_longer_a_return            ZFCVINDIA L1 -> L2 TRI return in [-30%, +30%] (was -84.1%)
test_attribution_reproduced              with --legacy-quotes: Spearman(final_score, unadjusted return) reproduces the red-team table within +-0.01
                                         (final: -0.063/+0.092/+0.117 ; momentum alone: -0.033/+0.030/+0.125 ; fundamentals: +0.045/+0.058/+0.050) ;
                                         and the adjusted, sector-relative version is printed beside it
test_defect_flags_present                every legacy scores row's snapshot has all applicable legacy_defects codes
test_legacy_never_clean                  runs.is_clean == 0 for all legacy runs ; no learning_curve_points with track 'legacy'
test_idempotent                          second invocation raises 'already migrated' and changes nothing
```

The four 2026 snapshots therefore become the first, flagged, never-clean points on every chart without pretending to be comparable to V2 months.

### 10.8 UI (vanilla HTML/JS/CSS, no build step; Chart.js vendored to `ui/vendor/chart.umd.js` so the README's "zero-dependency" claim becomes true offline; Google Fonts link removed)

```
data files       ui/data.js (ranking + per-stock detail), ui/data_learning.js, ui/data_scoreboard.js, ui/data_factors.js, ui/data_kb.js  (all written by quant ui export)
tabs             Ranking   | per sector_group ; champion and challenger side by side ; eligibility badges and reasons ; factor z's with missing markers ;
                            filter chips: "negative FCF, high growth" (the former Turnaround tab), "below 200-day average" (neutral badge, TREND_200 z) ; nothing called accepted/rejected
                 Learning  | panels A–C + inset (7.7) ; backfill dashed, legacy hollow, decision markers ; falsification checklist (1.3) with live status
                 Scoreboard| 8.4 rows with verdict words ; NAV vs benchmarks ; turnover ; cost drag ; years to significance ; PF_EW_HIER_v1 always visible
                 Factors   | registry: status, months in status, hypothesis link, live/backfill mean IC +- CI by horizon, coverage, last 12 monthly ICs, next review month
                 Sectors   | group table ; sector_features ; portfolio group weights vs universe
                 Data      | gate history 24 months ; flag counts ; CA flags awaiting confirmation ; imputed share ; hypothesis budget ledger
                 Knowledge | decisions list (status, tier, approver, ratification due) ; proposals queue ; lessons ; links to ADR markdown
rendering rule   any IC or return shown without n_eff and a band is a bug ; ui_export refuses to emit such a record
footer           as_of, run_id, git sha, "Statistics with n_eff < 6 are not evidence."
per-stock detail per-factor z, family scores, composite, rank in group, eligibility reason, flags, cost bucket ; the DCF paragraph may remain labelled "not used in ranking"
```

---

## 11. Phased roadmap

```
Month 1  (target 2026-10-05; first live snapshot as_of 2026-09-30, else 2026-10-30)   "a clean loop that runs"
  WS00–WS07 + WS10 + the run/report/ui-export parts of WS11 ; price backfill 2016– ; TRI + reconciliation ; universe/identity/sector_map ; fundamentals PIT + raw archive ;
  gates ; all launch factors registered (hypotheses H-2026-001..011) ; EW_HIER_v1, EW_FLAT_v1, MOM_ONLY_v1, IC_SHRUNK_v1 (gate closed) ; labels ; evaluations ; HAC ; leakage T1–T10 ;
  evidence_curve + learning_curve_points (backfill panel populated, live panel empty) ; legacy migration with its 8 acceptance tests ; monthly report v1 ; ledger export ; UI Ranking + Learning + Data
  acceptance: pytest -q >= 150 tests green offline in < 90 s ; `run monthly` completes < 60 min status ok ; `run backfill-track` yields >= 120 3M points for mom_12_1 and the chart renders ;
              migration attribution test passes ; falsifier "mom_12_1 backfill 12M IC > 0" checked and written into the report
Month 3  (2026-12)   "measured, with costs"
  paper portfolios, cost model, scoreboard (WS08) ; proposals/approve/apply with tiers, ADR generation, lessons (WS09 full) ; FS split as decision D-2026-12-xx with before/after group table ;
  statement-based shadow factors computing ; UI Scoreboard/Factors/Knowledge tabs ; legacy scripts moved to legacy/ ; first 1M live points ; first shuffle/planted/PIT on real data
Month 6  (2027-03)   "sector-aware beyond neutralisation"
  sector_features + SECTOR_OVERLAY_v1 registered as a hypothesis ; optional adapters stubbed (bhavcopy, NSDL flows, AMFI) ; first 3M live points (3) ; first `factors review` output
  (nothing passes yet; the report says so) ; crowding monitors ; quarterly-cadence comparison ; annual-review template dry-run on the backfill track
Month 12 (2027-09)   "a year of clean data"
  ~9 realised 3M points ; first 12M point at month 13 ; first yearly budget close-out and BH review ; annual ADR summarising every hypothesis ; cost recalibration proposal if turnover warrants
Month 15 (2027-12)   gate opens: n_eff = 4 ; IC_SHRUNK_v1 first deviates (alpha 0.14) ; paper record of the challenger starts to mean something
Month 24 (2028-09)   first meaningful HAC t on 3M IC ; challenger vs champion paired test first readable ; checkpoint 2
Month 36 (2029-09)   checkpoint 3 ; first mb36 cohort ; targets in 1.2 become judgeable
```

If only four weeks exist: keep, in order, WS00 → WS01 → WS02 → WS03 (info + statements) → WS04 (blocking gates only) → WS05 (price factors + roce/accruals/earnings_yield/book_to_price) → WS06 (EW only; challenger as a pure function with tests but no versions churn) → WS07 (labels 1/3/12; IC + HAC; shuffle + planted + PIT tests; curves) → WS10 → the report and `run monthly` from WS11. Cut: WS08, proposals/approval CLI (write ADRs by hand with `kb decision`), sector features, FS split, remaining UI tabs. The rule for the cut: anything that changes *what is stored about the past* ships in weeks 1–4; anything that only *reads* the past can come later. What must never be cut: PIT storage with `available_from`, TRI from raw facts with reconciliation, ISIN identity, gates, pre-registration before the first live cohort, the shuffle and PIT tests.

---

## 12. Risks, failure modes and open questions

```
failure mode                  mechanism (one line)                                          guard in this design                                          residual
data snooping                 test many ideas, keep the winners                              pre-registration, 6/yr budget, t_crit(m), immutable versioned   the owner reads shadow tables monthly
                                                                                             scores, review-month-only proposals                            and forms opinions; disclosed, unfixable
survivorship                  losers vanish from the sample                                  PIT membership, track-to-maturity, delisted flags, cohorts       price backfill is survivor-only; quarantined to descriptive use
unadjusted corporate actions  a split looks like a crash                                     in-engine TRI, overlap reconciliation, jump detector, T7         demergers/rights need a human confirmation each month
look-ahead in fundamentals    using a number before it was public                            available_from with basis, raw archive, T3/T4/T6                 Yahoo's own lag unknown; live rows safe, backfilled statements unused
costs eating the spread       turnover x cost > gross alpha                                  net-first, buffer, T+1 fills, liquidity screen, 0.5x/1x/2x       impact assumptions are guesses; sensitivity discloses it
regime change                 what worked stops                                              EW champion, shrinkage K=24, no decay, CUSUM view                 a 3-year bull market flatters any momentum-heavy ranking
factor crowding               everyone owns the same names                                   overlap with index products, valuation spread of top decile       monitor only
operator abandonment          the ritual stops; state rots                                   one command, < 60 min, one-page report, re-runnable months, ledger rebuild   owner attention is the single point of failure
silent data drift             vendor changes units/coverage                                  field contracts, PSI, version pin, raw archive, BLOCK gates       new fields can arrive wrong; first contract is a guess
small effective N             overlapping windows are not independent observations           n_eff everywhere, HAC, bootstrap, verdict words, gate at n_eff 4   pressure to loosen K or min_n_eff (Tier-2, ADR required)
challenger proliferation      ten challengers, one wins by luck                              <= 3 live, budget, deflated t, BH review, EW floor                 bundling ("one hypothesis, five variants") is a convention to reject
LLM rubber-stamping           an approver finds reasons to approve                           Tier-1 only, provisional, criteria check with no false, 60-day     a human who ratifies without reading; the ADR exists for the later reader
                                                                                             ratification or auto-revert, share of LLM decisions printed
Yahoo as single free source   fields vanish, encodings change, 429s                          one module, pinned tests, raw archive, adapters interface, resumable   a multi-month outage leaves a hole nobody can backfill PIT
financials group              101 names, incompatible accounting                             FS split at month 3, applies_to_financials, yield-form value       roce/accruals meaningless for banks: they score on fewer families
git growth                    binary DB deltas                                               ledger CSV canonical, DB once a month + VACUUM, prices outside git  mistaken commit of the daily store (.gitignore covers)
implementer "simplifies"      bitemporal table becomes latest-value; learner gets a learning rate   T3 written in week 2; worked-example test pins the arithmetic  --
a brilliant first year        +0.10 IC for 12 months                                         years_to_significance column, checkpoints, "not evidence" footer   temptation to size real money before month 36
```

Confidence, split: that this design measures whatever skill exists correctly and cannot flatter itself — 85%. That the live composite shows a positive, cost-surviving 12M IC by month 36 — 35–40%. The gap is the point: the first is process and under our control; the second is the market and is not.

---

## 13. Decisions log

Each entry: decision, alternatives rejected, reasoning, what evidence would reverse it.

```
D01 Horizons: learn at 3M, headline at 12M, track 1/3/6/12/24/36.
    Rejected: 12M as learning horizon (1 independent obs/yr -> 2 data points by 2029); 1M (mostly trend, per red team).
    Reverse if: by month 24 the 3M and 12M IC pictures diverge persistently -> rule_change hypothesis to learning_m = 6.
D02 Label: sector-relative log total return, median group adjustment, universe fixed at as_of, delisted tracked to maturity.
    Rejected: mean adjustment (one 5x name moves the group); arithmetic returns for IC (skew; irrelevant for Spearman anyway).
D03 Identity by ISIN with symbol_history. Rejected: ticker as key (ZOMATO -> ETERNAL breaks continuity).
D04 Canonical sector = NSE sector column of the constituent CSV; FS split via Yahoo industry from month 3; min group 8 with a merge table; PIT sector_map.
    Rejected: four-level NSE/AMFI hierarchy (not scriptable: 403 / irregular spreadsheet); Yahoo as canonical (disagrees ~1 in 5 with NSE).
    Reverse if: within-group 12M dispersion at month 6 shows the FS split or a merge is wrong -> new group_def version.
D05 Prices: store unadjusted close + Yahoo actions + adj_close side column; TRI in-engine; overlap reconciliation; jump detector; daily bars in a git-ignored SQLite.
    Rejected: trusting Yahoo Adj Close (rewritten backward on every dividend); Parquet/DuckDB (not installed; new binary dependency on Python 3.14); +-60% filter (hides real moves).
D06 Fundamentals: long-format bitemporal rows with available_from (earnings date or LODR lag) and fetched_at; raw payload archive committed; no imputation anywhere.
    Rejected: period_end as availability (1–3 month look-ahead); wide latest-value table (cannot audit).
D07 Git: ledger CSVs canonical + committed; quant.db committed once a month; legacy DB frozen; daily prices not committed; no LFS.
    Rejected: DB-only (binary deltas bloat history; unmergeable); CSV-only (owner wants the DB tracked; convenience).
D08 Factors continuous; FactorInputs is the only data access; winsor -> group gaussian rank -> clip; missing stays NaN.
    Rejected: 0–100 buckets; z = 0 imputation (hides missingness); percentile ranks without gaussianisation (composite arithmetic on uniforms is fine but less standard).
D09 Launch set: 5 active families (11 factors incl. flows when covered), 9 shadows, 3 controls, dc_flag diagnostic; growth active as the owner's pre-registered thesis.
    Rejected: 6 actives only (too few families for a robust EW baseline); 22 actives (budget and family-stuffing risk).
D10 Champion = hierarchical equal weight by family, permanent; references EW_FLAT and MOM_ONLY.
    Rejected: flat EW (adding a factor silently re-weights families); India-prior weights as champion (an opinion must earn its place as a challenger).
D11 Challenger = family-level shrunk-IC weights, pure function, K=24, gate n_eff >= 4 (12 labelled months), bounds [0.5/F, 2/F], promotion at >= 24 paired months with deflated t.
    Rejected: exponentiated gradient (stateful; the legacy bug); per-factor weights (22 parameters on 4 effective observations); gate at n_eff 12 (36 months without any learning signal;
    the shrinkage already keeps alpha at 0.14 when the gate opens, and the champion stays EW until promotion).
    Reverse if: checkpoint at month 48 (learned never beats EW) -> weights equal forever.
D12 Screens: liquidity, series, coverage, sector only; all screened names scored and evaluated as cohorts. Rejected: any prediction-based filter; multipliers.
D13 Sector sleeve: separate overlay challenger capped at 0.20, zero at launch. Rejected: sector terms inside the stock composite (contaminates the selection claim).
D14 Statistics: HAC Bartlett lag h-1, block bootstrap, n_eff, t_crit(m) Bonferroni floor 2.0; no cross-sectional t. Rejected: fixed t >= 2.5 for everything (less transparent than the m-dependent bar).
D15 Leakage tests T1–T10 run monthly on real data as gates. Rejected: CI-only tests.
D16 Benchmarks built from own TRI; replicated MOM30/QUAL30 from monthly constituent files; ETFs cross-check. Rejected: "top 30 by our own momentum" as a benchmark (a portfolio of our signal).
D17 Portfolio: top 30 EW, buffer 60, sector cap 6, T+1 close, costs 12 bp + 10/25/50 impact, ADV buckets 50/10/2 crore, stress 1.5x. Rejected: decile portfolio as primary (50 names, high turnover); notional-agnostic costs.
D18 Governance: 6 hypotheses/yr, tiers 0/1/2, LLM as first reader with provisional Tier-1 approvals ratified within 60 days. Rejected: LLM may promote models; no budget.
D19 Legacy migration: 06-12 superseded by 06-14; weekend dates normalised; returns recomputed from TRI; performance_tracking not imported; every defect flagged; legacy never clean.
D20 Backfill track: price factors only, survivorship-labelled, never used for promotion/weights/scoreboard; falsifier F "mom_12_1 backfill 12M IC > 0" checked in month 1.
D21 Run grid: as_of = last NSE trading day from the ^CRSLDX series; run in the first week; fundamentals stamped fetched_at. Rejected: calendar month-end assumption; holiday YAML as primary.
D22 No new dependencies at month 1; Chart.js vendored. Rejected: pyarrow/duckdb/statsmodels/click.
D23 Death cross -> trend_200 continuous + dc_flag diagnostic + killed-cohort report for 24 months. Rejected: keeping the 0.0x kill as an "invariant" (the red team measured it costing return in one of three months and destroying rank information for a third of the universe).
D24 Two curves: evidence_curve (fixed subject) and learning_curve_points (re-fitted model vs EW at the same k). Rejected: one cumulative-IC chart called "learning" (rises for a model that never learns).
```

---

## 14. Glossary

```
as_of              the NSE trading day a snapshot describes; every input used must have been public on or before it
available_from     first date a fundamental value could have been public (earnings date or SEBI LODR lag); the PIT filter key
backfill track     price-only factor history 2016– on today's constituents; survivorship-biased; descriptive only
buffer (hold band) a holding stays while rank <= 60 although entry requires rank <= 30; halves turnover
challenger         a model scored and paper-traded alongside the champion; may replace it only via 6.5 P1–P5
clean month        a live run that passed every blocking gate with no override; the x-axis of the learning curve
cohort             a group of scored names treated separately in evaluation (illiquid, unscored, dc_flag, turnaround view)
composite          weighted mean of family scores; re-ranked within sector group to N(0,1)
evidence curve     cumulative statistics of a fixed subject vs clean months (converges)
family             a group of factors expressing one idea (momentum, low_risk, quality, value, growth, flows, sector)
gaussian rank      Phi^-1((rank - 0.5)/n) within group: mean 0, sd ~1 regardless of group size
HAC t              Newey–West t-statistic with Bartlett kernel, lag = horizon - 1; the statistic of record for IC series
IC (Rank IC)       Spearman correlation between a score and the subsequent sector-relative return across stocks at one as_of
ICIR               mean IC / sd IC over months; shown only when n_eff >= 6
learning curve     OOS IC of the re-fitted model vs the EW baseline at the same amount of training data k
ledger             the committed monthly CSV export of every table; canonical; quant.db is rebuildable from it
lift               precision@decile / base rate for the 36-month doubling label; 1.0 = no skill
n_eff              labelled months / horizon months; the honest count of independent observations
PIT                point-in-time: a value is usable for a date only if it was public on that date
pre-registration   hypothesis, formula, direction, horizon and first_oos_as_of recorded before any out-of-sample evaluation
sector_group       the neutralisation bucket (NSE sector after FS split and small-group merges), versioned
shadow             a registered factor computed and evaluated monthly with weight 0
t_crit(m)          max(2.0, Phi^-1(1 - 0.05/m)); the promotion bar after m hypotheses in 24 months
TRI                total-return index built in-engine: TRI_d = TRI_{d-1} * (close_d + dividend_d) / close_{d-1}
track              live | backfill | legacy | pre_registration | counterfactual — every stored evaluation carries one
```

---

## 15. Open questions deferred to implementation (take the default if the owner is unavailable)

```
Q1  Capital scale for the paper portfolio.                       Default: notional_inr = 5,000,000 ; drives buckets and the "largest safe notional" line.
Q2  Approver of record; may an LLM approve Tier-1 items?          Default: yes, provisional, human ratification within 60 days; kinds in config.approval.
Q3  First live snapshot 2026-09-30 or 2026-10-30?                 Default: 2026-09-30 if `run monthly --as-of 2026-09-30` passes gates by 2026-10-05; else 2026-10-30.
Q4  Commit quant.db as well as the ledger?                        Default: yes, once per month; `db verify` in CI; conflict -> rebuild from ledger.
Q5  Institutional holdings update cadence on Yahoo.               Default: capture monthly, log change dates 12 months, then re-specify inst_hold_chg_3m if not quarterly.
Q6  FS split effective month.                                     Default: month 3 as decision D-2026-12-xx with before/after group table; month 1 ships FS as one group.
Q7  Leverage as net debt / EBITDA vs gross D/E.                   Default: net debt / EBITDA (shadow); gross D/E as a later version if coverage is poor.
Q8  Keep any DCF computation?                                     Default: UI explainer only, computed in ui_export from stored inputs, labelled "not used in ranking".
Q9  Does the backfilled price track's evidence count toward the budget or priors?   Default: descriptive only; discount 0.5 as a stated prior; no budget consumption.
Q10 Holiday calendar source.                                      Default: ^CRSLDX trading days; config/holidays.yaml fallback updated yearly; missing year = WARN.
Q11 Should Nifty Total Market be fetched for the extended (unscored) universe?   Default: yes, stored, not scored (keeps price history for names that drop out).
Q12 Bhavcopy adapter for liquidity/delivery?                      Default: optional adapter stub; Yahoo close x volume for ADV in v1.
Q13 Delisting treatment.                                          Default: last price + delisted_partial; report the -50% sensitivity until a real case occurs.
Q14 Quarterly vs monthly rebalancing as the headline portfolio.   Default: monthly with buffer is headline; quarterly tranches run in parallel; decide by paired scoreboard at month 18.
Q15 May an LLM ever activate a factor?                            Default: Tier-1 provisional only when every criterion is true; never a model promotion.
```
