# CLAUDE.md — Multi-Bagger Stocks ML Train Loop & Quant Engine

This file guides Claude Code (and other AI agents) when working in this repository.

## 🎯 Repository Overview
An institutional-grade Quantitative Machine Learning engine for the **Nifty 500** Indian equity universe. It pulls fundamental, technical, and sentiment data, computes a composite multi-factor score, and optimizes factor weights via continuous out-of-sample Rank-IC exponentiated gradient descent across historical regime transitions.

---

## 🏗️ Architecture & Key Files

| File | Purpose |
| :--- | :--- |
| `harness_v16_learning.py` | Data acquisition pipeline for 500 Nifty stocks using `yfinance`. Saves snapshots into SQLite. |
| `weight_optimizer.py` | V18 Multi-Period Panel Optimizer. Computes cross-sectional Rank ICs & updates factor weights. |
| `quant_math.py` | Core mathematical formulas: DCF, Bank P/B valuation, Margin of Safety, CAGR, factor scoring. |
| `eval_portfolio_health.py`| Quantitative validation suite: checks Rank IC, quintile monotonicity, bounded values, and non-degeneracy. |
| `update_ui_v16.py` | Exports latest SQLite predictions and active weights into `ui/data.js`. |
| `concall_analyzer.py` | NLP sentiment analyzer for earnings conference call transcripts. |
| `quant_engine.db` | SQLite database storing all historical snapshots, active weights, and tracking data. |
| `ui/` (`index.html`, `app.js`, `style.css`) | Glassmorphism dashboard displaying Top Picks, Factor Breakdowns, and Turnaround screen. |
| `daily_cron.sh` | Daily orchestration script. |

---

## ⚡ Developer & Operational Commands

### Environment Setup
```bash
# Recommended Python version: 3.10+
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Running the Machine Learning Loop
```bash
# 1. Fetch market data & record predictions for today (takes ~4 mins due to 0.5s rate-limiting)
python harness_v16_learning.py

# 2. Run multi-period panel gradient descent to optimize factor weights (< 2 seconds)
python weight_optimizer.py

# 3. Regenerate frontend UI data (instant)
python update_ui_v16.py

# 4. Run portfolio health & out-of-sample backtest suite
python eval_portfolio_health.py
```

### Running Tests
```bash
# Run all unit tests for quantitative math formulas
pytest test_quant_math.py

# Run unit tests with coverage
pytest --cov=quant_math test_quant_math.py
```

### Viewing the Dashboard
```bash
# macOS
open ui/index.html

# Linux
xdg-open ui/index.html
```

---

## 🗄️ Database Schema (`quant_engine.db`)

### `daily_predictions`
* `id` (INTEGER PRIMARY KEY)
* `date` (TEXT, ISO format: `YYYY-MM-DD`)
* `ticker` (TEXT, e.g. `HEROMOTOCO.NS`)
* `price` (REAL)
* `quality_score`, `valuation_score`, `growth_score`, `moat_score`, `risk_score`, `bs_score`, `cap_alloc_score`, `smart_money_score` (REAL, 0-100)
* `final_score` (REAL, 0-100)
* `trap_score` (REAL, 0-100)
* `momentum_multiplier` (REAL, 0.0, 0.8, or 1.0)
* `raw_json` (TEXT JSON containing all underlying metrics: P/E, ROCE, SMA 50/200, FCF arrays)

### `active_weights`
* `id` (INTEGER PRIMARY KEY)
* `last_updated` (TEXT)
* `quality_weight`, `growth_weight`, `valuation_weight`, `risk_weight`, `moat_weight`, `bs_weight`, `cap_alloc_weight`, `smart_money_weight` (REAL)
* **Invariant:** Sum of all 8 weights must equal strictly `1.000`. Individual bounds: `[0.05, 0.30]`.

### `performance_tracking`
* `id` (INTEGER PRIMARY KEY)
* `prediction_id` (INTEGER FK)
* `forward_date` (TEXT)
* `forward_price` (REAL)
* `return_pct` (REAL)

---

## 📐 Quantitative Factor Rules & Invariants

1. **Weight Constraints (Box Simplex):**
   * No factor weight may drop below `0.05` (5%) or exceed `0.30` (30%).
   * Weights must always sum to `1.000` (`100.0%`).
2. **Margin of Safety (MoS):**
   * Strictly bounded to `[-99.9, +99.9]` via clipping in `calculate_margin_of_safety`.
3. **Momentum Hard-Kill:**
   * A "Death Cross" (`price < sma50` AND `sma50 < sma200`) forces a `0.0x` multiplier. The stock score is hard-zeroed.
4. **DCF Growth Capping:**
   * DCF growth rates are floored at `2%` and capped at `18%` to prevent terminal value explosion.
5. **Rate-Limiting Protection:**
   * In `harness_v16_learning.py`, keep `time.sleep(0.5)` between Yahoo Finance requests to prevent IP bans.
6. **Zero-Latency In-DB Optimization:**
   * In `weight_optimizer.py`, transition prices for historical snapshots must be queried directly from SQLite, never re-fetched over the web.

---

## 🎨 Frontend Architecture
* The dashboard is **zero-dependency static HTML/JS/CSS**.
* Data flows via `ui/data.js` containing `window.dashboardData = { accepted: [...], rejected: [...], turnarounds: [...], weights: {...} }`.
* Never introduce heavy Node.js or bundler dependencies to `ui/` unless explicitly requested; keep it browser-native and instant to open.
