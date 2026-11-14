# Month-end routine and its schedule

The first clean monthly list needs three things in time: financials captured before the list's date,
closing prices recorded on the evening itself, and a person reviewing price-jump flags before midnight.
This page describes the routine that does the first two, and reminds you about the third.

## 1. What runs, and when

One script does the work: `scripts/capture_routine.sh`. It is safe to run on any day and any number of
times, because every mode first checks whether there is anything to do.

| Mode | What it does | When it does nothing |
|---|---|---|
| `fundamentals` | `universe capture`, then `data capture` (about 60-75 minutes) | On an ordinary day: the last capture is under 3 days old. On the month-end evening: the last capture finished after 16:00 IST today. On any session day: the market has not closed yet (before 16:00 IST) |
| `prices` | `prices update --through TODAY`, then checks coverage | Weekend; before 16:00 IST; or at least 98% of today's closes are already recorded |
| `status` | Prints capture times, today's close coverage, the latest live list, the write lock, open price-jump flags, the last backup and `ACTION NEEDED` lines | Never writes anything |

Exit codes: 0 means done or nothing to do. Exit code 10 from `prices` means the closes are still incomplete; run it again later that evening. Exit code 1 means a failure, or, for `prices` and `fundamentals`, that there is no readable state database yet (one line says so). Exit code 64 means a bad mode. `status` always exits 0: a part it cannot read is printed as "no database yet" or "unknown".

What `status` prints about the lock is one line: `write lock: free`, or `write lock: held by <actor> (<kind>, pid N) since <started_at>`. It reads this from the `write_lock` field of `python -m quant status`.

The scheduled tasks (Claude desktop app, Scheduled tab) run in local time on the 24th to the 31st of each
month. They cover the last trading day of every month without needing a holiday calendar.

```
17:10            fundamentals      captures only when needed (see the table)
20:35            prices + status   first attempt; the report lists what needs you
22:05, 23:05     prices            retries if closes were incomplete; the last one leaves time before 23:59:59 IST
1st-3rd, 10:05   status            reminds you to run the monthly pipeline and the backup
```

The app starts a recurring task a few minutes after its set time, so the times above are the earliest starts.

The routine never approves a corporate action and never runs the monthly pipeline. Those stay with you.

## 2. What you do

```
month-end evening, after the 20:35 report   review the open price-jump flags:
                                            scripts/q.sh triage list --as-of DATE
                                            record approvals with data actions-resolve before 23:59 IST
next day or later                           scripts/q.sh run monthly
afterwards                                  scripts/backup_state.sh
```

`scripts/q.sh` runs any engine command with the pinned code against the real data (section 4). Use the
copy in `~/Desktop/Projects/mb-runtime/scripts/`. A plain `python3 -m quant ...` typed inside another
checkout runs that checkout's code, which may be older or half-edited.

If the closes were recorded before midnight, the list is published clean. If they arrived later, the
list is still published, flagged "scored late" and kept out of clean evidence
(see [late_price_admission.md](late_price_admission.md)).

Yahoo has often delivered a session's closes only the next day. Observed: 67% or less on the evening or
the next morning for 30 Sep, 4 Nov and 13 Nov; complete the same evening on 11 Nov. So a month can still
be flagged "scored late" even when every task ran.

## 3. Limits you should know

- **The Mac must be awake and the Claude app open.** A task that is due while the app is closed runs when
  the app next opens. If that is after midnight, the month's list will be flagged late. To have the Mac
  wake itself, set a wake schedule yourself (System Settings, or `sudo pmset repeat wake MTWRF 20:25:00`).
- **Holidays.** The script treats the last weekday of the month as the month-end evening. If the exchange
  is closed that day, the true last session is earlier. The daily window (24th to 31st) still records
  that session's closes; only the "tonight is the cohort evening" reminder is off by a day.
- **A capture started before the market closes would record an unfinished bar.** The script refuses to
  download prices, and to start the fundamentals capture on a session day, before 16:00 IST. The guard
  applies whenever the date being captured is the real current India date.
- **One writer at a time.** The routine waits up to 15 minutes for another writer
  (see [concurrency.md](concurrency.md)).

## 4. Where it runs from

Scheduled jobs and real runs use one clean, pinned checkout of the code. The data stays in one place.

```
code   ~/Desktop/Projects/mb-runtime                       git worktree on branch `runtime`
data   ~/Desktop/Projects/multi-bagger-stocks-ml-train-loop   quant.db, data/, knowledge/, ui/
```

The script gets the data location from `MBQ_DATA_ROOT` and the interpreter from `QUANT_PYTHON`.

To avoid typing these each time, put them in a file named `routine.env` in the root of the code checkout
(`mb-runtime`). Git ignores this file. Two example lines:

```
MBQ_DATA_ROOT=/Users/you/Desktop/Projects/multi-bagger-stocks-ml-train-loop
QUANT_PYTHON=/Users/you/path/to/venv/bin/python
```

Rules. One `KEY=VALUE` per line. Only these two keys are read; any other line is ignored, as are blank
lines and lines that start with `#`. A variable that is already set in the environment wins over the file.
The value is taken exactly as written: no quotes, no `$` expansion, no commands. The file is never run.
`capture_routine.sh`, `backup_state.sh` and `q.sh` read it.

With the file in place the short commands need no environment variables:

```bash
~/Desktop/Projects/mb-runtime/scripts/capture_routine.sh status
~/Desktop/Projects/mb-runtime/scripts/backup_state.sh
~/Desktop/Projects/mb-runtime/scripts/q.sh status
```

`status` also prints one `last backup:` line. If there is no backup, or the newest is older than 35 days
(change the limit with `MBQ_BACKUP_MAX_AGE_DAYS`), it adds an `ACTION NEEDED` line. See
[storage_and_backup.md](storage_and_backup.md).

To move the pinned code forward after a reviewed change:

```bash
git -C ~/Desktop/Projects/mb-runtime merge --ff-only origin/codex/v2-implementation
```

Agents edit code in their own worktrees, never in `mb-runtime`.

## 5. Checking and changing the schedule

- See the tasks: Claude desktop app, Scheduled tab. Their definitions are files under
  `~/.claude/scheduled-tasks/`.
- See what ran: `data/logs/routine_YYYY-MM.log` in the data folder.
- Run a step by hand (without `routine.env`, name the data location yourself):

```bash
MBQ_DATA_ROOT=~/Desktop/Projects/multi-bagger-stocks-ml-train-loop ~/Desktop/Projects/mb-runtime/scripts/capture_routine.sh status
```

- Rehearse a past day. Set `MBQ_REHEARSAL=1` together with `MBQ_TODAY=YYYY-MM-DD` and, if you want,
  `MBQ_HOUR=HH`. The script then acts as if it were that India date and hour. A rehearsal date in the past
  skips the 16:00 guard, because that session is complete. `MBQ_TODAY` and `MBQ_HOUR` without
  `MBQ_REHEARSAL=1` are ignored and the script prints one warning line, so a stray variable cannot switch
  the guard off. A rehearsal of `prices` or `fundamentals` really downloads, so point it at a copy of the
  data when you want to test only the logic.
