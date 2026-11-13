# Open owner decisions as of 2026-11-11, with tonight's rerun as evidence

Evidence: `decisions_2026-11-11_evidence/` (ladder results, sandbox ranking, drivers, real rerun log).
Supersedes the recommendation in `latest_run_2026-11-04.md` section 2.1 (see "Corrections").

> The real pipeline is still blocked for October and no rerun can change that: October's closing prices
> were recorded on 4 Nov, after the 30 Oct deadline. A sandbox run on tonight's close (11 Nov) passes the
> close-price gate with genuinely on-time prices and publishes once the faulty September cohort is set
> aside. Six decisions are open. Decisions 1 and 2 gate the first clean cohort (as_of 2026-11-30).

## Tonight's runs

```
run                                                         result
real DB, as_of 2026-10-30, no new capture                   BLOCKED  G3 (closes recorded 4 Nov, after the cutoff), G4
sandbox, as_of 2026-11-11, today's rules                    BLOCKED  G9 (4,042 of 12,024 values differ replaying live:2026-09-30), G10
sandbox, September cohort skipped at G4 and G9              BLOCKED  G10 (leak audit T3_REPLAY replays the same cohort)
sandbox, September cohort skipped at G4, G9 and G10         PUBLISHED  484 ranked; G3 99.20%; no factor dropped
```
Sandbox = copy of the real state; prices captured 20:33 IST with the true clock (before the 23:59:59
cutoff); run clock set just after the cutoff; mid-month as_of allowed. Top 10: PNB, EMMVEE, LUPIN, MRF,
LICHSGFIN, GILLETTE, PRIVISCL, SAGILITY, PVRINOX, TMCV. Overlap with the verified 26 Oct top 30: 27.

## Decisions

| # | Decision | Recommendation | If nothing is decided | Needed by |
|---|---|---|---|---|
| 1 | Source and timing of month-end closing prices | Add NSE's official daily file (bhavcopy) as the source of the as-of closes; rule unchanged. Owner reads NSE's terms first | Each month-end depends on Yahoo delivering that evening (met 11 Nov; missed 30 Sep, 4 Nov) | 30 Nov |
| 2 | The September cohort (published from prices fetched 17 days late) | Record it as invalid by human decision; keep it; G4, G9, G10, evaluation and the scoreboard skip invalid cohorts | Every later cohort blocks at G9 and G10 (verified) | 30 Nov |
| 3 | Denominator of the 95% price-factor coverage rule | Count only members that can have the factor (a year of history, not cut by an unresolved suspect) | Passes tonight with a margin of 4 names (480 vs 476); failed on 3 Nov (473) and dropped two factors | before the margin is used |
| 4 | Where the state lives (quant.db is 163 MB; GitHub blocks files over 100 MiB) | Stop committing the database binary; commit closed monthly journals compressed (67.7 MB -> 2.5 MB); verify with `db rebuild`; then reduce journal volume (D8) | Real state stays uncommitted on one machine | soon |
| 5 | The other agent's uncommitted changes in the main checkout; who may run the real pipeline | Review and adopt its two price-provenance checks; discard its regenerated UI payloads; only the owner starts real runs; agents use sandboxes | Main checkout cannot fast-forward; a second unsupervised run can repeat September | this month |
| 6 | Month-end routine for 30 Nov (Monday) | Schedule captures: fundamentals in the last week; prices at 20:30 IST with retries at 22:00 and 23:30 (`prices update`); approvals and `run monthly` stay manual | Repeat of 30 Sep and 30 Oct (no capture before the cutoff) | before 30 Nov |

Decisions 1-3 change engine rules or sources. MASTER_SPEC 9.3 calls these Tier 2: human approval only.

### Decision 1 options
| Option | What changes | Cost | Weak point |
|---|---|---|---|
| A. NSE official file for the as-of closes (recommended) | New source for one day's closes; cutoff rule unchanged | One work package; owner accepts NSE's terms | Terms not read by anyone yet |
| B. Grace period for closes (for example 48 h) | Cutoff relaxed for closes only | Small code change and a spec edit | Yahoo gaps have lasted 4+ days (7 Sep bar) |
| C. Keep Yahoo and the rule; retry the price capture through the evening | Nothing | None | The month is lost whenever Yahoo is late |

Observed Yahoo close coverage (members with a close, time observed IST):
```
30 Sep bar   1 Oct 07:44    67%        4 Nov bar    5 Nov 00:45     0%
26 Oct bar  27 Oct 14:47    99.6%      4 Nov bar    5 Nov 11:50    67%
11 Nov bar  11 Nov 20:33    99.2%  (MRF 127,125.00 and RELIANCE 1,335.40 equal NSE's file of the same evening)
```
NSE's file for 11 Nov was already published at about 20:30 IST
(https://archives.nseindia.com/products/content/sec_bhavdata_full_11112026.csv, fetched once to check timing).

### Decision 2 options
| Option | What changes | Cost | Weak point |
|---|---|---|---|
| A. Mark invalid, keep, skip (recommended) | A recorded decision; three selection points and the scoreboard honour it | One work package; paper positions opened from that cohort (12,279 filled) need a closing rule | None found |
| B. Restore the state to the last clean commit and re-enter the four approvals | No code | Removes the record of the incident; price store must be rebuilt to match the capture registry | Contradicts append-only history |
| C. Leave | Nothing | None | Every cohort blocks |

## Also found tonight
- 28 current index members had no fundamentals in the real state (26 fetchable). Cause: `capture.run` selects
  securities tracked at the run's target as_of (30 Oct), which predates the 4 Nov universe capture. A full
  `data capture` with today's as_of was started on the real state at 20:45 IST. Code fix: capture the union
  of the target members and the current members.
- Concord Biotech: Yahoo shows the price halving on 1 Oct and doubling back on 2 Nov; needs evidence triage.
- HEG: still waiting for the spin-off listing.

## Corrections to `latest_run_2026-11-04.md`
1. "Yahoo delivers closes only the next day" was an over-generalisation from three observations. Tonight it
   delivered the same evening. The accurate statement: delivery is unreliable.
2. Recommended option "B, until 09:00 next morning" is withdrawn: coverage was 67% at that hour on two days.
3. The Swiggy (-29%) and JSW Dulux (-91.5%) jumps on 2 Nov were artefacts of the 5 Nov download (246 rows
   quarantined in that capture). Tonight's download shows no such moves.

## Confidence
```
these are the right six decisions, and 1 and 2 are the ones gating November:    ~85%
November publishes cleanly if 1, 2 and 6 are done:                               ~75%
```

## Addendum, 2026-11-14: the owner's "count a closing price by its own date" rule (option 1D)

Owner's point: a closing price is fixed and public at 15:30 on its date, so the time we recorded it should
not matter. Tested in a sandbox copy of the real state (`decisions_2026-11-11_evidence/bar_date_rule_experiment.py`):
price bars for a cohort are read at a pinned price vintage (the time the cohort is scored); fundamentals,
index membership and corporate-action records keep the cutoff.

```
                                      today's rule               option 1D
replay of live:2026-09-30             4,042 of 12,024 differ     0 of 12,024 differ
run monthly, as_of 2026-10-30         blocked (G3, G4)           published; G1-G10 pass; 491 eligible
```
The October sandbox cohort used fundamentals captured 2026-09-11 and the 2026-09-11 member list (the only
captures before its cutoff): point-in-time correct, seven weeks stale.

Revised recommendation:
- **Decision 1: option 1D** replaces 1A. No new data source; the NSE terms question falls away. Run once at
  least 98% of the as-of closes are in. Store the price vintage with the cohort.
- **Decision 2: no invalidation needed.** September's content is reproducible. Mark cohorts scored after their
  cutoff day as "scored late"; show them, and keep them out of model-promotion evidence.
- Decisions 3-6 unchanged.

Limits: simulated by patching the price-store reads, not implemented (the code-identity guard refused the
first attempt, as it should). A late download can miss a stock delisted in between, and vendor corrections
change history between downloads, which is why the vintage must be pinned. The rule does not extend to
fundamentals: the vendor shows only the current version, so the provable knowledge time is our fetch time.
Correction: my earlier statement that late-recorded prices "could carry later knowledge", and that this is
what went wrong with September, was wrong for prices. What went wrong was that record times were altered to
pass a check.
