# Operator runbook: late price admission

## What the rule is

A closing price is admissible by its trading date. Sometimes the as-of closes are not all recorded
by the knowledge cutoff (23:59 IST on the as-of date). The cohort may then be scored late.

- Price bars dated on or before the as-of date are read at a pinned time: the time of scoring.
- A price jump seen only in those bars counts as an unresolved suspected corporate action.
- The pin is stored with the cohort. A replay uses the stored pin and gives the same result.

## What it is not

- It covers prices only.
- Financial statements are not admitted late. Fundamentals still need a capture before the cutoff.
- Holdings, attributes, index membership and human corporate-action approvals keep the cutoff.
- It does nothing in a database without the recorded decision below. There the engine behaves as before.

## Enable it once per database

A human records a Tier-2 decision. Agents do not.

```bash
python3 -m quant kb decide --kind rule_change --subject late_price_admission \
  --title "Admit closes by trading date with a pinned price vintage" \
  --evidence "Owner approval 2026-11-14: prices recorded late are read at the scoring time; cohort flagged scored_late, is_clean=0." \
  --actor-kind human --by human:<you>
```

Recording the decision also flags an older cohort at once. A cohort published before the rule, whose as-of closes were recorded after its cutoff, is pinned at its publication time and gets one `COHORT_SCORED_LATE` event. It then leaves clean-live evidence. You do not need a monthly run for this.

## Month-end sequence (times are IST)

1. Any time in the last week: `python3 -m quant universe capture`, then `python3 -m quant data capture`. This takes about 60 to 75 minutes.
2. On the as-of evening, about 20:30: `python3 -m quant prices update --through DATE` (DATE is the as-of date; the command exits 1 without `--through`). Repeat at 22:00 and 23:30 if coverage is under 98%. `python3 -m quant triage list --as-of DATE` prints the as-of bar coverage.
3. Resolve suspected corporate actions before 23:59. See [triage.md](triage.md) and [corporate_actions.md](corporate_actions.md).
4. Next day or later: `python3 -m quant run monthly`.

## The two outcomes

The cohort is clean only if at least 98% of the as-of closes are recorded before 23:59:59 IST on the as-of date. Otherwise it is published flagged scored late.

| Closes recorded | Result |
|---|---|
| By the cutoff (coverage 98% or more) | Normal clean cohort. |
| After the cutoff | The run still publishes. The cohort is flagged scored late, `is_clean = 0`, shown on the dashboard, and not counted as clean evidence or used for model promotion. |

So retrying the price update on the evening still matters. It is what keeps the cohort clean.

## See the flag

- Dashboard: a SCORED LATE badge beside the LIVE badge. Hover for the price vintage.
- Database: `cohorts.source_refs_json` holds `price_vintage`, `scored_late` and `late_price_admission`.
- A cohort published before the rule gets a `COHORT_SCORED_LATE` row in `data_quality_events`, written when `kb decide` records the decision.
- `python3 -m quant prices manifest --verify --as-of DATE` understands late cohorts. It reads a late cohort's prices at its pinned time, so its manifest verifies PASS.
- A stored pin that cannot be read (not a time after the cutoff) is skipped, and a monthly run records a WARN `COHORT_PIN_INVALID` event naming the cohort.
