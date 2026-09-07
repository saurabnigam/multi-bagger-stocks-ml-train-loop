# WS11 — Integration, UI and acceptance

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 9.1,10,11; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0 and 9.1,10,11.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: WS00, WS01, WS03, WS02, WS04, WS05, WS06, WS07, WS08, WS09, WS10. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS11.01 — Monthly orchestration and recovery

**Owns:** `quant/run.py`, `quant/commands/run.py`.
**Test:** `tests/integration/test_ws11_01.py`. **Contract:** C11.
**Fixed examples:** `pit_cutoff`, `execution`, `evaluation_revisions` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Test MASTER_SPEC9.1 step order: settle/mature/evaluate before fit; lock and publication precheck; coldstart BLOCKED no scores; pre-cutoff capture allows later publication; crash before publish leaves no cohort; retry and concurrent start cannot duplicate; failed scores still produce earlier labels/report.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/integration/test_ws11_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/integration/test_ws11_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS11.01: <concrete behavior>`.

### WS11.02 — Offline UI and evidence export

**Owns:** `quant/ui_export.py`, `ui/index.html`, `ui/app.js`, `ui/style.css`, `ui/vendor/chart.umd.js`, `ui/vendor/VERSION`.
**Test:** `tests/integration/test_ws11_02.py`. **Contract:** C11.
**Fixed examples:** `hac_insufficient`, `revision_tracks` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Eight tabs work offline with empty/blocked/live/legacy states; output no numeric inference without uncertainty status; reject missing band labeled estimable; show generation/exec dates and pending orders; no console errors or font/CDN requests.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/integration/test_ws11_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/integration/test_ws11_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS11.02: <concrete behavior>`.

### WS11.03 — Verification, scheduling script and owner documentation

**Owns:** `quant/verify.py`, `quant/status.py`, `scripts/signoff.sh`, `monthly_cron.sh`, `README.md`, `AGENTS.md`.
**Test:** `tests/integration/test_ws11_03.py`. **Contract:** C11.
**Fixed examples:** `governance` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Sign-off prints engineering/operational/longitudinal separately with PASS/FAIL/DEFERRED; real checks on isolated DB and frozen source; persisted report reproduces old evidence even after revisions; cron script only, no schedule installed; root docs distinguish unchanged legacy invariants from V2; no automatic pushes.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/integration/test_ws11_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/integration/test_ws11_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS11.03: <concrete behavior>`.

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
