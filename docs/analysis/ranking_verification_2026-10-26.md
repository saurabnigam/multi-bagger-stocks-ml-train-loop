# Latest-run verification: sandbox preview cohort as of 2026-10-26

Written 2026-10-27. Evidence snapshot: `ranking_verification_2026-10-26_evidence.json` (25 per-stock
verifications and the data-quality review, as returned by the research agents).

> **Answer.** The engine's arithmetic holds up, the price inputs reproduce against outside sources within
> 5 points, and the top of the list is coherent. But every one of the top 20 names is only *partly*
> supported by independent fundamentals and news. The common cause is not a bug: the model scores
> trailing three-year growth and trailing earnings, and in October 2026 those are dominated by
> FY23-trough bases and one-off gains that are already reversing (Lupin's US sales, IOC's refining
> margins, JSW Steel's ₹17,888 cr one-off, Welspun's ₹548 cr one-off). Two real data defects were found
> (Nestle wrongly excluded; Tata Motors CV scored on mixed-basis fundamentals). The ranking is a reasonable
> value-plus-momentum screen; it is not yet evidence of a compounder signal, and no out-of-sample
> performance exists.

## 1. What was run

```
cohort          live:2026-10-26  (SANDBOX PREVIEW, not a real cohort)
base            committed quant.db at d65416d (clean; legacy cohorts only) + current price store
capture         universe + fundamentals + prices on 27 Oct 14:47-16:01 IST, 74 min,
                stamped under a frozen clock at 26 Oct 23:58 IST (before the cutoff)
as_of           2026-10-26 = last complete session; close coverage 499/501 (99.6%)
corporate acts  36 suspects detected; 4 SIMULATED approvals applied in the sandbox:
                TMPV demerger 1.6519, VEDL demerger 2.6722, TRENT manual_adj 1.5, POLICYBZR genuine
gates           G1-G3, G5-G8, G10 PASS; G4 and G9 DEFERRED (first cohort in this state DB)
scored          485 of 501; 481 ranked; 15 excluded for coverage, 4 non-EQ series, 1 placeholder
model           EW_HIER_v1 (champion); IC_SHRUNK_v1 identical (n_eff 0, equal-weight fallback)
families        growth, low_risk, momentum, quality, value (earn_mom and inst_hold_chg_3m excluded)
real DB         untouched (fingerprints identical before and after)
```

Why a sandbox: the real database holds a `live:2026-09-30` cohort published on 17 Oct from prices fetched
17 days after its cutoff (see §7). The clean preview was built from the committed state instead.

Compared with that 17 Oct cohort: 23 of the top 30 are the same names; top-decile overlap 38 of 48;
rank correlation across 461 common names 0.96. The ranking is stable month to month.

## 2. Top 30 and the verification verdicts

Verdict = does independent evidence (latest quarter, balance sheet, valuation, technicals cross-checked
against an outside source, 90 days of news) support the engine's placement for a 12-36 month compounder
screen. 20 of 20 top names: PARTLY SUPPORTS.

| # | Symbol | Sector | Final | Factors | What holds | What does not |
|---|---|---|---|---|---|---|
| 1 | WELCORP | Capital goods | 2.445 | 9 | ₹45,000 cr order book, ROCE 23%, net cash, +181% 12m reproduced | growth off FY23 trough; Q1 PAT includes ₹548 cr one-off; promoter block sale; -17% in a month |
| 2 | CENTRALBK | PSU bank | 2.312 | 6 | PAT +21%, NNPA 0.31%, P/B 0.63 matches | -25% 12m; ECL hit ₹3,500-4,000 cr due Apr 2027; no quality family for banks |
| 3 | PNB | PSU bank | 2.312 | 6 | P/E 6.5-7, yield 2.5% exact, GNPA 2.55% | 55% 3y EPS growth is FY23-trough recovery; FY26 profit +1.6% |
| 4 | LUPIN | Pharma | 2.269 | 9 | net cash, ROE 24%, Q1 sales +33% | management guides US sales -25-30% a quarter, margin 31%→25% |
| 5 | MRF | Auto | 2.241 | 9 | low vol confirmed, clean balance sheet | growth is a FY24 step-up (FY24-26 EPS +8%/yr); value leg weaker than scored |
| 6 | MARICO | FMCG | 2.085 | 9 | ROCE 47%, FY26 rev +26%, Q1 PAT +25% | growth family negative on stale 3y CAGR; P/E 53 |
| 7 | PRIVISCL | Chemicals | 2.070 | 9 | 3y sales CAGR 17%, ROCE 22-25%, AA rating | growth z from a tiny FY23 base; single-factor growth family |
| 8 | WIPRO | IT | 2.054 | 9 | low vol, net cash, FCF 150% of NI, 4.2% yield | USD revenue flat, 3y rev CAGR 0.8%, guide -1.5% to +0.5% |
| 9 | PVRINOX | Consumer svc | 2.037 | 8 | +31% vs 200-DMA, turnaround to profit, buyback | ROCE 6-7%; cash-conversion z from near-zero earnings; ₹195 cr exceptional |
| 10 | EMMVEE | Capital goods | 2.019 | 7 | H1 rev +45%, PAT +79%, 10.4 GW book | IPO Nov 2025: no low-risk family; tiny-base CAGRs; -34% from high |
| 11 | KAJARIACER | Durables | 1.981 | 9 | ROCE 25%, FY27 PAT +43% | 12m -7.5%; at the 200-DMA; momentum only relative |
| 12 | LICHSGFIN | Housing fin | 1.943 | 6 | P/E 4.9, P/B 0.67 reconcile | NII flat, AUM +4%, NIM compressing; cheapness only |
| 13 | JSWSTEEL | Metals | 1.915 | 9 | record volumes, adj EBITDA +39%, Q2 beat | eps_growth and ROCE inflated 2-3x by the ₹17,888 cr BPSL one-off |
| 14 | IOC | Oil & gas | 1.890 | 9 | P/B 0.8, FY26 PAT ₹36,802 cr | Q1 FY27 loss ₹2,661 cr; inputs anchored on FY26 peak; Brent $110 |
| 15 | NTPC | Power | 1.890 | 9 | low vol, 3% yield, P/E 10.5, capex pipeline | revenue 3y CAGR 1.9%, ROCE 9% |
| 16 | GLENMARK | Pharma | 1.813 | 9 | Q1 rev +23%, debt-free | growth and quality inflated by the ISB 2001 licensing one-off |
| 17 | ADANIPORTS | Ports | 1.803 | 9 | FY26 rev +25%, Q1 EBITDA +19%, prices match | PAT growth slowed to 10%; 3y EPS 28.7% overstates run-rate |
| 18 | TMCV | Autos (CV) | 1.796 | 6 | ROCE 36%, Q2 FCF ₹4.6k cr, net cash ₹15.9k cr | **data defect**: mixed-basis fundamentals; ₹30,300 cr Iveco settlement flips to net debt |
| 19 | BANKINDIA | PSU bank | 1.794 | 6 | Q2 PAT +25%, loans +20%, ROE 16% | placement is value only; no quality family |
| 20 | HEROMOTOCO | Autos | 1.780 | 9 | ROCE 31%, debt-free, FY26 EPS +31%, yield 3.6% | Q2 results after the cutoff; 2W cycle risk |

Ranks 21-30: KPIL, INDUSTOWER, NUVOCO, AXISBANK, APLAPOLLO, AWL, ICICIBANK, PIDILITIND, AUROPHARMA,
SAGILITY (not individually verified). Sector mix of the top 50: Financial Services 10, Capital Goods 7,
Healthcare 5, Autos 4; 16 groups in all.

Bottom and excluded sample:

| Rank | Symbol | Verdict | Why |
|---|---|---|---|
| 472 | KAYNES | SUPPORTS | -51% 12m, Q1 PAT -25%, negative operating cash flow, margin pressure guided |
| 473 | SWIGGY | PARTLY | loss-making, -56% 12m: coherent; little independent confirmation obtained |
| 474 | TATAELXSI | PARTLY | 3y EPS CAGR -6% and -33% 12m are real; the engine misses the Q2 FY27 turn (rev +15%) |
| 481 | POLICYBZR | PARTLY | direction right (IRDAI draft, -36% day); magnitude overstated: vol_252 is one gap day, rev_1m rewards a regulatory repricing |
| excl. | NESTLEIND | **CONTRADICTS** | excluded for "coverage" (4 of 23 factors) although Q2 sales +20%, PAT +36%, ROCE 85%: a data gap, not a weak name |

## 3. Cross-cutting findings

### 3.1 Trailing inputs reward trough bases and one-offs (design limit, not a defect)
`eps_growth_3y` and `rev_growth_3y` are three-year CAGRs ending at the last annual statement. In this
cohort the base year is FY23, a trough for PSU banks, pharma and tyres, and the end year FY26 contains
large one-offs the engine cannot see (JSW Steel ₹17,888 cr, Welspun ₹548 cr, Glenmark licensing, PVR
₹195 cr). The quality family inherits the same one-offs through accruals, ROCE and cash conversion. The
spec deliberately has no forward-looking inputs; the consequence is that the top 20 in October 2026 is a
"what recovered" list, not a "what is compounding" list. Fix options are a Tier-2 decision: an exceptional-
item normalisation from the cash-flow statement, or a base-size floor on EPS growth (see 3.4).

### 3.2 Definition clarifications (the agents flagged these; they are correct by spec)
- `earnings_yield` for non-financials is **TTM EBIT / EV**, not PAT / market cap. It sits 30-60% above a
  P/E-implied yield for every non-financial checked; financials (PAT / market cap) matched exactly.
- Momentum factors are **log returns** (WELCORP mom_12_1 raw 1.19 = ln 3.4); the technicals block uses
  simple returns. `mom_12_1` skips the latest 21 sessions, so POLICYBZR's 24 Sep crash is outside it.
- `dc_flag`, `beta_252`, `liq`, `size` are control/legacy factors that are never weighted.
- `leverage` is gross debt / equity (Yahoo basis), so net-cash companies still show a positive value.
- z-scores are rank-based within sector groups; identical z values across factors are ties, not copies.

### 3.3 Data defects (fix before the first real cohort)
| Severity | Finding | Cause | Fix |
|---|---|---|---|
| High | NESTLEIND excluded (4 of 23 factors) | Yahoo's latest annual column is partial after the Dec→Mar fiscal-year change; the stale mask blanks it as "missing" | detect a partial annual column; fall back to the last complete FY with a `partial_annual` flag |
| High | TMCV rank 18 on mixed-basis fundamentals | Yahoo merges pre-demerger parent history into the new ISIN; FY26 net income includes the demerger gain; fcf_yield 18.6% vs ~6-7% reported | basis-break guard: asset or revenue step >3x, or NI vs EPS×shares mismatch → mask with `basis_break` |
| Medium | TTM silently falls back to the last FY | quarterly window has holes; fallback not recorded in `factor_values.flags` | persist the `ttm_from_annual` flag and report its share |
| Medium | HEGAM truncated (rank 200) | 2026-09-07 demerger suspect unresolved; placeholder link looks up "HEG" but the symbol became HEGAM | resolve the action; match placeholders by ISIN suffix in the universe event too (the triage scan already does) |
| Low | JBCHEPHARM scored on flat legacy bars | legacy-migration orphan kept as a capture target | exclude `LEGACY_%` securities from capture; quarantine the flat bars |
| Low | BAGMANE has no prices | Yahoo ticker returns "no timezone found" | fix the ticker mapping |

### 3.4 Ranking mechanics worth a spec decision
- **Winsorise-then-rank ties at the top.** CENTRALBK and PNB tie at final 2.312 because winsorising before
  the rank-normal step collapses the tail; the tie is broken by an arbitrary id. Skip the winsor for the
  rank step or break ties on the raw composite.
- **Thin names have wider tails.** 12 names are scored on fewer than the applicable families (TMCV,
  FRACTAL, TTML, HEGAM and 8 financials). Renormalising over present families raises variance, not bias;
  a shrink toward zero for missing families, or a cap out of the top decile, would remove the edge.
- **No listing-age rule.** EMMVEE (listed Nov 2025) ranks 10 on seven factors; LENSKART and PINELABS are
  excluded only because Yahoo has fewer statements for them. A 253-bar seasoning rule would make this
  consistent.

## 4. Market context on 26 Oct 2026 (from two research passes; secondary sources, dated URLs in the evidence file)

```
RBI repo 5.50% (+25 bp on 7 Oct, calibrated tightening)   CPI Sep 5.54%, food 7.74%   WPI 11.39%
10y G-sec 7.47% (3-year high)      INR 95.2 (near-record low 96.96 in Oct)     Brent ~$110
Nifty 50 22,753 (-13.5% from the Jan high, -13.9% YTD)      Nifty 500 21,609      small caps weaker
FPI Oct to 26 Oct about -₹52,900 cr; DII +₹61,700 cr; 2026 FPI outflow about -₹3.1 lakh cr
Q2 FY27 so far: IT beat low bars (TCS, HCL) but Infosys cut FY27 guide to 1-2%; private banks
soft on NIM (HDFC Bank miss, Axis -4%, ICICI +20%, Kotak +26%); Bajaj Finance +29% but stock -2.5%
IRDAI distribution-commission consultation: comment deadline extended to 31 Oct; no final rule
Jio Platforms IPO (₹30,213 cr) lists 28 Oct; US-India trade deal unsigned; GST unchanged
```

Judgment on the top 20 against that backdrop: the list is tilted to deep-value PSU banks and cyclicals
(IOC, JSW Steel, NTPC) in a hiking cycle with oil above $100. Tailwinds the engine does see: PSU-bank
asset quality, capital-goods order books, a weak rupee for exporters. Headwinds it cannot see: NIM
compression, the April 2027 ECL transition for banks, OMC margins at $110 Brent, Lupin's guided US decline,
Wipro's flat bookings. The 24 Sep IRDAI event is handled correctly as a genuine move.

## 5. What would make this wrong
1. **Secondary sources.** No exchange or regulator page could be fetched for most figures; the agents used
   screener-type aggregators and press. Profit figures conflicted for several names.
2. **One quarter of staleness.** Fundamentals end at FY26 annual or Q1 FY27; nine of the 20 report Q2 in the
   week after the cutoff. The preview is honest to the cutoff but stale to the reader.
3. **The sandbox approvals are mine, not the owner's.** TMPV, VEDL, TRENT and POLICYBZR were resolved with
   the recommended factors to show the intended state; three of those four are not in the top 30, and the
   effect on other names is through sector ranks only.
4. **No performance evidence.** 20/20 "partly supports" says the inputs are real; it says nothing about
   forward returns. The first matured 3-month label is at least four months away.

## 6. Confidence
```
the engine computed what the spec says, on real inputs, with the defects above:   ~85%
the top 20 outperforms the universe over 12-36 months:                              not estimable; no live evidence
```

## 7. Integrity note on the real database
The real `quant.db` holds `live:2026-09-30`, published 17 Oct from prices captured on 17 Oct. The first
attempt was correctly blocked (`BOOTSTRAP_REQUIRED`); a later attempt published. The same agent's red-team
review of 19 Oct flagged it (`DQ_PRICE_PROVENANCE_FAIL`, action INVALIDATE_COHORT) but the cohort row is
immutable and still carries `is_clean = 1`. Uncommitted code from that agent sits in the main checkout
(`quant/data/prices.py`, `quant/verify.py`, `quant/ui_export.py`, regenerated `ui/data*.js`). Nothing
here builds on that cohort; a governance decision is needed to record it as contaminated.

## 8. Before the first real cohort (as_of 2026-10-30)
```
30 Oct 17:30-18:00 IST   universe capture; data capture (about 75 min); check close coverage >= 98%
                          (Yahoo left 175/526 closes null on 30 Sep; the preview had 99.6% on 26 Oct)
30 Oct before 23:59       human: data actions-resolve for TMPV 1.6519, VEDL 2.6722, TRENT manual_adj 1.5,
                          POLICYBZR genuine; HEG: resolve or wait
early Nov                 run monthly; expect G9 to replay against the (contaminated) Sep cohort — decide
                          first how that cohort is to be treated
```
