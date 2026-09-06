# WS01 — Universe snapshots, ISIN identity, sector taxonomy and neutralisation groups

## 1. Mission
Make "who is in the universe, what is it called, and which sector group does it belong to on date d" answerable point-in-time from committed files. Every later join keys on `security_id` (ISIN) and reads `sector_map` valid on `as_of`. This is what makes the composite sector-neutral and the record survivorship-safe.

## 2. Read first
1. MASTER_SPEC §3 (all), §4.1 (flow), §10.4 `[sources]`, `[universe]`, `[sectors]`, §13 D03/D04, §15 Q6/Q11
2. `subagents/README.md` interfaces (WS00 provides; you provide)
3. `harness_v16_learning.py::get_nifty_500_tickers` (the old fetch, for the URL and header handling only)

## 3. Scope
In: fetch/save/parse the three constituent CSVs; `securities`, `symbol_history`, `universe_membership`; rename detection; manual overrides; `sector_group_def` v1 (CSV → table); `sector_map` maintenance with validity ranges; FS split rule (code present, disabled by config until month 3); merge rule; Yahoo→NSE crosswalk; `sectors show` and `universe fetch` commands.
Out: sector *features* (WS05 `quant/factors/sector.py`); prices; anything Yahoo except reading `security_attributes.yahoo_industry` (WS03) for the FS split.

## 4. Dependencies
WS00 merged: `python -m quant db init --db /tmp/q.db` works; `tests.synthetic.make_world` importable.

## 5. Interfaces you consume
`quant.config.load`, `quant.db.core.connect/apply_schema/upsert/replace_as_of`, `quant.run.RunContext`, `quant.cli.register`, `quant.data.calendar.last_trading_day_on_or_before`. From WS03 (optional at runtime): `security_attributes.yahoo_industry` for the FS split; when absent, FS stays one group and a WARN event is written.

## 6. Interfaces you provide
```python
# quant/data/universe.py
LISTS = {'NIFTY500': 'ind_nifty500list.csv', 'NIFTY200MOM30': 'ind_nifty200Momentum30_list.csv', 'NIFTY200QUAL30': 'ind_nifty200Quality30_list.csv'}
def fetch_list(name: str, as_of: str, cfg) -> Path                 # GET with User-Agent; save verbatim to data/universe/<prefix>_<as_of>.csv (nifty500_, idx_mom30_, idx_qual30_); on failure return the latest prior file and raise nothing; caller logs UNIVERSE_STALE
def parse_list(path: Path) -> pd.DataFrame                          # columns isin, symbol, company_name, nse_sector, series ; asserts 5 expected headers, unique isin & symbol
def snapshot(conn, as_of: str, cfg, fetch: bool = True) -> UniverseSnapshot   # for each list: fetch/parse, upsert securities & symbol_history, write universe_membership rows (source nse_csv|nse_csv_stale), sha256
def members_at(conn, as_of: str, index_name: str = 'NIFTY500') -> pd.DataFrame    # latest snapshot with as_of_snapshot <= as_of
# quant/data/identity.py
def upsert_security(conn, isin: str, name: str, symbol: str, as_of: str, source: str) -> int
def resolve_security_id(conn, isin: str | None = None, symbol: str | None = None, as_of: str | None = None) -> int | None
def yahoo_ticker(conn, security_id: int, as_of: str) -> str        # symbol_history row valid at as_of; config/manual_ticker_overrides.csv wins
def tracked_securities(conn, as_of: str) -> list[int]               # members_at(as_of) ∪ securities with any labels row whose end_date > as_of ∪ open paper positions
def detect_renames(conn, parsed: pd.DataFrame, as_of: str) -> list[tuple[int, str, str]]   # (security_id, old_symbol, new_symbol); closes/opens symbol_history rows
# quant/sectors/taxonomy.py
def load_group_def(conn, cfg) -> pd.DataFrame                       # config/sector_group_def_v1.csv -> sector_group_def (version from cfg.sectors.group_def_version)
def assign_groups(members: pd.DataFrame, group_def: pd.DataFrame, yahoo_industry: pd.Series | None, cfg) -> pd.DataFrame   # security_id, nse_sector, sector_group, macro_sector, merged_from
def update_sector_map(conn, as_of: str, cfg) -> SectorUpdate        # closes/opens validity rows only on change; writes SECTOR_RECLASS / GROUP_MERGED events via WS04 record_event if available (else stores in .events for the caller)
def sector_group_at(conn, as_of: str, security_ids=None) -> pd.Series
def group_table(conn, as_of: str) -> pd.DataFrame                   # sector_group, n, merged_from, min_ok
# quant/sectors/crosswalk.py
def yahoo_to_nse(yahoo_sector: str, yahoo_industry: str | None) -> tuple[str, float]   # from config/yahoo_to_nse_crosswalk_v1.csv (yahoo_sector, yahoo_industry_pattern, nse_sector, share)
def refresh_crosswalk(conn, as_of: str) -> pd.DataFrame            # recompute modal shares from names classified by NSE this month; writes the CSV with a version bump only via decision (report only otherwise)
```

## 7. Deliverables
`quant/data/universe.py`, `quant/data/identity.py`, `quant/sectors/__init__.py`, `quant/sectors/taxonomy.py`, `quant/sectors/crosswalk.py`, `quant/commands/universe.py` (registers `universe fetch`, `sectors show`), `config/sector_group_def_v1.csv`, `config/yahoo_to_nse_crosswalk_v1.csv`, `config/manual_ticker_overrides.csv` (header only + comment), `tests/fixtures/nifty500_2026-09-05.csv` (the real file; commit it), `tests/fixtures/idx_mom30_2026-09-05.csv`, `tests/fixtures/idx_qual30_2026-09-05.csv`, `tests/unit/test_universe.py`, `tests/unit/test_identity.py`, `tests/unit/test_sectors.py`, `tests/unit/test_crosswalk.py`, PROGRESS.md entry.

## 8. Implementation plan
1. Download the three real CSVs once (`curl -A Mozilla/5.0 https://niftyindices.com/IndexConstituent/ind_nifty500list.csv`) into `tests/fixtures/` — these are the parse fixtures and the first `data/universe/` files.
2. `parse_list`: read with `csv.DictReader`; strip whitespace; rename `ISIN Code`→`isin`, `Symbol`→`symbol`, `Company Name`→`company_name`, `Industry`→`nse_sector`, `Series`→`series`; assert header set; assert uniqueness; sort by isin.
3. `snapshot`: for `NIFTY500` (required) and the two index lists (optional; failure → WARN only): fetch, sha256 of bytes, save verbatim, upsert securities (`first_seen`/`last_seen`), symbol_history (open row per security; rename detection by ISIN with different symbol), `universe_membership` rows with `index_name`.
4. `sector_group_def_v1.csv` columns: `version,nse_sector,yahoo_industry_pattern,sector_group,macro_sector,merge_into,min_group_size,registered_on,note`. Encode MASTER_SPEC §3.4: 20 NSE sectors → groups; FS rows with regex patterns for Banks / Lenders / Markets (pattern NULL row maps FS to `Financial Services` when `split_financials=false`); merge targets for Textiles→`Consumer Durables & Textiles`, Media→`Consumer Services & Media`, Diversified→`Services & Diversified`, Telecommunication→`Services & Diversified` when below min.
5. `assign_groups`: apply FS rule iff `cfg.sectors.split_financials`; then the merge table; then the generic `< min_group_size → merge_into or OTHER` rule; return `merged_from` for the report.
6. `update_sector_map`: compare the assignment with the open `sector_map` row per security; on change set `valid_to = as_of` and open a new row `valid_from = as_of`; on first sight open with the snapshot's as_of; `source` per the fallback chain (`nse_csv`, `nse_csv_prior` ≤ 24 months, `yahoo_crosswalk`, else `UNCLASSIFIED` with confidence 0). Never touch closed rows.
7. Crosswalk CSV: seed with MASTER_SPEC §3.4's mapping (Financial Services→Financial Services 1.0; Industrials→Capital Goods 0.55 / Construction 0.15 / Services 0.15 …). `refresh_crosswalk` recomputes shares from this month's NSE-classified names and prints a diff; writing a new version is a decision (WS09) — here only print.
8. Commands: `universe fetch --as-of D` prints `NIFTY500 500 (sha ..); MOM30 30; QUAL30 30; new ISINs k; renames j; sector_map: n reclass, m merges; groups: <table>`; `sectors show --as-of D` prints the group table.
9. Tests, PROGRESS entry, commit `WS01: universe, identity, sectors`.

## 9. Tests you must write
```
tests/unit/test_universe.py::test_parse_real_fixture_500_rows_20_sectors_all_eq
tests/unit/test_universe.py::test_parse_rejects_wrong_header
tests/unit/test_universe.py::test_snapshot_writes_membership_and_sha           (monkeypatch fetch -> fixture path)
tests/unit/test_universe.py::test_fetch_failure_falls_back_to_prior_file_and_flags
tests/unit/test_universe.py::test_members_at_uses_latest_snapshot_not_future
tests/unit/test_identity.py::test_rename_detected_by_isin_closes_and_opens_symbol_history   (ZOMATO -> ETERNAL synthetic)
tests/unit/test_identity.py::test_manual_override_wins
tests/unit/test_identity.py::test_tracked_includes_dropped_name_with_open_label
tests/unit/test_sectors.py::test_group_def_loads_20_sectors_and_merge_targets
tests/unit/test_sectors.py::test_min_group_size_merges_into_target_then_other   (synthetic 3-member group)
tests/unit/test_sectors.py::test_fs_split_disabled_by_default_enabled_by_config  (patterns on 'Banks - Regional', 'Credit Services', 'Capital Markets')
tests/unit/test_sectors.py::test_reclass_closes_old_row_opens_new_row_history_untouched
tests/unit/test_sectors.py::test_lookup_before_reclass_sees_old_group           (synthetic world event at month 30)
tests/unit/test_sectors.py::test_fallback_chain_order_and_confidence
tests/unit/test_sectors.py::test_real_fixture_groups_all_at_least_min_size
tests/unit/test_crosswalk.py::test_known_mappings_and_share_bounds
```
Command: `python -m pytest tests/unit/test_universe.py tests/unit/test_identity.py tests/unit/test_sectors.py tests/unit/test_crosswalk.py -q`.

## 10. Verification checklist
- `python -m quant universe fetch --as-of 2026-09-30 --db /tmp/q.db` (network) or with `QUANT_OFFLINE=1` using fixtures: prints 500 / 30 / 30 and a group table whose smallest group ≥ 8; `data/universe/nifty500_2026-09-30.csv` byte-identical to the download.
- `sqlite3 /tmp/q.db "select count(*) from securities; select count(distinct isin) from securities; select sector_group, count(*) from sector_map where valid_to is null group by 1 order by 2 desc"` → 500 (or 501+ with index lists), groups as printed.
- Reclass simulation: edit a fixture copy changing one name's `Industry`, run `update_sector_map` for a later as_of, assert two rows for that security with disjoint validity.
- `python -m pytest tests/unit -q` green.

## 11. Definition of done
- [ ] Real fixtures committed; parse test green
- [ ] Membership, identity, sector_map written PIT; reclass and rename tests green
- [ ] Group table printed by `sectors show`; smallest group ≥ 8 on the real fixture
- [ ] FS split code present, disabled by config, tested
- [ ] Crosswalk CSV seeded and tested
- [ ] PROGRESS.md entry; commit

## 12. Handoff notes
- `sector_group_at(as_of)` is the only sanctioned way to get groups; WS05's `FactorInputs.sector_group` calls it.
- `tracked_securities` needs `labels` (WS07) and `portfolio_positions` (WS08); until those tables have rows it returns current members only — that is correct.
- The FS split flips on via `config.sectors.split_financials = true` under decision D-2026-12-xx (WS09); when flipped, `update_sector_map` opens new rows for ~100 securities on that as_of — expected, logged as reclass.

## 13. Risks, gotchas
- NSE occasionally serves an HTML error page with HTTP 200; `parse_list` must fail on header mismatch, not on row count.
- `&` in symbols is fine for Yahoo; never use symbols in file names.
- Do not derive `nse_sector` from Yahoo for names present in the CSV; the crosswalk is a last resort only.
- Do not backfill sector history from today's map without `source='current_backfill'` and confidence 0.5.
