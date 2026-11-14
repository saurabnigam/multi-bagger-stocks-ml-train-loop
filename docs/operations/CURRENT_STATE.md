# Current state (shared by every agent)

Last updated: 2026-11-14. Update this file in the same change that alters anything below.
Rules for working together are in [AGENTS.md](../../AGENTS.md), section "Working Together".

## 1. Where things are

```
main working directory   ~/Desktop/Projects/multi-bagger-stocks-ml-train-loop
                         branch codex/v2-implementation; the real data (quant.db, data/, knowledge/,
                         ui/data*.js); the one Python environment (venv/)
runtime checkout         ~/Desktop/Projects/mb-runtime   branch `runtime`; pinned, reviewed code;
                         scheduled jobs and real runs start here (scripts/q.sh, capture_routine.sh)
backups                  ~/Desktop/Projects/mb-backups   snapshot-* folders (same disk only)
scheduled tasks          Claude desktop app, Scheduled tab: mbq-fundamentals, mbq-prices-evening,
                         mbq-prices-retry, mbq-month-start-reminder
price inbox              data/inbox/prices in the main working directory (exchange end-of-day files)
```

Runbooks: [month_end.md](month_end.md), [price_sources.md](price_sources.md),
[late_price_admission.md](late_price_admission.md), [concurrency.md](concurrency.md),
[storage_and_backup.md](storage_and_backup.md), [triage.md](triage.md).

## 2. The real data today

| Item | State |
|---|---|
| Published lists | `live:2026-09-30` (flagged scored late), `live:2026-10-30` (scored late, not clean evidence) |
| Next list | as of 2026-11-30 (Monday). It is the first one that can be clean |
| Late price admission (R13) | Enabled by owner decision D-2026-11-08 |
| Other price sources (R18) | Enabled by owner decision D-2026-11-09, manual inbox mode (the owner puts the exchange file into `data/inbox/prices`) |
| Last captures | Index list 4 Nov; financials and prices 11 Nov |
| Open price-jump flags | 3 (HEG spin-off waiting for listing; two Concord Biotech entries) |
| Committed record | Journal `data/ledger/2026-09..11/events.jsonl.gz`; `db verify` matched 45 of 45 tables on 14 Nov |
| Last backup | `snapshot-20261114T082719Z` |
| Main working directory | At the branch head, clean, `db verify` 45 of 45 tables on 14 Nov after the update |

## 3. Open items that need the owner

1. **On a month-end evening when Yahoo is late,** download the exchange's end-of-day file into the price inbox
   (see price_sources.md). The sources are enabled; the file is the manual step.
2. **Manual or automatic exchange files.** The default is manual: a person downloads the day's file into the
   inbox. NSE's terms of use prohibit automated data collection, so automatic fetching is the owner's choice.
3. **When Yahoo is late and no exchange file is supplied,** the month is published "scored late" and does not
   count as clean evidence. Decide whether a short grace window should keep such a month clean.
4. **Approve each scheduled task once** (Run now, then allow the command), or the first real run waits.
5. **Pushes.** Nothing is pushed without the owner's word.

## 4. Known hazards

- **The Mac must be awake and the Claude app open** when a scheduled task is due.
- **Backups are on the same disk** as the data, and the disk was 98% full on 14 Nov.
- **Yahoo delivered a session's closes on time on 1 of the 4 evenings checked.** The exchange file for the
  same day was published at about 16:35 India time. On real data for 13 Nov the NSE file alone filled 500 of 501
  members and matched Yahoo on every name both had.
- **Do not run `python -m quant` by hand inside a checkout with unreviewed code against the real data.** Use
  `scripts/q.sh` in the runtime checkout.

## 5. Deferred work (one project, after the first clean list)

Results counted by announcement day instead of fetch time; Nestlé excluded by a partial annual column;
Tata Motors CV scored on mixed-basis history. All three need new versions of every financial factor.
Also open: registering a challenger model; a second vendor for financials.

## 6. How to change this file

Keep it short. State facts with dates. Remove an item when it is done instead of marking it done.
