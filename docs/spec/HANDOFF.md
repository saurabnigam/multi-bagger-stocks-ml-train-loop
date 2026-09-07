# Handoff for Gemini Flash or another coding LLM — revision 2

Open a coding assistant with shell/file access at this repository root. This is a complete build specification, not a benchmark of any particular Gemini version. It requires Python3.11+, the dependencies in requirements.txt, Git, and opt-in network access for recorded source checks. No Codex-only skills or subagent framework are required.

The correction record is [REVISION_2_REVIEW.md](REVISION_2_REVIEW.md). It maps the review gaps to the revised contracts and implementation tests, and distinguishes this pass's completed checks from pending V2 work.

First verify the documents, without installing V2:

```bash
python3 docs/spec/check_spec.py
```

Use an existing verified Python environment or create one with `python3 -m venv venv` and `venv/bin/python -m pip install -r requirements.txt`, then activate it. Record actual versions; freeze an exact dependency lock after the initial successful suite. Paths are repository-relative; no private scratch interpreter is required.

## 1. Copy this build prompt into the coding assistant

```text
Implement the complete V2 Quant Engine in this repository from specification revision 2.

Read AGENTS.md, docs/spec/MASTER_SPEC.md, docs/spec/INTERFACES.md,
docs/spec/TEST_AND_VERIFICATION_PLAN.md, subagents/README.md,
subagents/_workstreams.json and subagents/PROGRESS.md before coding.
The master owns behavior; contracts/schema.sql, price_schema.sql and config.toml
own exact DDL/config; INTERFACES.md owns signatures; golden_cases.json owns fixed
synthetic answers. Historical drafts are context only.

Run python3 docs/spec/check_spec.py first. Inspect Git branch/status/log and preserve
unrelated changes. Create codex/v2-implementation from the current HEAD if absent,
or inspect and resume that branch if present. Never reset user work. Run the existing
offline tests and record baseline results. Treat legacy modules and quant_engine.db
as read-only; V2 changes the new package only. The root legacy invariants do not
require adding their retired filters to V2; MASTER_SPEC defines V2 policy separately.

Implement all 43 task IDs from subagents/_workstreams.json in order:
WS00, WS01, WS03, WS02, WS04, WS05, WS06, WS07, WS08, WS09, WS10, WS11.
Use one bounded task at a time. An agent-capable harness may use a fresh worker per
task, passing its exact document, contract blocks and prerequisite evidence; otherwise
execute sequentially yourself. No speculative parallel workstreams.

For each task: write the listed acceptance tests against production APIs, observe
the initial failure, implement the owned behavior, run its exact verification command,
then scripts/check.sh. Append actual results and next task to PROGRESS.md and commit
as WSxx.nn: concrete behavior. Before check.sh exists, run the current offline pytest
suite and the spec checker. Continue through all tasks without asking approval for
routine implementation choices already covered by the spec. Do not push.

Never weaken an oracle, omit a failing test, implement a fake passing stub, backdate
captures/trades, impute missing fundamentals, overwrite published evidence, or record
an agent decision as human. Use the canonical append/revision APIs. Respect throttle,
track separation, actual timestamps and the documented policy defaults.

If normative sources conflict, record a minimal reproducer and block the affected
task rather than inventing a policy. Finish independent authorized work. Do not
change the model rules or acceptance expectations to obtain a favorable backtest.

At completion run scripts/signoff.sh --phase engineering. Run operational checks
when their actual prerequisites exist, and report deferred calendar/source/history
checks separately. A first monthly run needs captures before its cutoff; do not
manufacture a September2026 live cohort or three months of PIT history. Software
completion does not require a positive market IC or demonstrated trading skill.

Produce docs/spec/SIGNOFF_YYYY-MM-DD.md with task coverage, actual test counts and
runtimes, engineering/operational/longitudinal results, source archives, real-run
summary if available, deviations, deferred prerequisites and owner actions. Keep
working until all available engineering acceptance passes or a concrete unresolved
contract blocks it. Leave all changes committed locally; do not push.
```

The short instruction to share with another LLM is:

```text
Read docs/spec/HANDOFF.md and execute section 1, completing every task in the revision-2 manifest and reporting phased sign-off.
```

## 2. Resume prompt

```text
Resume the V2 implementation using docs/spec/HANDOFF.md section 1 and specification
revision 2. Re-read the master authority/policies, canonical interfaces, verification
plan, task manifest and PROGRESS.md. Inspect git status, diff and recent commits;
reconcile recorded task completion with actual code and test evidence. Preserve
uncommitted work. Run the spec checker and current offline suite, diagnose regressions,
then continue the first genuinely incomplete task. Record any change in the documents'
spec fingerprint. Re-run affected task checks if contracts changed. Do not push or
turn deferred live-data/history checks into passes.
```

## 3. Independent verification prompt

```text
Audit the V2 implementation against revision-2 MASTER_SPEC, INTERFACES, canonical
SQL/config and TEST_AND_VERIFICATION_PLAN. Run scripts/signoff.sh for the phases whose
prerequisites exist. Inspect code as well as test output, including failure/retry,
actor authority, historical revisions and actual execution times. All adversarial
mutations must use isolated database/repository copies. Do not weaken checks or alter
live data. Report each mismatch with severity, reproduction and task owner. Repairs
may be made only within the already specified behavior, with regression evidence
recorded in PROGRESS.md. Produce SIGNOFF_YYYY-MM-DD.md distinguishing engineering
completion, operational readiness, deferred longitudinal checks and research outcomes.
No positive-performance requirement and no unmeasured success claims.
```

## 4. Owner operation after engineering acceptance

Use a real capture before the next intended scoring cutoff:

```bash
python -m quant db init
python -m quant data capture
```

Initial bootstrap registration is recorded by the fixed spec-authorized system seed;
subsequent policy/model changes require actual decisions. Capture again near the last
session if needed for complete current price windows. In the following month:

```bash
python -m quant run monthly --commit
python -m quant status
```

Read the generated report. Missing pre-cutoff captures means BOOTSTRAP_REQUIRED; target
a later month. Resolve source issues through evidence-backed proposals/decisions and
revision APIs. Do not repeatedly retry a gate that needs unavailable historical data.
Paper orders remain pending until actual execution closes exist; use portfolio settle
or the next monthly run. Review each proposal individually; approve and reject are
separate commands, not the shell expression approve|reject.

For an actual owner decision, replace the example ID/name and supply a substantive note:

```bash
python -m quant kb approve P-EXAMPLE --actor-kind human --by human:OWNER --note "Evidence and decision rationale"
python -m quant kb apply --as-of YYYY-MM-DD
```

Only a human operator/explicit recorded human authorization may use human identity.
Review git status, ledger verification and intended changes before committing decisions
and pushing. Installing cron or pushing is not implicit in engineering sign-off.

## 5. Scheduled-agent operation

An agent runs the monthly loop, reads the report, and drafts evidence-based proposal
reviews using its actual identity. If authorized to perform Tier1 decisions, use
`--actor-kind llm --by llm:gemini-flash`; all required criteria must be true and the
result is provisional. Leave Tier2 proposals for the owner, and surface ratification
due within 60 days. Never type human mode for the owner, invent a name/signature, approve
a placeholder, or change thresholds to unblock a run. Apply only authorized effects.
Push only when the owner explicitly enabled it for the scheduled operation.

## 6. What the owner should expect

| Milestone | Earliest possible outcome assuming complete monthly captures |
|---|---|
| Before first live cutoff | Fixed bootstrap registration, raw captures and honest empty/blocked UI |
| First clean live cohort | Ranking, source proof, pending paper orders, descriptive backfill and flagged legacy data |
| Month4 | First realized3M IC; statistical uncertainty may still be unavailable |
| Month13 | First realized12M IC; insufficient history for reliable inference |
| Month15 | Earliest12 matured3M cohorts; weight gate may open, but EW can remain optimal under the rule |
| Month24 onward | Larger evidence set; no guaranteed statistical significance or promotion |
| Month37 | First36M doubling cohort |

Read counts and prerequisites, not calendar promises. A stable report with no eligible
changes can be correct operation. A negative research result is also a legitimate result.
