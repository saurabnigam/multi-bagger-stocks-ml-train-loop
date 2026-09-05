> **Correction (2026-09-05).** This report is preserved as written on 2026-09-03. A subsequent red-team review found that (a) the Aug→Sep composite Rank IC of +0.117 is mostly the death-cross trend filter (momentum flag alone: +0.125; fundamentals alone: +0.050); (b) the "+2.21% downside alpha" compares the zeroed death-cross bucket with everyone else, and the Q5−Q1 spread among ranked survivors was +0.47%; (c) Period 1 numbers include an unadjusted 6:1 split (ZFCVINDIA, −84%); (d) the Information Ratios are computed from three observations and the Cap-Alloc factor was corrupted by a dividend-yield unit bug. See [red_team_review.md](red_team_review.md) for the corrected tables.

# Multi-Period Market Regime Audit & Empirical Backtest (Summer 2026)

## 📌 Executive Summary
Between June 14, 2026 and September 3, 2026, the **V18 Quantitative ML Engine** tracked and evaluated 500 Nifty stocks across three distinct market regimes. This document records the empirical findings, factor out-of-sample predictive power (Rank IC), quintile spread performance, and lessons learned from the multi-month walk-forward backtest.

---

## 📅 The Three Market Regimes

The dataset spans 81 calendar days across 500 stocks, cleanly segmented into three out-of-sample forward holding periods:

```
[June 14, 2026] ──(27 Days: Speculative Rally)──> [July 11, 2026]
[July 11, 2026] ──(34 Days: Flight to Quality)───> [August 14, 2026]
[August 14, 2026] ──(20 Days: Market Pullback)────> [September 03, 2026]
```

### Summary Comparison Table
| Metric | Period 1 (Jun 14 - Jul 11) | Period 2 (Jul 11 - Aug 14) | Period 3 (Aug 14 - Sep 03) | Multi-Period Aggregate |
| :--- | :---: | :---: | :---: | :---: |
| **Duration** | 27 days | 34 days | 20 days | **81 days total** |
| **Universe (N)** | 499 stocks | 498 stocks | 499 stocks | **500 stocks** |
| **Market Mean Return** | $+4.49\%$ | $+1.02\%$ | $-0.82\%$ | $+4.69\%$ |
| **Market Median Return** | $+3.60\%$ | $-0.07\%$ | $-0.79\%$ | $+2.74\%$ |
| **Market Volatility (Std)** | $9.70\%$ | $8.75\%$ | $7.12\%$ | $8.52\%$ |
| **Model Composite Rank IC** | $-0.066$ | $+0.092$ | **$+0.117$** | **$+0.048$** |
| **Top Quintile (Q5) Return** | $+5.36\%$ | **$+2.16\%$** | **$-0.49\%$** | **$+7.03\%$** |
| **Bottom Quintile (Q1) Return**| $+6.03\%$ | $+0.27\%$ | $-2.71\%$ | $+3.59\%$ |
| **Spread (Q5 - Q1)** | $-0.66\%$ | **$+1.89\%$** | **$+2.21\%$** | **$+3.44\%$ Alpha** |

---

## 🔍 Detailed Period Breakdowns

### Period 1: The High-Beta / Growth Rally (June 14 $\to$ July 11, 2026)
* **Market Context:** Broad speculative momentum across Indian equities. Mean market gain $+4.49\%$.
* **Factor Dynamics:**
  * **Growth** was the runaway winner with a **$+0.135$ Rank IC**. Stocks with explosive revenue and earnings growth were rewarded aggressively.
  * **Smart Money (Institutional Net Flow)** was second best with **$+0.098$ Rank IC**.
  * **Balance Sheet ($-0.112$ Rank IC)** and **Quality ($-0.055$ Rank IC)** severely lagged. The market favored leverage and risk over conservative cash cows.
  * Traditional **Valuation ($-0.060$ Rank IC)** penalized low-P/E names, as cheap value stocks underperformed high-multiple compounders.

### Period 2: The Defensive Regime Rotation (July 11 $\to$ August 14, 2026)
* **Market Context:** Market momentum flattened ($+1.02\%$ mean return, median negative at $-0.07\%$). Breadth weakened significantly.
* **Factor Dynamics:**
  * A dramatic 180-degree rotation into safety!
  * **Balance Sheet Health surged to $+0.171$ Rank IC** (the single strongest factor performance across the entire summer). Low-debt, high-liquidity companies dominated.
  * **Business Quality jumped to $+0.148$ Rank IC** (companies with high ROCE and strong cash conversion protected capital).
  * **Capital Allocation (Dividends & Payout)** delivered **$+0.100$ Rank IC**.
  * **Growth collapsed to $-0.048$ Rank IC**, as high-multiple growth names faced valuation compression.
  * **Quintile Performance (Monotonic):**
    * Q1 (Lowest Score): $+0.27\%$
    * Q2: $+0.33\%$
    * Q3: $+0.82\%$
    * Q4: $+1.52\%$
    * Q5 (Top AI Picks): **$+2.16\%$**
    * Delivered **$+1.89\%$ outperformance with perfect step-by-step monotonicity**.

### Period 3: The Market Drawdown & Downside Alpha (August 14 $\to$ September 03, 2026)
* **Market Context:** Market retreated with a mean return of **$-0.82\%$**. Small and mid-caps saw pronounced selling.
* **Factor Dynamics:**
  * **Smart Money (Institutional Flow)** proved decisive with a **$+0.085$ Rank IC**. Institutional accumulation signaled where institutions were defending positions.
  * **Growth bounced back to $+0.082$ Rank IC**, as resilient top-line growth became scarce and valued.
  * **Composite Model Performance:**
    * Delivered the **highest single-period Rank IC of the year: $+0.117$**.
    * Q1 (Lowest Score): **$-2.71\%$**
    * Q2: $-1.45\%$
    * Q3: $-0.79\%$
    * Q4: $-0.68\%$
    * Q5 (Top AI Picks): **$-0.49\%$**
    * **Downside Alpha:** While low-ranked speculative stocks tumbled $-2.71\%$, the AI's top recommendations held value, losing only $-0.49\%$—generating **$+2.21\%$ excess return**.

---

## 📈 Multi-Period Factor Performance Summary

```
Factor           Aggregate Rank IC    IC Volatility    Information Ratio (IR)
-----------------------------------------------------------------------------
Smart Money           +0.0641            0.0383                +1.335  ⭐⭐⭐
Growth                +0.0524            0.0770                +0.648  ⭐⭐
Risk Safety           +0.0143            0.0394                +0.330  ⭐
Balance Sheet         +0.0371            0.1158                +0.200  ⭐
Quality               -0.0052            0.1040                +0.015  
Moat                  -0.0148            0.0431                -0.198  
Capital Allocation    -0.0142            0.0797                -0.140  
Valuation (Low P/E)   -0.0120            0.0383                -0.472  
```

### Key Takeaways for Quant Strategy
1. **Smart Money has the Highest Sharpe:** With an Information Ratio of **$+1.335$**, institutional delta was the only factor that remained consistently positive across every single market environment.
2. **Growth vs Balance Sheet Barbell:** Growth and Balance Sheet act as complementary counter-cyclical hedges. When Growth lags, Balance Sheet surges, and vice versa.
3. **The Death of Simple "Deep Value":** P/E and simple valuation metrics had negative correlation across the panel ($-0.472$ IR). Buying purely "cheap" stocks resulted in severe value traps in the Indian market during 2026.
