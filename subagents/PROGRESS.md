# Implementation progress log

Append one section per workstream (format in `subagents/README.md` → Progress protocol). Never rewrite earlier sections.

## Baseline — 2026-09-06 — spec author
Built: docs/spec/MASTER_SPEC.md, subagents/*.md, docs/spec/TEST_AND_VERIFICATION_PLAN.md, docs/spec/HANDOFF.md
Tests: `pytest -q` (legacy suite) -> 58 passed
Notes: no V2 code exists yet. WS00 starts from an empty `quant/` package.

## Specification revision 2 — 2026-09-07 — Codex

State: documentation correction complete; all 43 V2 implementation tasks pending.
Base commit: `435b0c0a9875e59c094df1e349953865e0f1f0a4` on `v2-engine-spec`.
Spec fingerprint: `3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f`.

Files: master, canonical interfaces, SQL/config contracts, 23 synthetic examples, frozen legacy inventory, executable spec checker, verification plan, handoff, correction record, all twelve workstream docs and the 43-task manifest. Historical design drafts remain non-normative.

Acceptance: `python3 docs/spec/check_spec.py` -> exit 0; 10 specification check groups PASS, 45 state tables and 4 cache tables checked; dependencies and task/document references validated. This check does not execute a V2 implementation.

Regression: the existing dependency environment's Python 3.14.3 ran `-m pytest -q` -> exit 0; 58 passed in 5.47 seconds. The default system Python lacked pytest, so the pre-existing environment was used; no dependency installation was needed. `git diff --check` -> exit 0. Frozen legacy file hashes and the 2543/12/4773 source row counts match the recorded inventory.

Review: see `docs/spec/REVISION_2_REVIEW.md` for gap-to-contract/test mapping and limitations. New checks include annual/Q4 coexistence, nullable-key rejection, immutable revisions, track separation, separate entry/exit order identities, integer weights, tied ranks, sign orientation, corporate-action arithmetic, HAC, maturity and promotion-budget examples.

Deferred: production APIs, all task acceptance suites, recorded/live adapters, operational runs, longitudinal replay and any actual Gemini Flash execution. No V2 engineering completion or predictive-performance claim is made. Legacy source/database files remain unchanged.

Next: hand the revision-2 HANDOFF section 1 prompt to the implementing assistant. Begin at WS00.01 after checking this spec fingerprint and the unchanged legacy baseline. No implementation task should be marked complete based on this documentation entry.

## WS00.01 — Configuration and common types — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: fa5e5c471fedc7ebe467f001fb3cba32158a1bef
Files: config/quant.toml, quant/__init__.py, quant/config.py, quant/types.py, quant/errors.py, tests/unit/test_ws00_01.py
Acceptance: `python -m pytest tests/unit/test_ws00_01.py -q` -> exit 0, 7 passed in 0.05 seconds
Regression: `python3 docs/spec/check_spec.py && python -m pytest -q` -> exit 0, 10 spec check groups PASS, 65 passed in 1.95s
Contract cases: pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS00.02

## WS00.02 — Canonical schemas and protected writes — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 5fca6f809985ba3b490f20bbd5c7f8fb258756e6
Files: quant/db/__init__.py, quant/db/core.py, quant/db/schema.sql, quant/db/price_schema.sql, tests/unit/test_ws00_02.py
Acceptance: `python -m pytest tests/unit/test_ws00_02.py -q` -> exit 0, 5 passed in 0.54 seconds
Regression: `python3 docs/spec/check_spec.py && python -m pytest -q` -> exit 0, 10 spec check groups PASS, 70 passed in 5.02s
Contract cases: annual_quarterly, evaluation_revisions, revision_tracks
Deviations: none
Deferred real checks: none
Next: WS00.03

## WS00.03 — Trading calendar and execution clock — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: b5daea6e4b470bf7817eb48624128f133488f72c
Files: quant/data/__init__.py, quant/data/calendar.py, tests/unit/test_ws00_03.py
Acceptance: `python -m pytest tests/unit/test_ws00_03.py -q` -> exit 0, 3 passed in 0.47 seconds
Regression: `python3 docs/spec/check_spec.py && python -m pytest -q` -> exit 0, 10 spec check groups PASS, 73 passed in 1.18s
Contract cases: calendar_lags, execution, cohort_maturity, pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS00.04

## WS00.04 — Run lifecycle and ledger recovery — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 8e37e71dbbc82226292e105e4685ff86cb32cfd5
Files: quant/run.py, quant/db/ledger.py, tests/unit/test_ws00_04.py
Acceptance: `python -m pytest tests/unit/test_ws00_04.py -q` -> exit 0, 3 passed in 0.61 seconds
Regression: `python3 docs/spec/check_spec.py && python -m pytest -q` -> exit 0, 10 spec check groups PASS, 76 passed in 1.44s
Contract cases: evaluation_revisions
Deviations: none
Deferred real checks: none
Next: WS00.05

## WS00.05 — Offline fixtures, CLI and check runner — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: f151eb996503c5ea65d21a24d2719a9dbd445c5c
Files: pytest.ini, tests/conftest.py, tests/synthetic.py, tests/helpers.py, quant/cli.py, quant/__main__.py, scripts/check.sh, tests/unit/test_ws00_05.py
Acceptance: `python -m pytest tests/unit/test_ws00_05.py -q` -> exit 0, 4 passed in 0.31 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 80 passed in 1.60s
Contract cases: planted_rank, split_dividend
Deviations: none
Deferred real checks: none
Next: WS01.01

## WS01.01 — Constituent parsing and capture archive — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 291b032d8858e3ce685121b14a2c91823ae3b951
Files: quant/data/universe.py, quant/commands/__init__.py, quant/commands/universe.py, tests/unit/test_ws01_01.py
Acceptance: `python -m pytest tests/unit/test_ws01_01.py -q` -> exit 0, 4 passed in 0.26 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 84 passed in 1.78s
Contract cases: pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS01.02

## WS01.02 — Identity and tracking to every maturity — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 8197d0315264b3834fca2fec2cc5e25287cba59b
Files: quant/data/identity.py, tests/unit/test_ws01_02.py
Acceptance: `python -m pytest tests/unit/test_ws01_02.py -q` -> exit 0, 3 passed in 0.17 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 87 passed in 1.76s
Contract cases: cohort_maturity
Deviations: none
Deferred real checks: none
Next: WS01.03

## WS01.03 — Sector rules and point-in-time groups — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: da227351631ff2c68f76378e90e7552ea647f12e
Files: quant/sectors/__init__.py, quant/sectors/taxonomy.py, quant/sectors/crosswalk.py, config/sector_groups_v1.csv, config/yahoo_crosswalk_v1.csv, tests/unit/test_ws01_03.py
Acceptance: `python -m pytest tests/unit/test_ws01_03.py -q` -> exit 0, 6 passed in 0.12 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 93 passed in 4.62s
Contract cases: pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS03.01

## WS03.01 — Throttled client, archive and units — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 00c8e285a852a32c2534f3b73361005fa7571ec8
Files: quant/data/yahoo.py, tests/unit/test_ws03_01.py
Acceptance: `python -m pytest tests/unit/test_ws03_01.py -q` -> exit 0, 4 passed in 2.84 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 97 passed in 1.76s
Contract cases: source_basis, cost
Deviations: none
Deferred real checks: none
Next: WS03.02

## WS03.02 — Bitemporal statements and TTM — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 7a8ff9c9e53bf68dbf392dd758ca93bf34268e3d
Files: quant/data/fundamentals.py, tests/unit/test_ws03_02.py
Acceptance: `python -m pytest tests/unit/test_ws03_02.py -q` -> exit 0, 5 passed in 0.53 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 102 passed in 2.34s
Contract cases: annual_quarterly, pit_cutoff, calendar_lags
Deviations: none
Deferred real checks: none
Next: WS03.03

## WS03.03 — Holdings, attributes and capture command — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 04a432a1bb43b8be92df2709e3e1104e76c72e34
Files: quant/data/holdings.py, quant/data/attributes.py, quant/data/capture.py, quant/commands/data.py, tests/unit/test_ws03_03.py
Acceptance: `python -m pytest tests/unit/test_ws03_03.py -q` -> exit 0, 5 passed in 0.76 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 107 passed in 2.46s
Contract cases: holdings, pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS02.01

## WS02.01 — Price schema, basis normalization and TRI — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 05b348149e6fbc77dcf95eb774a3f6834b07fe41
Files: quant/data/prices.py, tests/unit/test_ws02_01.py
Acceptance: `python -m pytest tests/unit/test_ws02_01.py -q` -> exit 0, 6 passed in 0.46 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 113 passed in 3.03s
Contract cases: source_basis, split, dividend, split_dividend
Deviations: none
Deferred real checks: none
Next: WS02.02

## WS02.02 — Reconciliation, quarantine and decisions — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 17de7a1c0dcf2b92fae3f191ae32d3ca4a1c5d90
Files: quant/data/actions.py, quant/data/prices.py, quant/commands/prices.py, tests/unit/test_ws02_02.py
Acceptance: `python -m pytest tests/unit/test_ws02_02.py -q` -> exit 0, 4 passed in 0.63 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 117 passed in 3.40s
Contract cases: evaluation_revisions, split
Deviations: none
Deferred real checks: none
Next: WS02.03

## WS02.03 — Monthly panels, benchmark aggregates and manifest recovery — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 9ad5fa3f80c61c77ce8276fdbfa2db7dfa30554c
Files: quant/data/benchmarks.py, quant/data/prices.py, tests/unit/test_ws02_03.py
Acceptance: `python -m pytest tests/unit/test_ws02_03.py -q` -> exit 0, 4 passed in 0.59 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 121 passed in 4.60s
Contract cases: pit_cutoff, cohort_maturity, source_basis
Deviations: none
Deferred real checks: none
Next: WS04.01

## WS04.01 — Field bounds and drift — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: f65626b1cb1e5b15be81fe8817a3791a85cb992b
Files: quant/data/contracts.py, config/field_contracts_v1.json, tests/unit/test_ws04_01.py
Acceptance: `python -m pytest tests/unit/test_ws04_01.py -q` -> exit 0, 7 passed in 0.03 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 128 passed in 3.07s
Contract cases: pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS04.02

## WS04.02 — Pre-computation gates — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 05ed00f915fa258416d649d0ca3ba302caae7a5e
Files: quant/data/gates.py, tests/unit/test_ws04_02.py
Acceptance: `python -m pytest tests/unit/test_ws04_02.py -q` -> exit 0, 6 passed in 0.36 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 134 passed in 3.70s
Contract cases: pit_cutoff
Deviations: none
Deferred real checks: none
Next: WS04.03

## WS04.03 — Post-compute coverage and replay callbacks — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: b4fe770d10b7596ae3eb4b14d3c3332c94314c24
Files: quant/data/gates.py, tests/unit/test_ws04_03.py
Acceptance: `python -m pytest tests/unit/test_ws04_03.py -q` -> exit 0, 6 passed in 0.45 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 140 passed in 3.35s
Contract cases: constant_rank, rank_ties
Deviations: none
Deferred real checks: none
Next: WS05.01

## WS05.01 — Restricted FactorInputs — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 168dda7e704172f8832a8cb4200424566378e9f5
Files: quant/factors/__init__.py, quant/factors/base.py, quant/factors/inputs.py, tests/unit/test_ws05_01.py
Acceptance: `python -m pytest tests/unit/test_ws05_01.py -q` -> exit 0, 4 passed in 0.48 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 144 passed in 3.46s
Contract cases: pit_cutoff, holdings
Deviations: none
Deferred real checks: none
Next: WS05.02

## WS05.02 — Centered bounded ranks — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: be40c2ee5d57b2fe5fc373bb2621cb47eb79ca4f
Files: quant/factors/standardise.py, tests/unit/test_ws05_02.py
Acceptance: `python -m pytest tests/unit/test_ws05_02.py -q` -> exit 0, 4 passed in 0.58 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 148 passed in 4.96s
Contract cases: rank_ties, constant_rank, negative_direction
Deviations: none
Deferred real checks: none
## WS05.03 — Price factors and diagnostics — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: be8adc7ae9a9ceb82860eeea90ff24c135770681
Files: quant/factors/inputs.py, quant/factors/momentum.py, quant/factors/low_risk.py, quant/factors/controls.py, quant/factors/legacy.py, tests/unit/test_ws05_03.py
Acceptance: `python -m pytest tests/unit/test_ws05_03.py -q` -> exit 0, 4 passed in 0.52 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 152 passed in 6.89s
Contract cases: split, source_basis, planted_rank
Deviations: none
Deferred real checks: none
Next: WS05.04

## WS05.04 — Fundamental and flow factors — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: ebfb7a7f45cbb6b0769aa94142f314c1fa0bfa90
Files: quant/data/fundamentals.py, quant/factors/inputs.py, quant/factors/standardise.py, quant/factors/quality.py, quant/factors/value.py, quant/factors/growth.py, quant/factors/flows.py, tests/unit/test_ws05_04.py
Acceptance: `python -m pytest tests/unit/test_ws05_04.py -q` -> exit 0, 4 passed in 1.18 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 156 passed in 3.78s
Contract cases: annual_quarterly, holdings, negative_direction
Deviations: none
Deferred real checks: none
Next: WS05.05

## WS05.05 — Registry, provenance and sector features — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 7a58ef5805225c56092d527efe8ed4da591a743d
Files: quant/cli.py, quant/config.py, quant/factors/registry.py, quant/factors/sector.py, quant/commands/factors.py, tests/unit/test_ws05_05.py
Acceptance: `python -m pytest tests/unit/test_ws05_05.py -q` -> exit 0, 5 passed in 1.01 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 161 passed in 5.64s
Contract cases: pit_cutoff, constant_rank
Deviations: none
Deferred real checks: none
Next: WS06.01

## WS06.01 — Exact integer family weights — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: f2e769dc24c4dac4e44e66ce6fe06fcc322e4ac5
Files: quant/model/__init__.py, quant/model/learn.py, tests/unit/test_ws06_01.py
Acceptance: `python -m pytest tests/unit/test_ws06_01.py -q` -> exit 0, 7 passed in 0.11 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 168 passed in 5.13s
Contract cases: weight_fit, equal_weights
Deviations: none
Deferred real checks: none
Next: WS06.02

## WS06.02 — Hierarchical and flat composites, coverage and screens — 2026-09-07 — Gemini 3.8 Flash
Spec: revision 2; 3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f
State: complete
Base commit: 543425cba2553d16af09c99481a22ac5df6e4284
Files: quant/model/composite.py, quant/model/screens.py, tests/unit/test_ws06_02.py
Acceptance: `python -m pytest tests/unit/test_ws06_02.py -q` -> exit 0, 5 passed in 0.72 seconds
Regression: `scripts/check.sh` -> exit 0, 10 spec check groups PASS, 173 passed in 4.71s
Contract cases: negative_direction, rank_ties
Deviations: none
Deferred real checks: none
Next: WS06.03
