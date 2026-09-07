# WS08 — Paper portfolios, costs and scoreboard

Specification revision 2. This is an implementation plan for another LLM, including Gemini Flash; no platform-specific skill is required. Build only after prerequisites pass.

## 1. Mission and scope

Implement the tasks below in order. The complete behavior is MASTER_SPEC sections 2.4,8; exact signatures and shapes are in INTERFACES.md. This workstream produces code; it does not authorize changes to investment policy or historical records.

## 2. Read first

1. `docs/spec/MASTER_SPEC.md` sections 0 and 2.4,8.
2. `docs/spec/INTERFACES.md` contract blocks listed per task.
3. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and the referenced golden cases.
4. `subagents/PROGRESS.md` and `subagents/_workstreams.json`; verify Git rather than trusting a completion label.

## 3. Dependencies and boundaries

Prerequisites: WS06, WS07. Default execution is sequential. Temporary dependency fixtures are test-only; production may not silently fall back to them. Readiness of a provider is established by its acceptance tests, not existence of a module.

## 4. Shared constraints

Preserve legacy files/database byte-for-byte. No new runtime dependencies. Paths through Config. Capture time never backdated. Published facts append-only; differences need defined revisions. Every source read for a factor passes through FactorInputs. Keep real acquisition throttled; tests use fake transport/clock/sleep.

## 5. Task procedure

For each task, write the named acceptance tests using the fixed cases or declared isolated fixtures; run the command and observe the expected initial failure before implementation. Implement the listed public contracts and only needed internal helpers. Re-run, then run the regression command. Do not weaken a case or invent a successful real observation. Record actual evidence and commit only task-owned changes.

## 6. Interfaces

The contract IDs below resolve to `docs/spec/INTERFACES.md`. They define exact parameters and result columns. Do not copy a competing signature into this file. Any unresolved contradiction blocks that task and is recorded with the smallest reproducer.

## 7. Tasks and acceptance

### WS08.01 — Construction and cost arithmetic

**Owns:** `quant/portfolio/costs.py`, `quant/portfolio/construct.py`.
**Test:** `tests/unit/test_ws08_01.py`. **Contract:** C08.
**Fixed examples:** `cost` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Bucket boundaries are inclusive as specified; rank45 retained/rank61 removed; sector cap skips entrants; bucketC <=.02 and infeasible residual goes to cash; ties byID; .10 weight B trade costs.00037.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws08_01.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws08_01.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS08.01: <concrete behavior>`.

### WS08.02 — Pending orders, dated fills and NAV

**Owns:** `quant/portfolio/paper.py`, `quant/commands/portfolio.py`.
**Test:** `tests/unit/test_ws08_02.py`. **Contract:** C08.
**Fixed examples:** `execution`, `split_dividend`, `cost` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Orders cannot fill before actual eligible close; repeated settlement adds no fill; NAV spans actual event dates with no pre-inception returns; factor/model cohort TOP_Q20 and MATCHED_EW books include entry and exit costs, remain pending when prices are missing and provide frozen selection evidence.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws08_02.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws08_02.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS08.02: <concrete behavior>`.

### WS08.03 — Matched benchmarks, spreads and scoreboard

**Owns:** `quant/portfolio/scoreboard.py`.
**Test:** `tests/unit/test_ws08_03.py`. **Contract:** C08.
**Fixed examples:** `cost`, `hac_insufficient` in `docs/spec/contracts/golden_cases.json`; examples are test inputs, never market results.

Required observations: Compare net with net using identical universe/execution; matched EW minus itself is0; cost events inside full interval are summed; no simulated quintile book means unavailable net spread; no forced TOP30-turnover-below-DEC10 inequality; IR absent below 24 portfolio months.

- [ ] Write tests for every required observation above, including refusal/missing-data branches; reuse the shared isolated fixtures specified in the verification plan.
- [ ] Run `python -m pytest tests/unit/test_ws08_03.py -q`; record the initial failure caused by absent behavior.
- [ ] Implement the contract in the owned files. Do not embed a fake oracle response in production.
- [ ] Run `python -m pytest tests/unit/test_ws08_03.py -q`; every task test must pass.
- [ ] Run `scripts/check.sh` (before WS00.05 exists, run `python -m pytest -q`); legacy tests remain collected and green.
- [ ] Append task result, exact command/output, spec revision, file list, unresolved issues and next task to PROGRESS.md; commit with `WS08.03: <concrete behavior>`.

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
