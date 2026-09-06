# WS03 — Yahoo client, unit normalisation, bitemporal fundamentals, holdings, attributes, raw archive

## 1. Mission
Own every call to Yahoo Finance and every fundamental number. Store statements as long, bitemporal rows (period_end, available_from with its basis, fetched_at); normalise units in exactly one place with tests pinned to observed values; archive the raw payloads so the point-in-time claim can be audited; capture institutional holdings into our own dated history. The 349% dividend yield and the None→0 ROE were born in this layer; they must be impossible here.

## 2. Read first
1. MASTER_SPEC §4.4, §4.1, §4.5, §10.6 (field contracts), §10.4 `[yahoo]`, §13 D06, §15 Q5
2. Context brief §2 (verified yfinance field behaviours)
3. `harness_v16_learning.py::_row_series`, `_fcf_series` (statement parsing to copy, with tests), `quant_math.normalize_yield` (reuse)

## 3. Scope
In: `YahooClient` (throttle, batch download used by WS02, per-ticker bundles, 429 back-off, raw archive), unit normalisers, `fundamentals` ingest with `available_from` rules and restatement-as-new-row, `ttm`, `pit_frame`, `holdings`, `security_attributes`, adapter stubs (interfaces only), `fundamentals fetch` command.
Out: factors (WS05), gates (WS04) — you expose the data they check.

## 4. Dependencies
WS00, WS01 (`identity.yahoo_ticker`, `tracked_securities`). Verify `python -m quant universe fetch --as-of <D>` populated `securities`.

## 5. Interfaces you consume
`quant.data.identity.yahoo_ticker/tracked_securities`, `quant.data.calendar.add_trading_days`, `quant.db.core.upsert`, `quant.run.RunContext`.

## 6. Interfaces you provide
```python
# quant/data/yahoo.py  — THE ONLY module importing yfinance
class YahooClient:
    def __init__(self, cfg): ...                                     # per_ticker_sleep_s, batch_size, batch_sleep_s, on_429_sleep_s, max_retries
    def download_batch(self, tickers: list[str], start: str, end: str | None = None) -> pd.DataFrame   # yf.download(..., auto_adjust=False, actions=True, group_by='ticker', threads=False, progress=False); tz stripped; MultiIndex (ticker, field)
    def bundle(self, ticker: str) -> RawBundle                         # info dict, financials, balance_sheet, cashflow, quarterly_financials, quarterly_balance_sheet, earnings_dates(limit=12); each guarded; sleeps per_ticker_sleep_s once per ticker; retries with back-off on 429
    def archive(self, as_of: str, bundles: dict[str, RawBundle], data_dir: Path) -> Path             # data/raw/fundamentals/<as_of>.jsonl.gz ; one line per ticker: {"ticker","fetched_at","info":{...},"statements":{name:{"columns":[...],"index":[...],"data":[[...]]}}}
def norm_dividend_yield(dividend_rate: float | None, close: float | None) -> float   # rate/close ; NaN if either missing or close <= 0 ; capped at 0.25 with flag by caller
def norm_debt_to_equity(x) -> float          # percent -> ratio (357 -> 3.57) ; NaN for None
def norm_fraction(x) -> float                # heldPercent* : passthrough in [0,1] ; NaN otherwise
def to_nan(x) -> float                       # None/'' -> NaN ; never 0
def strip_tz(index) -> pd.DatetimeIndex
INFO_FIELDS = {'marketCap': ('market_cap_inr','inr'), 'sharesOutstanding': ('shares_outstanding','shares'), 'floatShares': ('float_shares','shares'), 'enterpriseValue': ('ev_inr','inr'),
               'trailingPE': ('trailing_pe','x'), 'priceToBook': ('price_to_book','x'), 'dividendRate': ('dividend_rate_inr','inr'), 'beta': ('beta','x'),
               'heldPercentInstitutions': ('inst_held_frac','frac'), 'heldPercentInsiders': ('insider_held_frac','frac'), 'sector': ('yahoo_sector','text'), 'industry': ('yahoo_industry','text')}
STATEMENT_FIELDS = {'income': ['Total Revenue','EBIT','EBITDA','Net Income','Diluted EPS','Basic EPS','Interest Expense'],
                    'balance': ['Total Assets','Current Liabilities','Total Debt','Cash And Cash Equivalents','Stockholders Equity','Ordinary Shares Number'],
                    'cashflow': ['Operating Cash Flow','Capital Expenditure','Free Cash Flow']}
# quant/data/fundamentals.py
def available_from(period_end: str, freq: str, earnings_dates: pd.DataFrame | None, calendar) -> tuple[str, str]   # ('YYYY-MM-DD', basis) per MASTER_SPEC 4.4 rules 1–3
def ingest(conn, client: YahooClient, security_ids: list[int], as_of: str, cfg, statements: bool = True) -> IngestReport   # n_ok, n_failed, basis_counts, n_restated, archive_path, http_429s
def pit_frame(conn, as_of: str, statement: str, field: str, freq: str, n_periods: int, security_ids) -> pd.DataFrame      # security_id x period_rank(0..n-1) ; only available_from <= as_of ; latest fetched_at <= as_of per (period_end) ; NaN absent
def ttm(conn, as_of: str, field: str, security_ids) -> tuple[pd.Series, pd.Series]      # sum of last 4 quarters all available; else latest annual with flag 'ttm_from_annual'; NaN if neither
def latest(conn, as_of: str, statement: str, field: str, freq: str, security_ids) -> pd.Series
# quant/data/holdings.py
def capture(conn, bundles: dict[int, RawBundle], as_of: str, run_date: str) -> int    # holdings rows with captured_at = run_date (NOT as_of)
def series(conn, as_of: str, lag_runs: int, security_ids) -> pd.Series                 # capture with captured_at <= as_of, `lag_runs` captures earlier ; NaN if absent
# quant/data/attributes.py
def capture(conn, bundles: dict[int, RawBundle], as_of: str, close_raw: pd.Series) -> int   # security_attributes row per security ; dividend_rate_inr ; adv from WS02 filled later by build_monthly_panel
def at(conn, as_of: str, field: str, security_ids) -> pd.Series
# quant/data/adapters/  (stubs: interface + NotImplementedError + docstring on the data source)
bhavcopy.py: fetch_day(date) -> pd.DataFrame ; nsdl_flows.py: fetch_fortnight(date) ; amfi_taxonomy.py: load(path) ; equity_l.py: fetch() -> listing dates
```

## 7. Deliverables
`quant/data/yahoo.py`, `quant/data/fundamentals.py`, `quant/data/holdings.py`, `quant/data/attributes.py`, `quant/data/adapters/{__init__,bhavcopy,nsdl_flows,amfi_taxonomy,equity_l}.py`, `quant/commands/fundamentals.py` (`fundamentals fetch --as-of D [--statements] [--limit N] [--isins ...]`), `tests/fixtures/yahoo_info_samples.json` (recorded `info` for HEROMOTOCO.NS, PNB.NS and one small cap, with their `None` fields), `tests/fixtures/yahoo_statements_heromotoco.json` (recorded statements + earnings_dates), `tests/unit/test_yahoo_units.py`, `tests/unit/test_yahoo_client.py`, `tests/unit/test_fundamentals.py`, `tests/unit/test_holdings.py`, `tests/unit/test_attributes.py`, PROGRESS entry.

## 8. Implementation plan
1. Record fixtures with a handful of live calls (one `bundle` per ticker) and save them as JSON; mark the recording date in the file. Everything else runs offline.
2. `YahooClient.bundle`: wrap each accessor in try/except → `None` frame; `time.sleep(per_ticker_sleep_s)` once per ticker (keep AGENTS.md's 0.5 s); on an HTTP 429-like exception sleep `on_429_sleep_s` and retry up to `max_retries`; count 429s. `download_batch` for WS02.
3. Statement parsing: copy `_row_series` logic; produce long rows `(statement, freq, period_end ISO, field, value float, unit 'inr')` for the fields in `STATEMENT_FIELDS` only; EPS unit `inr`; shares `shares`.
4. `available_from`: implement the four rules; earnings dates matched to a period_end when `0 < (earnings_date - period_end).days <= 60` and `Reported EPS` not NaN; add one trading day via calendar; rule 4 when `fetched_at > rule date`.
5. `ingest`: for each security: bundle → statement rows → for each `(statement, period_end, field)` compare with the latest stored `value`; if absent or different → insert a new row with this `fetched_at` (never update); `info` point fields → rows with `statement='info', freq='P', period_end=''`, `available_from = as_of`, basis `run_date`; holdings and attributes captured; raw archive written; report.
6. `pit_frame`/`ttm`/`latest`: SQL with `available_from <= ?` and window over `fetched_at`; return wide frames indexed by `security_id`.
7. Normalisers with pinned tests: `norm_dividend_yield(75.0, 5300.0) == 0.01415`; `norm_debt_to_equity(357) == 3.57`; `norm_fraction(0.3905) == 0.3905`; `to_nan(None)` is NaN; and a test that asserts `dividendYield` is **never read** (grep the module for the string).
8. Command output: `fundamentals: 500/500 ok (29m 40s); available_from basis: earnings_date 311, lodr_45d 146, lodr_60d 43, first_fetch 0; restated rows 12; holdings 500; archive data/raw/fundamentals/2026-10-30.jsonl.gz (612 KB)`.
9. Tests; PROGRESS; commit `WS03: yahoo client, PIT fundamentals, holdings`.

## 9. Tests you must write
```
tests/unit/test_yahoo_units.py::test_dividend_yield_from_rate_over_close       75/5300 ; NaN on None ; NaN on close 0
tests/unit/test_yahoo_units.py::test_debt_to_equity_percent_to_ratio          357 -> 3.57
tests/unit/test_yahoo_units.py::test_none_stays_nan_never_zero
tests/unit/test_yahoo_units.py::test_dividend_yield_field_never_read          source of quant/data/yahoo.py contains no 'dividendYield' outside a comment
tests/unit/test_yahoo_client.py::test_sleep_per_ticker_and_429_backoff        mock yfinance; assert sleeps and retries
tests/unit/test_yahoo_client.py::test_bundle_tolerates_empty_frames           quarterly_cashflow empty (HEROMOTOCO fixture)
tests/unit/test_yahoo_client.py::test_archive_roundtrip                       write gz, read back, same statements
tests/unit/test_fundamentals.py::test_available_from_rules                    Q ending 2026-06-30 without earnings date -> 2026-08-14 (+1 td) 'lodr_45d'; annual 2026-03-31 -> 2026-05-30 (+1 td) 'lodr_60d'; with earnings date 2026-07-22 -> 2026-07-23 'earnings_date'
tests/unit/test_fundamentals.py::test_value_invisible_before_available_from  visible at 2026-08-31, invisible at 2026-07-31
tests/unit/test_fundamentals.py::test_restatement_inserts_new_row_keeps_old
tests/unit/test_fundamentals.py::test_pit_frame_period_rank_order
tests/unit/test_fundamentals.py::test_ttm_four_quarters_else_annual_flagged
tests/unit/test_fundamentals.py::test_no_imputation_anywhere                  a security missing EBIT yields NaN, not 0 or 0.15
tests/unit/test_holdings.py::test_captured_at_is_run_date_and_lag_series      this run's capture invisible at as_of; last month's visible
tests/unit/test_attributes.py::test_info_fields_mapped_and_units
```

## 10. Verification checklist
- `python -m quant fundamentals fetch --as-of 2026-09-30 --limit 5 --db quant.db` (network) completes; `select basis, count(*) from (select available_from_basis basis from fundamentals) group by 1` shows the three bases; `data/raw/fundamentals/2026-09-30.jsonl.gz` exists.
- `select count(*) from fundamentals where field='Net Income' and value = 0` is small and each is a real zero in the archive (spot-check 2).
- `select count(*) from security_attributes where dividend_rate_inr/ (select close_raw from prices_monthly p where p.security_id=security_attributes.security_id and p.as_of=security_attributes.as_of) > 0.25` = 0.
- `python -m pytest tests/unit/test_yahoo_units.py tests/unit/test_yahoo_client.py tests/unit/test_fundamentals.py tests/unit/test_holdings.py tests/unit/test_attributes.py -q` green.

## 11. Definition of done
- [ ] Fixtures recorded; all tests green offline
- [ ] `available_from` rules and restatement semantics proven
- [ ] Raw archive written and committed for the first live run
- [ ] `dividendYield` never read; units pinned
- [ ] Holdings captured with `captured_at = run date`
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS05 reads only through `pit_frame`/`ttm`/`latest`/`holdings.series`/`attributes.at` wrapped by `FactorInputs`.
- WS04 gates read `security_attributes` and `fundamentals` coverage; W1 staleness uses `period_end`.
- Statement refresh cadence switches to quarterly after `cfg.yahoo.statements_every_run_until`.

## 13. Risks, gotchas
- `quarterly_cashflow` is empty for many names; `quarterly_balance_sheet` has 2–3 half-years: never assume 4 quarters exist.
- `earnings_dates` is sparse for small caps; the LODR lag rule is the common path.
- Do not store `info` as a JSON blob that factors read; only `security_attributes` columns are readable.
- Statement values are rupees; never convert to crores here.
