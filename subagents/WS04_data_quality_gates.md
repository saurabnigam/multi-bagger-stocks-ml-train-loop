# WS04 — Field contracts, drift, run gates, data-quality events

## 1. Mission
Decide, before any factor is computed, whether this month's data is fit to score, and record why. The legacy health suite could not fail on a 349% dividend yield or a factor constant for 85% of the universe; the gates here would have blocked both on day one. A blocked month is a first-class, recorded outcome.

## 2. Read first
1. MASTER_SPEC §4.6 (gates G1–G10, W1–W6), §10.6 (field contracts), §9.2 (`runs.dq_status`), §4.3 (events from reconciliation), §13 D15
2. `eval_portfolio_health.py` (near-constant detection to port; the unit checks it added)
3. `subagents/README.md` interfaces

## 3. Scope
In: `field_contracts` load/check, PSI drift, gate runner writing `dq_runs` + `data_quality_events`, the `Blocked` path, `record_event` used by every workstream, `gates run` command, G9 reproducibility hash check (uses WS05/WS06 when present; skipped with WARN before they exist), G10 shuffle smoke (uses WS07 `rank_ic` when present; else skipped with WARN).
Out: fixing data; deciding overrides (WS09 decisions; WS11 wires `--override-gate`).

## 4. Dependencies
WS01 (membership, sector coverage), WS02 (prices, revisions), WS03 (fundamentals, attributes). Verify all three have populated a DB (synthetic world suffices).

## 5. Interfaces you consume
`universe.members_at`, `sectors.taxonomy.sector_group_at`, `PriceStore.close_raw/adv_inr`, `fundamentals.latest`, `attributes.at`, `data_quality_events` rows written by WS02 reconciliation, `factors.registry.values_frame` + `models.score_one` (G9, optional), `evaluation.metrics.rank_ic` (G10, optional).

## 6. Interfaces you provide
```python
# quant/data/contracts.py
def load(cfg) -> dict[str, dict]                                   # config/field_contracts_v1.yaml -> {field: {unit, min_value, max_value, max_null_rate, source, notes}} ; mirrors into field_contracts table
def check_field(values: pd.Series, contract: dict) -> ContractResult    # n, n_null, null_rate, n_out_of_range, violators (index list), ok
def psi(current: pd.Series, reference: pd.Series, bins: int = 10) -> float   # population stability index on decile edges of `reference`
# quant/data/gates.py
GATES = ['G1','G2','G3','G4','G5','G6','G7','G8','G9','G10']  ; WARNINGS = ['W1','W2','W3','W4','W5','W6']
def run(conn, store, as_of: str, cfg, run_id: int, strict: bool = True) -> GateReport   # GateReport(passed: bool, dq_status: 'passed'|'passed_with_warnings'|'blocked', rows: DataFrame[gate, value, threshold, passed, blocking, detail]) ; writes dq_runs + events ; raises Blocked if any blocking gate failed and strict
def record_event(conn, run_id: int, as_of: str, severity: str, code: str, security_id: int | None = None, field: str | None = None, detail: dict | None = None) -> int
def events(conn, as_of: str | None = None, severity: str | None = None, code: str | None = None) -> pd.DataFrame
def near_constant_share(values: pd.Series) -> tuple[float, float]  # (modal_share, modal_value)
```

## 7. Deliverables
`quant/data/contracts.py`, `quant/data/gates.py`, `config/field_contracts_v1.yaml` (every field in MASTER_SPEC §10.6 plus the derived ones), `quant/commands/gates.py` (`gates run --as-of D`), `tests/unit/test_contracts.py`, `tests/unit/test_gates.py`, PROGRESS entry.

## 8. Implementation plan
1. YAML → dict; mirror into `field_contracts` on load (upsert keyed by field).
2. `check_field`: coerce to float; nulls; out-of-range count and violator ids; `ok = null_rate <= max_null_rate and n_out_of_range <= cfg.gates.G6_max_violators`.
3. `psi`: decile edges from `reference` (drop NaN), 1e-6 floor on shares, standard formula; return 0.0 when either series has < 50 values (with a note).
4. Gate implementations, each a function `g1(ctx) -> GateRow` receiving a small context object (conn, store, as_of, cfg, members, groups). Thresholds only from `cfg.gates`. Specifics:
   - G4: compare `close_raw` at `as_of` with the previous month-end's for the same securities; share equal < 5%.
   - G6: iterate contracts with `unit in (frac, x, inr)` that have a live column (dividend yield derived from `dividend_rate_inr/close_raw`, D/E from statements, `trailing_pe`, `inst_held_frac`, `market_cap_inr`, `total_assets`); NULL the violators' values in a working copy (do not mutate stored rows), record events per violator, block when violators per field > `G6_max_violators`.
   - G8: coverage per active factor from WS05's registry statuses and `factor_values` of THIS as_of if already computed; when called before factors (normal order) evaluate coverage of each active factor's `inputs` instead (documented approximation); excluded factors listed in the report; block if ≥ 3.
   - G9: if a previous passed run exists: recompute last month's `scores.input_hash` for the champion from stored `factor_values` and compare; mismatch → BLOCK; skipped (INFO) if WS06 not present.
   - G10: shuffle smoke if WS07 present and last month's 1M labels matured; else INFO skip.
   - W3: `near_constant_share` on every active factor's raw values (last computed month); ≥ 0.80 → WARN with the value; 3 consecutive months → proposal hint (WS09 reads events).
   - W6: PSI of each active-factor input field vs the pooled previous 3 months; > 0.25 → WARN; ≥ 3 fields → BLOCK.
5. `run`: execute all; write `dq_runs` rows; events for failures; `dq_status`; update `runs.dq_status`; raise `Blocked` listing failing gates.
6. Command prints the gate table verbatim with PASS/WARN/FAIL and exits 0/1/2.
7. Tests; PROGRESS; commit `WS04: contracts and gates`.

## 9. Tests you must write
```
tests/unit/test_contracts.py::test_yaml_has_every_spec_field
tests/unit/test_contracts.py::test_check_field_counts_nulls_and_ranges
tests/unit/test_contracts.py::test_psi_zero_for_identical_large_for_shifted
tests/unit/test_gates.py::test_g1_blocks_below_480_rows
tests/unit/test_gates.py::test_g3_blocks_at_97_9_pct_price_coverage
tests/unit/test_gates.py::test_g4_blocks_when_prices_duplicate_previous_cohort      (the 06-12/06-14 case)
tests/unit/test_gates.py::test_g6_dividend_yield_349pct_blocks                       (the legacy bug: inject dividend_rate = 349*close/100 ... ensure > 0.25 caught)
tests/unit/test_gates.py::test_g6_five_violators_warn_six_block
tests/unit/test_gates.py::test_g7_unclassified_over_1pct_blocks
tests/unit/test_gates.py::test_g8_excludes_low_coverage_factor_and_blocks_at_three
tests/unit/test_gates.py::test_w3_near_constant_factor_warns                          (85% at one value)
tests/unit/test_gates.py::test_w6_psi_drift_warn_then_block_at_three_fields
tests/unit/test_gates.py::test_blocked_run_records_dq_runs_events_and_status
tests/unit/test_gates.py::test_passed_with_warnings_status
tests/unit/test_gates.py::test_blocked_run_cannot_replace_passed_run_without_force   (via RunContext + gates)
```

## 10. Verification checklist
- `python -m quant gates run --as-of <synthetic as_of> --db /tmp/w/quant.db` prints the full table; exit 0.
- Inject a 349% yield into a copy of the synthetic DB (`update security_attributes set dividend_rate_inr = close*3.49 ...`) → exit 2, `data_quality_events` has `G6` BLOCK rows, `runs.dq_status='blocked'`.
- `select gate, passed, blocking from dq_runs where run_id = <last>` lists all 16 gates.
- `python -m pytest tests/unit/test_contracts.py tests/unit/test_gates.py -q` green.

## 11. Definition of done
- [ ] All 16 gates implemented with thresholds from config only
- [ ] Blocked path writes rows, status, raises `Blocked`, exit 2
- [ ] Contracts YAML complete and mirrored
- [ ] PROGRESS; commit

## 12. Handoff notes
- Every workstream records events through `record_event`; do not write `data_quality_events` directly.
- WS11 passes `--override-gate Gx --decision-id D` which sets `strict=False` for that gate only after WS09 confirms the decision is Tier-2 approved.
- G9 and G10 become active automatically once WS06/WS07 exist; leave the INFO-skip path in place.

## 13. Risks, gotchas
- Do not mutate stored values when NULLing violators; gates read, they never write data tables.
- PSI on fields with heavy ties (e.g. dividend rate 0) needs the epsilon floor; test it.
- A gate that reads `factor_values` of the current month before factors run must not crash; return the approximation and say so in `detail`.
