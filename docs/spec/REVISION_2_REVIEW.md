# Revision 2 — implementation-readiness corrections

Updated 2026-09-07. Baseline commit: `435b0c0a9875e59c094df1e349953865e0f1f0a4`.

The first handoff described the intended engine but left contradictions that a coding model would have had to resolve itself. Revision 2 supplies one set of contracts, smaller tasks, fixed examples and staged acceptance. The corrections below are documented requirements; production behavior remains to be implemented and tested. No Gemini Flash implementation run has been performed.

## What changed and where to verify it

| Gap | Revision-2 resolution | Implementation verification |
|---|---|---|
| Next-month collection could not satisfy the earlier live cutoff | Real capture timestamps, pre-cutoff bootstrap and explicit BOOTSTRAP_REQUIRED state | WS03; WS11; P1/P5 |
| Paper fills could precede creation of the ranking | Next verified session after actual generation date; pending orders and idempotent settlement | WS00.03; WS08.02; execution case |
| Annual and quarterly statements could collide | Frequency belongs to the fundamental primary key | WS00.02; WS03.02; annual_quarterly case |
| Nullable evaluation window keys permitted duplicates | Non-null natural keys, evidence hashes, explicit revisions | WS00.02; WS07.03; evaluation_revisions case |
| Overwrite/recompute language contradicted immutable evidence | Append-only protected facts, staging, prospective definitions and revised views | WS00.02/04; WS07; P5-P7 |
| Live/backfill labels could share an identity despite different members | Cohort and track keys, frozen membership/groups, complete affected-group revisions | WS07.01; revision_tracks case |
| Factor direction could be applied twice | Standardization applies direction once; decisive IC uses oriented values | WS05.01; WS09.02; negative_direction case |
| Rounded weight example failed exact normalization | Integer units, capped-simplex projection and deterministic remainder allocation | WS06.01; weight_fit/equal_weights cases |
| Tied ranks were required to have impossible variance properties | Centered, bounded, tie-preserving transform; constant groups are missing | WS05.01; rank_ties/constant_rank cases |
| Statistical tests included guessed expected values | Hand-calculated HAC, deterministic planted ranks, undefined-result statuses | WS07.02; hac/planted_rank cases |
| Model review timing and multiple-look thresholds disagreed | One formula; cumulative trial count; factor and model look schedules; consume each look once | WS09.02; promotion_budget case |
| Positive backfill and future real history were required for build success | Separate specification, engineering, operational and longitudinal results | Verification plan sections 1/8; WS11.03 |
| Source re-download was treated as guaranteed reproduction | Retained source vintages, adjustment metadata and manifest mismatch failure | WS02.01/03; P3/P12 |
| Reports could silently change after data correction | Content-addressed reports, pinned evidence manifests and frozen control summaries | WS09.04; WS11.03 |
| Scheduled agents were told to identify as human | Explicit actor kinds, provisional Tier1, real human Tier2, no placeholder approvals | WS09.03; HANDOFF sections 4/5 |
| Migration depended on governance that was not built yet | Governance precedes migration; factual system annotations replace fake policy approvals | WS09; WS10 |
| Family, evaluation and price interfaces had dependency conflicts | Single API document; supplied learning history; injected gate callbacks; corrected order | INTERFACES C00-C11; task manifest |
| Factor promotion lacked a source of actual net-selection evidence | Dedicated subject/cohort TOP_Q20 and matched EW paper books, including entry/exit costs | WS08.02; WS09.02 |
| First-run fixture dependencies preceded their providers | Incremental foundation helpers and explicit test contexts; no future production imports | subagents README; WS00 |
| Month-count examples promised evidence too early | First 3M label at month 4, first 36M label at month 37; missing inputs defer eligibility | HANDOFF section 6; cohort_maturity case |

Additional clarified cases include eight quarters for earnings momentum, four admissible holdings captures, unchanged-but-fresh universe files, source price/volume adjustment basis, model-specific coverage, actual post-compute gates, late approval/expiry handling and preserving frozen legacy files in place.

## Package map

- `MASTER_SPEC.md`: behavior, formulas, time, storage, governance and scope.
- `INTERFACES.md`: public signatures, return shapes and dependency boundaries.
- `contracts/schema.sql` and `price_schema.sql`: executable canonical database definitions.
- `contracts/config.toml`: canonical configuration with repository-relative paths.
- `contracts/golden_cases.json`: 23 synthetic examples; production must calculate their answers.
- `contracts/legacy_inventory.json`: source hashes and row counts to protect the legacy engine.
- `TEST_AND_VERIFICATION_PLAN.md`: isolation, adverse cases, actual commands and phased sign-off.
- `subagents/`: 12 workstreams and 43 tasks, with files, API references, acceptance and commands.
- `HANDOFF.md`: complete build, resume, audit, owner and scheduled-agent prompts.
- `check_spec.py`: specification-only checks available before V2 exists.

## Evidence from this documentation pass

| Check | Observed result | Limit |
|---|---|---|
| `python3 docs/spec/check_spec.py` | 10 check groups PASS | Executes canonical SQL and selected arithmetic examples; validates configuration, task/document references and frozen legacy inventory. It is not the V2 test suite. |
| Legacy pytest suite, using the existing dependency environment | 58 passed in 5.47 seconds | Confirms existing tests; does not validate unimplemented V2 behavior. The default system interpreter had no pytest. |
| `git diff --check` | PASS | Whitespace/error-marker check only. |
| Frozen source inventory | All recorded hashes and counts match | Legacy data and listed source/test files were not changed. |

The state schema currently has 45 tables and the separate price schema has 4; these are observations of the checked DDL, not an alternative table-count contract. All 43 implementation tasks are pending. The current specification fingerprint is recorded in the latest PROGRESS entry and can be regenerated with check_spec.py.

The checker deliberately does not certify every prose requirement, every provider field mapping, all 23 cases through production APIs, or the eventual quality of the model's implementation. Those require the task tests and phased sign-off. No source fetch, new live cohort, broker action or production database mutation was performed during this correction pass.

## External contract checks

SQLite treats NULL values as distinct for UNIQUE constraints, which is why revision 2 removes nullable evaluation-window key fields. See the official [CREATE TABLE documentation](https://www.sqlite.org/lang_createtable.html).

Yahoo history handling includes adjustment/repair behavior that needs a recorded adapter contract; the spec therefore requires explicit column bases and retained fixtures instead of relying only on an auto_adjust flag. References: the [yfinance price-repair documentation](https://ranaroussi.github.io/yfinance/advanced/price_repair.html) and [history implementation](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py). These references do not replace validation against the installed package and actual archived responses.

## Next action

Give Gemini Flash shell/file access to this repository and paste the full build prompt from HANDOFF section 1. It starts at WS00.01, uses the fixed order, records evidence after each task, commits locally and does not push. A framework with subagents is optional; the same bounded tasks can run sequentially. Engineering acceptance can finish before enough real history exists for operational and longitudinal acceptance.
