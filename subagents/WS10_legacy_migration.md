# WS10 — Legacy migration of quant_engine.db and the four 2026 snapshots

## 1. Mission
Bring the legacy record into the V2 schema as the first, flagged, never-clean points on every chart: map the six snapshot dates correctly (two are not cohorts), key everything by ISIN, recompute returns from the V2 total-return store so the ZFCVINDIA split disappears, store the legacy scores and factors under `track='legacy'`, record every known defect, and reproduce the red-team attribution table as an acceptance test. The legacy database itself is never modified.

## 2. Read first
1. MASTER_SPEC §10.7 (all), §4.5 (tracks), §13 D19, `docs/analysis/red_team_review.md` (the numbers to reproduce), `docs/analysis/historical_runs_log.md`
2. `weight_optimizer.py::weights_in_force, forward_returns, period_stats` (reuse for the reconciliation), `quant_math.trap_penalty_multiplier`
3. WS01/WS02/WS05/WS06/WS07 docs §6

## 3. Scope
In: `quant/migrate/legacy.py`, the `db migrate-legacy` command, `tests/fixtures/legacy_sample.db` (20-ticker extract built by a documented script), acceptance tests, the reconciliation printout.
Out: the seed ADR texts (WS09 writes them; you reference the ids and create placeholder decision rows if WS09's files are absent, flagged for WS09 to replace).

## 4. Dependencies
WS01 (identity/sector_map), WS02 (TRI at legacy dates; `prices backfill --tickers-from legacy` must have run), WS05 (`legacy.py` ids, `compute_one` for price factors), WS06 (`scores` writing conventions), WS07 (`labels.mature` on track legacy). Verify: `select count(*) from prices_daily where date='2026-06-12'` ≈ 500 in the store.

## 5. Interfaces you consume
`identity.upsert_security/resolve_security_id`, `sectors.taxonomy.update_sector_map`, `PriceStore.tri/close_raw/adv_inr`, `build_monthly_panel`, `factors.registry.compute_one/values_frame`, `factors.standardise.gaussian_rank`, `db.core.replace_as_of`, `evaluation.labels.mature`, `evaluation.metrics.rank_ic`, `gates.record_event`, `RunContext(kind='migrate', track='legacy')`.

## 6. Interfaces you provide
```python
# quant/migrate/legacy.py
LEGACY_DEFECTS = ['DIV_YIELD_X100','ROE_NONE_AS_ZERO','SENTIMENT_OUTSIDE_BUDGET','GROWTH_IMPUTED_15PCT','DCF_SINGLE_YEAR_FCF','NEAR_CONSTANT_FACTOR','NO_DATA_FLAGS','UNADJUSTED_QUOTE','WEEKEND_RUN','HINDSIGHT_TICKER_LISTS','BUCKETED_SCORES','NO_SECTOR_NEUTRAL','NO_KNOWN_AT','DUPLICATE_SNAPSHOT_0612']
def snapshot_map(legacy_conn, calendar) -> pd.DataFrame          # legacy_date, as_of, is_full, superseded_by, defects_json  (MASTER_SPEC §10.7 table)
def run(conn, legacy_db_path: str, store, cfg, dry_run: bool = False, legacy_quotes: bool = False) -> MigrationReport   # steps 1–14 ; raises AlreadyMigrated if legacy_snapshot_map has rows ; report has per-step counts and the reconciliation table
def reconciliation(conn, legacy_conn, store) -> pd.DataFrame       # per legacy transition: red-team numbers vs recomputed (unadjusted quotes) vs adjusted sector-relative
def build_sample_db(src: str, dst: str, tickers: list[str]) -> None   # for tests/fixtures/legacy_sample.db (20 tickers incl. ZFCVINDIA.NS, HEROMOTOCO.NS, PNB.NS, MCX.NS, JBCHEPHARM.NS)
```

## 7. Deliverables
`quant/migrate/{__init__,legacy}.py`, `quant/commands/migrate.py` (`db migrate-legacy [--dry-run] [--legacy-db PATH]`), `scripts/build_legacy_sample.py`, `tests/fixtures/legacy_sample.db`, `tests/integration/test_migrate_legacy.py`, PROGRESS entry.

## 8. Implementation plan
1. Open `quant_engine.db` with `sqlite3.connect('file:...?mode=ro', uri=True)`; never write.
2. `snapshot_map` exactly per §10.7 step 2 (06-04 partial; 06-12 superseded by 06-14; 06-14→as_of 06-12; 07-11→07-10; 08-14; 09-03). Assert the 06-12/06-14 price identity (`price` equal for all common tickers) and record it as `DUPLICATE_SNAPSHOT_0612`.
3. Securities: for each of the 501 legacy tickers, symbol = ticker minus `.NS`; resolve via today's CSV (WS01 fixture / `securities`); unresolved → `manual_ticker_overrides.csv` lookup → else `LEGACY:<symbol>` with an INFO event.
4. `universe_membership` (source `legacy_snapshot`, index NIFTY500) for L1–L4 at their as_of; `sector_map` rows via WS01 with `source='legacy_backfill'`, confidence 0.5, `valid_from='2026-06-01'` (only for securities without an existing row).
5. `prices_monthly` for the four as_of via `build_monthly_panel` (requires the store); `quote_legacy` from `daily_predictions.price`.
6. `fundamentals` from `raw_json` per §10.7 step 6 with `available_from = as_of`, basis `run_date`, `fetched_at = legacy date`; do not migrate the corrupt fields as values; `holdings` from `Inst_Holdings_%/100` with `captured_at = legacy date`.
7. `factor_values` track legacy: `legacy_<name>@0` for the 8 scores + `legacy_trap@0` + `legacy_momentum_mult@0` + `dc_flag` (from `Momentum_Status` contains 'Death Cross'); `raw` = stored score; `z` = gaussian rank within the current sector group; plus V2 price factors via `compute_one(track='legacy')` at each as_of.
8. `scores`: `LEGACY_V18` (`final = final_score`), `LEGACY_V18_BASE` (`base_score` or reconstructed Σ weight_in_force × score with sentiment excluded, flag `reconstructed`); `model_version` = in-force `active_weights.id`; `eligible = final > 0`; `scored = 1`; `input_hash` = sha of the 8 scores; `models` rows role `legacy`; `model_versions` 12 rows from `active_weights`.
9. `labels.mature(track='legacy')` for the legacy as_of dates (WS07) — call it; `performance_tracking` is summarised (row count, forward dates) into the migration ADR text, not imported.
10. `legacy_defects` rows + WARN events per defect per snapshot; `runs` rows kind `migrate`, `dq_status='legacy_defects'`, `is_clean=0`.
11. `reconciliation`: reuse `weight_optimizer.forward_returns/period_stats` on the legacy tables to print the red-team table; compute the same Spearman with V2 unadjusted quotes (`--legacy-quotes`) and with adjusted sector-relative labels; print side by side.
12. Decisions: if WS09's D-2026-09-01..07 files exist, reference them; else insert placeholder `decisions` rows (tier 2, status approved, `adr_path` pointing to the WS09 target path) and an event `LEGACY_ADR_PLACEHOLDER` so WS09 completes them.
13. `build_legacy_sample.py`: copies schema + rows for 20 tickers across all dates into the fixture (≈ 120 rows).
14. Tests; PROGRESS; commit `WS10: legacy migration`.

## 9. Tests you must write (`tests/integration/test_migrate_legacy.py`, on the fixture; the sign-off runs them on the real DB)
```
test_legacy_db_untouched                 counts before == after for the three legacy tables
test_snapshot_map_six_rows_and_normalised_dates
test_scores_and_factor_values_counts     fixture: 20 tickers x 4 cohorts ; real: 1,997 scores per legacy model, 1,997 x 11 legacy factor rows
test_split_no_longer_a_return            ZFCVINDIA L1->L2 label r_arith in [-0.30, 0.30]
test_attribution_reproduced              with legacy_quotes=True: final -0.063/+0.092/+0.117 ; momentum -0.033/+0.030/+0.125 ; fundamentals +0.045/+0.058/+0.050 within +-0.01 (real DB only; fixture asserts the function runs)
test_defect_flags_present_on_every_snapshot
test_legacy_never_clean_and_no_learning_points
test_idempotent_second_run_raises_and_changes_nothing
test_dry_run_writes_nothing
```

## 10. Verification checklist
- `python -m quant db migrate-legacy --dry-run` prints the snapshot map and per-step planned counts; no rows written.
- `python -m quant db migrate-legacy` (real DB, after `prices backfill --tickers-from legacy`) prints the reconciliation table; then `select count(*) from scores where track='legacy' and model_id='LEGACY_V18'` = 1,997; `select count(*) from legacy_snapshot_map` = 6; `select count(*) from daily_predictions` (legacy DB) unchanged = 2,543.
- `python -m pytest tests/integration/test_migrate_legacy.py -q` green on the fixture; `QUANT_LEGACY_REAL=1 python -m pytest tests/integration/test_migrate_legacy.py -q -k attribution` green on the real DB.

## 11. Definition of done
- [ ] Legacy DB read-only; snapshot map; identity; sectors; prices; fundamentals; holdings; factors; scores; versions; labels; defects; runs
- [ ] Reconciliation reproduces the red-team table and prints the corrected version beside it
- [ ] Fixture DB and tests; idempotent; dry-run
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS11's UI Legacy view reads `legacy_snapshot_map` and `legacy_defects`; charts draw `track='legacy'` as hollow points.
- WS09 replaces placeholder decisions with the real ADR texts if it lands after you.

## 13. Risks, gotchas
- 07-11 was a Saturday and 09-03 a Thursday possibly intraday; prices at those as_of come from the V2 store's close, not the legacy quote — that is the point.
- Do not import `performance_tracking` as returns: it mixes unadjusted quotes and 4-day periods.
- Legacy `Div_Yield_%` and `FCF_Yield_%` must not become `fundamentals` values; they are evidence of a defect, stored only in `legacy_defects`.
