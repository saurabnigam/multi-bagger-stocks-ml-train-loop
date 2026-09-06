# WS06 — Composite, screens, models, champion/challengers, family-weight learning, invariants, model review

## 1. Mission
Turn factor z-scores into rankings for several models at once, with no multipliers and no filters that zero a rank; keep a permanent equal-weight champion; implement the challenger's weight rule as a pure, idempotent function of matured evidence with the gate and shrinkage from the spec; assert the invariants that replace the legacy 5%–30% rule; and compute the challenger-vs-champion review criteria.

## 2. Read first
1. MASTER_SPEC §6 (all), §5.3 (families), §7.2 (embargo), §7.3 (t_crit), §13 D10/D11/D12/D23
2. `weight_optimizer.py::project_weights` (reuse the projection), `docs/analysis/red_team_review.md` §3 (why the old optimizer drifted)
3. WS05 doc §6 (`values_frame`, `family_of`), WS07 doc §6 (`ic_series`, `labels_available`)

## 3. Scope
In: `composite.py`, `screens.py`, `learn.py`, `models.py` (definitions, versions, score_all/one, invariants, review, ic_history), `score` and `model` commands, seed rows for `models` (EW_HIER_v1, EW_FLAT_v1, MOM_ONLY_v1, IC_SHRUNK_v1, SECTOR_OVERLAY_v1 registered but inactive until WS09's hypothesis, LEGACY_V18 placeholders).
Out: evaluations (WS07), promotion decisions (WS09), paper portfolios (WS08).

## 4. Dependencies
WS05 merged (`values_frame` works on the synthetic world). WS07 not required to build (ic_history returns empty → gate closed) but required for the review test to be meaningful.

## 5. Interfaces you consume
`factors.registry.values_frame/flags_frame/statuses/family_of`, `sectors.taxonomy.sector_group_at`, `PriceStore.adv_inr/days_traded`, `universe.members_at` (series column), `evaluation.evaluate.ic_series` and `evaluation.walkforward.labels_available` (WS07; guarded import), `evaluation.stats.t_crit` (WS07), `weight_optimizer.project_weights` (legacy module, pure).

## 6. Interfaces you provide
```python
# quant/model/learn.py
def fit_family_weights(ic_hist: pd.DataFrame, horizon_m: int = 3, k_shrink: float = 24.0, floor_mult: float = 0.5, cap_mult: float = 2.0, min_n_eff: float = 4.0) -> tuple[dict[str, float], dict]
    # MASTER_SPEC §6.3 verbatim; diagnostics = {'n_months','n_eff','alpha','gate':'open'|'closed','ic_bar':{...},'raw':{...},'clamped':[...]}
# quant/model/composite.py
def family_scores(z: pd.DataFrame, families: dict[str, str]) -> pd.DataFrame          # security_id x family ; nanmean ; NaN if all NaN
def compose(z: pd.DataFrame, families: dict[str, str], weights: dict[str, float], groups: pd.Series, cfg, sleeve: pd.Series | None = None, w_sleeve: float = 0.0) -> pd.DataFrame
    # columns family_scores_json, composite, composite_neutral, sector_tilt, final, scored, n_factors_used, rank_all, rank_group, decile, quintile ; §6.1 rules (>= 3 families, >= 60% factors)
# quant/model/screens.py
def apply(conn, store, as_of: str, df: pd.DataFrame, cfg) -> pd.DataFrame            # adds eligible, exclusion_reason ('illiquid'|'series'|'sector'|'coverage'|None), liquidity_bucket ; rank over eligible
# quant/model/models.py
LAUNCH_MODELS = [...]                                                                   # §6.5 table as dicts
def seed(conn, cfg, as_of: str) -> None                                                # inserts models + version 1 rows if absent (factor_set from registry statuses at as_of)
def definition_at(conn, model_id: str, as_of: str) -> ModelVersion                    # version valid at as_of: factor_set, families, weights, sleeve
def bump_version(conn, model_id: str, factor_set: list[dict], weights: dict, decision_id: str | None, valid_from: str, note: str = '') -> int
def ic_history(conn, model_id: str, horizon_m: int, through: str) -> pd.DataFrame     # as_of x family 3M IC of family scores, ONLY as_of in labels_available(through, h) ; empty frame if WS07 absent
def score_one(conn, store, as_of: str, model_id: str, cfg, track='live') -> pd.DataFrame   # resolves version; z frame for its factor_set; weights (equal / fit_family_weights / overlay) ; compose ; screens ; input_hash ; replace_as_of scores + model_weights
def score_all(conn, store, as_of: str, cfg, track='live') -> ScoreReport
def check_invariants(conn, as_of: str | None = None) -> list[str]                     # I1–I6 violations as strings; empty = ok
def review(conn, as_of: str, cfg) -> ReviewReport                                      # per challenger: P1–P5 values, passes, criteria_check_json
```

## 7. Deliverables
`quant/model/{__init__,composite,screens,learn,models}.py`, `quant/commands/model.py` (`score --as-of D [--model ID] [--track]`, `model list|check|review`), `tests/unit/test_composite.py`, `tests/unit/test_screens.py`, `tests/unit/test_learn.py`, `tests/unit/test_models.py`, PROGRESS entry.

## 8. Implementation plan
1. `learn.fit_family_weights` first, with the worked example as the first test (numbers in MASTER_SPEC §6.3). Reuse `weight_optimizer.project_weights` for the box-simplex projection (floor/ceil parameters); round to 4 dp; residue to the largest.
2. `composite.family_scores` via `groupby(families, axis=1).mean()` with `min_count=1`; `compose`: weighted sum over present families with renormalised weights; `composite_neutral = gaussian_rank within groups` (import from WS05 `standardise.gaussian_rank`); sleeve mix; `scored` rule; ranks with `method='first'` on `final` descending for `rank_all` (ties broken by security_id for determinism), `rank_group`, decile/quintile over scored names.
3. `screens.apply`: ADV and days traded from `PriceStore`; series from `universe_membership.series`; sector from groups (`UNCLASSIFIED`); coverage from `scored`; bucket via `cfg.costs.bucket_adv_inr` (A/B/C/D); `rank` over eligible names only (NULL otherwise).
4. `models.seed`: five models per §6.5 with `params_json` (EW_HIER: {kind:'equal', hierarchy:'family'}; IC_SHRUNK: {k_shrink:24, min_n_eff:4, floor_mult:0.5, cap_mult:2.0, horizon_m:3}; SECTOR_OVERLAY: {base:'EW_HIER_v1', w_sleeve:0.10}, role 'challenger' but `registered_on` NULL until WS09 registers its hypothesis → skipped by `score_all`; MOM_ONLY, EW_FLAT references). Version 1 `factor_set` = active factors at seed time; families from `family_of`.
5. `score_one`: weights by kind: equal (hierarchical: 1/F per family), flat (compose with weights that give each factor 1/N → implement by passing per-factor weights option to `compose`), shrink (`fit_family_weights(ic_history(...))`; when gate closed the dict equals hierarchical EW), overlay (base weights + sleeve from `sector_features` standardised). `input_hash = sha256(z frame values rounded 10 dp + weights json)`. Write `scores` and `model_weights` (including `gate`, `n_eff`, `alpha`).
6. `check_invariants`: I1 sums, I3 bounds, I4 weight 0 for non-active, I6 hash presence, I5 (challenger weights never applied to champion: champion's `model_weights` equal hierarchical EW for every as_of).
7. `review`: for each challenger vs champion: paired monthly 3M IC series via WS07 `ic_series` (guarded); P1 count ≥ 24; P2 HAC t of differences ≥ `t_crit(m_models)`; P3 from `portfolio_returns` (WS08; skip with 'n/a' if absent); P4 12M sign check; P5 turnover ratio (WS08) and overrides (from `runs.override_decision_id`); `criteria_check_json` with a boolean per criterion.
8. Command output per MASTER_SPEC §10.3 `[6 score]` line.
9. Tests; PROGRESS; commit `WS06: scoring, models, learning`.

## 9. Tests you must write
```
tests/unit/test_learn.py::test_worked_example_matches_spec_to_4dp
tests/unit/test_learn.py::test_gate_closed_below_min_n_eff_returns_exact_equal
tests/unit/test_learn.py::test_negative_family_never_below_floor_never_negative
tests/unit/test_learn.py::test_bounds_clamped_and_sum_exactly_one
tests/unit/test_learn.py::test_pure_and_idempotent                                 same input twice -> identical dict ; no state
tests/unit/test_learn.py::test_uses_only_complete_labels                           a row for an unmatured month must be excluded by the caller: ic_history test below
tests/unit/test_composite.py::test_family_mean_then_family_mean                    three value factors do not triple value's weight
tests/unit/test_composite.py::test_missing_family_renormalises_weights_and_flags
tests/unit/test_composite.py::test_scored_requires_3_families_and_60pct_factors
tests/unit/test_composite.py::test_neutral_rerank_is_gaussian_per_group
tests/unit/test_composite.py::test_no_multipliers_anywhere                         grep quant/model for 'trap' / 'momentum_multiplier' / '* 0.0' patterns -> none
tests/unit/test_screens.py::test_illiquid_series_sector_coverage_reasons_and_buckets
tests/unit/test_screens.py::test_screened_names_still_scored_and_ranked_all
tests/unit/test_models.py::test_seed_creates_launch_models_and_version_1
tests/unit/test_models.py::test_challenger_equals_champion_before_gate             synthetic world, months < 15
tests/unit/test_models.py::test_ic_history_respects_embargo                        no as_of newer than through - 3 months
tests/unit/test_models.py::test_input_hash_reproducible_and_changes_with_inputs
tests/unit/test_models.py::test_check_invariants_detects_violations                inject bad weights row -> messages
tests/unit/test_models.py::test_review_criteria_json_shape_and_t_crit
tests/unit/test_models.py::test_bump_version_closes_previous_and_scores_use_valid_version
```

## 10. Verification checklist
- `python -m quant score --as-of <synthetic month 40> --db /tmp/w/quant.db` → `[6 score] EW_HIER_v1#1 EW_FLAT_v1#1 MOM_ONLY_v1#1 IC_SHRUNK_v1#1 -> 240 rows; scored 58; eligible 55 (...); gate=closed n_eff=0.0`.
- `python -m quant model check` → `invariants: ok`.
- `select model_id, family, weight from model_weights where as_of=? ` shows EW_HIER = 1/F each; IC_SHRUNK identical (gate closed).
- With WS07 present and the synthetic world's 48 months: `python -m quant model review --as-of <month 47>` prints P1–P5 with P1 count and `t_crit(m)`.
- `python -m pytest tests/unit/test_learn.py tests/unit/test_composite.py tests/unit/test_screens.py tests/unit/test_models.py -q` green.

## 11. Definition of done
- [ ] Worked example test green; gate/shrinkage/bounds proven; pure function
- [ ] Composite hierarchical, sector-neutral, no multipliers; screens evaluated not zeroed
- [ ] Models seeded; versions; `input_hash`; invariants I1–I6
- [ ] Review criteria computed with `criteria_check_json`
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS07 evaluates `scores.final` per model and `family_scores_json` per family; WS08 reads `rank`, `eligible`, `liquidity_bucket`.
- WS09's `apply()` calls `bump_version` when a factor is activated/retired and when a model is promoted (role change in `models`).
- WS10 writes LEGACY_V18 rows directly via `replace_as_of` (track legacy) — not through `score_one`.

## 13. Risks, gotchas
- The most likely "improvement" is adding a learning rate or momentum to `fit_family_weights`. Don't. The worked-example test exists to catch it.
- `rank` must be over eligible names only; `rank_all` over scored names; portfolios use `rank`.
- Never apply the challenger's weights to the champion's row set; I5 test guards it.
- Family renormalisation when a family is missing must not silently favour names with fewer factors: `scored` requires ≥ 3 families.
