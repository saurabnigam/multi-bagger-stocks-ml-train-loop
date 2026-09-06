# WS00 — Foundations: package, config, schema, ledger, calendar, run context, test harness

## 1. Mission
Create the `quant/` package skeleton every other workstream lands in: configuration, the SQLite schema as one file, the ledger export/rebuild/verify cycle that makes git history text-based, the NSE trading calendar, the run context that stamps every write with a `run_id` and code/config hashes, the CLI registry, and the synthetic test world. Nothing else can be built or tested without this; get it right and small.

## 2. Read first
1. `docs/spec/MASTER_SPEC.md` §10.1 (layout), §10.2 (DDL index; every `CREATE TABLE` in §3–§10), §10.3 (CLI conventions), §10.4 (config), §4.2 (git policy), §2.4 (timing), §9.2 (`runs`, `schema_version`)
2. `subagents/README.md` (interfaces you provide; conventions)
3. Existing `config.py`, `db_setup.py` (the `ensure_schema` idempotent pattern to reuse), `weight_optimizer.py` (nothing to reuse here beyond style)

## 3. Scope
In: package skeleton; `config/quant.toml` with every key from §10.4; `quant/db/schema.sql` containing every table in the master spec; `quant.db.core`; `quant.db.ledger`; `quant.data.calendar`; `quant.run.RunContext`; `quant.cli` registry with `db`, `status`, `version` commands; `quant.errors`; `tests/synthetic.py` + `tests/conftest.py`; `scripts/check.sh` (pytest + `db verify` + ruff-free lint via `python -m pyflakes` if available else compileall); `subagents/PROGRESS.md` first entry.
Out: any data fetching, factors, models. `quant.run.monthly` orchestration body (WS11) — you create the module with `RunContext` only.

## 4. Dependencies
None. Verify the environment: `python -c "import pandas, numpy, scipy, yfinance, pytest; print('ok')"` and `python --version` (3.11+; 3.14 is what the owner has). `sqlite3.sqlite_version >= 3.35` for `RETURNING`/`ALTER` niceties (print it).

## 5. Interfaces you consume
None.

## 6. Interfaces you provide (contracts)
```python
# quant/config.py
REPO_DIR: Path
def load(path: str | None = None) -> Config      # tomllib; Config exposes nested attribute access (cfg.paths.db, cfg.gates.G3_price_cov ...) and .sha256 (of the file bytes)
                                                 # env overrides: QUANT_DB_PATH -> paths.db, QUANT_DATA_DIR -> paths.data_dir, QUANT_LEGACY_DB_PATH -> paths.legacy_db, QUANT_PRICES_DB_PATH -> paths.prices_db
# quant/db/core.py
def connect(path: str | None = None) -> sqlite3.Connection    # row_factory=sqlite3.Row; PRAGMA journal_mode=WAL; foreign_keys=ON; busy_timeout=5000
def apply_schema(conn) -> None                                # executes quant/db/schema.sql idempotently; inserts schema_version if missing
def upsert(conn, table: str, df: pd.DataFrame, keys: list[str]) -> int          # INSERT ... ON CONFLICT(keys) DO UPDATE; returns rows affected; empty df -> 0
def replace_as_of(conn, table: str, as_of: str, df: pd.DataFrame, track: str = 'live') -> int   # DELETE WHERE as_of=? [AND track=?] then insert, in one transaction
def table_hash(conn, table: str, where: str = '', params=()) -> str            # sha256 of sorted rows; used by ledger.verify and gate G9
# quant/db/ledger.py
def export(conn, as_of: str, ledger_dir: Path | None = None) -> list[Path]     # data/ledger/YYYY-MM/<table>.csv for every table having rows with as_of/month_end/captured_at == as_of (or run_id in that run); deterministic column order & sort
def rebuild(ledger_dir: Path, db_path: Path) -> None                            # fresh DB from all ledger folders + data/universe/*.csv
def verify(conn, ledger_dir: Path) -> VerifyReport                              # per-table row counts + hashes: rebuilt vs live; .ok bool; .diff list
def size() -> dict                                                              # repo size, largest files, quant.db size
# quant/data/calendar.py
def trading_days(source) -> pd.DatetimeIndex     # source: PriceStore (WS02) or a DataFrame of ^CRSLDX closes; fallback config/holidays.yaml + weekday rule when the series is absent (flag)
def last_trading_day_on_or_before(d: str, source) -> str
def month_ends(start: str, end: str, source) -> list[str]
def add_trading_days(d: str, n: int, source) -> str
# quant/run.py
class RunContext:  # with RunContext(as_of, kind, track='live', cfg=None) as rc: rc.run_id, rc.conn, rc.cfg, rc.git_sha, rc.code_sha256 (sha256 over sorted quant/**/*.py bytes), rc.config_sha256, rc.registry_sha256 (sha256 of factor_registry rows; '' if table empty)
                   # inserts runs row status 'running'; on normal exit status 'ok' unless rc.status set; on Blocked -> 'blocked'; on exception -> 'failed' (re-raised); finished_at set
# quant/errors.py
class Blocked(Exception): ...   # gate failure; exit 2
class Refused(Exception): ...   # governance refusal; exit 3
class LookaheadError(Exception): ...
class AlreadyMigrated(Exception): ...
# quant/cli.py
def register(group: str, name: str, fn: Callable[[argparse.Namespace], int], help: str) -> None
def main(argv=None) -> int      # global options --as-of --db --config --dry-run ; maps exceptions to exit codes 1/2/3 ; prints "[group name] as_of=.. run_id=.. git=.. wrote <table>=<n> ..."
# tests/synthetic.py
def make_world(tmp_path, n_securities=60, n_groups=6, months=48, seed=0) -> World
    # World: db_path, prices_db_path, cfg, security_ids, groups (Series), as_of_list (month-ends 2022-01..), planted: {factor_name: rho}, events: split(sec, date, 6.0), dividend(sec, date, 100.0),
    # reclass(sec, date, old_group, new_group), missing_fundamental(sec, field). Prices are geometric random walks with a planted cross-sectional signal so IC tests have a known answer.
    # Writes securities/symbol_history/universe_membership/sector_map/prices_daily/prices_monthly/fundamentals/holdings/security_attributes for all months.
```

## 7. Deliverables
`quant/__init__.py` (version string), `quant/__main__.py`, `quant/cli.py`, `quant/config.py`, `quant/errors.py`, `quant/run.py`, `quant/db/__init__.py`, `quant/db/schema.sql`, `quant/db/core.py`, `quant/db/ledger.py`, `quant/db/migrate.py` (schema_version bump helper; forward-only `migrations/NNN_*.sql`), `quant/data/__init__.py`, `quant/data/calendar.py`, `config/quant.toml`, `config/holidays.yaml` (2026–2027 NSE holidays; comment says it is a fallback), `tests/conftest.py`, `tests/synthetic.py`, `tests/unit/test_config.py`, `tests/unit/test_db_core.py`, `tests/unit/test_ledger.py`, `tests/unit/test_calendar.py`, `tests/unit/test_run_context.py`, `tests/unit/test_cli.py`, `tests/unit/test_synthetic_world.py`, `scripts/check.sh`, `.gitignore` additions (`data/prices_daily.sqlite`, `.cache/`, `*.sqlite-wal`, `*.sqlite-shm`), `subagents/PROGRESS.md` entry.

## 8. Implementation plan
1. `config.py`: load TOML with `tomllib`; wrap dicts in a `Config` class with `__getattr__`; compute `sha256`; env overrides; `REPO_DIR = Path(__file__).resolve().parents[1]`. Test: every key referenced in MASTER_SPEC §10.4 exists (parametrised list).
2. `schema.sql`: transcribe every `CREATE TABLE` from MASTER_SPEC §3–§10 verbatim, in the §10.2 order, all `CREATE TABLE IF NOT EXISTS`, indexes `IF NOT EXISTS`. Add `CHECK` constraints as written. Add a trailing `INSERT OR IGNORE INTO schema_version(version, applied_at, note) VALUES (1, datetime('now'), 'initial')`.
3. `core.py`: `connect` (Row factory, WAL, FK on). `upsert` builds `INSERT INTO t (cols) VALUES (...) ON CONFLICT(keys) DO UPDATE SET c=excluded.c` in executemany batches of 5,000; NaN → NULL; pandas Timestamps → ISO text. `replace_as_of` checks the table has an `as_of` column (else `month_end`, `captured_at`) and a `track` column if `track` given. `table_hash` = sha256 over `SELECT * ORDER BY <pk cols>` rows serialised as JSON with 12-dp floats.
4. `ledger.py`: table→date-column map (`as_of`, `month_end`, `captured_at`, `run_id` join for tables without a date). Export writes CSV with fixed column order (schema order), sorted by primary key, floats `%.12g`, NULL as empty. Rebuild: `apply_schema` on a fresh file, then load every CSV in `data/ledger/*/` and `data/universe/*.csv` (universe files are re-parsed by WS01's `parse_list`; until WS01 exists, rebuild loads the `universe_membership.csv` ledger export instead — document). Verify: rebuild into a temp file, compare per-table counts and `table_hash`.
5. `calendar.py`: trading days = dates with a `^CRSLDX` close in the price store (or a DataFrame passed in tests); if unavailable, weekdays minus `config/holidays.yaml`, and return a `source='fallback'` flag. Weekend/holiday resolution: 2026-06-14 → 2026-06-12; 2026-07-11 → 2026-07-10; 2026-10-02 (Gandhi Jayanti) → 2026-10-01.
6. `run.py`: `RunContext` per contract. `code_sha256` over `sorted(Path('quant').rglob('*.py'))` bytes; `git_sha` via `subprocess git rev-parse HEAD` (fallback 'nogit'). Unique index `(as_of, kind, track)`: a second run for the same key must first delete or be `--force` (raise `Refused` otherwise) — except `kind='adhoc'`.
7. `cli.py`: argparse with subparsers per group; `register()` used by later workstreams at import time (`quant/cli.py` imports `quant.commands.*` lazily via a `COMMAND_MODULES` list each workstream appends to). Commands now: `db init|rebuild|verify|size`, `status`, `version`. Every handler returns an int; `main` catches `Blocked`→2, `Refused`→3, other→1 with a one-line error.
8. `tests/synthetic.py`: deterministic (`numpy.random.default_rng(seed)`). 60 securities, 6 groups of 10, 48 month-ends starting 2022-01-31 with ~21 trading days each (weekday grid; no holidays needed). Daily log returns = market 0.3·m_t + group 0.3·g_t + idio; planted factor `plant_mom` correlated 0.10 with the next 3-month sector-relative return (construct returns from the signal). Events: security 5 gets a 6:1 split (close_raw ÷ 6, split_ratio 6.0) on a mid-sample date; security 7 pays a ₹100 dividend; security 9 is reclassified from group 1 to group 2 at month 30; security 11 has `Total Assets` missing for FY2024. Fundamentals: annual rows for 5 FYs with `available_from = period_end + 61 days` and quarterly for 6 quarters `+46 days`; holdings monthly. Writes both DBs via `core.upsert`.
9. `scripts/check.sh`: `python -m pytest -q && python -m quant db verify` (verify is skipped with a message if no ledger exists yet).
10. Register the PROGRESS.md entry; commit `WS00: foundations`.

## 9. Tests you must write
```
tests/unit/test_config.py::test_all_spec_keys_present                 parametrised over the §10.4 keys
tests/unit/test_config.py::test_env_override_db_path
tests/unit/test_db_core.py::test_apply_schema_idempotent               apply twice; table list identical; schema_version == 1
tests/unit/test_db_core.py::test_every_spec_table_exists               parametrised over the §10.2 list (39 tables)
tests/unit/test_db_core.py::test_upsert_inserts_then_updates          conflict path updates non-key columns; NaN -> NULL
tests/unit/test_db_core.py::test_replace_as_of_is_transactional       inject a failure mid-insert -> old rows intact
tests/unit/test_db_core.py::test_table_hash_stable_under_row_order
tests/unit/test_ledger.py::test_export_rebuild_verify_roundtrip       synthetic world -> export all months -> rebuild -> verify.ok
tests/unit/test_ledger.py::test_export_is_deterministic               two exports byte-identical
tests/unit/test_calendar.py::test_weekend_and_holiday_resolution       2026-06-14->06-12, 2026-07-11->07-10, 2026-10-02->10-01 (fixture series)
tests/unit/test_calendar.py::test_month_ends_from_series
tests/unit/test_calendar.py::test_fallback_flagged_when_no_series
tests/unit/test_run_context.py::test_run_row_lifecycle                running -> ok ; exception -> failed and re-raised ; Blocked -> blocked
tests/unit/test_run_context.py::test_hashes_recorded                  code/config sha non-empty; git sha or 'nogit'
tests/unit/test_run_context.py::test_duplicate_run_refused_without_force
tests/unit/test_cli.py::test_every_command_has_help                   `python -m quant --help`, each group `--help` exit 0
tests/unit/test_cli.py::test_exit_codes                               handlers raising Blocked/Refused/Exception -> 2/3/1
tests/unit/test_synthetic_world.py::test_world_shapes_and_events      60x48 prices; split visible in close_raw and split_ratio; reclass changes sector_map rows; missing fundamental absent
tests/unit/test_synthetic_world.py::test_planted_signal_has_known_ic  Spearman(plant, fwd 3m sector-relative) in [0.06, 0.14] averaged over months
```
Command: `python -m pytest tests/unit -q` (target < 20 s).

## 10. Verification checklist
- `python -m quant db init --db /tmp/q.db` prints `schema version 1; 39 tables` (count from `sqlite_master`).
- `python -c "import quant.db.core as c; conn=c.connect('/tmp/q.db'); print(sorted(r[0] for r in conn.execute(\"select name from sqlite_master where type='table'\")))"` lists every table in MASTER_SPEC §10.2.
- `python -m pytest tests/unit -q` green.
- `python - <<'EOF2'\nfrom tests.synthetic import make_world; import pathlib; w=make_world(pathlib.Path('/tmp/w')); print(w.db_path, len(w.as_of_list))\nEOF2` prints 48.
- `python -m quant db verify --db /tmp/w/quant.db` after `python -m quant db export --as-of <each month>` (loop in a shell) → `verify: ok (N tables)`.
- `git status` shows no `*.sqlite` files staged.

## 11. Definition of done
- [ ] All tests in §9 exist and pass offline in < 20 s
- [ ] `quant/db/schema.sql` contains every table and index named in MASTER_SPEC with identical column names and CHECK constraints
- [ ] Ledger round-trip proven on the synthetic world
- [ ] Calendar resolves the three known dates
- [ ] `RunContext` records hashes and statuses; duplicate runs refused
- [ ] `config/quant.toml` complete; `Config` attribute access works; env overrides tested
- [ ] `scripts/check.sh` runs; `.gitignore` updated; PROGRESS.md entry appended; commit `WS00: foundations`

## 12. Handoff notes for downstream
- Everyone writes through `upsert`/`replace_as_of` inside a `RunContext`; never raw `INSERT`.
- `tests.synthetic.make_world` is the shared fixture: extend it (new columns) only by adding, never by changing existing values (other workstreams' expected numbers depend on it).
- Schema changes: only in `quant/db/schema.sql` + `tests/unit/test_db_core.py::test_every_spec_table_exists`; forward-only migrations in `quant/db/migrations/` once a real `quant.db` exists.
- `calendar.trading_days` needs the `^CRSLDX` series from WS02; until then it falls back and flags it.

## 13. Risks, gotchas, what NOT to do
- Do not use `pandas.DataFrame.to_sql` (it ignores conflicts and types); use `upsert`.
- SQLite `ON CONFLICT` requires the conflict target to be a UNIQUE/PK; every table in the spec has one.
- pandas 3.0: no chained assignment; default string dtype; tz-aware indexes from yfinance are stripped in WS03, not here.
- Do not add `click`, `typer`, `pydantic`, `sqlalchemy`, `pyarrow`. Stdlib only.
- Do not "simplify" the ledger to a single big CSV; per-month folders are what keeps git diffs small.
