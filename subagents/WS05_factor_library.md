# WS05 — Factor library: plugin contract, FactorInputs, standardisation, registry, launch factors, sector features

## 1. Mission
Replace eight bucketed, partly hand-typed scores with continuous, sector-neutral, pre-registered factors whose only data access is a point-in-time view that physically cannot see the future. Ship the launch set (11 active-family factors, 9 shadows, 3 controls, the `dc_flag` diagnostic, sector features) with a hypothesis file each, and the truncation test that makes leakage a test failure rather than a review finding.

## 2. Read first
1. MASTER_SPEC §5 (all), §3.5, §2.4 (timing), §4.4 (what `pit_frame`/`ttm` return), §13 D08/D09/D23, §15 Q7/Q9
2. `subagents/README.md` interfaces; WS01–WS03 docs §6 (what you consume)
3. `quant_math.py` (only `sector_tokens`; do NOT port the bucketed scorers), `docs/analysis/red_team_review.md` §4 (why near-constant factors are fatal)

## 3. Scope
In: `base.py`, `inputs.py` (FactorInputs builder), `standardise.py`, `registry.py` (REGISTRY, sync, statuses, compute_all/one, values_frame, test_factor), all factor modules, `sector.py` (features + sector-level factors), `knowledge/hypotheses/H-2026-001..011.md`, `factors` commands, tests including T3 (PIT truncation) and per-factor synthetic tests.
Out: composite/weights (WS06); evaluation (WS07); status transitions beyond `registered→shadow` on first compute (WS09 decides the rest).

## 4. Dependencies
WS01, WS02, WS03 merged and populating the synthetic world. Verify `tests.synthetic.make_world` yields `prices_monthly`, `fundamentals`, `holdings`, `security_attributes`, `sector_map` for all months.

## 5. Interfaces you consume
`sectors.taxonomy.sector_group_at`, `universe.members_at`, `identity.tracked_securities`, `PriceStore.tri/close_raw/volume/adv_inr/benchmark_series`, `fundamentals.pit_frame/ttm/latest`, `holdings.series`, `attributes.at`, `gates.record_event`, `db.core.replace_as_of`.

## 6. Interfaces you provide
```python
# quant/factors/base.py       FactorSpec, Factor, FactorInputs exactly as MASTER_SPEC §5.1 (copy the block; do not paraphrase)
# quant/factors/inputs.py
def build(conn, store, as_of: str, cfg, track: str = 'live', members: pd.Index | None = None) -> FactorInputs
    # every accessor filters date <= as_of / available_from <= as_of / captured_at <= as_of ; raises LookaheadError if a caller asks for a later date ; caches frames per call
# quant/factors/standardise.py
def standardise(raw: pd.Series, groups: pd.Series, spec: FactorSpec, cfg, is_financial: pd.Series | None = None) -> pd.DataFrame   # columns raw, winsor, z, flags ; MASTER_SPEC §5.2 exactly
def gaussian_rank(x: pd.Series) -> pd.Series      # Phi^-1((rank-0.5)/n), average ties, NaN preserved
# quant/factors/registry.py
REGISTRY: dict[str, Factor]                       # factor_id -> Factor ; populated by importing the family modules
def sync(conn) -> SyncReport                      # upsert factor_registry rows (status 'registered' for new ids; never downgrades an existing status); code_sha256 = sha256(inspect.getsource(module)); refuses unknown inputs (not in INFO/STATEMENT fields, 'prices', 'holdings', 'benchmark', 'sector_features')
def statuses(conn) -> dict[str, str]
def compute_all(conn, store, as_of: str, cfg, track: str = 'live', statuses_filter=('shadow','active','probation','retired','control')) -> ComputeReport   # builds FactorInputs once; computes each factor; standardises; replace_as_of factor_values ; sets 'registered'->'shadow' and first_live_as_of on first LIVE compute ; report coverage per factor, excluded list
def compute_one(conn, store, as_of: str, factor_id: str, cfg, track='live') -> pd.DataFrame
def values_frame(conn, as_of: str, factor_ids: list[str], track: str = 'live', column: str = 'z') -> pd.DataFrame     # security_id x factor_id
def flags_frame(conn, as_of: str, factor_ids, track='live') -> pd.DataFrame
def test_factor(name: str) -> TestReport          # runs the per-factor synthetic tests + planted-signal pass-through for that factor
def family_of(factor_id: str) -> str
# quant/factors/sector.py
def compute_features(conn, store, as_of: str, cfg) -> pd.DataFrame   # sector_features rows for MASTER_SPEC §3.5 ; writes them
# sector-level Factor objects (level='sector') read sector_features through FactorInputs.sector_feature(feature_id)
```

## 7. Deliverables
`quant/factors/{__init__,base,inputs,standardise,registry,momentum,low_risk,quality,value,growth,flows,controls,sector,legacy}.py`, `quant/commands/factors.py` (`factors sync|list|test NAME|compute --as-of D [--factor ID] [--track]|backfill --factor ID`), `knowledge/hypotheses/H-2026-001.md … H-2026-011.md` (one per active factor; template MASTER_SPEC §5.4; `registered_on` = the first V2 run date; `first_oos_as_of` = first live as_of) plus `H-2026-S01..S09` for shadows (same template, status shadow), `tests/unit/test_factor_contract.py`, `tests/unit/test_inputs_pit.py`, `tests/unit/test_standardise.py`, `tests/unit/test_factors_momentum.py`, `_low_risk.py`, `_quality.py`, `_value.py`, `_growth.py`, `_flows.py`, `_controls.py`, `_sector.py`, `tests/unit/test_registry.py`, PROGRESS entry.

## 8. Implementation plan
1. Copy `FactorSpec/Factor/FactorInputs` from MASTER_SPEC §5.1 verbatim into `base.py`.
2. `inputs.build`: resolve `members` (`members_at(as_of)` for live; for `track='backfill'` the current list); `sector_group_at`; `is_financial` = group startswith `FS_` or `nse_sector == 'Financial Services'`; lazy accessors calling WS02/WS03 with `end=as_of`; every accessor asserts requested dates ≤ as_of (`LookaheadError`). `sector_feature(feature_id)` reads `sector_features` at as_of.
3. `standardise`: implement §5.2 in order: financial NaN → coverage check → winsor (universe-wide quantiles on non-NaN) → within-group average-tie rank → `gaussian_rank` → `* direction` → clip ±3 → small-group NaN → flags. Return frame indexed by security_id.
4. Factor modules: one `Factor` per spec row in §5.3, formulas exactly as written; `compute` returns raw values indexed by `security_id`; low-history → NaN. Examples: `mom_12_1`: `tri(273)`; `p1 = tri.iloc[-22]`, `p12 = tri.iloc[0]`; NaN where `< 230` valid obs. `roce`: `ttm('EBIT') / (latest balance Total Assets − Current Liabilities)`; NaN where denominator ≤ 0. `earnings_yield`: non-financials `ttm(EBIT)/ev_inr`; financials `ttm(Net Income)/mcap_inr`. `eps_growth_3y`: `ln(EPS_A0/EPS_A3)/3` from `pit_frame(income, 'Diluted EPS', 'A', 4)`; NaN if either ≤ 0. `earn_mom`: needs 8 quarters; `(ttm_now − ttm_4q_ago)/|ttm_4q_ago|`. `inst_hold_chg_3m`: `holdings(0) − holdings(3)`. Controls compute but are flagged `control`. `dc_flag` in `legacy.py`.
5. `sector.py`: features per group from FactorInputs (EW mean of members' 6m log TRI change, breadth over SMA200, median 3-run change in holdings, median earnings yield minus universe median, dispersion); sector-level Factor objects `sector_mom_6m`, `sector_breadth_200`, `sector_flow_proxy` with `level='sector'`, standardised across groups.
6. `registry.sync`: for every Factor: upsert row; `hypothesis_id` must exist in `hypotheses` (WS09 table; until WS09 lands, accept the H-id string and warn) — the H markdown files are written here regardless.
7. `compute_all`: order by family; coverage report; G8-style exclusion flag in the report (the gate itself is WS04); write rows with `track`; on first live compute set `status='shadow'` for `registered` factors and `first_live_as_of`.
8. `factors backfill --factor ID`: for `backfillable` factors compute over `calendar.month_ends('2016-01-31', first_live)` with `track='backfill'` on the current constituent list (survivorship label in the report); refuse for non-backfillable.
9. Hypothesis files: fill the §5.4 template per factor with honest evidence lines (index products, literature) and a "why it might fail in India" line.
10. Tests (incl. T3), PROGRESS, commit `WS05: factor library`.

## 9. Tests you must write
```
tests/unit/test_factor_contract.py::test_every_registered_factor_has_hypothesis_direction_formula_inputs
tests/unit/test_factor_contract.py::test_compute_is_pure_no_network                 monkeypatch yfinance import -> raise; all computes still run on FactorInputs
tests/unit/test_factor_contract.py::test_financial_exclusion_yields_nan
tests/unit/test_factor_contract.py::test_launch_set_matches_spec_table               names, families, directions, horizons, statuses == MASTER_SPEC §5.3
tests/unit/test_inputs_pit.py::test_accessors_refuse_dates_after_as_of               LookaheadError
tests/unit/test_inputs_pit.py::test_truncation_invariance_T3                         compute all factors at as_of on the synthetic DB; physically delete every row dated after as_of into a copy; recompute; frames equal exactly
tests/unit/test_inputs_pit.py::test_holdings_lag_uses_prior_capture                  T8
tests/unit/test_standardise.py::test_group_mean_zero_sd_about_one_any_size           21-member and 63-member groups
tests/unit/test_standardise.py::test_direction_minus_one_reverses_rank
tests/unit/test_standardise.py::test_nan_stays_nan_and_flagged                       never 0
tests/unit/test_standardise.py::test_small_group_nan_flag
tests/unit/test_standardise.py::test_constant_within_group_yields_nan_and_fails_coverage   (the legacy moat=50 case)
tests/unit/test_factors_momentum.py::test_mom_12_1_on_geometric_path_exact ; test_trend_200_zero_when_price_equals_sma ; test_rev_1m_sign
tests/unit/test_factors_low_risk.py::test_vol_252_matches_numpy ; test_max_ret_21
tests/unit/test_factors_quality.py::test_roce_uses_ttm_ebit_and_latest_balance ; test_accruals_sign ; test_cash_conversion_nan_when_ni_sum_le_0 ; test_leverage_net_debt_ebitda
tests/unit/test_factors_value.py::test_earnings_yield_financial_vs_nonfinancial ; test_book_to_price ; test_div_yield_from_rate ; test_fcf_yield_3y_mean
tests/unit/test_factors_growth.py::test_eps_growth_nan_on_nonpositive_endpoint_never_imputed ; test_earn_mom_needs_8_quarters ; test_rev_growth_3y
tests/unit/test_factors_flows.py::test_inst_hold_chg_nan_until_three_captures
tests/unit/test_factors_controls.py::test_size_liq_beta_and_dc_flag
tests/unit/test_factors_sector.py::test_features_per_group_and_sector_factor_standardised_across_groups
tests/unit/test_registry.py::test_sync_pins_code_sha_and_refuses_unknown_inputs
tests/unit/test_registry.py::test_changed_source_without_version_bump_detected
tests/unit/test_registry.py::test_first_live_compute_moves_registered_to_shadow
tests/unit/test_registry.py::test_planted_signal_pass_through                       synthetic 'plant_mom' recovers IC in [0.06, 0.14] after standardise (T2 half); production refuses a factor reading forward data
tests/unit/test_registry.py::test_backfill_refused_for_non_backfillable
```

## 10. Verification checklist
- `python -m quant factors sync --db /tmp/w/quant.db` → `27 factors registered (11 active-family, 9 shadow, 3 control, 1 diagnostic, 3 sector)`.
- `python -m quant factors compute --as-of <synthetic month 40> --db /tmp/w/quant.db` → coverage per factor ≥ 0.95 for price factors on the synthetic world; `select factor_id, count(*), avg(z), sum(z is null) from factor_values where as_of=? group by 1` shows mean ≈ 0, sd ≈ 1 per group (check one group).
- T3 by hand: `python -m pytest tests/unit/test_inputs_pit.py::test_truncation_invariance_T3 -q` green.
- On the real DB (after WS02/WS03 ran): `factors compute --as-of 2026-09-30` prints coverage; no factor is > 80% modal (W3 clean) except `inst_hold_chg_3m` (expected NaN).
- `ls knowledge/hypotheses | wc -l` ≥ 20.

## 11. Definition of done
- [ ] Contract copied verbatim; FactorInputs raises on look-ahead; T3 green
- [ ] All §5.3 factors implemented with per-factor synthetic tests and NaN-never-imputed tests
- [ ] Standardisation proven (mean 0 / sd 1 per group; NaN preserved; constant → NaN)
- [ ] Registry sync/status/first_live semantics tested
- [ ] Hypothesis files written for every non-control factor
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS06 consumes `values_frame(as_of, active_ids, 'live', 'z')` and `family_of`.
- WS07 evaluates every factor with status ≠ registered; it reads `factor_values.z` and `flags`.
- WS10 uses `legacy.py` ids `legacy_*@0` and `compute_one` for price factors at legacy dates.
- Coverage of `inst_hold_chg_3m` is 0 until run 3 by design.

## 13. Risks, gotchas
- The most likely implementer shortcut is bypassing `FactorInputs` "just for one factor". The contract test greps every factor module for `sqlite3`, `pandas.read_sql`, `yfinance` and fails if found.
- `gaussian_rank` with `scipy.stats.norm.ppf`; guard `u` in (0,1).
- Do not port `score_quality`, `score_growth`, the DCF or the WACC table as factors. The DCF may live in `ui_export` (WS11) as an explainer only.
- Sector-level z is across ~18 groups: tiny cross-section; the sleeve cap in WS06 is what keeps it harmless.
