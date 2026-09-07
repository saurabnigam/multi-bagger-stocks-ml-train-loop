# WS09 — Bootstrap, hypotheses, governance and reports

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 5.4,9; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0 and 5.4,9.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: WS06, WS07, WS08. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS09.01 — Recorded bootstrap and hypothesis budget

**Owns:** `quant/knowledge/bootstrap.py`, `quant/knowledge/registry.py`.
**Test:** `tests/unit/test_ws09_01.py`. **Contract:** C09.
**Fixed examples:** `governance` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Seed only exact launch definitions with system/spec-hash decision, idempotently; no fake human or Tier2 approval; annual seventh and family fourth registration refused; launch exemption does not exclude hypotheses from reported trial counts; first cutoff is after registration.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws09_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws09_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS09.01: <concrete behavior>`.

### WS09.02 — Criteria and fixed review opportunities

**Owns:** `quant/knowledge/review.py`.
**Test:** `tests/unit/test_ws09_02.py`. **Contract:** C09.
**Fixed examples:** `negative_direction`, `hac_insufficient`, `promotion_budget` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Decision vectors test each criterion independently; positive oriented IC is good for both raw directions; use the single look-adjusted t_crit and cumulative trial count; factor looks are 12/24/36 and model looks 24/36/48, consumed once even when ancillary criteria fail; unavailable cost/ablation is unmet; model review consumes the same paired interval.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws09_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws09_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS09.02: <concrete behavior>`.

### WS09.03 — Approval, ratification and prospective changes

**Owns:** `quant/knowledge/proposals.py`, `quant/knowledge/adr.py`, `quant/commands/kb.py`.
**Test:** `tests/unit/test_ws09_03.py`. **Contract:** C09.
**Fixed examples:** `governance`, `evaluation_revisions` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: LLM actor plus human prefix refused; Tier2 refused; Tier1 criteria all true yields provisional; human ratifies; expiry appends reversion and prospective model version; failed reversion blocks next publication; original scores unchanged; ADR exists for every applied decision.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws09_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws09_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS09.03: <concrete behavior>`.

### WS09.04 — Reports and knowledge mirrors

**Owns:** `quant/knowledge/report.py`, `quant/knowledge/lessons.py`, `knowledge/README.md`.
**Test:** `tests/unit/test_ws09_04.py`. **Contract:** C09.
**Fixed examples:** `hac_insufficient`, `evaluation_revisions` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Render persisted evidence only; separate tracks, originally-published vs revised view, actual cutoff/generated times and pending orders; preserve content-addressed report manifests with exact references and frozen control summaries; reproduce an old report after later revisions; finite band required only when status says estimable; include ratifications and phase-specific acceptance, no unmeasured performance claims.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws09_04.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws09_04.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS09.04: <concrete behavior>`.

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
