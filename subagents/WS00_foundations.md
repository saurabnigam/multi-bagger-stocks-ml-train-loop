# WS00 — Foundations

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 0,2,4.1,4.2,10; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0, 2, 4.1, 4.2 and 10.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: none; start from the existing legacy repository. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS00.01 — Configuration and common types

**Owns:** `quant/config.py`, `quant/types.py`, `quant/errors.py`, `config/quant.toml`.
**Test:** `tests/unit/test_ws00_01.py`. **Contract:** C00.
**Fixed examples:** `pit_cutoff` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Every canonical TOML key loads; relative paths resolve from repository root; test directory overrides do not alter policy hash; FrozenClock preserves UTC microseconds.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws00_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws00_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS00.01: <concrete behavior>`.

### WS00.02 — Canonical schemas and protected writes

**Owns:** `quant/db/schema.sql`, `quant/db/price_schema.sql`, `quant/db/core.py`.
**Test:** `tests/unit/test_ws00_02.py`. **Contract:** C00.
**Fixed examples:** `annual_quarterly`, `evaluation_revisions`, `revision_tracks` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Annual and Q4 coexist; same-key same-value append is a no-op; same-key different-value raises ImmutableConflict; UPDATE and DELETE of protected rows fail; both databases enable foreign keys.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws00_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws00_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS00.02: <concrete behavior>`.

### WS00.03 — Trading calendar and execution clock

**Owns:** `quant/data/calendar.py`.
**Test:** `tests/unit/test_ws00_03.py`. **Contract:** C01.
**Fixed examples:** `calendar_lags`, `execution`, `cohort_maturity` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Weekend resolution uses supplied sessions; next execution is strictly after generation date; missing session range is refused; no network or implicit clock in Calendar. Future publication dates are refused by the runner in WS11.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws00_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws00_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS00.03: <concrete behavior>`.

### WS00.04 — Run lifecycle and ledger recovery

**Owns:** `quant/run.py`, `quant/db/ledger.py`.
**Test:** `tests/unit/test_ws00_04.py`. **Contract:** C00.
**Fixed examples:** `evaluation_revisions` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: One failed attempt leaves no partial cohort; run status/error survives; journal inserts and control updates replay in sequence; changed before hash fails replay; restored tables and ledger_events match source.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws00_04.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws00_04.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS00.04: <concrete behavior>`.

### WS00.05 — Offline fixtures, CLI and check runner

**Owns:** `tests/conftest.py`, `tests/synthetic.py`, `tests/helpers.py`, `quant/cli.py`, `quant/__main__.py`, `quant/__init__.py`, `scripts/check.sh`, `pytest.ini`.
**Test:** `tests/unit/test_ws00_05.py`. **Contract:** C00.
**Fixed examples:** `planted_rank`, `split_dividend` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: All external transport is disabled by default; fixture seed 0 repeats byte-identically; source/event fixtures use explicit known_at; CLI help and error codes work; default collection includes the two unchanged legacy test modules.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws00_05.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws00_05.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS00.05: <concrete behavior>`.

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
