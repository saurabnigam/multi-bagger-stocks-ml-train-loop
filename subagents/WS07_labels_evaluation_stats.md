# WS07 — Labels, overlap-aware statistics, walk-forward, evaluations, leakage tests, evidence and learning curves

## 1. Mission
Produce every number the owner will ever quote, correctly: sector-relative total-return labels that follow every scored stock to maturity; Spearman ICs with Newey–West errors and honest effective sample sizes; a walk-forward embargo implemented once; ten leakage tests that run on real data every month; the two curves that distinguish "we know more" from "the system learned"; and the null benchmarks. The legacy `ic * sqrt((n-2)/(1-ic^2))` with n = 500 stocks must not exist here.

## 2. Read first
1. MASTER_SPEC §2 (labels, horizons, KPI), §7 (all), §4.5 (tracks), §13 D01/D02/D14/D15/D24
2. `docs/analysis/red_team_review.md` §2–§3, §10 (what went wrong statistically)
3. WS02 (`tri`), WS05 (`values_frame`), WS06 (`scores`) docs §6

## 3. Scope
In: `labels.py`, `stats.py`, `metrics.py`, `walkforward.py`, `leakage.py`, `evaluate.py`, `curves.py`, benchmark evaluation rows, `NULL_RANDOM`/`NULL_BEST`, `labels`, `evaluate`, `verify leakage|pit` commands, backfill replay.
Out: portfolio returns (WS08), report rendering (WS09), UI (WS11).

## 4. Dependencies
WS02, WS05, WS06 merged; synthetic world scored for all 48 months (`score_all` in a loop) — write a helper in your tests that does this once per session.

## 5. Interfaces you consume
`PriceStore.tri/close_raw`, `calendar.month_ends/add_trading_days`, `sectors.taxonomy.sector_group_at`, `universe.members_at`, `identity.tracked_securities`, `factors.registry.values_frame/statuses/family_of`, `models.definition_at`, `scores` table, `benchmarks.series/ew_tri`, `gates.record_event`.

## 6. Interfaces you provide
```python
# quant/evaluation/labels.py
def mature(conn, store, as_of: str, cfg) -> LabelReport                        # for every (s, h) with end month <= as_of and no label row: compute r_log, r_arith, r_group_median, l_rel, r_uni, status; mb36 for h=36 ; writes labels
def frame(conn, as_of: str, horizon_m: int, scope: str = 'eligible', track: str = 'live', model_id: str = 'EW_HIER_v1') -> pd.DataFrame   # security_id, l_rel, r_log, status, sector_group, eligible
def multibagger(conn, as_of: str, model_id: str) -> dict                        # base_rate, precision, recall, lift, wilson_lo, wilson_hi, n
# quant/evaluation/stats.py
@dataclass HacResult: mean, se, t, ci_lo, ci_hi, n, n_eff, naive_se, naive_t
def hac_mean_test(x: np.ndarray, lag: int) -> HacResult                          # §7.3 exactly; lag = h - 1 ; guards N <= lag + 1 (returns NaNs with n) and zero variance
def block_bootstrap_ci(x: np.ndarray, block: int, n: int = 1000, q: float = 0.90, seed: int = 0) -> tuple[float, float]
def t_crit(m: int, alpha: float = 0.05, floor: float = 2.0) -> float             # max(floor, norm.ppf(1 - alpha/m))
def wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]
def icir(x) -> float | None                                                       # None when n_eff < 6 (caller prints 'n/a')
# quant/evaluation/metrics.py
def rank_ic(score: pd.Series, label: pd.Series) -> float                          # Spearman on the intersection; NaN dropped; 0.0 if < 3 or constant
def quintiles_within_group(score: pd.Series, label_arith: pd.Series, groups: pd.Series) -> pd.DataFrame   # q1..q5 mean/median/trimmed ; spread ; monotonicity
def decile_turnover(prev_members: set, cur_members: set) -> float
def partial_ic(z_new: pd.Series, z_active: pd.DataFrame, label: pd.Series) -> float
def corr_matrix(z: pd.DataFrame) -> pd.DataFrame
def fm_slope(label: pd.Series, z: pd.Series, size: pd.Series) -> float
# quant/evaluation/walkforward.py
def labels_available(conn, T: str, h: int, track: str = 'live') -> list[str]    # as_of with as_of + h months' end date <= T and a labels row ; the ONLY embargo implementation
def replay_backfill(conn, store, cfg, start: str = '2016-01-31', end: str | None = None) -> None   # compute -> score -> mature -> evaluate for track 'backfill' monthly
# quant/evaluation/leakage.py
def run_all(conn, store, as_of: str, cfg) -> LeakageReport                        # T1..T10 ; each returns (passed, detail) ; writes BLOCK events on failure
def t1_shuffle(...); t2_planted(...); t3_pit_truncation(...); t4_asof_boundary(...); t5_embargo(...); t6_forward_shift(...); t7_corporate_action(...); t8_holdings_lag(...); t9_survivorship(...); t10_sector(...)
# quant/evaluation/evaluate.py
def run(conn, store, as_of: str, cfg, track: str = 'live') -> EvalReport         # §7.1 per-month rows for everything matured; then cumulative/rolling rows (window_start/window_end) ; benchmark rows ; cohorts ; idempotent ; evaluations_log on change
def ic_series(conn, subject_kind: str, subject_id: str, horizon_m: int, scope: str = 'eligible', track: str = 'live', through: str | None = None) -> pd.Series   # indexed by as_of ; only matured
def random_composite_percentile(conn, as_of: str, cfg) -> float                   # NULL_RANDOM ; 1,000 Dirichlet family weights ; champion IC percentile ; seeded
def best_single_factor(conn, as_of: str, horizon_m: int) -> tuple[str, float]
# quant/evaluation/curves.py
def update(conn, as_of: str, cfg) -> None                                          # evidence_curve rows for every subject/horizon/track ; learning_curve_points for IC_SHRUNK_v1 (re-fit at each k) with ew_oos_ic
```

## 7. Deliverables
`quant/evaluation/{__init__,labels,stats,metrics,walkforward,leakage,evaluate,curves}.py`, `quant/commands/evaluate.py` (`labels mature --as-of D`, `evaluate --as-of D [--track]`, `verify leakage --as-of D`, `verify pit --months 3`, `run backfill-track`), `tests/unit/test_stats.py`, `tests/unit/test_metrics.py`, `tests/unit/test_labels.py`, `tests/property/test_walkforward.py`, `tests/leakage/test_T1_T10.py`, `tests/unit/test_evaluate.py`, `tests/unit/test_curves.py`, PROGRESS entry.

## 8. Implementation plan
1. `stats.hac_mean_test` (25 lines of numpy) and its three tests first; `t_crit` table test; `wilson`.
2. `labels.mature`: for each scored `as_of` (from `scores` distinct as_of per track) and `h` in `cfg.horizons.tracked_m` with `end = calendar.month_ends` target ≤ current as_of: `tri` at both dates for every security scored at s (not just current members); `r_log`; group median over the same set with `G(i,s)` from the scores row's `sector_group`; statuses per §2.1 (delisted_partial when the TRI series ends early; excluded_ca when an unresolved `SUSPECTED_UNRECORDED_CA` event lies in the window; missing); `mb36` and `mb36_touch` for h=36; `price_manifest_sha` from WS02 manifest.
3. `metrics`: Spearman via `scipy.stats.spearmanr`; quintiles formed within group by rank then pooled; trimmed mean 5%; monotonicity = Spearman(quintile index, mean).
4. `walkforward.labels_available`: SQL on `labels` joined to the calendar; property test that no training window overlaps the test date.
5. `evaluate.run`: subjects = factors (status ≠ registered) via `values_frame(z)`, families via `scores.family_scores_json`, models via `scores.final`, `dc_flag`, cohorts (`exclusion_reason` groups), sector features; for each newly matured `(s, h)` write per-month metric rows (`ic`, `ic_uni`, `q1..q5`, `spread_q5_q1`, `spread_net` placeholder until WS08 provides costs → write `spread_gross` now and `spread_net` when `portfolio.costs` importable), `hit_rate`, `partial_ic` (shadows), `n`; then recompute cumulative rows (`window_start` = first live as_of, `window_end` = as_of) with `mean_ic, hac_se, hac_t, n, n_eff, ci90_lo/hi, hit_rate, icir` and rolling-24 rows; benchmark rows (`BM_*` h-returns); `NULL_RANDOM` percentile and `NULL_BEST`. Upsert by the unique key; log changes.
6. `leakage`: implement T1–T10 as functions taking the same context; T2 constructs `plant` from `labels.l_rel` at h=3 (this is the only place forward data is touched, inside the test); T3 copies the DB to a temp file, deletes rows dated after `as_of` (prices in the store are filtered by date; fundamentals by `available_from`; holdings by `captured_at`), recomputes via WS05 `compute_all` into the copy, compares frames exactly; T6 shifts `available_from` −90 days in a copy and recomputes fundamental factor ICs; T7 uses WS02's split/dividend injection helper; T9 compares evaluated set with `universe_membership`; T10 IC of group dummies.
7. `curves.update`: evidence rows per subject; learning points: for each `k` (clean months of training data) re-fit `fit_family_weights` on `ic_history(through=train_end)` → score the test month with those weights using stored z (call `compose`) → OOS IC when its label exists; `ew_oos_ic` from the champion at the same test month.
8. `replay_backfill`: monthly loop 2016-01-31 → first live month on `track='backfill'` using the current constituent list (WS01 `members_at` with `source='current_backfill'`), price factors only; then `mature` and `run` on that track.
9. Commands; PROGRESS; commit `WS07: labels, evaluation, leakage, curves`.

## 9. Tests you must write
```
tests/unit/test_stats.py::test_hac_equals_naive_for_iid                          within 15%
tests/unit/test_stats.py::test_hac_inflates_for_overlapping_sums                 ratio within 25% of sqrt(h), N=600, h in (3, 12)
tests/unit/test_stats.py::test_hac_constant_series_flagged_no_zero_division
tests/unit/test_stats.py::test_t_crit_table                                      m=1 2.00, 3 2.13, 6 2.39, 12 2.64, 24 2.87 (2 dp)
tests/unit/test_stats.py::test_wilson_and_icir_gate
tests/unit/test_metrics.py::test_rank_ic_matches_scipy_and_handles_constants
tests/unit/test_metrics.py::test_quintiles_formed_within_group_then_pooled
tests/unit/test_metrics.py::test_partial_ic_zero_for_duplicate_factor
tests/unit/test_labels.py::test_label_uses_tri_ratio_log_and_group_median
tests/unit/test_labels.py::test_incomplete_when_end_price_missing_and_statuses
tests/unit/test_labels.py::test_follows_dropped_name_to_maturity                 T9 half
tests/unit/test_labels.py::test_split_and_dividend_do_not_create_return          synthetic world events
tests/unit/test_labels.py::test_multibagger_lift_on_planted_cohort
tests/property/test_walkforward.py::test_no_training_window_overlaps_test_date   hypothesis-free: loop over all T, h
tests/leakage/test_T1_T10.py::test_t1_shuffle_kills_ic
tests/leakage/test_T1_T10.py::test_t2_planted_signal_recovered_and_refused_in_production
tests/leakage/test_T1_T10.py::test_t3_truncation_invariance
tests/leakage/test_T1_T10.py::test_t4_asof_boundary_sql
tests/leakage/test_T1_T10.py::test_t5_embargo_row_ignored
tests/leakage/test_T1_T10.py::test_t6_forward_shift_raises_ic_for_leaky_factor   construct a deliberately leaky synthetic factor -> detected ; clean ones -> not
tests/leakage/test_T1_T10.py::test_t7_corporate_action_invariance
tests/leakage/test_T1_T10.py::test_t8_holdings_lag
tests/leakage/test_T1_T10.py::test_t9_survivorship_set_equality
tests/leakage/test_T1_T10.py::test_t10_sector_dummies_ic_zero
tests/unit/test_evaluate.py::test_rows_written_idempotent_and_logged_on_change
tests/unit/test_evaluate.py::test_cumulative_rows_carry_n_eff_and_ci
tests/unit/test_evaluate.py::test_no_cross_sectional_t_in_output                  grep evaluations.method values: no 'xsec_t' ; naive only labelled 'naive'
tests/unit/test_evaluate.py::test_random_composite_percentile_seeded
tests/unit/test_curves.py::test_evidence_curve_x_axis_skips_blocked_months
tests/unit/test_curves.py::test_learning_points_have_ew_baseline_and_refit_per_k
tests/unit/test_curves.py::test_planted_world_learning_curve_beats_ew            on the synthetic world with a planted family, learned - EW > 0 by month 40 (sanity, wide tolerance)
```

## 10. Verification checklist
- Synthetic world: `python -m quant labels mature --as-of <month 48>` then `python -m quant evaluate --as-of <month 48> --db /tmp/w/quant.db` → prints matured counts, cumulative 3M IC of `plant_mom` in [0.06, 0.14] with n_eff ≈ 15, leakage `T1..T10 PASS`.
- `select method, count(*) from evaluations group by 1` shows `spearman`, `hac_l2`, `hac_l11`, `bootstrap_b3`, `naive`; nothing else.
- `python -m quant verify pit --months 3 --db /tmp/w/quant.db` → `PIT reproducibility: 3/3 months identical`.
- Real DB after WS02/03/05/06 ran for one month: `evaluate --as-of 2026-09-30` writes nothing live (no matured labels yet) and prints the backfill block if `run backfill-track` was executed: `mom_12_1 backfill 12M IC cum > 0` (falsifier check).
- `python -m pytest tests/unit/test_stats.py tests/unit/test_metrics.py tests/unit/test_labels.py tests/property tests/leakage tests/unit/test_evaluate.py tests/unit/test_curves.py -q` green.

## 11. Definition of done
- [ ] HAC, bootstrap, t_crit, Wilson tested; no cross-sectional t anywhere
- [ ] Labels follow every scored name; statuses; mb36
- [ ] Embargo implemented once and property-tested
- [ ] T1–T10 implemented, tested on synthetic data, runnable on real data
- [ ] Evaluations idempotent with change log; cumulative rows carry n_eff/CI
- [ ] Both curves written; learning points carry `ew_oos_ic`
- [ ] Backfill replay works on the real store
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS06 `ic_history` and `review` read `ic_series`; WS09 criteria engine reads cumulative rows by `(subject_kind, subject_id, horizon_m, metric, window_end)`; WS08 fills `spread_net` costs; WS11 renders curves from `evidence_curve`/`learning_curve_points`.
- Everything is recomputed from scratch each month; do not add incremental state.

## 13. Risks, gotchas
- `spearmanr` on < 3 points or constant input returns NaN: guard.
- Overlapping horizons: never average per-month ICs and call the naive t "significance"; the HAC row is the record.
- T3 must copy the DB; never run it against the live file.
- Backfill rows must carry `track='backfill'` on every table; a single mislabelled row contaminates the live curve.
