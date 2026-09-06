# Handoff — prompts for the implementing LLM

Copy one of the blocks below into a fresh session of the coding assistant you choose, opened at the repository root. The assistant needs shell access, Python 3.11+ with pandas/numpy/scipy/yfinance/pytest installed, and network access for the few recorded-fixture downloads and the first real run.

Repository path (adjust if different): `/Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop`
Working Python (has the dependencies): `/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/venv/bin/python` — or create `venv/` in the repo with `python3 -m venv venv && venv/bin/pip install -r requirements.txt`.

## 1. Build everything (the main prompt)

```
You are implementing the V2 Quant Engine in the git repository at /Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop (branch: create `v2-implementation` from the current HEAD).

Read, in this order, before writing any code:
1. docs/spec/00_context_brief.md
2. subagents/README.md            (build order, interface contracts, rules of engagement, progress protocol)
3. docs/spec/MASTER_SPEC.md       (the contract; read it fully once)
4. docs/spec/TEST_AND_VERIFICATION_PLAN.md
5. subagents/PROGRESS.md          (what has already been done)

Then implement the workstreams strictly in this order: WS00, WS01, WS02, WS03, WS04, WS05, WS06, WS07, WS10, WS08, WS09, WS11. For each workstream:
  a. Open subagents/WSxx_*.md and follow it section by section. Its §6 interfaces are contracts: implement the exact names, signatures and return shapes. Its §9 tests are mandatory; write them before or alongside the code. Its §10 verification checklist must be run and its outputs pasted into subagents/PROGRESS.md.
  b. Where the workstream doc and docs/spec/MASTER_SPEC.md disagree, MASTER_SPEC wins; record the disagreement under "Deviations" in PROGRESS.md.
  c. Where a decision is not covered, take the default from MASTER_SPEC section 15 and record "Qn -> default" in PROGRESS.md. Stop and ask me only if no default exists or if a gate blocks on real data and the fix would change what is stored about the past.
  d. Run `python -m pytest -q` (must be green, offline, < 90 s) and `scripts/check.sh` before committing.
  e. Append the PROGRESS.md section (format in subagents/README.md) and commit with the message "WSxx: <summary>". Do not push.
Hard rules (from subagents/README.md, repeated because they matter): never impute a missing input (NaN + flag); never overwrite stored factor_values/scores/labels/evaluations; every factor reads data only through FactorInputs; no new third-party dependency; no schema edits outside quant/db/schema.sql; the legacy scripts and quant_engine.db are never modified (opened read-only); never write a performance number into any document that did not come from `quant evaluate` with its n_eff and confidence band.
When WS11 is complete, run scripts/signoff.sh, paste its table into PROGRESS.md, and report: what was built, the test counts, the first real run's summary lines, the sign-off table, every deviation, and every open question you took a default on.
```

## 2. Resume after an interruption

```
You are continuing the V2 Quant Engine implementation in /Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop on branch v2-implementation.
Read subagents/PROGRESS.md to find the last completed workstream and any partial work; read subagents/README.md for the build order and rules; then read the doc of the next workstream in subagents/ and continue from its first unmet item in §11 (Definition of done). Run `python -m pytest -q` first and fix anything red before adding code. Follow the same per-workstream procedure and progress protocol as the main prompt. Do not push.
```

## 3. Verify everything and produce a sign-off report

```
You are auditing the V2 Quant Engine implementation in /Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop on branch v2-implementation. Do not modify code except to fix a failing check, and record every fix in subagents/PROGRESS.md.
Execute docs/spec/TEST_AND_VERIFICATION_PLAN.md end to end: sections 3 (property invariants), 4 (leakage tests), 5 (end-to-end synthetic scenario), 6 (real legacy migration with QUANT_LEGACY_REAL=1), 7 (runtime and size budget, measured), 8 (the 15-row sign-off checklist via scripts/signoff.sh), and 9 (try to break it: every bullet).
Then produce docs/spec/SIGNOFF_<date>.md containing: the sign-off table with PASS/FAIL per row and the observed output, the measured runtimes and sizes, the list of adversarial attempts and what happened, every deviation from MASTER_SPEC found in the code with a severity, and a one-paragraph verdict on whether the system is ready to run monthly. Quote no performance number without its n_eff and band; state explicitly that no out-of-sample skill has been demonstrated yet.
```

## 4. Monthly operation (after sign-off; for the owner or a scheduled agent)

```
Run the monthly loop for the V2 Quant Engine in /Users/saurabhnigam/Desktop/Projects/multi-bagger-stocks-ml-train-loop: `python -m quant run monthly` (as_of resolves to the last NSE trading day of the previous month). If it exits 2 (blocked), read the gate table in knowledge/reports/<month>.md and the data_quality_events, fix only data-source issues (corporate actions via `quant data ca add`, revisions via `quant data accept-revision`), never stored rows, and re-run. When it exits 0, read the report, then for each open proposal run `python -m quant kb approve|reject <id> --by human:<name> --note "<reason citing the evidence rows>"` (you may draft the LLM first-reader checklist with `--by llm:<model>`, which creates provisional Tier-1 decisions only), then `python -m quant kb apply`, commit, and push. Never edit config thresholds without a Tier-2 decision. Never call anything alpha unless the report does.
```

## 5. What the owner should expect

```
month 1     a clean loop that runs; a backfill chart for price factors (dashed, survivorship-biased); four hollow legacy points; no live evidence
month 4     first live 3-month IC point; wide bands
month 12    ~9 live 3M points; first 12M point at month 13; nothing promotable yet; the report says so
month 15    the challenger's weights first deviate from equal (alpha 0.14); the champion stays equal-weight
month 24    first meaningful HAC t on the 3M IC; challenger-vs-champion first readable
month 36    the falsification checkpoint that matters; first multi-bagger cohort
```

If the verdict line says the same thing for six months ("EW IC +0.02, CI includes 0, gate closed, no proposals"), that is the system working, not failing.
