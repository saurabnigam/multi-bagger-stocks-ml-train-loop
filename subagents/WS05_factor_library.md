# WS05 — Factor inputs, standardisation and library

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 3.4,5; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0 and 3.4,5.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: WS04. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS05.01 — Restricted FactorInputs

**Owns:** `quant/factors/base.py`, `quant/factors/inputs.py`.
**Test:** `tests/unit/test_ws05_01.py`. **Contract:** C05.
**Fixed examples:** `pit_cutoff`, `holdings` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Physically filtered inputs expose no connection/client/labels; future or undeclared fields raise LookaheadError; each result records exact observation refs; old as_of gets same data after a future revision.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws05_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws05_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS05.01: <concrete behavior>`.

### WS05.02 — Centered bounded ranks

**Owns:** `quant/factors/standardise.py`.
**Test:** `tests/unit/test_ws05_02.py`. **Contract:** C05.
**Fixed examples:** `rank_ties`, `constant_rank`, `negative_direction` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Match every golden z within 1e-12; tied values stay tied; constant group is NaN; mean0 and abs<=3 for finite groups; do not assert unit variance; negative direction is applied once.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws05_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws05_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS05.02: <concrete behavior>`.

### WS05.03 — Price factors and diagnostics

**Owns:** `quant/factors/momentum.py`, `quant/factors/low_risk.py`, `quant/factors/controls.py`, `quant/factors/legacy.py`.
**Test:** `tests/unit/test_ws05_03.py`. **Contract:** C05.
**Fixed examples:** `split`, `source_basis`, `planted_rank` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Geometric TRI fixes momentum endpoints;253 bars required for 252 returns; sample shortage produces flags; beta uses aligned index returns; dc_flag is diagnostic only; size reads captured mcap and is not backfillable.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws05_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws05_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS05.03: <concrete behavior>`.

### WS05.04 — Fundamental and flow factors

**Owns:** `quant/factors/quality.py`, `quant/factors/value.py`, `quant/factors/growth.py`, `quant/factors/flows.py`.
**Test:** `tests/unit/test_ws05_04.py`. **Contract:** C05.
**Fixed examples:** `annual_quarterly`, `holdings`, `negative_direction` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: All launch formulas match MASTER_SPEC5.3; nonfinancial exclusions, nonpositive denominators, missing quarters and fiscal alignment are tested; low accruals gets higher oriented z; no default growth or ROE.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws05_04.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws05_04.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS05.04: <concrete behavior>`.

### WS05.05 — Registry, provenance and sector features

**Owns:** `quant/factors/registry.py`, `quant/factors/sector.py`, `quant/commands/factors.py`.
**Test:** `tests/unit/test_ws05_05.py`. **Contract:** C05.
**Fixed examples:** `pit_cutoff`, `constant_rank` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Sync rejects changed code/helper hashes without affected version bumps; registered future hypotheses stay out of earlier cohorts; controls use valid lifecycle shadow; compute returns staging rows only; sector features use frozen groups.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws05_05.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws05_05.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS05.05: <concrete behavior>`.

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
