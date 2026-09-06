# WS02 — Daily price store, corporate actions, total-return index, monthly panel, benchmarks

## 1. Mission
Own every price. Store what Yahoo delivered (unadjusted close, volume, dividend, split ratio) in a git-ignored SQLite file, build the total-return index in-engine, reconcile every month against the prior download, detect what Yahoo does not record (demergers, rights), produce the committed monthly panel, and build every benchmark from the same series. The legacy engine's worst single number (a 6:1 split recorded as −84%) is this workstream's reason to exist.

## 2. Read first
1. MASTER_SPEC §4.3, §4.5, §7.4, §8.2, §2.4, §10.4 `[yahoo]` `[returns]`, §13 D05/D16/D20
2. `subagents/README.md` interfaces
3. Context brief §2 (verified yfinance behaviours)

## 3. Scope
In: `PriceStore` (backfill, monthly update, reconciliation, TRI, ADV, manifest), `corporate_actions` writing from Yahoo columns, unrecorded-action detector and manual add/clear, `prices_monthly` panel, benchmark series and replicated indices, `NULL_RANDOM` helper data (weights drawn in WS07), `prices` and `data ca` commands.
Out: fundamentals/info (WS03); labels (WS07) — but `tri()` is what labels read.

## 4. Dependencies
WS00 (`core`, `calendar`, `RunContext`), WS01 (`identity.yahoo_ticker`, `tracked_securities`, `members_at`). Verify: `python -m quant universe fetch` has run on your DB (or the synthetic world exists).

## 5. Interfaces you consume
`quant.data.identity.yahoo_ticker`, `tracked_securities`, `members_at`; `quant.data.calendar` (note: calendar consumes your `^CRSLDX` series — break the cycle by passing a DataFrame); `quant.data.yahoo.YahooClient.download_batch` (WS03) — until WS03 lands, use a thin local `_download(tickers, start, end)` wrapper around `yf.download` with the same signature and swap later (log in PROGRESS).

## 6. Interfaces you provide
```python
# quant/data/prices.py
class PriceStore:
    def __init__(self, path: str | Path): ...                          # data/prices_daily.sqlite ; creates table prices_daily(security_id INTEGER, date TEXT, open, high, low, close_raw, yahoo_adj_close, volume, dividend, split_ratio, pulled_at TEXT, PRIMARY KEY(security_id, date)) WITHOUT ROWID + index on date ; symbols table for benchmarks (security_id < 0 reserved for benchmark symbols, mapping table bm_symbols(security_id, symbol))
    def backfill(self, tickers: dict[int, str], start: str, cfg, resume: bool = True) -> BackfillReport      # batches of cfg.yahoo.batch_size, threads=False, auto_adjust=False, actions=True, sleep cfg.yahoo.batch_sleep_s; tz stripped; resumable per security
    def update(self, security_ids: list[int], as_of: str, cfg, tickers: dict[int, str]) -> UpdateReport     # trailing cfg.yahoo.lookback_months; upsert; then reconcile_overlap
    def reconcile_overlap(self, security_id: int, new: pd.DataFrame, cfg) -> list[Event]                     # identical | SPLIT_RESTATED (apply ratio to stored history, write corporate_actions source 'reconcile') | UNEXPLAINED_PRICE_REVISION (quarantine)
    def close_raw(self, security_ids, start: str, end: str) -> pd.DataFrame     # dates x security_id
    def volume(self, security_ids, start, end) -> pd.DataFrame
    def tri(self, security_ids, start, end) -> pd.DataFrame                    # TRI_0 = 100 at each security's first stored date; TRI_d = TRI_{d-1} * (close_d + div_d) / close_{d-1}
    def adv_inr(self, security_ids, as_of: str, window: int = 63) -> pd.Series # mean(close_raw * volume) over the last `window` trading days <= as_of ; also n_days traded
    def days_traded(self, security_ids, as_of: str, window: int = 63) -> pd.Series
    def manifest_write(self, path: Path | None = None) -> Path                 # per security: first_date, last_date, n_rows, sha256 of (date, close_raw, dividend, split_ratio), pulled_at, yfinance_version
    def manifest_verify(self, path: Path | None = None) -> list[str]           # differences vs current store
    def benchmark_series(self, symbol: str, start, end, field='close_raw') -> pd.Series
def build_monthly_panel(conn, store: PriceStore, as_of: str, cfg) -> pd.DataFrame   # rows for prices_monthly: close_raw, tri, adv_63_inr, n_days_63, mcap_inr (from security_attributes if present), shares_out ; writes via replace_as_of
# quant/data/actions.py
def record_yahoo_actions(conn, store, security_ids, since: str) -> int          # corporate_actions rows kind split|dividend from the daily columns, source 'yahoo_actions'
def detect_unrecorded(store, security_ids, since: str, log_threshold: float) -> list[Event]   # |ln(close_d/close_{d-1})| > threshold with no dividend/split -> SUSPECTED_UNRECORDED_CA
def add_ca(conn, store, isin: str, ex_date: str, kind: str, factor: float, decision_id: str | None, note: str = '') -> None   # writes corporate_actions kind manual_adj (or demerger/rights) with adj_factor; rebuilds TRI for that security (TRI is computed on read, so this just records; tri() applies adj_factor to closes before ex_date)
def clear_flag(conn, isin: str, date: str, by: str) -> None                      # resolves the event; labels with excluded_ca in that window are re-evaluated by WS07 on next run
# quant/data/benchmarks.py
BENCHMARKS = ['BM_EW', 'BM_EW_SECTOR', 'BM_CW', 'BM_N500PR', 'BM_N500TR_PROXY', 'BM_MOM30_C', 'BM_QUAL30_C', 'BM_MOM30_ETF', 'BM_QUAL30_ETF', 'BM_LOWVOL_ETF', 'BM_N50_ETF', 'BM_MID150_ETF']
def update(conn, store, as_of: str, cfg) -> None            # pulls index/ETF symbols into the store; computes month-end TRI levels for every benchmark id into benchmarks_monthly
def series(conn, benchmark_id: str, start: str, end: str) -> pd.Series           # month-end TRI levels
def ew_tri(store, security_ids: list[int], dates: list[str]) -> pd.Series        # equal-weight monthly-rebalanced TRI of a set
def replicate_index(conn, store, index_name: str, as_of: str) -> float           # EW of the constituent list valid at as_of (universe_membership index_name)
```

## 7. Deliverables
`quant/data/prices.py`, `quant/data/actions.py`, `quant/data/benchmarks.py`, `quant/commands/prices.py` (registers `prices backfill|update|monthly|manifest`, `data ca detect|add|clear-flag|accept-revision`), `data/MANIFEST.json` (after the first real backfill), `.gitignore` entry verified, `tests/unit/test_prices.py`, `tests/unit/test_actions.py`, `tests/unit/test_benchmarks.py`, `tests/fixtures/prices_zfcvindia_2026.csv` (raw download around the split, 2026-06-01..07-15, recorded once), `tests/fixtures/prices_heromotoco_div_2026.csv` (around the 2026-07-24 ₹75 dividend), PROGRESS entry.

## 8. Implementation plan
1. Record the two fixtures with one network call each (`yf.download('ZFCVINDIA.NS', start='2026-06-01', end='2026-07-15', auto_adjust=False, actions=True)`); strip tz; save CSV. These pin the split (`Stock Splits == 6.0` on 2026-06-24; `Close` continuous) and the dividend (`Dividends == 75.0` on 2026-07-24).
2. `PriceStore` schema and upsert (own tiny `executemany`; do not depend on `quant.db.core` for this file, but reuse its NaN→NULL helper). `security_id < 0` for benchmark symbols via `bm_symbols`.
3. `backfill`: chunk `tickers` in 25s; `yf.download(list, start=start, auto_adjust=False, actions=True, group_by='ticker', threads=False, progress=False)`; per ticker frame → rows; `split_ratio = Stock Splits or 1.0`; `dividend = Dividends or 0.0`; `pulled_at = today`; sleep between batches; resume = skip securities whose `max(date)` is within 5 trading days of `end`; record failures.
4. `tri`: per security, pull `(date, close_raw, dividend)` ascending; apply `adj_factor` from manual `corporate_actions` (multiply closes before `ex_date` by factor); compute the cumulative product; return a wide frame. Cache per call only.
5. `reconcile_overlap`: for the overlapping dates compare `new.close_raw / stored.close_raw`; median within 0.05% → nothing; a constant ratio `r` with `|r - k| < 1%` for a reported split `k` (new frame's `Stock Splits` in the window or Yahoo's ratio) → multiply stored closes before the ex-date by `1/k`, write `corporate_actions(source='reconcile')`, event `SPLIT_RESTATED`; otherwise `UNEXPLAINED_PRICE_REVISION`: write new rows to `prices_daily_quarantine`, leave the store untouched, event WARN. `accept-revision` moves quarantine → store.
6. `detect_unrecorded`: daily log returns > `cfg.returns.jump_log_threshold` (ln 1.40) with `dividend == 0 and split_ratio == 1` → event per (security, date).
7. `build_monthly_panel`: for `tracked_securities(as_of)`: `close_raw` at as_of, `tri`, `adv_63_inr`, `n_days_63`, `mcap_inr = shares_out * close_raw` if `security_attributes` has shares (WS03) else NULL; `replace_as_of('prices_monthly', ...)`.
8. Benchmarks: pull `cfg.yahoo.index_symbols` into the store; `BM_N500PR` = `^CRSLDX` close; `BM_N500TR_PROXY` = `^CRSLDX` × exp(0.012 × years); `BM_EW` = `ew_tri(eligible members)` (eligibility from `scores` when WS06 exists; until then all members — document); `BM_CW` = mcap-weighted; `BM_EW_SECTOR` needs portfolio group weights (WS08) — compute the per-group EW series here and let WS08 combine; `BM_MOM30_C`/`BM_QUAL30_C` = `replicate_index` from `universe_membership` rows with `index_name` NIFTY200MOM30/QUAL30; ETFs = adjusted close series relabelled. Write `benchmarks_monthly`.
9. Commands with summary lines per MASTER_SPEC §10.3.
10. Tests; PROGRESS; commit `WS02: prices, actions, TRI, benchmarks`.

## 9. Tests you must write
```
tests/unit/test_prices.py::test_batching_25_and_sleep_called                 (mock yf.download: 553 tickers -> 23 calls, 22 sleeps, threads=False, auto_adjust=False)
tests/unit/test_prices.py::test_tz_stripped_and_dates_iso
tests/unit/test_prices.py::test_zfcvindia_split_fixture_tri_continuous       monthly TRI return over the split window in [-0.20, +0.20]; never -0.84
tests/unit/test_prices.py::test_heromotoco_dividend_in_tri                   TRI return over 2026-07-24 exceeds price return by ~75/close
tests/unit/test_prices.py::test_synthetic_split_and_dividend_invariance      inject 6:1 split + Rs 100 dividend into a copy -> TRI unchanged to 1e-6 (T7)
tests/unit/test_prices.py::test_reconcile_identical_noop
tests/unit/test_prices.py::test_reconcile_restates_split_and_writes_action
tests/unit/test_prices.py::test_reconcile_unexplained_quarantines
tests/unit/test_prices.py::test_adv_and_days_traded_window
tests/unit/test_prices.py::test_manifest_changes_when_history_changes
tests/unit/test_prices.py::test_monthly_panel_rows_and_keys
tests/unit/test_actions.py::test_detect_unrecorded_flags_40pct_gap_without_action
tests/unit/test_actions.py::test_add_ca_factor_applied_before_ex_date_only
tests/unit/test_benchmarks.py::test_ew_tri_equals_mean_of_members_monthly
tests/unit/test_benchmarks.py::test_replicated_index_uses_list_valid_at_as_of
tests/unit/test_benchmarks.py::test_n500_tr_proxy_accrues_1_2pct
```

## 10. Verification checklist
- `python -m quant prices backfill --start 2016-01-01 --db quant.db` (network, ~5–10 min): prints securities count, batches, 429s, rows; `data/prices_daily.sqlite` ≈ 100–160 MB; `git status` does not show it; `data/MANIFEST.json` written.
- `python -m quant prices monthly --as-of 2026-08-31` → `prices_monthly` rows ≈ 500; `select count(*) from prices_monthly where tri is null` = 0 for members with ≥ 1 day of data.
- ZFCVINDIA check on real data: `python - <<'EOF2'\nfrom quant.data.prices import PriceStore; s=PriceStore('data/prices_daily.sqlite'); t=s.tri([<id>], '2026-06-12','2026-07-10'); print(t.iloc[-1]/t.iloc[0]-1)\nEOF2` → between −0.20 and +0.20.
- `python -m quant data ca detect --as-of 2026-08-31` lists suspected demergers/rights (expect a handful, e.g. ITC Hotels if in window).
- `python -m pytest tests/unit/test_prices.py tests/unit/test_actions.py tests/unit/test_benchmarks.py -q` green.

## 11. Definition of done
- [ ] Fixtures recorded and committed; split/dividend tests green
- [ ] Store, TRI, ADV, manifest, reconciliation implemented and tested
- [ ] Monthly panel writes `prices_monthly`; benchmarks write `benchmarks_monthly`
- [ ] Real backfill run once; MANIFEST committed; daily store git-ignored
- [ ] PROGRESS entry; commit

## 12. Handoff notes
- `tri()` is the only return series anyone may use (labels, portfolios, benchmarks). Never read `yahoo_adj_close` for returns.
- `tracked_securities` grows as labels open; backfill those names too (`prices backfill --tickers-from legacy` pulls legacy tickers).
- `BM_EW` eligibility scope will change once WS06 writes `scores.eligible`; keep the scope parameter explicit.

## 13. Risks, gotchas
- yfinance silently drops symbols that fail in a batch: compare requested vs returned columns and record the misses.
- Motilal Oswal ETFs return NaN closes; use the ICICI ones listed in config.
- Never call `history(period='max', auto_adjust=True)` for storage; adjusted series are rewritten backward.
- A 60% monthly move is legitimate in Indian small caps; do not reintroduce the ±60% filter. The jump detector works on daily gaps without actions.
