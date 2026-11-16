# V2 implementation workstreams — revision 2

This folder is the execution plan for another coding LLM. There are twelve workstreams and 43 individually accepted tasks in `_workstreams.json`; each workstream document contains owned files, contract IDs, fixed cases, verification commands and completion checkboxes. These are task specifications, not running subagents or proof of implementation.

## Read order and authority

Read root AGENTS.md for legacy protection, MASTER_SPEC.md section 0 for V2 authority, INTERFACES.md for exact APIs, TEST_AND_VERIFICATION_PLAN.md for acceptance, then PROGRESS.md and the current task document. All paths below are relative to repository root. No Codex-specific skills/plugins are needed to execute this plan.

Canonical contracts live only in docs/spec/INTERFACES.md and docs/spec/contracts/. If a task example contradicts them, record the contradiction and stop that affected task; do not improvise a second interface. Historical design drafts and old progress entries are non-normative.

## Build order

`WS00 -> WS01 -> WS03 -> WS02 -> WS04 -> WS05 -> WS06 -> WS07 -> WS08 -> WS09 -> WS10 -> WS11`

YahooClient precedes its price-store consumer. Model fitting is pure and accepts supplied history; evaluation reads models, not vice versa. Governance owns review and is implemented before migration writes its factual ADRs. Defaults are sequential, including on tools with subagents. Parallel execution is optional only after proving completed prerequisites and disjoint ownership; this revision prescribes no concurrent workstreams.

| Workstream | Deliverable | Tasks |
|---|---|---|
| WS00 | types/config, schemas, calendar, state journal, offline harness |5|
| WS01 | captured membership, stable identity, sector groups |3|
| WS03 | throttled source adapter, PIT fundamentals, holdings/attributes |3|
| WS02 | price basis, TRI, revisions, benchmarks/recovery |3|
| WS04 | field contracts and staged gates |3|
| WS05 | restricted inputs, ranks, factors, registry |5|
| WS06 | integer weights, composites/screens, model staging |3|
| WS07 | revisioned labels, statistics, causal evaluations/curves/backfill |5|
| WS08 | costs/constraints, pending fills/NAV, scoreboard |3|
| WS09 | bootstrap/budget, criteria, approval/reversion, reports |4|
| WS10 | legacy source/sample, migration, reconciliation |3|
| WS11 | transactional runner, offline UI, phased sign-off/docs |3|

## Execution and handoff protocol

Before starting, run `git status --short`, `git branch --show-current`, `git log -5 --oneline` and `python3 docs/spec/check_spec.py`. Preserve unrelated user changes. Create codex/v2-implementation only if it does not exist; otherwise inspect and resume it. Never reset or force-checkout a branch with uncommitted changes. Select the first task not proven complete by both progress evidence and code/tests.

For each task:

1. Load its referenced master sections and contract blocks plus prerequisite handoff notes.
2. Write the specified production-API acceptance tests; observe initial failure. Production code must calculate results, not return oracle constants.
3. Implement the owned behavior and test refusals/missing inputs. No fake approvals, stub-success paths or fabricated source responses.
4. Run the task command, then scripts/check.sh; before that script exists, run the current offline pytest suite and the specification checker.
5. Record results, self-review the diff against the contract, and commit a coherent task change with `WSxx.nn: concrete behavior`. Do not push. Continue to the next task without a new approval request for routine authorized work.

An optional independent reviewer can run the same task's tests and inspect the specification comparison before the next task. The implementing model's own tests are not independent scientific validation; the golden examples and phased acceptance are the external contract.

Foundation ordering: WS00.01 may create the minimal shared test helpers it needs (cfg, clock and spec_case). Extend them as providers land; WS00.05 consolidates the full fixture harness. WS00.02 tests the core write helper with an explicit minimal test context rather than importing the future RunContext; WS00.04 replaces it with the real context in integration tests. Shared tests/conftest.py, tests/helpers.py and package __init__.py files are permitted supporting files for their first consumer. Production providers must never import a future consumer merely to supply a type annotation or a test fixture. Sequential downstream fixes may touch a prerequisite file when necessary; record its owner and rerun both task suites.

## Progress protocol

Append events; do not rewrite earlier claimed results. Record partial tasks before context interruption. Current truth is the newest entry plus Git/tests, not the first historical completion label.

```text
## WSxx.nn — title — UTC date — actual model/actor
Spec: revision 2; spec fingerprint
State: in_progress | complete | blocked
Base commit: existing SHA before this task
Files: task-owned changes
Acceptance: exact command -> observed exit code, test count and seconds
Regression: exact command -> observed result
Contract cases: case IDs exercised
Deviations: none, or exact conflict and repro; never silently approved
Deferred real checks: prerequisite and expected earliest opportunity
Next: task ID and any unfinished working-tree state
```

Do not insert the SHA of the commit containing this entry into itself. Record that commit's SHA in the next task entry or use `git log` to resolve it. Keep long logs under ignored local logs plus concise checked-in evidence; do not paste sensitive source credentials.

## Stop and recovery rules

Stop affected work for a contradictory contract, destructive action outside scope, absent required owner authority, or unavailable real-data prerequisite. Finish independent engineering work that does not depend on it. Never mark a missing provider as passed. Network/calendar/history prerequisites are DEFERRED only in the appropriate acceptance phase. Test failures must be diagnosed, not resolved by weakening the assertion or adding an unconditional skip.

Published historical rows cannot be overwritten. Resume a failed prepublication attempt using staging; retry a published date is a no-op. Legacy source files and quant_engine.db are never modified or moved. All operator/agent approval rules are in MASTER_SPEC9.3 and HANDOFF.md.
