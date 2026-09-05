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

---

## 🗂️ Codebase Architecture & File Map

```
.
├── AGENTS.md                   # This operating manual
├── CLAUDE.md                   # Claude Code specific instructions
├── README.md                   # Public repository documentation
├── requirements.txt            # Python dependencies
├── quant_engine.db             # SQLite persistent database
│
├── harness_v16_learning.py     # Data extraction pipeline (Nifty 500)
├── weight_optimizer.py         # V18 Multi-Period Panel ML Optimizer
├── quant_math.py               # Core quantitative scoring formulas
├── eval_portfolio_health.py    # Health & out-of-sample backtest suite
├── update_ui_v16.py            # SQLite to ui/data.js export generator
├── concall_analyzer.py         # NLP sentiment parser for conference calls
├── daily_cron.sh               # Shell script for automated scheduling
├── test_quant_math.py          # 38 pytest unit tests
│
├── docs/
│   └── analysis/
│       ├── multi_period_regime_audit.md     # Empirical backtest report across 3 regimes
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
* `raw_json` (TEXT JSON, full financial details including P/E, ROCE, SMA 50/200, FCF arrays)

### 2. `active_weights`
Tracks the historical evolution of the AI's internal brain weights:
* `id` (INTEGER PRIMARY KEY)
* `last_updated` (TEXT, `YYYY-MM-DD`)
* `quality_weight`, `growth_weight`, `valuation_weight`, `risk_weight`, `moat_weight`, `bs_weight`, `cap_alloc_weight`, `smart_money_weight` (REAL)

### 3. `performance_tracking`
Records forward-return verification pairs:
* `prediction_id` (INTEGER FK to `daily_predictions.id`)
* `forward_date` (TEXT)
* `forward_price` (REAL)
* `return_pct` (REAL)

---

## 🧠 Empirical Multi-Period Backtest Context

When designing new factors or reviewing model performance, rely on the empirical results established across our multi-period panel:

### 1. The 3 Historical Regimes
* **Period 1 (June 14 $\to$ July 11, 2026, 27d, $+4.49\%$ Mkt Ret):**
  * Speculative bull rally. **Growth ($+0.135$ Rank IC)** and **Smart Money ($+0.098$ Rank IC)** dominated. Quality and Balance Sheet lagged.
* **Period 2 (July 11 $\to$ August 14, 2026, 34d, $+1.02\%$ Mkt Ret):**
  * Flight to safety. **Balance Sheet ($+0.171$ Rank IC)** and **Quality ($+0.148$ Rank IC)** surged. Growth dropped to $-0.048$ Rank IC. Top quintile generated $+2.16\%$ vs $+0.27\%$ bottom quintile.
* **Period 3 (August 14 $\to$ September 03, 2026, 20d, $-0.82\%$ Mkt Ret):**
  * Market correction. **Composite Model achieved its highest Rank IC ($+0.117$)**. Top AI picks lost $-0.49\%$ vs bottom-ranked $-2.71\%$ (**$+2.21\%$ excess return**).

### 2. Factor Information Ratio (IR) Hierarchy
* **Smart Money (Institutional Net Flow):** $\text{IR} = \mathbf{+1.335}$ (Most consistent alpha driver).
* **Growth Momentum:** $\text{IR} = \mathbf{+0.648}$ (Primary driver of multi-baggers).
* **Risk Safety:** $\text{IR} = \mathbf{+0.330}$ (Defensive downside protection).
* **Balance Sheet:** $\text{IR} = \mathbf{+0.200}$ (Counter-cyclical hedge during pullbacks).
* **Valuation (Low P/E):** $\text{IR} = \mathbf{-0.472}$ (Traditional value traps penalized).

---

## 🛠️ Agent Operational Playbooks

### Playbook A: Running a Full Data & Training Cycle
```bash
# 1. Activate environment
source venv/bin/activate

# 2. Acquire fresh market data (4 mins)
python harness_v16_learning.py

# 3. Optimize factor weights via Exponentiated Gradient (< 2 secs)
python weight_optimizer.py

# 4. Refresh frontend UI data
python update_ui_v16.py

# 5. Run health checks
python eval_portfolio_health.py

# 6. Commit and push to GitHub
git add .
git commit -m "Automated ML Train Loop update ($(date +'%B %Y'))"
gh auth switch --user saurabnigam
git push origin main
```

### Playbook B: Running Health Checks & Unit Tests
```bash
# Run pytest unit tests
pytest test_quant_math.py

# Run institutional portfolio health audit
python eval_portfolio_health.py
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
3. **Rounding Residue in Weights:**
   * When normalizing active weights to 3 decimal places, roundoff can produce a sum of `0.999` or `1.001`. Always add the remainder to the top-performing factor to guarantee `sum == 1.000`.
