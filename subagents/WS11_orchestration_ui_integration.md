# WS11 — Orchestrator, backfill replay, UI export and dashboard, cron, docs, end-to-end acceptance and sign-off

## 1. Mission
Wire everything into one command that runs the monthly loop in the spec's order, stops on a blocked gate, is resumable and idempotent; export the dashboard payloads and build the vanilla UI tabs; move the legacy scripts aside unchanged; update AGENTS.md and README to describe V2 truthfully; run the first real month; and produce the sign-off report against `docs/spec/TEST_AND_VERIFICATION_PLAN.md`.

## 2. Read first
1. MASTER_SPEC §9.1 (loop), §10.3 (CLI and expected output), §10.5 (cron), §10.8 (UI), §11 (month-1 acceptance), §1.3 (checklist to display)
2. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` (you execute it), `docs/spec/HANDOFF.md` (the "verify everything" prompt is yours to satisfy)
3. Every other workstream's §12 handoff notes; `ui/index.html`, `ui/app.js`, `ui/style.css`, `update_ui_v16.py` (current UI to extend)

## 3. Scope
In: `quant.run.monthly` (steps 1–12; `--skip-fundamentals`, `--stop-after`, `--override-gate`, `--push`, `--dry-run`, `--force`), `run backfill-track` wrapper, `status`, `ui_export.py` + five data files, UI tabs and vendored Chart.js, `monthly_cron.sh`, moving legacy scripts to `legacy/` with a README, AGENTS.md/README.md rewrite for V2 (prime directives updated: no multipliers; invariants I1–I6; paths), `scripts/signoff.sh`, `tests/integration/test_run_monthly.py`, `tests/integration/test_e2e_synthetic.py`, the first real run and its report.
Out: new statistics or rules (everything you display comes from tables).

## 4. Dependencies
All other workstreams merged and green. Verify `python -m pytest -q` passes and `scripts/check.sh` passes.

## 5. Interfaces you consume
Everything in `subagents/README.md`'s contract table, by name.

## 6. Interfaces you provide
```python
# quant/run.py (orchestrator half)
def monthly(as_of: str, cfg, skip_fundamentals=False, stop_after: str | None = None, override: tuple[str, str] | None = None, push=False, dry_run=False, force=False) -> int
    # steps 1–12 of MASTER_SPEC §9.1 in order inside one RunContext(kind='monthly') ; Blocked after step 4 -> still runs steps 7–8 (labels, evaluate) and 12 (report) then exit 2 ;
    # each step prints its summary line ; idempotent (replace_as_of) ; refuses to replace a passed run without force ; git commit at the end unless dry_run ; push only with push
def backfill_track(cfg, start='2016-01-31', end=None) -> int
def status(cfg) -> dict                                                              # last run, dq, open proposals, ratifications due, months to next label maturity, repo size
# quant/ui_export.py
def export(conn, cfg) -> list[Path]                                                  # ui/data.js (ranking + per-stock), ui/data_learning.js, ui/data_scoreboard.js, ui/data_factors.js, ui/data_kb.js ; every IC record must carry n_eff and ci; raise if not
```

## 7. Deliverables
`quant/run.py` (extended), `quant/commands/run.py` (`run monthly`, `run backfill-track`, `status`, `ui export`, `verify report`), `quant/ui_export.py`, `ui/index.html` (tabs: Ranking, Learning, Scoreboard, Factors, Sectors, Data, Knowledge, Legacy), `ui/app.js` split into `ui/js/{ranking,learning,scoreboard,factors,sectors,data,knowledge,legacy}.js`, `ui/style.css` (extend), `ui/vendor/chart.umd.js` (pinned version noted in `ui/vendor/VERSION`), `monthly_cron.sh`, `legacy/` (moved scripts + `legacy/README.md`; their tests moved to `legacy/tests/` and still collected), `AGENTS.md` (rewritten for V2; keep the prime directives that survive: 0.5 s throttle, DB in git, no frontend build step; replace the multiplier/weight-bound invariants with I1–I6 and the tier rules), `README.md` (status paragraph: what is measured, what is a target), `scripts/signoff.sh`, `tests/integration/test_run_monthly.py`, `tests/integration/test_e2e_synthetic.py`, `knowledge/reports/<first-month>.md` (real), PROGRESS entry with the sign-off table.

## 8. Implementation plan
1. `monthly`: call, in order: `universe.snapshot` → `prices.update` + `benchmarks.update` + `build_monthly_panel` → `fundamentals.ingest` (+holdings, attributes; skipped with `--skip-fundamentals`) → `gates.run` → `factors.compute_all` (+`sector.compute_features`) → `models.score_all` → `labels.mature` → `evaluate.run` + `curves.update` + `leakage.run_all` → `paper.rebalance_all` + roll-forward → `review_all` → `proposals.draft` → `report.render` + `lessons` + `ui_export.export` + `ledger.export` + `VACUUM` + git commit. Print MASTER_SPEC §10.3's line format per step. `--stop-after` names a step; `--override-gate Gx --decision-id D` verifies via WS09 that D is an approved Tier-2 `gate_override` for Gx and as_of, else `Refused`.
2. Blocked path: catch `Blocked` after gates; run labels/evaluate/report; exit 2; `runs.status='blocked'`.
3. `backfill_track`: `walkforward.replay_backfill` then `curves.update`; prints the survivorship caveat; writes `knowledge/reports/backfill_<date>.md` via `report.render_backfill_block`.
4. `ui_export`: build the five JS constants from tables per MASTER_SPEC §10.8; per-stock explain text generated from factor z's and family scores (no legacy "FATAL MULTIPLIER" prose); optional DCF explainer computed from stored inputs and labelled "not used in ranking"; validation: refuse IC records without `n_eff` and band; payload size printed.
5. UI: extend `index.html` with tabs; each tab module renders from its constant; Chart.js vendored; footer with as_of/run_id/git sha and the "n_eff < 6" sentence; Legacy tab lists the four snapshots with defect badges.
6. `legacy/` move with `git mv`; fix imports in the moved tests only by adding `legacy/` to `sys.path` in `legacy/tests/conftest.py`; `pytest` still collects them; `quant.migrate.legacy` and `quant.model.learn` import `legacy.weight_optimizer.project_weights` after the move (update WS06/WS10 import paths and note in PROGRESS).
7. AGENTS.md rewrite: mission, V2 architecture map, DDL pointer, invariants I1–I6, tiers, playbooks (`run monthly`, `kb approve/apply`, `verify report`, adding a factor), edge cases carried over (yfinance units, splits, ISIN), "what the evidence shows" pointing to the latest report rather than quoting numbers. README status paragraph.
8. `scripts/signoff.sh`: runs `pytest -q`, `db verify`, `verify leakage --as-of <last>`, `verify pit --months 3`, `verify report --as-of <last>`, `model check`, `kb` ADR check, repo size; prints the sign-off table from `TEST_AND_VERIFICATION_PLAN.md` §7 with PASS/FAIL.
9. First real run: `python -m quant run monthly --as-of 2026-09-30` (or 10-30 per Q3); commit; paste the summary lines into PROGRESS.
10. Tests; PROGRESS (with the sign-off table); commit `WS11: orchestration, UI, integration`.

## 9. Tests you must write
```
tests/integration/test_run_monthly.py::test_full_loop_on_synthetic_world_exit_0      < 60 s offline ; all tables populated ; report exists ; ui files exist
tests/integration/test_run_monthly.py::test_blocked_gate_path_exit_2_and_partial_outputs   inject the 349% yield -> exit 2 ; labels/evaluations still updated ; report says blocked
tests/integration/test_run_monthly.py::test_idempotent_rerun_same_month_no_new_rows
tests/integration/test_run_monthly.py::test_stop_after_and_skip_fundamentals
tests/integration/test_run_monthly.py::test_override_requires_tier2_decision
tests/integration/test_run_monthly.py::test_passed_run_not_replaced_without_force
tests/integration/test_e2e_synthetic.py::test_three_month_loop_knowledge_state      run months 46,47,48 ; assert: 3 runs ok ; labels matured for earlier months ; evidence_curve rows ; learning points with ew_oos_ic ; at least one proposal for the planted factor by month 48 ; approve (human) + apply -> factor active from month 49 ; ADR exists ; report verdict line present
tests/integration/test_e2e_synthetic.py::test_ui_export_refuses_ic_without_band
tests/integration/test_e2e_synthetic.py::test_backfill_track_labels_everything_backfill
tests/integration/test_e2e_synthetic.py::test_legacy_tests_still_collected_and_green   58 legacy tests pass from legacy/tests
```

## 10. Verification checklist (this IS the sign-off; paste outputs into PROGRESS.md)
- `python -m pytest -q` → all green (target ≥ 150 tests) in < 90 s.
- `python -m quant run monthly --as-of <synthetic m48> --db /tmp/w/quant.db` → exit 0; every `[n step]` line printed; `knowledge/reports/<m48>.md` exists.
- Real: `python -m quant run monthly --as-of 2026-09-30` → status ok in < 60 min; `knowledge/reports/2026-09.md` (or 2026-10) committed; `ui/data*.js` written; `quant.db` < 60 MB; `git status` clean of `*.sqlite`.
- `python -m quant run backfill-track` → ≥ 120 3M points for `mom_12_1`; the report's backfill block shows `mom_12_1 12M IC cum > 0` (falsifier) with the survivorship caveat.
- `python -m quant db migrate-legacy` done and `QUANT_LEGACY_REAL=1 pytest tests/integration/test_migrate_legacy.py -q` green.
- `python -m quant verify report --as-of <real as_of>` → `diff: empty`; `python -m quant verify leakage` → T1..T10 PASS; `python -m quant verify pit --months 3` → identical.
- `python -m quant model check` → ok; `python -m quant kb queue` → proposals listed (may be 0); ADR check → ok.
- Open `ui/index.html` in a browser: eight tabs render; Learning shows the backfill panel dashed and four hollow legacy points; no console errors; footer sentence present.
- `scripts/signoff.sh` prints the table with all PASS.

## 11. Definition of done
- [ ] `run monthly` implements §9.1 with blocked/resume/idempotent semantics and the documented output
- [ ] UI tabs and export rule; Chart.js vendored; legacy prose gone
- [ ] Legacy scripts moved unchanged; their tests green; AGENTS.md/README rewritten truthfully
- [ ] First real run committed; backfill track and migration done on the real DB
- [ ] Sign-off script green; PROGRESS.md sign-off table; commit

## 12. Handoff notes (to the owner)
- Monthly ritual: `python -m quant run monthly` (or cron) → read `knowledge/reports/YYYY-MM.md` → `kb approve|reject` → `kb apply` → commit. Under 30 minutes of human time.
- Nothing about weights will move until month 15; that is by design and printed in every report.

## 13. Risks, gotchas
- Do not let `ui_export` compute any statistic; it reads tables.
- Do not "tidy" the legacy scripts while moving them; the migration test imports them.
- Cron on a laptop: the run must survive being started twice (the second exits immediately).
- The first real run will surface real data surprises (429s, a demerger, a missing statement); handle them through events and proposals, not by editing stored rows.
