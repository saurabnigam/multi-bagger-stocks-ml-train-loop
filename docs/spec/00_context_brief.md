# Context Brief for the V2 Engine Design (written 2026-09-05)

This is the single page every designer, reviewer and implementer reads first. It states what the owner wants, what exists today, what was found wrong with it, and the constraints. It does not decide the design; it seeds it. Challenge the seed hypotheses where they are wrong.

---

## 1. What the owner asked for (paraphrased, verbatim intent)

1. **Sector categorisation.** Identify and use sectors properly: classify every stock, and make the model sector-aware (sector-neutral ranking, sector-level signals such as sector momentum and sector flows).
2. **Run year after year.** The owner will operate this engine monthly for years. The design must be built for accumulation, not for a one-off backtest.
3. **Predictability must increase over time.** There must be a measurable learning curve: as clean periods accumulate, out-of-sample predictive power should rise, and the system must be able to show that on a chart. Alpha is the eventual goal, not a day-one claim.
4. **Feedback captured in a knowledge base.** Every monthly evaluation, every finding, every decision (add a factor, retire a factor, change a weight rule) must be captured in a structured knowledge base plus human-readable records, so the system remembers why it is the way it is.
5. **Parameters added as needed.** New factors/parameters must be addable safely when the feedback loop suggests them, with pre-registration and promotion criteria, without contaminating the historical record.
6. **Monthly loop engineering.** A monthly "loop" (ingest → score → evaluate realised returns → update knowledge → propose changes → approve → record) that improves the system as a whole, with a human or an LLM in the approval seat.
7. **Deliverable of this exercise is documentation, not code.** A well-reasoned master spec, a `subagents/` folder with one self-contained handoff document per workstream, a test-and-verification plan, and a single command/prompt the owner can hand to a different LLM to build the whole thing inside this repository.

## 2. What exists today (branch `red-team-review-sep-2026`, PR #1)

```
Repo: /Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop
Python that works: /Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/venv/bin/python
   (pandas 3.0, numpy 2.4, scipy, yfinance 1.4, pytest). No venv inside the repo.
Data:  quant_engine.db (SQLite, 5 MB, tracked in git)
       daily_predictions: 4 full snapshots of ~500 Nifty 500 stocks
         2026-06-14, 2026-07-11, 2026-08-14, 2026-09-03 (+ two partial/early runs)
         per row: date, ticker, price (unadjusted quote at run time), 8 factor scores (0-100, bucketed),
                  trap_score, momentum_multiplier (0/0.8/1.0), final_score, base_score (new),
                  raw_json (P/E, ROCE, SMA50/200, 4-yr OCF/FCF arrays in crores, inst holdings, Data_Flags ...)
       active_weights: 12 rows, weights in [0.05, 0.30] summing to 1.000, trained_through (new)
       performance_tracking: prediction_id → forward price/return per transition
Code:  harness_v16_learning.py (yfinance pull + scoring, 0.5 s sleep per ticker, ~7 HTTP calls/stock)
       quant_math.py (bucketed scoring functions, DCF, justified P/B for financials)
       weight_optimizer.py (Rank-IC exponentiated gradient, now idempotent; split filter; attribution)
       eval_portfolio_health.py (bounds, units, near-constant factors, walk-forward learning test)
       update_ui_v16.py + ui/ (vanilla HTML/JS/CSS dashboard; Chart.js from CDN)
       db_setup.py (ensure_schema migrations), config.py (repo-relative paths)
Tests: 58 pytest tests (scoring math + optimizer pure functions)
Docs:  AGENTS.md (operating manual; CLAUDE.md symlinks to it), docs/analysis/red_team_review.md
```

Yahoo Finance field quirks verified live (keep these in mind for any data-layer design):
- `dividendYield` is a percent (3.48 == 3.48%); `dividendRate` is rupees/share (unambiguous).
- `debtToEquity` is a percent (357 == 3.57x). `returnOnEquity`, `currentRatio` are often `None` for Indian names.
- `sector` / `industry` follow the Yahoo taxonomy ("Consumer Cyclical" / "Auto Manufacturers"), not NSE/AMFI.
- `currentPrice` is unadjusted; `Ticker.history(auto_adjust=True)` gives split/dividend-adjusted closes; `Ticker.splits` and `Ticker.dividends` are available. A 6:1 split (ZFCVINDIA, 2026-06-24) appeared as a −84% "return" in the old backtest.
- Annual statements arrive with a 6–12 month lag; quarterly statements exist (`quarterly_financials`) but coverage varies.
- Nifty 500 constituents: `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv` (columns include Symbol, Industry).

## 3. What the red-team found (full text: docs/analysis/red_team_review.md)

```
Measured over 3 monthly periods (Jun→Jul, Jul→Aug, Aug→Sep 2026), ~500 stocks:
  composite Rank IC              -0.063  +0.092  +0.117   mean +0.049
  momentum flag alone (3 values) -0.033  +0.030  +0.125   mean +0.041
  fundamentals only              +0.045  +0.058  +0.050   mean +0.051
Walk-forward: weights learned from earlier periods (+0.031) did NOT beat equal weights (+0.046).
Three factors (risk, moat, balance sheet) sit on one value for 84–96% of the universe.
Unit bugs corrupted Cap-Alloc (dividend yield ×100) and the trap score (ROE None → 0) until Sep 2026.
Scores are 5-level buckets; nothing is sector-neutral; returns are price-only; no cost model.
Objective (1-month IC) is mismatched with the goal (multi-year compounders).
```

Conclusion carried into V2: the plumbing pattern (snapshot → forward return → evaluate → adjust) is right; the objective, the data discipline, the factor construction and the statistics are not.

## 4. Hard constraints

- **Stack:** Python 3.11+, pandas/numpy/scipy, SQLite (DuckDB or Parquet files are acceptable for bulk price history if justified), pytest. Frontend stays vanilla HTML/JS/CSS with no build step. No paid data feeds as a dependency; optional adapters are fine.
- **Rate limits:** Yahoo Finance polling keeps a ≥0.5 s per-request throttle. Bulk history downloads should use batched `yf.download` with modest batch sizes.
- **Database in git:** the owner wants history committed. A 10-year daily price history for ~500 names is ~1.3 M rows; the spec must decide how bulk data is stored and what is/is not committed (e.g. Parquet + git LFS, or regenerate-from-source with a committed manifest).
- **Migration, not deletion:** the four 2026 snapshots and the weight history must be migrated into the new schema as the first data points, with their known defects flagged.
- **Implementer is a different LLM** working only from the documents in this repo. Every handoff must be self-contained: exact paths, schemas, signatures, commands, expected outputs, and how to verify.
- **Cadence:** monthly loop, run manually or by cron, India time (IST). Should complete in under an hour on a laptop.
- **Honesty:** no document may claim performance that has not been measured out of sample. Targets are stated as targets.

## 5. Seed hypotheses (challenge these; do not accept them by default)

1. Prediction target: forward **12-month** sector-neutral relative return (also track 1, 3, 6, 24, 36 months). Multi-bagger recall over 36 months as a slow KPI.
2. Sector taxonomy: NSE/AMFI industry classification (Macro-Economic Sector → Sector → Industry → Basic Industry) as canonical, mapped point-in-time; Yahoo sector/industry as fallback; a versioned `sector_map` table.
3. Factors: continuous, sector-neutral percentile ranks, winsorised; each factor is a plugin with metadata (hypothesis, expected sign, horizon, status: candidate/active/retired, registered_on). Initial set should lean on what has evidence in India: 12-1 momentum, low volatility, quality (ROE/ROCE stability, accruals, cash conversion), growth, value (EV/EBITDA, earnings yield, P/B within sector), institutional/promoter holding changes, liquidity.
4. Model: equal-weight composite as the permanent baseline; learned weights via shrinkage toward equal weights, only allowed to deviate once ≥12 non-overlapping evaluation periods exist; champion/challenger with paper track record; the current death-cross 0.0× hard kill becomes a continuous trend factor rather than a filter.
5. Evaluation: point-in-time `as_of` discipline everywhere; walk-forward with embargo; overlap-aware t-stats (Newey–West/HAC) for 12-month horizons evaluated monthly; benchmarks = equal-weight universe, Nifty 500, Nifty 200 Momentum 30, Nifty 500 Quality 50 proxies; cost-adjusted quintile spreads; leakage tests (shuffle test → IC ≈ 0; planted-signal test → recovered).
6. Knowledge base: SQLite tables (experiments, hypotheses, factor_versions, evaluations, decisions, data_quality_events) plus `knowledge/` markdown (ADR-style decision records, auto-generated monthly reports, lessons ledger). Pre-registration required before any new factor sees out-of-sample data; promotion/retirement criteria explicit; multiple-testing control (count of hypotheses tried, deflated thresholds).
7. Portfolio layer: paper portfolio with turnover, cost model (basis points by liquidity bucket), liquidity screen (ADV), monthly/quarterly rebalance; realised paper P&L vs benchmarks is the alpha scoreboard.
8. Architecture: a `quant/` Python package (data, sectors, factors, model, evaluation, portfolio, knowledge, cli), one CLI entry point (`python -m quant <command>`), SQLite for state, Parquet for bulk history, migration of the legacy DB.

## 6. Open decisions the spec must settle (with reasoning)

- Primary horizon and objective function; how the multi-bagger goal is expressed as a measurable target.
- Where bulk price history lives and what is committed to git.
- Canonical sector source, fallback chain, and how reclassifications are handled point-in-time.
- The exact learning rule, its guard rails, and the evidence threshold before it may move weights.
- Whether any hard filters remain (trend, liquidity, data-quality) and how filtered names are evaluated.
- The approval protocol in the monthly loop (human vs LLM; what is auto-applied vs proposed).
- Benchmark set and the exact definition of "alpha" the scoreboard reports.
- How many hypotheses per year may be tested and the significance threshold given that count.
- Minimum data-quality gates that block a monthly run.

## 7. Definitions used across documents

- **Rank IC:** Spearman correlation between a score and subsequent return across stocks at one date. ±0.05 is typical for a real factor at 1–12 months; +0.10 sustained is excellent.
- **ICIR:** mean IC / std IC across periods. Needs ≥ 12 independent periods to mean anything.
- **Sector-neutral:** ranked or standardised within sector before combining, so the composite does not bet on sectors unless a sector factor is deliberately included.
- **Point-in-time (`as_of`):** a value is usable for a date only if it was publicly available on that date; stored with the date it became known.
- **Embargo:** gap between the end of a training window and the start of the test window equal to the prediction horizon, so overlapping returns do not leak.
- **Pre-registration:** the hypothesis, formula, expected sign and horizon of a factor are recorded before it is evaluated on new data.

## 8. Success metrics the owner should be able to read off a dashboard

```
Primary   rolling 12-month sector-neutral Rank IC of the live composite (with HAC t-stat and CI)
          net-of-cost top-minus-bottom quintile spread at 12 months
Learning  learning-curve chart: out-of-sample IC vs months of accumulated clean data, per factor and composite
Slow KPI  36-month multi-bagger recall: share of eventual 2x+ names that were top-decile at t0
Hygiene   data-quality gate pass rate, share of imputed inputs, number of hypotheses tested YTD
Honesty   composite vs equal-weight vs benchmarks, all on the same paper-portfolio basis
```
