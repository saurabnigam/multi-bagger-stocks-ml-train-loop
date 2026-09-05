# AGENTS.md — Autonomous Coding Agent Operating Manual

Welcome, Agent. This document is your primary context and operational guide for maintaining, extending, and running the **Multi-Bagger Stocks ML Train Loop & Quant Engine**.

---

## 🧭 Repository Mission
This repository is an institutional-grade Quantitative Machine Learning platform that identifies, scores, and tracks multi-bagger compounder stocks across the **Nifty 500** Indian equity universe. 

It combines:
1. **Multi-Factor Fundamental & Technical Scoring** (`quant_math.py`)
2. **Autonomous Daily/Periodic Data Acquisition** (`harness_v16_learning.py`)
3. **Multi-Period Regime-Aware Gradient Descent** (`weight_optimizer.py`)
4. **Institutional Health & Out-of-Sample Backtesting** (`eval_portfolio_health.py`)
5. **Zero-Dependency Glassmorphism Dashboard** (`ui/`)

---

## 📋 Prime Directives for Agents

1. **NEVER Bypass Rate-Limiting:** Always maintain the `time.sleep(0.5)` throttle in `harness_v16_learning.py` when polling Yahoo Finance. Hitting 500 stocks concurrently will result in an immediate HTTP 429 IP ban.
2. **Preserve Mathematical Invariants:**
   * **Active Weights Bounds:** Every factor weight in `active_weights` must be $\ge 0.05$ (5%) and $\le 0.30$ (30%).
   * **Active Weights Normalization:** The sum of all 8 factor weights must equal **strictly 1.000**.
   * **Margin of Safety (MoS):** Must be strictly clipped to `[-99.9%, +99.9%]`.
   * **Death Cross Multiplier:** Must enforce a `0.0x` hard-kill multiplier.
3. **Use Zero-Latency In-DB Returns for Optimization:** When running `weight_optimizer.py`, transition prices for historical snapshots must be queried directly from SQLite (`daily_predictions`), never re-fetched over the web.
4. **Keep Frontend Zero-Dependency:** The `ui/` directory must remain browser-native Vanilla HTML/JS/CSS. Do not introduce npm, bundlers, or frameworks.
5. **Keep Database Tracked in Git:** `quant_engine.db` contains months of irreplaceable historical predictions and must be committed and pushed to GitHub.
6. **Never Hard-Code Paths:** All scripts resolve `quant_engine.db` and `ui/` relative to the repository via `config.py` (override with `QUANT_DB_PATH` / `QUANT_UI_DIR`). Do not reintroduce absolute paths.
7. **Optimizer Must Stay Idempotent:** `weight_optimizer.py` learns from each holding period exactly once (tracked in `active_weights.trained_through`). Running it twice on the same data must not move the weights. Use `--dry-run` to inspect, `--force` only deliberately.
8. **Store `base_score` Alongside `final_score`:** the pre-multiplier composite is what lets the audit separate fundamental alpha from the momentum filter. Any new scoring path must write both.
9. **Do Not Overstate the Evidence:** three monthly periods are not a track record. Read `docs/analysis/red_team_review.md` before quoting any IC, IR or spread from this repository.

---

## 🗂️ Codebase Architecture & File Map

```
.
├── AGENTS.md                   # This operating manual (CLAUDE.md, .cursorrules, .windsurfrules symlink here)
├── README.md                   # Public repository documentation
├── requirements.txt            # Python dependencies
├── config.py                   # Repo-relative DB/UI paths and audit thresholds
├── quant_engine.db             # SQLite persistent database
│
├── harness_v16_learning.py     # Data extraction + scoring pipeline (Nifty 500)
├── weight_optimizer.py         # Idempotent multi-period Rank-IC optimizer + attribution
├── quant_math.py               # Core quantitative scoring formulas
├── eval_portfolio_health.py    # Health suite incl. walk-forward test of the learning rule
├── update_ui_v16.py            # SQLite to ui/data.js export generator
├── concall_analyzer.py         # Headline keyword sentiment (diagnostic only; NOT concall NLP)
├── db_setup.py                 # Schema creation + idempotent migrations (ensure_schema)
├── daily_cron.sh               # Pipeline runner (harness → optimizer → UI → health)
├── test_quant_math.py          # Scoring-math unit tests + red-team regressions
├── test_optimizer.py           # Optimizer pure-function tests (no DB, no network)
├── harness_v15_institutional.py, update_ui_v15.py, v15_nifty50_top.csv   # legacy, unused
│
├── docs/
│   └── analysis/
│       ├── red_team_review.md               # READ FIRST: what the evidence does and does not show
│       ├── multi_period_regime_audit.md     # Original backtest report (see correction note at top)
│       ├── historical_runs_log.md           # Run-by-run log of weights & top stocks
│       └── turnaround_edge_case_study.md    # Case study on Genus Power & hyper-capex
│
└── ui/
    ├── index.html              # Dashboard markup
    ├── app.js                  # Frontend logic & Chart.js rendering
    ├── style.css               # Glassmorphism styling
    └── data.js                 # Exported data payload from update_ui_v16.py
```

---

## 📊 Database Schema Guide (`quant_engine.db`)

Agents must interact with SQLite using `sqlite3.Row` row factories:

### 1. `daily_predictions`
Stores daily/periodic snapshot runs of the entire 500-stock universe:
* `id` (INTEGER PRIMARY KEY)
* `date` (TEXT, ISO format `YYYY-MM-DD`)
* `ticker` (TEXT, e.g. `HEROMOTOCO.NS`)
* `price` (REAL, closing price on that date)
* `quality_score`, `growth_score`, `valuation_score`, `risk_score`, `moat_score`, `bs_score`, `cap_alloc_score`, `smart_money_score` (REAL, 0.0 - 100.0)
* `trap_score` (REAL, 0.0 - 100.0)
* `momentum_multiplier` (REAL, 0.0, 0.8, or 1.0)
* `final_score` (REAL, 0.0 - 100.0)
* `base_score` (REAL, weighted composite BEFORE trap/momentum multipliers; NULL for snapshots before Sep 2026)
* `raw_json` (TEXT JSON, full financial details including P/E, ROCE, SMA 50/200, FCF arrays, and `Data_Flags`: a list naming every input that was imputed or proxied)

### 2. `active_weights`
Tracks the historical evolution of the AI's internal brain weights:
* `id` (INTEGER PRIMARY KEY)
* `last_updated` (TEXT, `YYYY-MM-DD`)
* `quality_weight`, `growth_weight`, `valuation_weight`, `risk_weight`, `moat_weight`, `bs_weight`, `cap_alloc_weight`, `smart_money_weight` (REAL)
* `trained_through` (TEXT, end date of the last holding period this row learned from; drives optimizer idempotency)
* `note` (TEXT, provenance)

### 3. `performance_tracking`
Records forward-return verification pairs:
* `prediction_id` (INTEGER FK to `daily_predictions.id`)
* `forward_date` (TEXT)
* `forward_price` (REAL)
* `return_pct` (REAL)

---

## 🧠 Empirical Multi-Period Backtest Context

Read `docs/analysis/red_team_review.md` before using any of these numbers. Summary of what the three logged holding periods (Jun 14 → Jul 11 → Aug 14 → Sep 03, 2026) actually show after excluding one unadjusted 6:1 split:

### 1. Composite Rank IC and where it comes from
```
Rank IC vs next-period return                  Jun→Jul   Jul→Aug   Aug→Sep   mean
final score as stored                          -0.063    +0.092    +0.117   +0.049
momentum multiplier alone (0 / 0.8 / 1.0)      -0.033    +0.030    +0.125   +0.041
fundamental composite, no multipliers          +0.045    +0.058    +0.050   +0.051
equal-weight composite, no multipliers         -0.006    +0.115    +0.029   +0.046
```
The celebrated Aug→Sep +0.117 is mostly the death-cross trend filter. The "+2.21% downside alpha" that period was the death-cross bucket (-2.61%) vs everyone else (-0.35%); among ranked survivors the Q5-Q1 spread was +0.47% and not monotonic.

### 2. Per-factor Rank IC (three observations each; an IR from n=3 is not an estimate)
```
Factor          mean IC    periods > 0    caveat
Smart Money     +0.065     3/3            period-1 value was a 4-level holdings score, not flow
Growth          +0.056     2/3            pinned at the 30% weight ceiling
Balance Sheet   +0.026     2/3            84% of universe sits on one value
Risk            +0.017     2/3            85% of universe sits on one value
Quality         +0.003     1/3
Moat            -0.011     1/3            96% of universe sits on one value; hand-picked list
Cap Alloc       -0.014     1/3            corrupted by dividend-yield unit bug until Sep 2026
Valuation       -0.024     1/3            63% of universe scores 0 (no intrinsic value computable)
```

### 3. Walk-forward test of the learning rule (weights learned only from earlier periods)
```
mean Rank IC, fundamentals only:   learned +0.031   equal weights +0.046   weights actually used +0.051
```
No demonstrated edge from learning yet. Re-run `python eval_portfolio_health.py` after each new monthly snapshot; the verdict line updates itself.

### 4. Known data caveats baked into historical snapshots
* Snapshots up to 2026-09-03 carry dividend yields multiplied by 100 (Cap-Alloc factor inflated) and treat missing ROE as ROE < 5% (trap score inflated). The health suite fails on the September snapshot for this reason until the harness is re-run.
* Returns are price-only (no dividends) and computed from `info.currentPrice` at run time, not adjusted closes.
* Headline sentiment (`concall_sentiment_score`) contributed up to +10 points to historical `final_score`; it contributes 0 from September 2026 (`quant_math.SENTIMENT_SCALE`).

## 🛠️ Agent Operational Playbooks

### Playbook A: Running a Full Data & Training Cycle
```bash
# 1. Activate environment
source venv/bin/activate

# 2. Apply schema migrations (idempotent)
python db_setup.py

# 3. Acquire fresh market data with the CURRENT weights (~5 mins; ~7 HTTP calls per stock)
python harness_v16_learning.py

# 4. Learn from any holding period not yet seen (no-op otherwise). Add --dry-run to inspect.
python weight_optimizer.py

# 5. Refresh frontend UI data
python update_ui_v16.py

# 6. Run health checks (exit code 1 on unit/bound errors; read the warnings too)
python eval_portfolio_health.py

# 6. Commit and push to GitHub
git add .
git commit -m "Automated ML Train Loop update ($(date +'%B %Y'))"
gh auth switch --user saurabnigam
git push origin main
```

### Playbook B: Running Health Checks & Unit Tests
```bash
# Run all unit tests (scoring math + optimizer pure functions)
pytest -q

# Run institutional portfolio health audit
python eval_portfolio_health.py

# Inspect what the optimizer would do without writing anything
python weight_optimizer.py --dry-run
```

### Playbook C: Debugging Why a Stock Failed Screening
To inspect why a specific stock (e.g. `EICHERMOT.NS`) failed:
```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('quant_engine.db')
c = conn.cursor()
c.execute('SELECT raw_json FROM daily_predictions WHERE ticker = ? ORDER BY date DESC LIMIT 1', ('EICHERMOT.NS',))
row = c.fetchone()
if row:
    d = json.loads(row[0])
    for k, v in d.items():
        if not isinstance(v, (list, dict)):
            print(f'{k}: {v}')
"
```

---

## ⚠️ Known Edge Cases & Defenses

1. **The Hyper-Capex Turnaround Edge Case:**
   * High-growth companies undergoing massive capital expenditure (e.g. `GENUSPOWER.NS`, `ADANIENT.NS`, `WAAREEENER.NS`) have negative Free Cash Flow and are rejected by the core conservative scoring.
   * **Rule:** Do NOT weaken the core FCF rules in `quant_math.py`. Instead, maintain the isolated interceptor in `update_ui_v16.py` (`Growth_Score >= 80` AND `FCF < 0`) which channels them into the dedicated "Turnaround" UI tab.
2. **Git Authentication Mismatch:**
   * The local machine has multiple GitHub accounts configured. If `git push` returns `403 Permission denied to saurabhni_Zeta`, always execute:
     ```bash
     gh auth switch --user saurabnigam
     git push origin main
     ```
3. **yfinance Unit Drift:**
   * `dividendYield` is returned in percent by yfinance ≥ 1.x (3.48 == 3.48%) but was a fraction in older versions. The harness prefers `dividendRate / price` and falls back to `quant_math.normalize_yield`. `debtToEquity` is a percent (357 == 3.57x). `returnOnEquity` is frequently `None` for Indian names; never coerce `None` to 0 in a threshold test.
4. **Unadjusted Corporate Actions in Forward Returns:**
   * Prices are stored as quoted on the run date. A split between snapshots shows up as a huge negative return (ZFCVINDIA, 6:1, June 2026). `weight_optimizer.forward_returns` excludes |return| > 60% and prints each exclusion; keep that guard.
5. **Rounding Residue in Weights:**
   * When normalizing active weights to 3 decimal places, roundoff can produce a sum of `0.999` or `1.001`. Always add the remainder to the top-performing factor to guarantee `sum == 1.000`.
