# One writer at a time

## What the lock is

Every command that writes to the state database takes an exclusive write lock first. The lock is a file next to the database, named `<db>.lock` (for the default database, `quant.db.lock`). Only one process can hold it. The lock is advisory: it works because every writing command in this code base asks for it.

The operating system frees the lock when the process ends, however it ends. A crash or a kill leaves no stale lock. The file itself stays in place and is never deleted. The lock is per database path, so two different databases never block each other.

Inside one process the lock can be taken again. `quant run monthly` holds it for its whole run: the pipeline, the journal export and verification, the WAL checkpoint, VACUUM and the optional commit and push. It opens several run contexts on the same database inside that span. The lock is released when the outermost holder finishes. After it gets the lock, monthly checks once more whether the live cohort for the date is already published. If another run published it while this one waited, monthly exits 0 and runs nothing.

The lock belongs to the process, not to a thread, and the engine is single-threaded. A forked child process does not inherit it: the child is refused like any other second writer.

## What takes the lock

| Takes the lock | Takes no lock |
|---|---|
| Every run context (capture, prices, factors, model, evaluate, portfolio, kb, triage writes, and so on) | `status` |
| `run monthly`, for the whole run including export, verify, VACUUM and commit | `triage list`, `triage show` |
| `db init` | `db size` |
| `db export`, `db verify` (they write the journal files, the committed record) | `db rebuild --output PATH` (it writes a new file; it refuses a PATH that already exists) |
| `db migrate-legacy` (the schema bootstrap and the migration; `--dry-run` writes nothing and takes none) | |

## What a waiting writer sees

By default a writer that finds the lock held retries for about one second, so a momentary overlap is not refused, and then stops. It exits with code 1 and prints one line on stderr:

    State is locked by <actor> (<kind>, pid N) since <time>; retry later or set QUANT_LOCK_WAIT_S

`quant run monthly` prints one line on stdout naming the same holder and returns 1.

To wait instead of stopping, set `QUANT_LOCK_WAIT_S` to a number of seconds. The writer then checks every 0.25 seconds until the lock is free or the time is up. If time runs out, it stops with the message above. Scheduled jobs set `QUANT_LOCK_WAIT_S=900`. Interactive commands keep the default of 0.

## How to see the holder

Run `python -m quant status`. The output is one JSON document. Its `write_lock` key is the string `"free"`, or an object with `actor`, `kind`, `pid` and `started_at` of the holder. The routine script reads this key. Status takes no lock and never makes a real writer fail.

The holder is one JSON line inside the lock file: pid, actor, run kind, as-of date, command, start time in UTC and host. Status reports a holder when that pid is alive on this machine. A line written on another host is reported as held, because its liveness cannot be checked. A clean release empties the file. After a crash the line stays, its pid is dead, and status reports `"free"`.

## If the lock file cannot be opened

A missing or read-only directory (usually a mistyped database path) stops the command with `Refused(lock_unavailable)` and a message naming the file. The lock never creates directories.

## What the lock does not do

- It does not stop someone from editing the SQLite files directly, for example with the `sqlite3` shell or another program. Do not do that.
- It does not coordinate code edits. Each agent edits in its own git worktree. Scheduled jobs and real runs use one pinned, clean checkout.
- It does not replace the other protections: the code-identity check, immutable published tables and human-only decisions.
- It does not work across machines or over network file systems that do not support `flock`. Keep the database on a local disk.
