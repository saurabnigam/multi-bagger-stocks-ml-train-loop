# Storage and backup

## What is in git

- The code, the spec and the tests.
- The compressed journal: `data/ledger/YYYY-MM/events.jsonl.gz`, one file per month. This is the canonical record of every state change.
- Decisions, reports and lessons under `knowledge/`.
- Price manifests under `data/manifests/`.
- The dashboard data under `ui/`.
- `quant_engine.db`, the frozen legacy file. Never touch it.

## What is not in git

- The state database `quant.db`. It is derived from the journal, so it is git-ignored.
- The daily price store `data/prices_daily.sqlite`.
- The raw vendor archives under `data/archive/`.

Git cannot recover the price store or the archives. Back them up.

## Why

The database file grew to 216 MB. GitHub blocks files over 100 MiB and warns over 50 MiB. One month of journal is about 68 MB as plain text and about 2.5 MB gzipped. The export is deterministic: the same content always gives the same bytes, so git stores no change for a closed month.

## Rebuild the database on a fresh clone

```bash
python3 -m quant db rebuild --output quant.db
python3 -m quant db verify
```

`db rebuild` replays every event in order and checks each prior-row hash. `db verify` exports the journal, rebuilds a temporary copy and compares every table with your database. A truncated or corrupt `.gz` file stops both commands with an error that names the file. Restore it from git.

An older checkout may still hold a plain `events.jsonl` in a month folder. It still works. The next `db export` (also run by `run monthly`) writes `events.jsonl.gz` and removes the plain file. If both exist, the `.gz` wins.

## Getting a month's record into git

`run monthly --commit` commits the journal, manifests, knowledge and dashboard data in the git checkout that
holds the data folder. The commit names only those paths, so files another agent has staged there are left
alone. It never pushes without `--push`.

Known limit (2026-11-14). The data folder is the main checkout. It is 16 commits behind and holds another
agent's uncommitted code changes, so it cannot take that commit and push it yet. Until that is resolved,
copy `data/ledger`, `data/manifests`, `knowledge/` and `ui/data*.js` into a clean worktree and commit them
there. The main checkout also still tracks the old `quant.db` in its own history: a `git stash`,
`git reset --hard` or `git checkout -f` typed there replaces the real database with that old file. Run the
backup first if you ever need one of those commands there.

## Run the backup

```bash
scripts/backup_state.sh --dry-run          # prints the plan, writes nothing
scripts/backup_state.sh                    # writes a snapshot
scripts/backup_state.sh /path/to/folder    # choose the folder
```

Sources come from `QUANT_DB_PATH`, `QUANT_PRICES_DB_PATH` and `QUANT_ARCHIVE_DIR` when set. Otherwise they are `quant.db`, `data/prices_daily.sqlite` and `data/archive` under the data root. The data root is `MBQ_DATA_ROOT`, else the repository.

You can set `MBQ_DATA_ROOT` once in a file named `routine.env` in the root of the checkout that holds the script. Git ignores it. Example:

```
MBQ_DATA_ROOT=/Users/you/Desktop/Projects/multi-bagger-stocks-ml-train-loop
QUANT_PYTHON=/Users/you/path/to/venv/bin/python
```

Only those two keys are read. A variable set in the environment wins over the file. The value is literal: no quotes, no expansion. The file is never run. With it, `scripts/backup_state.sh` needs no environment variables. See [month_end.md](month_end.md).

The destination is the argument, else `QUANT_BACKUP_DIR`, else a folder named `mb-backups` next to the repository folder (the parent of the repository, then `mb-backups`). The script refuses a folder inside the repository or inside the data root, a path with `..` in it, and a path that contains a single quote or a newline.

The two databases are copied while the script holds the engine's state write lock (see [concurrency.md](concurrency.md)), so they are consistent with each other and no writer changes them during the copy. The script waits up to `QUANT_LOCK_WAIT_S` seconds for the lock (default 900) and then stops with a message that names the holder. While it runs, every other writer waits or stops the same way. The lock file `<database>.lock` appears next to the state database; that is normal.

The script needs the interpreter (`QUANT_PYTHON`, else `venv/bin/python`, else `python3`) to import the `quant` package. If it cannot, the script stops. It never falls back to a plain file copy.

Each run creates `snapshot-YYYYMMDDTHHMMSSZ/` with:

- `quant.db.gz` and `prices_daily.sqlite.gz`: copies made with the SQLite backup API from a read-only connection, then gzipped. The source files are never opened for writing; their size and modification time do not change. SQLite may leave empty `-wal` and `-shm` files beside a database that is in WAL mode; they are harmless.
- `archive.tar.gz`: the archive folder.
- `INVENTORY.json`: for each file its name, size and SHA-256, plus the source paths, the UTC time and the git commit of the code.

The script keeps the newest `KEEP` snapshots (default 6), counting the new one. It moves the new snapshot into place first and prunes afterwards, so with `KEEP=1` the new snapshot always survives. It deletes only older `snapshot-*` folders that hold an `INVENTORY.json`, so it never removes anything else. A missing source makes it stop with a message and a non-zero exit code.

To restore a database from a snapshot, `gunzip` the `.gz` file and put the result at the database path. Check it against the SHA-256 in `INVENTORY.json` first.

## The backup reminder

`capture_routine.sh status` prints a `last backup:` line. It looks in `QUANT_BACKUP_DIR`, else `mb-backups` next to the checkout, for the newest `snapshot-YYYYMMDDTHHMMSSZ` folder that holds an `INVENTORY.json`, and shows its age in days (from the time in the name, UTC). If there is none, or the newest is older than 35 days, `status` adds an `ACTION NEEDED` line that tells you to run `scripts/backup_state.sh`. Change the limit with `MBQ_BACKUP_MAX_AGE_DAYS`. `status` only reads; it still exits 0.

## An off-laptop copy

The script writes only to a local folder. Copying that folder to another machine or to cloud storage is your choice and your job. A backup that lives on the same laptop does not protect you from losing the laptop.

## Size limits

`python3 -m quant db size` prints the compressed journal bytes for each month and in total. It prints a `WARN` line when any one journal file is over 45 MiB. GitHub warns at 50 MiB and blocks at 100 MiB. A legacy plain `events.jsonl` counts too, so a large old file shows up until the next export replaces it.
