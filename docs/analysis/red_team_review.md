# Red-Team Review of the V18 Quant Engine (September 2026)

> **Answer up front.** The engine's headline claim, that a self-learning eight-factor model produced a +0.117 out-of-sample Rank IC and +2.21% top-minus-bottom spread in the August-September correction, does not survive inspection. Most of that number is a 50/200-day moving-average trend filter, not the fundamental factors. The learning loop has not yet shown any out-of-sample edge over equal weights. Three of the eight factors are close to constants. And two unit bugs in the data feed meant the September snapshot was scored on dividend yields of up to 1,256%. None of this means the idea is wrong. It means the evidence so far is much thinner than the documentation says, and several inputs were corrupt.
>
> Everything below was verified by re-running the numbers against `quant_engine.db` and, where units were in doubt, by live calls to Yahoo Finance. Code fixes shipped alongside this document are listed in section 9.

---

## 1. What the system does, in one picture

```
   Yahoo Finance ──► harness ──► 8 factor scores (0-100) ──► weighted sum = "base"
   (per stock)                                                    │
                                                                  ▼
                                    base × trap multiplier × momentum multiplier = final score
                                            (1.0/0.8/0.5/0.2)    (1.0 / 0.8 / 0.0)
                                                                  │
   next snapshot's price ──► forward return ──► Rank IC per factor ─┘
                                                     │
                                                     ▼
                              weights × exp(0.45 × 5 × IC)  →  clip to [5%, 30%], renormalise
```

Rank IC is the Spearman correlation between a score and the following month's return across the ~500 stocks. A value of +0.10 means "the ranking was mildly informative"; +0.05 is close to what a coin flip produces on 500 correlated stocks.

The "momentum multiplier" is the piece that matters most for this review. If a stock's price is below its 50-day average, which is itself below its 200-day average (a "death cross"), the final score is multiplied by zero. Roughly a third of the universe is zeroed on any given day.

```
Share of universe with final score exactly 0, by snapshot
  2026-06-14   175 / 499   35%
  2026-07-11    97 / 499   19%
  2026-08-14   103 / 499   21%
  2026-09-03   148 / 500   30%
```

---

## 2. Finding: the "composite alpha" is mostly the trend filter

Suppose you built no fundamental model at all and simply ranked stocks by the momentum multiplier (0, 0.8 or 1.0). Here is how that compares with the stored final score, period by period:

```
Rank IC vs next-period return                  Jun→Jul   Jul→Aug   Aug→Sep   mean
final score as stored                          -0.063    +0.092    +0.117   +0.049
momentum multiplier alone (3 values!)          -0.033    +0.030    +0.125   +0.041
fundamental composite, no multipliers          +0.045    +0.058    +0.050   +0.051
equal-weight composite, no multipliers         -0.006    +0.115    +0.029   +0.046
```

In the August-September period, the one the documentation celebrates, the three-valued trend flag on its own scored +0.125, higher than the full model's +0.117. The fundamentals contributed +0.050.

The quintile story changes even more. The audit reports "top picks lost only -0.49% while bottom-ranked lost -2.71%". But the bottom quintile in that period was almost entirely the death-cross bucket, which is a momentum bet, not a fundamentals bet. Remove the zeroed stocks and rank only the survivors:

```
                         killed (score 0)     survivors, quintile means (Q1 → Q5)
Jun→Jul  (+4.7% mkt)     +5.58%  (n=175)      +3.98  +2.95  +4.53  +4.00  +5.38    spread +1.40
Jul→Aug  (+1.0% mkt)     +0.24%  (n= 97)      +1.05  -0.60  +0.81  +2.08  +2.70    spread +1.66
Aug→Sep  (-0.8% mkt)     -2.61%  (n=103)      -0.75  +0.16  -0.63  -0.27  -0.28    spread +0.47
```

Two things stand out. In June-July the "killed" stocks were the best performers in the universe. And among survivors, the ordering is not monotonic in any period; the Aug-Sep spread among ranked stocks is +0.47%, not +2.21%.

**Mechanism.** The hard kill is a rule about price trend, and price trend has short-horizon autocorrelation in most markets. That is a legitimate signal, but it is a different signal from the one the repository claims to have built, and it is applied as a filter (destroying rank information for a third of the universe) rather than as a factor whose weight can be learned.

---

## 3. Finding: the learning loop has not shown out-of-sample value

The optimizer was never tested as a learning rule. The stored final score's IC is out-of-sample for the weights used at the time, but nobody asked: "if I had let the rule learn from period 1 only, would it have done better on period 2 than equal weights?" That test is now in `eval_portfolio_health.py` section 4:

```
Rank IC of a fundamental composite built with ...
period            learned-so-far    equal weights    weights actually in force
Jun→Jul              +0.011            -0.006             +0.045
Jul→Aug              +0.062            +0.115             +0.058
Aug→Sep              +0.018            +0.029             +0.050
MEAN                 +0.031            +0.046             +0.051
```

Learned weights underperform equal weights on average. The differences are far inside the noise band for three periods, so the honest statement is: **no evidence either way yet**. It is not "institutional-grade", it is "not yet measurable".

A second, mechanical problem made this worse: the optimizer was not idempotent. Every cron run re-applied the same gradient from the same three periods. The weight log shows it plainly:

```
run 11   2026-09-03   growth 28.2%   (V18 launch)
run 12   2026-09-03   growth 30.0%   (same day, same data, one more run)
```

Growth then sits pinned at the 30% ceiling, where the optimizer can no longer express any view. Fixed: each holding period is learned from exactly once, tracked by a new `trained_through` column; re-running is a no-op.

---

## 4. Finding: three factors are near-constants carrying 33% of the weight

```
Factor          weight   share of universe on the modal value    source of variation
risk            18.5%    85%  at 50                               a hand-typed ticker list + sector keywords
moat             9.0%    96%  at 50                               a hand-typed ticker list (18 names)
balance sheet    9.9%    84%  at 100                              two debt thresholds
```

A factor that is 50 for 426 of 500 stocks cannot produce a meaningful Rank IC; whatever correlation it shows comes from the few dozen exceptions. Worse, the exceptions are chosen with hindsight: MCX, BSE, CDSL and CAMS receive a moat of 100 because the author already knew they compounded. That is look-ahead bias written into the factor definition.

The strategic-risk factor also had a string bug. It tested `'IT' in sector`, and `'UTILITIES'` contains `'IT'`. Every utility was scored as a disruptable technology company with low ESG risk. Fixed with token-based matching, and regression-tested.

---

## 5. Finding: the data feed had unit bugs that corrupted two factors

Verified live against Yahoo Finance on 2026-09-05:

```
HEROMOTOCO.NS  dividendYield = 3.48        (already a percent)
               returnOnEquity = None
PNB.NS         dividendYield = 2.58
```

The harness multiplied `dividendYield` by 100 a second time and then compared it with thresholds written for fractions (`> 0.05` means 5%). Consequences in the September snapshot:

```
stocks with recorded dividend yield > 25%      327 / 500   (max 1,256%)
stocks with capital-allocation score ≥ 90      385 / 500   (every dividend payer got the +40 "high yield" bonus)
```

`returnOnEquity` is missing for many Indian names. The code treated `None` as 0, which scored as "ROE below 5%" and added +20 to the value-trap score. 288 of 500 stocks carried that phantom penalty.

A third bug was silent rather than corrupting: the value-trap check for "declining profits" could never fire, because the harness floored profit growth at +1% before passing it in.

All three are fixed in the harness and covered by tests. **The September snapshot in the database still contains the corrupted values**; the health suite now fails on them until the harness is re-run. That is the correct behaviour.

---

## 6. Finding: the backtest returns were not corporate-action adjusted

```
ZFCVINDIA.NS   2026-06-14  ₹14,714.00
               2026-06-24  6-for-1 stock split
               2026-07-11  ₹ 2,333.80        recorded "return": -84.1%
```

A single split moved one quintile's mean by 0.9 percentage points in a period where the total claimed spread was 0.66. Rank IC is robust to one outlier; quintile means are not. Any move beyond ±60% in a one-month window is now excluded and printed as a suspected corporate action. Dividends are also ignored throughout, which biases against high-payout names by roughly 0.1-0.3% per month.

---

## 7. Finding: the "concall sentiment" term bypassed the weight budget

The function named `concall_analyzer` does not read conference calls. It counts words such as "up", "record" and "fine" in the last five Yahoo news headlines, which are frequently about a different company (Hero MotoCorp's "catalyst" on 3 September was an Ather Energy story). Its output was added straight into the composite:

```
base_score += sentiment / 2          → up to +10 points, outside the eight weights
stocks receiving the maximum +10     206 / 500
```

Ten points is larger than the entire capital-allocation and smart-money weights combined. Its measured Rank IC was +0.114, -0.007, +0.040. That is not nothing, but it is not a validated signal either, and the input is unreliable. The term is now recorded and reported in the attribution table but contributes zero to the score (`SENTIMENT_SCALE = 0.0`). Set it back to 0.5 to restore the old behaviour.

---

## 8. Smaller findings

- **Every script hard-coded a private scratch path.** A fresh clone could not run anything in AGENTS.md's playbook. All paths are now relative to the repository, overridable with `QUANT_DB_PATH`.
- **The cron script ran the optimizer before the harness**, the reverse of the documented playbook, and from the wrong directory.
- **A failed harness run deleted the day's snapshot** before discovering it had nothing to write. Now a partial run cannot overwrite a full one.
- **The Nifty 500 fetch fell back to 50 stocks silently.** It now falls back to the last full snapshot's constituent list.
- **`performance_tracking` had 6,409 rows for 2,043 predictions** because "INSERT OR REPLACE" had no unique key. Deduplicated and indexed.
- **Growth defaults:** missing revenue or profit history was imputed as +15% growth, so a company with no data scored like a fast grower. Now flagged (`Data_Flags` in each record) and imputed at +5%. The composite's "EBIT" and "EPS" legs were both silently the net-profit CAGR; they now read the real rows when present.
- **DCF used one year of free cash flow.** Hero MotoCorp's FY26 FCF was 2.1x FY25 and was extrapolated at the 18% cap for five years, producing an intrinsic value of ₹11,111 against a ₹5,308 price. The DCF now uses the three-year average.
- **Sector taxonomy mismatch.** Yahoo returns "Basic Materials", "Industrials", "Consumer Cyclical"; the WACC table looked for "COMMODITY", "CAPITAL GOODS", "FMCG". Most of the universe fell through to the default. Mapped.
- **Unit tests asserted bugs.** One test was literally named "BUG: fcf_conv=0.5 gets 0 points" and passed. Thirty-eight tests passed while none of the above was caught. Twenty regression tests added.
- **The README calls the frontend zero-dependency**; it loads Chart.js from a CDN and fonts from Google. Not fixed; noted.
- **Survivorship** is minor here (one ticker dropped between snapshots) because each snapshot re-fetches the current constituent list, which is the right design.

---

## 9. What changed in the code

```
config.py                    new: repo-relative paths, thresholds
quant_math.py                token sector matching; Yahoo taxonomy in WACC; normalize_yield;
                             estimate_growth (flagged); normalized_fcf; calc_base_score;
                             SENTIMENT_SCALE = 0.0; None-safe trap score
harness_v16_learning.py      unit fixes; real EBIT/EPS growth; 3-yr FCF for DCF; data flags;
                             base_score stored; partial-run guard; universe fallback
weight_optimizer.py          idempotent (trained_through); split filter; attribution table;
                             t-stats; pure functions; --dry-run / --force
eval_portfolio_health.py     unit sanity; near-constant detection; reconciliation check;
                             walk-forward learning test; exit 1 on real errors
db_setup.py                  ensure_schema() migrations; unique index; dedupe
update_ui_v16.py, ui/app.js  repo paths; data-quality panel; snapshot meta; legacy unit display
daily_cron.sh                correct order, repo-relative
test_quant_math.py, test_optimizer.py    58 tests (was 38)
```

Not changed, deliberately: the 0.0x death-cross multiplier, the [5%, 30%] weight bounds, the 0.5s throttle. Those are documented invariants. The evidence in section 2 argues for turning the hard kill into a learnable factor, but that is a modelling decision for the owner.

---

## 10. Where this review could itself be wrong

1. **Small sample.** Three monthly periods. Every "mean IC" in this document has a standard error of roughly 0.05, larger than most of the differences discussed. The findings about bugs (sections 4-8) do not depend on the sample; the findings about alpha (sections 2-3) are "not proven", not "disproven".
2. **Correlated cross-section.** The t-statistics printed by the tools assume 500 independent observations. Stocks share market and sector moves, so the effective sample is smaller and significance is overstated. This cuts against the original claims, not in their favour.
3. **Price-only returns.** Without dividends, high-payout stocks look worse than they were. This could slightly understate the capital-allocation factor.
4. **The split filter could remove real moves.** A ±60% cap over 20-34 days will very rarely drop a genuine multi-bagger leg. It is printed each time so it can be inspected.
5. **Regime labelling is post hoc.** "Speculative rally", "flight to safety" and "correction" are names given after seeing the returns. A fourth month may not fit any of them.

---

## 11. The whole argument in one diagram

```
                     claimed                          measured
                ┌────────────────────┐          ┌──────────────────────────────┐
  composite IC  │ +0.117, "highest   │          │ +0.117, of which trend flag  │
  (Aug→Sep)     │ of the year"       │  ───►    │ alone +0.125, fundamentals   │
                │                    │          │ +0.050                       │
                ├────────────────────┤          ├──────────────────────────────┤
  Q5 - Q1       │ +2.21% downside    │          │ +0.47% among ranked stocks;  │
  spread        │ alpha              │  ───►    │ the rest is "avoid death-    │
                │                    │          │ cross names"                 │
                ├────────────────────┤          ├──────────────────────────────┤
  learning      │ self-correcting,   │          │ learned weights -0.016 IC    │
                │ exponentiated      │  ───►    │ vs equal weights; not        │
                │ gradient           │          │ idempotent; pinned at 30%    │
                ├────────────────────┤          ├──────────────────────────────┤
  inputs        │ institutional-     │          │ yields ×100, ROE None→0,     │
                │ grade              │  ───►    │ 3 factors ~constant, split   │
                │                    │          │ in returns                   │
                └────────────────────┘          └──────────────────────────────┘
```

**One sentence for leadership:** the pipeline is sound as plumbing and now has honest instrumentation, but after three months the only signal it has demonstrated is a moving-average trend filter, and the fundamental model and its learning loop need at least six more monthly periods of clean data before anyone should size a position on them.

**Confidence.** That the bugs and attribution described here are real: 95%. That the fundamental composite will show a positive, stable out-of-sample IC once the bugs are fixed and more periods accumulate: 40%.
