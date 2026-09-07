# WS07 — Labels, statistics, evaluation and curves

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 2.3,7; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0 and 2.3,7.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: WS02, WS05, WS06. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS07.01 — Cohort labels and appended corrections

**Owns:** `quant/evaluation/labels.py`.
**Test:** `tests/unit/test_ws07_01.py`. **Contract:** C07.
**Fixed examples:** `revision_tracks`, `evaluation_revisions`, `split_dividend` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Original cohort member count survives index dropout; no row before endpoint; missing quote is not automatically delisted; action resolution appends group-wide affected revisions; old rows unchanged; live/backfill same-date labels remain separate.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws07_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws07_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS07.01: <concrete behavior>`.

### WS07.02 — Statistical functions and oriented metrics

**Owns:** `quant/evaluation/stats.py`, `quant/evaluation/metrics.py`.
**Test:** `tests/unit/test_ws07_02.py`. **Contract:** C07.
**Fixed examples:** `hac`, `hac_insufficient`, `negative_direction`, `planted_rank` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Match hand-computed HAC gamma0/gamma1/se; short/constant vectors return unavailable uncertainty; oriented low-is-good signal IC+1; constant Spearman is None; paired tests use same cohorts; positive/negative raw signs are not applied twice.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws07_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws07_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS07.02: <concrete behavior>`.

### WS07.03 — Revision selection and causal training history

**Owns:** `quant/evaluation/evaluate.py`, `quant/evaluation/walkforward.py`, `quant/commands/evaluate.py`.
**Test:** `tests/unit/test_ws07_03.py`. **Contract:** C07.
**Fixed examples:** `evaluation_revisions`, `pit_cutoff` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Monthly window keys use empty strings; unchanged evaluation inserts0; revised evidence appends with supersedes; select exactly one latest-known revision before status filtering; no future endpoint, later revision or backfill enters family fitting; do not compress calendar gaps for HAC.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws07_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws07_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS07.03: <concrete behavior>`.

### WS07.04 — Leakage suite and stored-history curves

**Owns:** `quant/evaluation/leakage.py`, `quant/evaluation/curves.py`.
**Test:** `tests/unit/test_ws07_04.py`. **Contract:** C07.
**Fixed examples:** `planted_rank`, `pit_cutoff`, `negative_direction` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Run T1-T10 from the test plan with explicit prerequisites; deterministic planted rank matches exactIC; moving publication dates cannot bypass capture time; no production edits during injections; learning curves read original stored weights; missing bands display unavailable.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws07_04.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws07_04.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS07.04: <concrete behavior>`.

### WS07.05 — Backfill replay and warmup accounting

**Owns:** `quant/evaluation/backfill.py`.
**Test:** `tests/unit/test_ws07_05.py`. **Contract:** C07.
**Fixed examples:** `revision_tracks`, `cohort_maturity` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Replay only price/volume factors with warmup; no current mcap or statements; record requested/actual date counts and survivorship caveat; negative measured backfill is accepted as an outcome, not adjusted to force positivity.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws07_05.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws07_05.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS07.05: <concrete behavior>`.

## 8. Workstream verification

Run every task command above plus `scripts/check.sh`. Compare provider signatures against INTERFACES.md and schema/config copies against their canonical files. Integration that depends on future work is not marked passed; the exact dependency is recorded and WS11 acceptance exercises it.

## 9. Definition of done

- [ ] All task behavior tests pass offline; observed runtime and count recorded.
- [ ] No new dependency, legacy modification or unowned policy change.
- [ ] Public signatures match the canonical contracts.
- [ ] No production test doubles/placeholder approvals or fabricated historical observations.
- [ ] Progress identifies every completed task and any deferred real-data check.

## 10. Downstream handoff

Provider tests and canonical contracts are the downstream guarantees. A green import with missing behavior is not completion. Preserve the isolated reproduction for any defect fixed during integration, and re-run the affected provider and consumer task suites.
