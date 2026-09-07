# V2 test and verification plan — revision 2

This document defines acceptance; it does not report implementation results. Use the Python interpreter from the project environment (`python` below). The specification-only checker uses stdlib Python3.11+ and is runnable before V2 exists.

## 1. Four distinct acceptance stages

| Stage | What it establishes | When available | Result |
|---|---|---|---|
| Specification | SQL/config parse, regression examples agree, task graph and references are consistent | Now: `python3 docs/spec/check_spec.py` | PASS/FAIL; limited to the checks actually implemented |
| Engineering | Production implementation satisfies deterministic behavior, offline integration and recovery | After tasks are implemented | PASS/FAIL; no missing module counted as DEFERRED |
| Operational | The source adapters and bootstrap/migration/monthly commands work on retained or live data | With network, archives and a pre-cutoff capture | PASS/FAIL/DEFERRED with prerequisite |
| Longitudinal | Replay across real months and empirical evidence can be assessed | As real cohorts mature | PASS/FAIL/DEFERRED; no artificial history |

Engineering completion may precede the first operational live cohort. A final report must distinguish engineering complete from operationally ready and from any evidence of predictive skill. A nonpositive empirical IC is a valid measured outcome, not a failed software test. Missing internet/market date does not authorize fabricating a successful source call.

## 2. Test layout and isolation

Every task in subagents/_workstreams.json has its own pytest file and command. Organize shared cross-cutting tests under tests/property, tests/leakage and tests/integration. The original test_quant_math.py and test_optimizer.py remain unchanged at repository root and are collected by default. Test count is measured; no target count substitutes for coverage.

`python -m pytest -q` is offline by default and targets <90s after the full build. Mark network checks `network`, full-source migration `legacy_real`, and long performance runs `performance`; each is skipped unless its explicit opt-in flag/environment variable is set. Use pytest.ini registered markers. `QUANT_NETWORK=1`, `QUANT_LEGACY_REAL=1`, `QUANT_PERFORMANCE=1` enable the corresponding classes. An enabled test with unmet data preconditions must report the exact missing condition, not quietly pass.

WS00.05 provides fixtures:

- `cfg`: copied canonical config with all mutable paths under tmp_path; tests may lower universe/gate thresholds only in this explicitly synthetic config. Record the override; do not change production defaults to make a60-name fixture pass480-member gates.
- `clock`: FrozenClock initialized by each test; no real sleep or current-time dependence.
- `ctx`: RunContext over temporary state and price stores, actual isolated run metadata and synthetic actor. Never points at root quant_engine.db.
- `spec_case(name)`: loads the named object from docs/spec/contracts/golden_cases.json. Tests feed its inputs into PRODUCTION functions and compare output. Never import the spec checker as the implementation under test.
- `tests.helpers.seed_minimal_state(conn)`: deterministic security/model/hypothesis/run rows satisfying canonical foreign keys. Fake decisions are explicitly synthetic test data; never copied to real DBs.
- `tests.synthetic.make_world(tmp_path, *, seed=0, months=48) -> World`: writes the controlled world below. It does not call Yahoo, migrate the real DB, register human approvals or invoke Git.
- `fake_client`: records accessor/batch invocations, requested dates, retries and virtual sleep; returns fixed recorded/synthetic payloads.

Disable network in the default suite by patching the project's transport boundary and raising if any unmocked request escapes; also guard requests/urllib and yfinance in a session autouse fixture. Git effects are injected/faked in unit tests; integration Git tests use a newly initialized temporary repository. Tests deliberately editing/deleting databases always act on copies.

## 3. Fixed acceptance examples

The JSON case file has literal inputs and expected answers. It is not a source of mocked production outputs. The following tests illustrate the required comparison style; cfg and spec_case are fixtures above.

```python
import numpy as np
import pandas as pd
from quant.model.learn import fit_family_weights
from quant.factors.standardise import transform
from quant.evaluation.metrics import rank_ic
from quant.evaluation.stats import hac_mean_test

def test_exact_weight_example(cfg, spec_case):
    c = spec_case('weight_fit')
    history = pd.DataFrame([c['mean_ic']] * c['n_months'], columns=c['families'])
    units, diagnostics = fit_family_weights(history, cfg)
    assert [units[k] for k in c['families']] == c['expected_units']
    assert sum(units.values()) == 10000
    assert abs(diagnostics['alpha'] - c['expected_alpha']) < 1e-12

def test_ties_do_not_require_unit_variance(cfg, spec_case):
    c = spec_case('rank_ties')
    result = transform(pd.Series(c['raw']), pd.Series(c['groups']), c['direction'], cfg)
    np.testing.assert_allclose(result['z'], c['expected_z'], atol=1e-12, rtol=0)
    assert abs(result['z'].mean()) < 1e-12

def test_negative_direction_is_applied_once(cfg, spec_case):
    c = spec_case('negative_direction')
    z = transform(pd.Series(c['raw']), pd.Series(['A'] * 5), c['direction'], cfg)['z']
    ic, n, status = rank_ic(z, pd.Series(c['labels']))
    assert n == 5 and status == 'ok'
    assert abs(ic - c['expected_oriented_ic']) < 1e-12
    # The promotion evaluator consumes ic itself, never ic * direction again.

def test_hac_matches_hand_calculation(spec_case):
    c = spec_case('hac')
    result = hac_mean_test(c['values'], c['lag'])
    assert abs(result.mean - c['expected_mean']) < 1e-12
    assert abs(result.se - c['expected_se']) < 1e-12
    assert abs(result.t - c['expected_t']) < 1e-12
```

Add production-API tests for every named case, not just these four. SQL contract tests execute the copied production schema: A100 and Q25 for the same security/field/period/fetch coexist; duplicate evaluation natural key/evidence is a no-op through append_rows; changing evidence adds revision 2; source revision 1 stays byte-identical. Direct SQL UPDATE/DELETE on protected rows raises IntegrityError. Live and backfill labels at the same date have separate cohort IDs. Replaying a ledger must preserve their identities.

The HAC fixture is hand-calculable: mean.05, gamma0.0125, gamma1-.009375, Bartlett S.003125 at lag 1. The weights example allocates10000 integer units. These replace contradictory rounded-vector and guessed-variance expectations from revision 1.

## 4. Synthetic world

Use60 securities, IDs1..60, six groups of 10, monthly endpoints from January2022 for 48 months; session calendar is weekdays with close10:00UTC and explicitly no holidays. Start quoted prices at 1000+10*security_id. RNG is numpy.random.default_rng(seed); generate market normals(sd.003), group normals(sd.003), individual normals(sd.008) in that order over sorted dates/IDs. Economic daily log return is .0001 + market + group + individual. Seed and generation order are fixed once committed.

Inject security5 split6:1 on2023-06-15, security7 cash dividend100 on2023-09-20, security9 group change at 2024-07-31, security11 missing Assets for FY2024, security13 confirmed delisting2025-03-15. Generate quoted prices recursively as previous_close*exp(economic_return)/split_ratio - dividend_raw; positive starting capital avoids negative prices in the committed seed. This preserves economic TR across the injected corporate actions. Volume is a deterministic positive array spanning A-D liquidity buckets; specify it explicitly in the fixture source and archive fixture checksum before consumers use it.

Fundamentals include five annual periods and at least eight consecutive quarterly income periods, with actual capture times before the scoring cutoff; use available_from=max(estimated date,capture timestamp). Holdings contain four-month windows before flow eligibility. Missing quarters and cold-start worlds are separate fixture variants. Membership contains security13 through its delisting; earlier cohorts continue tracking it to their longest horizon. All registered definitions and synthetic bootstrap precede eligible cohort cutoffs.

The world tests plumbing, state and determinism. Do not require a random path to yield IC exactly.10, a promotion by month 48, or universally lower buffered turnover. Exact planted signal uses golden planted_rank; promotion criteria use constructed evidence rows with independently specified values around each threshold. A separate optional randomized power study reports measured recovery without changing code to achieve a target sample statistic.

## 5. Required invariants and adversarial checks

| ID | Test | Required observation |
|---|---|---|
| P1 | Capture boundary | future observation/attribute/membership/version cannot enter a prior cutoff |
| P2 | Weight accounting | integer sum10000, bounds, deterministic EW allocation, no double direction |
| P3 | Action invariance | split/dividend/joint golden cases preserve TR; source basis conversion preserves price*volume |
| P4 | Standardisation | ties/NaNs/constants handled; centered and bounded, no unit-variance assertion |
| P5 | Publication atomicity | failure before commit creates no partial cohort/factor/score batch |
| P6 | Idempotency | repeated successful monthly run adds no business rows/run; retry after failure may add attempts/events |
| P7 | Historical preservation | corrected labels/evaluations append; old values and decision refs unchanged |
| P8 | Track separation | same-date different membership/group tracks cannot share labels |
| P9 | Actual execution | no fill before next session after generated date; missing close leaves pending |
| P10 | Authority | LLM human-prefix/Tier2 attempts refused; overdue provisional state reverts prospectively |
| P11 | Causal learning | no future endpoint/later-known revision; weights from original cohort, not hindsight refit |
| P12 | Recovery | ledger restores state plus control changes; missing source archive is disclosed; revised vendor download fails old manifest |

Leakage checks T1-T10 follow MASTER_SPEC7.5. Test their failure paths using controlled mutations of temporary copies. T1 aggregates200 seeded permutations within groups; define Monte Carlo SE from permutation means and require its aggregate mean to lie within max(.005,5*MCSE) of zero. A single permutation exceeding2SE is not a software failure. T2 uses deterministic known rank labels and separately verifies a factor cannot access them through production inputs. T6 cannot bypass fetched_at by modifying available_from. T10 uses a balanced synthetic null, not an assumption about every realized market sample.

Important additional cases: same-day annual/Q4 ingestion; stale unchanged universe CSV; missing current versus missing past data; sparse growth quarters; split-source download whose adjustment horizon exceeds requested end; changed code dependency without version bump; non-active factor leakage; MOM_ONLY coverage exception; bucketC2% cap/cash fallback; no-position/no-label report; future as_of refusal; unknown actor; new hypothesis beyond annual/family cap; concurrent monthly calls; crash immediately before and after publication commit; write attempts against the read-only legacy source.

## 6. End-to-end engineering scenario

Replay the synthetic monthly sequence using FrozenClock and archived source data, progressing the clock rather than declaring future data already known. First test a cold installation: run monthly without earlier captures -> exit2 BOOTSTRAP_REQUIRED, gates/report exist, no live cohort. Then bootstrap/capture before a future cutoff, advance clock into the next month and run monthly -> exit0 and one coherent published cohort. Orders are pending until their actual execution close is introduced; settle twice -> only one fill per order.

Run sufficiently many synthetic months to exercise3/12/36-month maturity and the weight gate. Assert members/delisted statuses, versioned labels, curves from actual stored weights and separate backfill tracks. Gate opening is conditional on common finite evidence; do not assume random observed means must change EW weights. Inject a fixed admissible family-IC history to test the changed-weight branch.

Use constructed eligible candidate evidence to propose/approve/apply a Tier1 factor change. Assert future model version, ADR, provisional actor and eventual ratification/reversion. Hash earlier cohorts before/after. Resolve a CA exclusion via a synthetic authorized data decision: append revised group labels/evaluations, preserve old rows, and ensure fitting sees one current admissible revision only. Simulate a blocked new scoring month while earlier valid labels/settlements still progress.

Export reports/UI, reconstruct state from ledger in a new DB, and compare every table. Repeat report reproduction after a subsequent revision: original report is rebuilt from its pinned evidence references, not latest values. Test a subprocess crash with SQLite recovery and a concurrent second process against a temporary repository lock.

## 7. Recorded and real data checks

Record fixtures once, preserving actual capture date, package versions, source URL/request parameters, field adjustment bases and SHA256. Never label a response captured today as a2026-09-05 fixture. JSON normalized synthetic fixtures can pin legacy unit regressions; they are labeled synthetic, not recorded market responses. Check in manageable actual samples plus a capture manifest. A first install freezes a requirements lock after successful tests.

Operational checks use an isolated V2 state directory and the frozen legacy database opened read-only. Environment overrides must cover state, price cache, archives, knowledge and UI; no operational check may overwrite the owner's working output. `--commit/--push` stay off during acceptance.

Full legacy acceptance reads expected source counts2543/12/4773 and hash, verifies six snapshot mappings/four full cohorts,1997 scores per legacy model and 1997*11 legacy factor values. Quoted irregular-period attribution is compared to the existing red-team table within.01 using original scores, in-force weights and quote-return rules. A small extracted subset only tests mechanics, not whole-universe correlations. Adjusted results are measured and reported, never required to have a favorable sign. Second migration inserts0 and source hash is unchanged.

Real PIT replay requires earlier captured V2 vintages. Request `verify pit --months 3`: with only one available cohort, report one checked and two DEFERRED. Backfill/legacy points cannot supply the missing clean live months. The first valid live3M label requires three calendar months after the initial cohort. No date manipulation or current-data backfill may make this check pass early.

## 8. Sign-off commands and result rules

WS00 builds scripts/check.sh: run `python3 docs/spec/check_spec.py`, `python -m pytest -q`, compileall for quant, and a ledger round trip against an isolated fixture. It must not silently mutate or require the owner's production DB. Fail immediately on any failed required check.

WS11 builds scripts/signoff.sh with `--phase engineering|operational|longitudinal|all`, `--as-of DATE`, and `--output PATH`. It emits JSON plus markdown rows with id,phase,status,command,exit_code,observed,reason,artifact_refs. Preserve real subprocess exit codes and elapsed times. Exit0 means every requested applicable required check passed and no requested check was deferred; exit1 means any failure; exit2 means no failures but at least one deferred required check. `--phase engineering` can succeed before operational/longitudinal readiness.

| Row | Phase | Command/check | Acceptance |
|---|---|---|---|
| S01 | Engineering | `python3 docs/spec/check_spec.py` | spec checks pass; canonical copies unchanged |
| S02 | Engineering | `python -m pytest -q` | all default tests pass; measured runtime; legacy modules included |
| S03 | Engineering | `python -m pytest test_quant_math.py test_optimizer.py -q` | unchanged legacy suite passes |
| S04 | Engineering | isolated `db init`, schema FK/immutability checks | exact canonical tables/constraints, no magic count |
| S05 | Engineering | isolated `db export`, `db rebuild`, `db verify` | table hashes/control-state journal match |
| S06 | Engineering | synthetic complete/retry/concurrent/crash run | P1-P12 and relevant T1-T10 pass |
| S07 | Engineering | synthetic governance tests | identity, tiers, prospective versions and expiry behavior pass |
| S08 | Engineering | browser with fixed generated payloads |8 tabs, empty/blocked states, no errors/network dependencies |
| S09 | Operational | QUANT_NETWORK=1 adapter tests | captured source bases/units and actual archive manifest verified |
| S10 | Operational | QUANT_LEGACY_REAL=1 migration tests | real source unchanged; reconciliation and repeat no-op pass |
| S11 | Operational | real `run monthly --as-of DATE` | eligible pre-cutoff captures; actual successful cohort; or DEFERRED if calendar/capture prerequisite absent |
| S12 | Operational | size/runtime and archive inventory | measured targets and exceeded budgets listed; no invented timings |
| S13 | Longitudinal | real `verify pit --months 3` and report reproduction | available3cohorts identical; missing cohorts DEFERRED |
| S14 | Longitudinal | applicable real-label leakage/statistical checks | prerequisites and outcomes reported; insufficient labels DEFERRED |
| S15 | Research observation | backfill/live IC and curves | report observed sign/band/track; no required positive outcome |

S15 is included as an observation and never affects exit status. S12 exceeded runtime/UI budget is a failure of the stated engineering/operation target; storage warning levels generate a recorded capacity issue rather than a false claim of data corruption. No production deletion, real threshold edit or impersonated human approval belongs in sign-off. Inject adversarial changes only into isolated copies.

## 9. Final implementation report

Produce docs/spec/SIGNOFF_YYYY-MM-DD.md with spec revision/hash, base and final Git commits, implemented task IDs, actual test counts/runtimes, all phase tables, source capture/archive inventory, real run summary if available, every deviation, every deferred prerequisite and owner actions. State whether V2 is engineering-complete and whether a real admissible monthly cohort has succeeded. State that predictive skill is not established by software acceptance. Do not claim to have tested Gemini Flash unless an actual Flash run occurred.
