# Independent verification of cohort live:2026-09-11 — findings, decisions, tasks

Date: 2026-09-24. Branch: `review/verify-2026-09` (worktree `mb-verify`), commits `e13201d`, `fe9d9c2`,
`85b9cf4`, `bc84691` on top of `df35aa7`. Author: Claude Opus 5.5 with Sonnet verification agents.
Nothing here claims out-of-sample performance; no live cohort has been published from V2.

> **Answer.** The engine's arithmetic is right: every price factor, the standardisation, the composite, the
> eligibility screens and the ranking reproduce exactly from the raw vendor archives (501 of 501 securities,
> tolerance 1e-9). The inputs were not all right. Four data-handling defects distorted the lower half of the
> list: 311 of 488 ranks change and 73 move by 10 or more places once they are fixed. The top 30 set is
> unchanged; one top-15 name changes (MRF out, HEROMOTOCO in); 3 of the bottom 15 were wrong (TATAINVEST,
> TMPV, BEML). All 13 exclusions are correct. The defects are fixed on the review branch. The old-code
> `live:2026-09-11` in the main checkout should be discarded, not kept: next month's replay gate (G9) would
> reject it.

## 1. What "correct" was tested against

Suppose PNB is ranked first. To say that is correct, three separate things must hold: the raw inputs the
engine read are the vendor's numbers (inputs), the formulas turn those inputs into the published factor values
(arithmetic), and the ranking follows the specification from those values (pipeline). Each was checked by a
different agent that was not allowed to call engine code, then a second agent tried to refute each finding.

```
 raw archives (NSE list, Yahoo prices 2015-2026, 502 Yahoo statement bundles)
        |                         |                           |
  price verifier            fundamentals verifier       ranking verifier          web verifier
  58-name sample,           58 names x 11 factors,      all 501 names, all        public filings,
  7 price factors,          raw statement JSON          stages from factor        corporate actions,
  liquidity screens                                     values to rank            listing dates
        |                         |                           |                        |
        +------------- skeptic per verifier (re-derive, try to refute) -------------+
                                        |
                     orchestrator: adjudicate against raw data, fix, test,
                     re-run the cohort in an isolated sandbox, diff
```

Result per layer:

```
arithmetic   price factors      7 factors x 58 names   exact (max rel diff 7e-13), NaN pattern identical
             liquidity (ADV63)  57 of 57 priced names  exact; bucket agrees 3 ways
pipeline     standardisation    12,024 values          0 mismatches at 1e-9
             composite -> rank  501 of 501             exact, including tie order
inputs       fundamentals       638 cells              609 agree; 22 differ, 7 NaN mismatches, all traced
```

## 2. Verdicts on the names you asked about

### Top 15 (published) — correct except one swap

| Published | Symbol | Corrected rank | Verdict |
|---:|---|---:|---|
| 1 | PNB | 1 | correct |
| 2 | EMMVEE | 2 | arithmetic correct; ranked on 7 of 11 factors (listed 2025-11-18) — see decision D6 |
| 3 | LUPIN | 3 | correct (EPS growth used Basic EPS; 0.1% effect) |
| 4 | MRF | 20 | **fragile**: led HEROMOTOCO in its sector by 0.004 composite; TMPV's fix flipped them |
| 5–15 | LICHSGFIN, NYKAA, BBTC, SAGILITY, ATUL, WELCORP, JSWDULUX, YESBANK, JSWSTEEL, ADANIPORTS, BPCL | unchanged | correct |

NYKAA: book-to-price 0.0146 matches Yahoo's P/B of 68.5; ROCE 0.2857 reproduces from raw (TTM EBIT to
June 2026 of INR 5.29bn over total assets minus current liabilities of INR 18.5bn). Public ROCE (9.6–15%) is
lower because it uses FY2025 earnings and equity plus debt as capital. Definition, not a defect.

### Bottom 15 (published) — 3 wrong, 12 correct

| Published | Symbol | Corrected | Cause |
|---:|---|---:|---|
| 488 | TATAINVEST | 412 | 10:1 split: FY2023 EPS on the old share count read as -58.6%/yr EPS growth; corrected +18.1%/yr matches public restated EPS exactly |
| 486 | TMPV | 469 | Tata Motors demerger (2025-10-14) read as a -40% return |
| 478 | BEML | 437 | 2:1 split: -26.8%/yr read; corrected -3.6%/yr matches public exactly |
| 487, 485–479, 477–474 | KAYNES, MFSL, FIRSTCRY, ZYDUSWELL, MAPMYINDIA, FACT, ONESOURCE, COHANCE, BLUESTARCO, AIIL, JAINREC, SJVN | within 9 places | correct |

ZYDUSWELL ROCE 0.034 reproduces from raw (quarters not consecutive, so annual EBIT INR 3.26bn over INR
95.8bn of capital employed after an asset jump). The web verifier called TATAINVEST's -58.6% "plausible
for a holding company"; the data refutes that: the implied share ratio between the two EPS years is 10.001.

### The 13 exclusions — all correct

| Names | Reason | Evidence |
|---|---|---|
| HEG, HFCL | `non_eq_series` (not in NSE's EQ series) | NSE list shows series BE for both. HEG: demerger, record date 2026-09-07 |
| DUMMYHEG | `coverage` | NSE's own placeholder row for the spun-off HEG entity (fake ISIN DUM545A01024, no prices). Should get its own reason — task T9 |
| CANHLIFE, ICICIAMC, LGEINDIA, LENSKART, MEESHO, PWL, PINELABS, PIRAMALFIN, TATACAP, URBANCO | `coverage` | listed Sep–Dec 2025; 184–248 daily bars, below the 253 needed for 12-month factors |

### Names whose rank moved most after the fixes

```
VEDL        421 -> 40     demerger 2026-04-30 (-64.9% step) no longer read as a loss; now 6 factors until reviewed
TRENT       415 -> 122    vendor error: Yahoo applied the June-2026 bonus from 2026-01-01 only (-33% fake step)
TATAINVEST  488 -> 412    split-basis EPS
NETWEB      317 -> 372    TTM EBIT mixed three quarters of EBIT with one of Operating Income
HCLTECH      48 -> 83     same TTM mixing
```

38 of the 73 names that moved 10 or more places had no change in their own data. They share a sector with
VEDL, TMPV or TRENT (NSLNISP 313 -> 368, SAPPHIRE 286 -> 337, NATIONALUM 40 -> 69): standardisation and the
final score both rank within the sector, so fixing one member moves its peers. See decision D6.

## 3. Defects found and fixed

Each row: what the engine did before, what it does now.

| # | Defect | Before | After (commit) |
|---|---|---|---|
| 1 | Corporate actions read as returns | Demergers (TMPV, VEDL, HEG) and a vendor step (TRENT) entered momentum and volatility as real losses; `corporate_actions` empty | Unexplained jumps outside [1/1.4, 1.4] are recorded as suspected actions, stamped with the evidence's observation time; history before an unresolved suspect is not used; an approved value-transfer factor restores the economic return; labels spanning a suspect are `excluded_ca` (`fe9d9c2`) |
| 2 | Vendor alias ties | Diluted/Basic EPS, EBIT/Operating Income, Total/Operating Revenue share one fetch time; SQLite broke the tie alphabetically: EPS growth used Basic, revenue used Operating Revenue, equity used Common Stock Equity | Ties break by the field contract order; a newer fetch still wins (`bc84691`) |
| 3 | EPS growth across splits | Yahoo leaves one EPS endpoint on the old share count (TATAINVEST, BEML, HDFCBANK Diluted) or restates it twice (NEWGEN) | Implied share ratio (Net Income / EPS) compared with the split record; older EPS rescaled only when the ratio is within 1.5x of the split ratio (`bc84691`) |
| 4 | TTM mixing line items | Three quarters of EBIT plus one of Operating Income summed as "TTM EBIT" (NETWEB, URBANCO, HCLTECH) | Mixed quarters fall back to the latest annual value (`bc84691`) |
| 5 | Non-deterministic composite | Family order came from a Python set; floats differed across processes | Sorted families, canonical JSON (`e13201d`) |
| 6 | Duplicate open symbol rows | Every universe capture added a second open `symbol_history` row | One open row per security (`e13201d`) |
| 7 | Mid-month live cohorts | Runner accepted any `as_of` for the live track | Refused unless the as-of is the month's last session; sandbox harness opts out explicitly (`e13201d`) |
| 8 | Unbounded fact growth | Each capture re-inserted unchanged fundamentals | Stored again only when the value changes (`e13201d`) |
| 9 | Silent vendor holes | 2026-09-07: Close missing for 202 of 501 names; no gate looked past the as-of date | Non-blocking `W_PRICE_GAPS` warning names the dates (fires on this cohort: 200 members, 2026-09-07) (`e13201d`) |
| 10 | G8 allowance consumed on day one | earn_mom (needs 8 quarters) and inst_hold_chg_3m (needs 4 monthly captures) used both allowed exclusions | Factors declare a history prerequisite; G8 counts them only once it is met (`85b9cf4`) |

Tests: 430 pass (4 new regression test files, 2 updated); `check_spec.py` 10/10; sign-off engineering 8/8.

## 4. Runs performed

| Run | Code | Result |
|---|---|---|
| Reproduce published cohort | old | identical to the main checkout's cohort |
| Same cohort, different `PYTHONHASHSEED` | old | composite floats differed — defect 5 |
| Re-run on an existing state (idempotency) | fixed | no duplicate rows; G9 replay of prior cohort PASS |
| Second month on top (2026-09-23, sandbox only) | old and fixed | G9 PASS both; runtime 206–239 s vs 54–71 s |
| Alias fix only | `bc84691` part | 224 factor values change; 49 ranks, max HDFCBANK 231 -> 319; top 30 same |
| All fixes, two months (final) | `bc84691` | 311 ranks change vs published, 73 by 10+; G9 0 mismatches over 12,024 values; G10 9 checks pass |

All runs used APFS clones with every `QUANT_*` path overridden and a frozen clock; the main checkout was never
written to.

## 5. Architecture decisions

Status: **Accepted** = implemented on the branch; **Proposed** = needs the owner (Tier 1) or a Tier-2 decision
per MASTER_SPEC 12 before it changes behaviour. Record accepted ones with `python -m quant kb` after merge.

**D1 — Vendor aliases resolve by contract order (Accepted).** A field's aliases are a priority list, not a
set. Within one fetch the first alias present wins; across fetches the newest wins. Rejected: "pick the alias
closest to a consistent share count" (it cannot tell a split artefact from real dilution).

**D2 — Per-share history must be on one share basis (Accepted).** Growth of a per-share figure compares
implied share counts with the split record and rescales only on strong evidence (within 1.5x of the split
ratio); otherwise the vendor value stands. Only eps_growth_3y is exposed today (book-to-price, dividend yield
and revenue growth are totals or same-date ratios).

**D3 — Unexplained price jumps are quarantined until reviewed (Accepted).** Never read as a return, never
read as zero. The suspect row carries the evidence's observation time so earlier replays are unchanged.
Needs an operator path (task T2): today 4 names in the windows (TMPV, VEDL, HEG, TRENT) are scored on 6
factors until a human approves a factor from the company's filing.

**D4 — The main checkout's `live:2026-09-11` is not a published cohort (Proposed, owner).** It is
uncommitted, mid-month (the spec's live track is month-end), and built by pre-fix code. G9 recomputes the
prior cohort with current code and blocks on mismatch, so keeping it blocks October. First real cohort:
`live:2026-09-30`, run in early October. Alternative (design verifier): keep it, record a defect decision,
publish a superseding restatement and exclude the original from every evaluation. Rejected here because it was
never committed or shared, and a mid-month cohort is not a valid live cohort under the month-end rule, so
keeping it buys an audit trail for a record nobody relied on at the cost of a permanent G9 exception.

**D5 — Replay and code identity (Proposed, Tier 1).** Today a factor's code hash covers its spec text, not
its code or shared helpers, so defects 2–4 changed values under the same `@1` ids, and G9 is the only thing
that notices. Proposal: hash factor source plus the helper modules it imports; a changed hash forces a
version bump; G9 compares only factors whose hash is unchanged since the prior cohort and lists the rest
under a recorded decision.

**D6 — Sector re-ranking stays in the champion; a challenger drops it (Proposed, Tier 2).** `final`
re-ranks the composite within each sector group, so a sector's best name scores by group size (96-name
Financial Services tops out at 2.56, 10-name Telecom at 1.64), about 100 names tie exactly (broken by
security_id), and small composite gaps become large rank gaps (MRF vs HEROMOTOCO: 0.004 composite, 16
places). Ranking by composite changes 7 of the top 30. In this verification 38 names moved 10+ places only
because a sector peer's data was corrected. Register `EW_HIER_NR_v1` (no re-neutralisation,
ties by composite) as a challenger under MASTER_SPEC 6.4; decide after 24 matured cohorts.

**D7 — Thin-evidence names (Proposed, spec change).** A composite over 7 of 11 factors counts the same as one
over 11; 9 of the top 30 run on fewer than 9 factors. A mild shrinkage (final x sqrt(n_used / n_applicable))
moves EMMVEE from 2 to 5 and changes 1 of the top 30, so the effect is small; test it as a challenger, low
priority.

**D8 — Storage (Proposed, Tier 1).** +86 MB per monthly cohort; 1.08 GB after 12 months, 5.2 GB after 60,
against a 50 MB warning. Drivers: 29k paper orders per cohort (16% in books that are never weighted) and
the journal copying every row as JSON. The budget check only prints a message. Proposal: attribution books
only for active and promotable factors; journal bulk ingests as one event per archive; compress closed ledger
partitions; make the budget breach a recorded warning.

**D9 — UI exporter (Proposed, high severity).** For every stock the dashboard explains the rank with legacy
text such as "FATAL MULTIPLIER (0.0x): Death Cross" or "REJECTED: Value Trap detected"; V2 has no multipliers
(MASTER_SPEC 6.1), so the UI tells the reader a mechanism that did not produce the rank. The ratios shown next
to V2 ranks come from the frozen legacy database with threshold-based unit repairs; margins and growth are
computed in the exporter; the knowledge cutoff is ignored. `ui/data.js` is 7.7 MB against a 1.5 MB budget
because the ~500 stock records are serialised 3-4 times, including about 2.4 MB of globals the app never
reads. Proposal: export only stored V2 values for the published cohort, one stock list plus id arrays.

**D10 — Index placeholders (Proposed).** NSE's `DUMMY*` rows get their own exclusion reason and count as
corporate-action evidence for the parent.

## 6. What could make these conclusions wrong

1. **One vendor.** Every input is Yahoo. Recomputation proves the engine read the vendor faithfully; it
   cannot catch a vendor error that is internally consistent. HDFCBANK shows the limit: the share-basis fix
   moves its EPS growth from -22%/yr to +1%/yr, but public restated EPS gives about +6%/yr because Yahoo's
   FY2023 figures differ from the company's.
2. **Sample size.** Fundamentals were checked cell by cell on 58 names (638 cells), not 501. The alias and
   share-basis defects were then measured on the whole universe, but other rare defects may remain (one
   unexplained 0.7% mismatch: PINELABS roe_stability_3y).
3. **Secondary sources.** The web checks used aggregators (Screener, business press), not annual reports;
   Screener restates history automatically.
4. **Correct is not predictive.** A correctly computed rank says nothing about returns. The engine has no
   matured live evaluation yet.

## 7. Tasks

| ID | Task | Owner | Priority | Effort |
|---|---|---|---|---|
| T1 | Decide D4: discard the main checkout's uncommitted `quant.db`/ledger changes, merge `review/verify-2026-09` | owner | P0, before Oct run | 30 min |
| T2 | Review queue for suspected actions (`quant data actions list/approve`); approve TMPV, VEDL, HEG demerger factors and TRENT's 1.5 vendor fix from filings | owner + dev | P0 | 1 day |
| T3 | Implement D5 (code identity over source and helpers; G9 scoped by hash) | dev | P1 | 2 days |
| T4 | Cache TRI per run and vintage (111 of 212 s in TRI reads) | dev | P1 | 0.5 day |
| T5 | D8 storage: drop never-weighted attribution books, bulk-ingest journal events | dev + owner decision | P1 | 2–3 days |
| T6 | Register `EW_HIER_NR_v1` and a coverage-shrunk challenger (D6, D7) | owner (Tier 2) | P2 | 1 day |
| T7 | UI exporter per D9: remove the legacy multiplier narrative, read V2 values only, one stock list; payload under 1.5 MB | dev | P1 | 1–2 days |
| T8 | Latest-annual staleness rule (NESTLEIND used FY2025 silently; 1–4 names per field) | owner (spec) | P3 | 0.5 day |
| T9 | Index placeholders (D10); correct the `small_group` flag on factors that do not apply to financials | dev | P3 | 0.5 day |
| T10 | Run `python -m quant data capture` after the 2026-09-30 close, then `run monthly` | owner | P0, calendar | 70 min capture |

## 8. Confidence

```
interpretation (the engine computes what the spec says; the listed defects are real and fixed)   ~90%
outcome (the corrected ranking is materially right for October's cohort)                         ~70%
```

The gap is the vendor: the engine can now refuse obviously broken inputs, but it still trusts Yahoo where
Yahoo is consistent and wrong, and 4 names wait on human review.

## Mental model

```
 vendor data --(faithful read: verified)--> factor values --(exact: verified)--> ranking
      |                                                                           ^
      +-- defects were here: corporate actions, alias ties, share basis, TTM mixing
          now: detected -> quarantined -> human-approved factor -> restored
```

**For leadership:** the scoring engine does its maths exactly right; the errors were in how it read vendor
data around splits and demergers, those are fixed and tested, and the top of the list did not change.
