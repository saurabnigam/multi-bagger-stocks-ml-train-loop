# Pipeline rerun of 2026-11-04: real run blocked, three structural blockers found, preview ranking as of 2026-11-03

Written 2026-11-05. Evidence: `latest_run_2026-11-04_evidence/` (published and restored-factor rankings,
counterfactual validation).

> **Answer.** The four corporate-action approvals are recorded on the real database and the code is
> pushed. The real October run executed and was correctly blocked: no capture existed before the 30 Oct
> cutoff. The rerun also exposed three structural problems that will block the November cohort too unless
> decided on: (1) Yahoo does not deliver a session's closes until the next day, so no same-evening
> capture can ever pass the 98% close gate; (2) the contaminated September cohort fails the G9 replay for
> every later cohort; (3) the 95% coverage rule for price factors, measured over all 501 members, now
> trips on the 27 young names the September rebalance brought in, and silently drops the whole low-risk
> family. The preview ranking as of 3 Nov is shown twice: as the engine published it (two factors
> dropped) and with those factors restored (validated to reproduce the engine exactly). The restored list
> overlaps 26 of 30 with the 26 Oct verification cohort.

## 1. What was done on 4-5 Nov

```
push        feat/evidence-triage -> origin; origin/codex/v2-implementation d65416d -> cd71e88 (fast-forward)
approvals   D-2026-11-04 TMPV demerger 1.6519 | D-2026-11-05 VEDL demerger 2.6722
            D-2026-11-06 TRENT manual_adj 1.5 | D-2026-11-07 POLICYBZR genuine (manual_adj 1.0)
            actor human:saurabh; each ADR records the chat approval and that Claude executed the command
real run    run monthly --as-of 2026-10-30 (23:46-00:37 IST): universe 501, 529 fundamentals bundles,
            prices through 2026-10-30, 1 new suspect (CONCORDBIO 2026-10-01, gross 0.50)
            -> BLOCKED  G3 "no completed as_of closes at the cohort vintage" (capture was 4 Nov)
                        G4 "no common priced names" (a consequence of G3)
            state DB 163 MB (> 50 MB budget): the runner refused to stage it; nothing committed
```

## 2. Three blockers for the 30 Nov cohort

### 2.1 Yahoo closes arrive the next day (vendor, not engine)
```
bar        observed at            closes present
30 Sep     1 Oct 07:44 (T+16h)    351 / 526  (67%)
26 Oct     27 Oct 14:47 (T+1.5d)  499 / 501  (99.6%)
 4 Nov     5 Nov 00:45 (T+9h)       0 / 528  (0%)
 4 Nov     5 Nov 11:50 (T+20h)    335 / 501  (67%)   -> G3 66.87% vs 98%, preview attempt blocked
```
The spec's cutoff is 23:59:59 IST on the as_of day, and G3 needs 98% of members to have that day's close
observed before the cutoff. With Yahoo as the only close source that cannot happen. September and October
were lost to missing captures; November will be lost to this even with a perfect capture.

Options (owner decision, Tier 2 either way):
- **A. Official close source for the as_of bar** (NSE bhavcopy, published the same evening). Right fix,
  needs the NSE terms check and a small adapter; the spec already lists NSE bhavcopy as an optional adapter.
- **B. Price-only vintage extension**: allow the as_of session's bars to be observed until the next
  session's open (T+1 09:00 IST), everything else unchanged at T 23:59:59. No look-ahead on returns is
  possible (the next session has not traded), but it is a clock-contract change and must be written into
  the spec with a decision record.
- **C. Score at T-1**: as_of = the session before month-end. Simplest, but the label endpoints move.
Recommendation: B now (unblocks November with a one-line rule and a test), A as the durable fix.

### 2.2 G9 replay fails against the contaminated September cohort
Probed on a copy of the real DB: replaying `live:2026-09-30` with current code gives
```
factor rows 12,024   mismatches 4,042   -> G9 FAIL, blocking
```
because the September closes are no longer visible at the September vintage (they carry their true
17 Oct fetch time). `_g9_replay` and `gates._prior_cohort` select the latest live cohort by date with no
clean filter; `cohorts` is immutable and not in the control allowlist, so `is_clean` cannot be flipped.
Evaluation, curves, leakage and walk-forward do honour `is_clean`.
Fix (Tier 2 + ~20 lines): prior-cohort selection skips cohorts with a recorded invalidation
(`is_clean = 0` or a BLOCK event `DQ_PRICE_PROVENANCE_FAIL` / `COHORT_INVALIDATED`), plus a governed way
to record the invalidation (a decision kind `invalidate_cohort` writing the event). Do not rewrite history.

### 2.3 The 95% coverage rule drops the low-risk family
`price_min_coverage = 0.95` is measured over all 501 members. After the September rebalance 27 members
have under a year of history (Groww, Lenskart, LG India, Meesho, Tata Capital, ICICI AMC, Canara HSBC
Life, Pine Labs, the four Vedanta spin-offs, Emmvee, and others) and four more are truncated at unresolved
suspects. On 26 Oct `mom_12_1` and `vol_252` covered 95.0%+; on 3 Nov 473/501 = 94.4%, so G8 excluded
both, the low-risk family vanished, and banks were scored on 4 factors.
```
                                  published 3 Nov     with the two factors restored
top-30 overlap with 26 Oct             16 / 30                 26 / 30
Spearman between the two lists                     0.771
NTPC / ICICI Bank / Tech Mahindra        155 / 154 / 162          14 / 28 / 29
```
Fix (Tier 2): measure coverage over members that can structurally have the factor (listing age >=
lookback, history not truncated), or over eligible members; keep 95% as the bar. Until then the two most
important long-window factors are one IPO away from disappearing.

## 3. Preview ranking as of 2026-11-03 (sandbox)

Built from the committed clean state plus a frozen-clock capture stamped 3 Nov 23:58 IST (the capture
itself ran 5 Nov 00:45-11:44, 11 hours: Yahoo throttled after three back-to-back sessions). Approvals
replayed as on the real DB. 477 ranked; gates G1-G8, G10 pass; G4/G9 deferred (no prior cohort in that
state). The engine published it with `mom_12_1` and `vol_252` excluded (2.3). The "restored" column
re-runs the engine's own `score_all` on the stored factor values with only the two prerequisite-awaiting
factors excluded; with the published exclusion list it reproduces all 477 published ranks exactly.

| Restored rank | Published | Symbol | Sector | Final | Factors | Families G L M Q V |
|---|---|---|---|---|---|---|
| 1 | 1 | WELCORP | Capital goods | 2.445 | 9 | +1.46 −0.68 +2.22 +0.85 +0.44 |
| 2 | 22 | LICHSGFIN | Housing finance | 2.296 | 6 | +0.35 +0.83 +0.01 — +2.16 |
| 3 | 3 | PNB | PSU bank | 2.296 | 6 | +1.76 +0.62 +0.61 — +1.28 |
| 4 | 28 | LUPIN | Pharma | 2.276 | 9 | +1.78 +1.57 −0.75 +0.77 +1.33 |
| 5 | 42 | HEROMOTOCO | Autos | 2.241 | 9 | +0.72 +1.34 +0.05 +0.67 +0.78 |
| 6 | 27 | AWL | FMCG | 2.085 | 9 | +0.34 +0.04 −0.74 +0.95 +1.37 |
| 7 | 8 | PRIVISCL | Chemicals | 2.070 | 9 | +2.02 +0.15 +0.74 +1.17 −0.52 |
| 8 | 9 | SAGILITY | IT services | 2.054 | 9 | +1.55 +0.05 +0.00 +0.19 +1.24 |
| 9 | 10 | EMMVEE | Capital goods | 2.019 | 7 | +2.41 — −0.75 −0.15 +1.57 |
| 10 | 29 | NYKAA | Consumer services | 2.019 | 9 | +1.86 +0.86 +1.31 +1.06 −1.39 |
| 11 | 56 | KAJARIACER | Durables | 1.960 | 9 | +0.27 +0.32 +0.56 +0.44 +0.50 |
| 12 | 13 | JSWSTEEL | Metals | 1.915 | 9 | +1.83 +1.89 +0.62 +0.22 +0.37 |
| 13 | 12 | IOC | Oil & gas | 1.890 | 9 | +1.32 +0.46 −0.64 +0.85 +1.36 |
| 14 | 155 | NTPC | Power | 1.890 | 9 | +0.34 +1.89 +0.15 −0.31 +0.81 |
| 15 | 4 | GLENMARK | Pharma | 1.822 | 9 | +1.53 +0.24 +0.24 +1.39 +0.38 |
| 16 | 49 | ADANIPORTS | Ports | 1.803 | 9 | +1.73 +1.24 +0.80 +0.19 −0.31 |
| 17 | 66 | APLAPOLLO | Capital goods | 1.796 | 9 | +0.18 +2.00 +0.51 +1.31 −0.39 |
| 18 | 65 | MRF | Autos | 1.780 | 9 | +1.30 +1.52 −0.71 +0.29 +0.98 |
| 19 | 2 | BANKINDIA | PSU bank | 1.775 | 6 | +1.08 −0.06 +0.24 — +2.05 |
| 20 | 55 | KPIL | Construction | 1.769 | 9 | +0.40 +0.87 +0.86 +1.19 +0.10 |
| 21–30 | | INDUSTOWER, NUVOCO, CENTRALBK, TMCV, GILLETTE, AUROPHARMA, PIDILITIND, ICICIBANK, TECHM, MOTHERSON | | | | |

Versus the verified 26 Oct top 30: in — GILLETTE, MOTHERSON, NYKAA, TECHM; out — AXISBANK, MARICO,
PVRINOX, WIPRO. Sector mix of the top 50: Financial Services 9, Capital Goods 8, Healthcare 5, Autos 4.
The 26 Oct verification (`ranking_verification_2026-10-26.md`) still applies to the 26 common names:
every top name is only partly supported, for the trailing-base reasons described there.

Six names have no restored score (LGEINDIA, LENSKART, MEESHO, PWL, BHARATCOAL, CMPDI: too few factors
once both long-window factors count); the published list ranked them on four to five factors, which is
the thin-name problem noted on 26 Oct. POLICYBZR's "genuine" replay landed in this sandbox, so its
−36% counts as a real return (rank 464).

## 4. The November triage queue (sandbox `triage build --as-of 2026-11-30`)

| Anomaly | Proposal | Note |
|---|---|---|
| HEG (HEGAM) 2026-09-07, gross 0.37 | pending_listing, medium (R1, placeholder linked by ISIN) | wait for HEG Graphite listing |
| CONCORDBIO 2026-10-01 gross 0.50 and 2026-11-02 gross 0.42 | unknown, low | two large drops a month apart; attach filings |
| SWIGGY 2026-11-02, gross 0.71 | unknown, low | a −29% day after the 23 Oct −16%; likely genuine (Judgment) |
| JSWDULUX 2026-11-02, gross 0.085 | unknown, low | a −91.5% step is almost certainly a listing or vendor artefact (Speculation, High) |
| 31 older suspects (2015-2025) | hidden by default | resolve or leave truncated |

## 5. What would make this wrong
1. The Yahoo timing table has four observations; the fill time may vary by day and by ticker.
2. The restored ranking is my offline recomputation, validated on 477 of 477 published ranks with the
   published exclusion list; it is still not a published cohort.
3. Fundamentals in the preview were fetched on 5 Nov and stamped 3 Nov: results posted on 4 Nov may leak
   in (a few names in results season).
4. The 11-hour capture suggests rate limiting that I inferred, not logged; the client prints no 429s.

```
confidence in the three blockers (mechanism verified in code and by probe):            ~90%
confidence that option B alone makes the 30 Nov cohort publish cleanly:                ~70%
   (needs 2.2 and 2.3 fixed too, and no new vendor hole)
```

## 6. Next actions
```
owner      decide 2.1 (B now, A later), authorise 2.2 and 2.3 as Tier-2 changes; capacity decision for the
           163 MB state DB (ledger compaction D8); how the Sep cohort is recorded
code       implement 2.1B, 2.2, 2.3 with tests (one work package each), then re-run October as a
           counterfactual track if wanted
30 Nov     capture the same evening (fundamentals), prices the next morning before 09:00 IST under 2.1B;
           resolve the triage queue before the cutoff
```
