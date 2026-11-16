# AGENTS.md — Autonomous Coding Agent Operating Manual

Welcome, Agent. This document is your primary context and operational guide for maintaining, extending, and running the **Multi-Bagger Stocks ML Train Loop & Quant Engine (V2)**.

This file was corrected on 2026-09-10 after a review found it described invariants and
an architecture that do not match the shipped V2 code (see
`docs/spec/SIGNOFF_2026-09-08.md` for the superseded report and the list of
deviations). The corrections below reflect `docs/spec/MASTER_SPEC.md` revision 2,
which is the normative source; where this file and the spec disagree, the spec wins.

---

## 🧭 Repository Mission

This repository has two systems, kept deliberately separate:

1. **Legacy engine** (root-level scripts: `quant_math.py`, `weight_optimizer.py`,
   `harness_v16_learning.py`, `db_setup.py`, `config.py`, and the frozen
   `quant_engine.db`). This produced the four 2026 snapshots migrated read-only into
   V2 (see §10.6 of the spec). It is preserved for baseline reference only and is
   never executed by V2 code.
2. **V2 quant engine** (`quant/`): a from-scratch, spec-driven rebuild that scores,
   ranks, and tracks candidate multi-bagger compounder stocks across the Nifty 500
   universe, with point-in-time (PIT) data capture, evidence-based quality gates
   (G1-G10), oriented Rank-IC / HAC-adjusted evaluation, paper portfolios, and a
   governance ledger. **No live cohort has been published from V2 as of this
   writing, and no out-of-sample performance is claimed.**

It combines:
1. **Modular quantitative core (`quant/`)**: PIT data ingestion, quality gates
   (G1-G10), a factor registry, family-weight learning (§6.3), and HAC-adjusted
   evaluation.
2. **Paper portfolios & scoreboard (`quant/portfolio/`)**: simulated execution, a
   cost model, benchmark attribution, and next-session order settlement.
3. **Governance & decision ledger (`quant/knowledge/`)**: Architecture Decision
   Records (ADRs), proposals, a review budget, and human co-sign ratifications.
4. **Offline zero-dependency UI (`ui/`)**: an 8-tab dashboard with locally vendored
   assets (`ui/vendor/chart.umd.js`) and system font stacks.
5. **Frozen historical baseline (`quant_engine.db`)**: preserved byte-for-byte with
   SHA256 `03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8`. This is
   the **legacy** engine's database; it is not written to or read from by any V2
   scoring path.

The state database `quant.db` is **not committed** (it is git-ignored and derived). The
committed record is the compressed ledger `data/ledger/YYYY-MM/events.jsonl.gz`; rebuild
the database with `python -m quant db rebuild --output quant.db`. `quant_engine.db`
stays frozen and tracked.

---

## 📋 Prime Directives for Agents

1. **NEVER modify frozen legacy files or the legacy database:**
   * `quant_engine.db` has frozen SHA256 `03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8`. It is read-only.
   * Legacy scripts (`quant_math.py`, `weight_optimizer.py`, `harness_v16_learning.py`, `db_setup.py`, root `config.py`, `test_quant_math.py`, `test_optimizer.py`, etc.) are preserved unchanged for baseline reference.
2. **NO automatic remote pushes:**
   * Never execute `git push` from automated scripts, cron runners, or tool calls without explicit human authorization. `monthly_cron.sh` refuses a `--push` argument outright rather than forwarding it (MASTER_SPEC §10.5).
3. **V2 scoring invariants are those in MASTER_SPEC §6, not the legacy ones.** See "V2 Invariants" below — the legacy engine's bounds/normalization/multiplier rules do not apply to `quant/model`.
4. **Enforce temporal integrity & quality gates (G1-G10):**
   * Capture timestamps must be $\le$ the observation cutoff (`knowledge_cutoff`).
   * Publication timestamps must be $\ge$ the observation cutoff.
   * No future endpoints, backfilled data, or unadjusted corporate actions may leak into live inference.
   * Exception (MASTER_SPEC §2.1): price bars of a late-scored cohort are read at a pinned price vintage. Everything else keeps the cutoff. The cohort is flagged `scored_late`, `is_clean = 0`, and the path needs the recorded Tier-2 decision `late_price_admission`.
   * A fresh install with insufficient prior captures reports `BLOCKED: BOOTSTRAP_REQUIRED` and publishes no live cohort — this is expected, not a bug (MASTER_SPEC §2.2).
5. **Keep the frontend zero-dependency & fully offline:**
   * `ui/` must remain browser-native vanilla HTML/JS/CSS with **zero** external CDN or Google Font requests.
   * Chart.js is loaded from the locally vendored `ui/vendor/chart.umd.js`.
6. **Never hard-code paths:**
   * V2 modules resolve paths through `quant.config.Config` (repository-relative, with `QUANT_DB_PATH` / `QUANT_PRICES_DB_PATH` env overrides; see MASTER_SPEC §10.2 for the full override list).
7. **Adhere strictly to TDD & specification checks:**
   * Run `python3 docs/spec/check_spec.py` and `./scripts/check.sh` before any commit.
   * Run `./scripts/signoff.sh --phase engineering` for the fuller phased acceptance picture before claiming a workstream done.

---

## 🤝 Working Together: what is shared, what is throwaway

Several agents work on this repository at the same time (Claude Code, Antigravity, Codex, Cursor and
others). All of them read this file: `CLAUDE.md`, `GEMINI.md`, `.cursorrules` and `.windsurfrules` are
links to it. Follow these rules so that every agent sees the same code, the same data, the same
environment and the same facts.

### Shared and central: one copy, used by everyone

| What | Where | How it changes |
|---|---|---|
| Rules for agents | `AGENTS.md` (this file) | Edit here only. Never keep a private variant |
| Specification | `docs/spec/MASTER_SPEC.md` | A change of rule needs the owner's decision |
| Code | git, branch `codex/v2-implementation` | Work on your own branch in your own worktree. Merge after review. The owner authorises every push |
| Python environment | `venv/` in the main working directory, pinned by `requirements.lock` | `scripts/bootstrap_env.sh` creates or repairs it. Every script finds it through `scripts/resolve_python.sh`. Never create another environment somewhere else |
| Real data: state database, price store, raw archives, journal, reports, dashboard data | The main working directory: `quant.db`, `data/`, `knowledge/`, `ui/data*.js` | Only through the engine's commands, which take the write lock. Reach it with `scripts/q.sh` in the runtime checkout |
| Decisions | `knowledge/decisions/` and the `decisions` table | Recorded by the owner. An agent never records an approval |
| Current state, open items, hazards | `docs/operations/CURRENT_STATE.md` | Update it in the same change that alters the state |
| Runbooks | `docs/operations/` | Keep them matching the code |
| Findings worth keeping | `docs/analysis/`, with the evidence beside the report | Commit them. A finding that lives only in a chat or in one agent's private memory is lost to the others |

### Throwaway: stays in your scratch space

* Copies of the data for experiments, trial scripts, logs of your own runs, drafts and notes to yourself.
* Keep them in your tool's scratch or temporary folder, outside the repository. Never commit them and
  never leave them in the main working directory.
* If something in scratch turns out to matter to others (a finding, a script that will run again, a
  number someone will rely on), move it to its shared place in the table above before you finish.

### Before you start

1. Read this file and `docs/operations/CURRENT_STATE.md`.
2. Run `git worktree list` and `git log --oneline -5 codex/v2-implementation`. See what others are
   doing and build on the newest code.
3. Run `scripts/q.sh status`. The key `write_lock` shows whether someone is writing to the real data.

### While you work

* **Code:** your own worktree and branch. Do not edit code in the main working directory or in the
  runtime checkout, and do not leave uncommitted changes there.
* **Data:** experiments run on a copy. Point `MBQ_DATA_ROOT` (or the `QUANT_*` paths) at the copy. Real
  data is changed only by the engine's commands, with reviewed code.
* **Time:** never change a record's time to make a check pass. Record the true fetch time and let the
  run be flagged late. Gate G3 and `verify pit` reject a price row that claims to be older than its
  own download (MASTER_SPEC R17).
* **Files:** never hand-edit `quant.db`, the price store, the journal or a published report.

### When you finish

* Commit your work on your branch, with tests. Say in the commit what changed and why.
* Update `docs/operations/CURRENT_STATE.md` if the state, an open item or a hazard changed.
* Leave the main working directory clean: `git status` there shows no code change of yours.
* Tell the owner what you did not finish. Do not push; the owner authorises pushes.

---

## ⚖️ V2 Invariants (MASTER_SPEC §6.3, §6.4) — supersede the legacy invariants below

The legacy engine's "active weights in [0.05, 0.30] summing to strictly 1.000" and
"0.0x Death Cross hard-kill multiplier" are **legacy-only** facts, true of the frozen
2026 snapshots and nothing else. V2 does not use exponentiated-gradient optimization,
a death-cross multiplier, a trap-score multiplier, or a momentum multiplier at all.
The rules that actually govern `quant/model` are:

* **Family weights are integers, not floats.** For `F` included factor families, each
  family's weight is stored as an integer number of **units out of 10000**, bounded
  `[ceil(10000 * 0.5/F), floor(10000 * 2/F)]`. The units for all included families
  sum to **exactly 10000** (not "approximately 1.000" — an exact integer identity).
  Public weight = units / 10000.
* **EW_HIER_v1** is the initial champion: an equal-weight hierarchical model across
  globally covered active/probation families. It is the permanent reference model
  even after a challenger is promoted.
* **IC_SHRUNK_v1** is the initial challenger. It shrinks the equal-weight prior
  toward evidence-weighted family IC:
  ```
  n_eff = N_matured_3M_rows / 3
  alpha = n_eff / (n_eff + 24)
  target[k] = (1 - alpha)/F + alpha * normalized_positive_mean_IC_z[k]
  ```
  Below the gate `n_eff < 4`, `IC_SHRUNK_v1` falls back to the exact equal-weight
  policy — it does not extrapolate from thin evidence.
* **No multipliers, no prediction-based filters.** There is no death-cross kill
  switch, no trap-score multiplier, and no momentum multiplier anywhere in `quant/`.
  The "turnaround" view is a saved filter (high-growth top-quintile and negative
  FCF), not a separate scoring path.
* **Eligibility screens** (separate from the composite score itself): EQ series
  only, a known sector, 63-day average daily traded value (ADV63) of at least
  ₹2,00,00,000 (2 crore / 20,000,000 INR), and at least 54 positive-volume sessions
  out of the trailing 63.
* Promotion from champion to challenger is a human Tier-2 decision gated on
  ≥24 paired live matured 3M cohorts, a HAC paired-difference t-test, net paper
  performance, a 12M sign check, and a turnover cap — never automatic (MASTER_SPEC §6.4).

---

## 🏗️ Real Architecture

* **Every write to the state database is journaled automatically.** `quant.db.core.apply_schema`
  installs AFTER INSERT/UPDATE/DELETE triggers on every state table except
  `ledger_events` itself; they call `quant_run_id()` / `quant_now()` /
  `quant_row_json()` / `quant_sha256()`, registered on connections from
  `quant.db.core.connect`. Library code must never write to `ledger_events`
  directly, and must never call `conn.commit()` or use `with conn:` (connections
  are opened with `isolation_level=None`; `RunContext` owns the transaction).
* **`RunContext` (`quant/run.py`) owns staging.** It opens `SAVEPOINT staging` on
  enter; `ctx.checkpoint()` persists staged work and opens a fresh savepoint;
  `ctx.rollback_staging()` discards staged work but replays any rows registered
  with `ctx.keep_after_rollback(table, rows_df, keys)`. `db export` / `db rebuild`
  / `db verify` round-trip the ledger to reproduce a state database from its
  journal.
* **One writer at a time.** Every engine write to a state database holds an exclusive
  advisory lock `<db>.lock`: every `RunContext`, `db init`, `db export`, `db verify` (they write the journal files), `db migrate-legacy` and the whole of
  `run monthly` (through the journal export, verification, VACUUM and any commit or push). A second
  writer fails fast (after a one-second retry) or waits up to `QUANT_LOCK_WAIT_S` seconds, and the
  lock frees itself when its process ends. Read-only commands (`status`,
  `db size`, `db rebuild` to a new file) take none; `quant status` shows the holder under `write_lock`
  ([docs/operations/concurrency.md](docs/operations/concurrency.md)).
* **Several agents, separate workspaces.** Edit code in your own git worktree, never in the pinned
  runtime checkout (`mb-runtime`, branch `runtime`) that scheduled jobs and real runs use. Experiments
  run against a copy of the data: point `QUANT_DB_PATH`, `QUANT_PRICES_DB_PATH` and the other `QUANT_*`
  paths at the copy. Never run a writing command against the real database from a checkout with
  unreviewed code. The lock stops two writers from interleaving; it does not make unreviewed code safe.
  Real-data commands go through `scripts/q.sh` in the runtime checkout
  ([docs/operations/month_end.md](docs/operations/month_end.md)).
* **The ledger is the committed state.** `db export` writes deterministic gzip partitions
  (`events.jsonl.gz`); readers also accept an old plain `events.jsonl` and prefer `.gz`.
  The database file, the price store and the raw archives stay out of git
  ([docs/operations/storage_and_backup.md](docs/operations/storage_and_backup.md)).
* **Gates are computed from evidence, not asserted.** `quant.data.gates.run` derives
  G1-G10 from the actual captured/computed data for a draft cohort; it does not
  default a missing check to "passing."
* **A fresh install reports `BLOCKED: BOOTSTRAP_REQUIRED`.** Run
  `python -m quant data capture` at least once before the intended first cutoff
  (MASTER_SPEC §2.2) — there is no scoring cohort without it.
* **Closing prices have a source chain** (Yahoo, then the NSE and BSE end-of-day files from an inbox
  folder; auto-fetch is off by default). Enabling it is a human decision; an agent never records it.
  Runbook: `docs/operations/price_sources.md` (MASTER_SPEC R18).
* **CLI groups** (see MASTER_SPEC §10.3 for the full command list; run any group
  with `--help` for its exact current subcommands): `db`, `status`, `universe`,
  `prices`, `data`, `factors`, `model`, `evaluate`, `portfolio`, `kb`, `triage`,
  `run`, `verify`. Every command accepts `--db-path`, `--config`, `--actor-kind`, and
  `--by` before or after the subcommand.
* **Evidence triage (`quant/triage/`, CLI group `triage`) proposes; a human resolves.**
  It is deterministic and runs with no LLM: it lists suspected corporate actions,
  archives attached evidence into `captures`, and writes one `proposals` row per
  anomaly (`rule_id` `TRIAGE_RULES_V1`, or `TRIAGE_AGENT_V1` for an optional agent).
  It never changes a score or factor value and never writes `corporate_actions`;
  only `data actions-resolve --actor-kind human` does that, optionally bound to a
  proposal with `--proposal-id ID --confirm SHA8`. `collect`, `attach`, `build` and
  `submit` refuse once the clock is past the cutoff of `--as-of`; `list` and `show`
  are read-only. `--actor-kind` is a label, not authentication: an agent must use
  `--actor-kind llm --by llm:<model id>`. Runbook:
  [docs/operations/triage.md](docs/operations/triage.md).

---

## 🗂️ Codebase Architecture & File Map

```
.
├── AGENTS.md                   # This operating manual
├── README.md                   # Public repository documentation
├── requirements.txt            # Python dependencies
├── requirements.lock           # Exact versions every agent and every run uses (scripts/bootstrap_env.sh)
├── config.py                   # LEGACY path/threshold config (do not confuse with quant/config.py)
├── quant_engine.db             # FROZEN legacy SQLite reference database (SHA256: 03fe228b8...); tracked
├── quant.db                    # V2 state database: git-ignored, rebuilt from data/ledger (not committed)
├── data/ledger/YYYY-MM/        # events.jsonl.gz: the committed, canonical, compressed journal
│
├── quant/                      # V2 modular package
│   ├── config.py               # V2 Config & repository-relative paths
│   ├── types.py                # Core dataclasses & contracts (Actor, Clock, Draft, ...)
│   ├── run.py                  # RunContext, monthly pipeline orchestration & recovery
│   ├── ui_export.py            # Offline multi-tab payload generator
│   ├── verify.py               # Report & PIT verification (library functions)
│   ├── status.py               # Operational status & governance reporter
│   ├── universe/                # Constituents, ISINs, corporate actions
│   ├── data/                    # Captures, manifests, and quality gates G1-G10
│   ├── factors/                 # Pure factor calculations & registry
│   ├── model/                   # EW_HIER / IC_SHRUNK family-weight models (see V2 Invariants)
│   ├── evaluation/               # Oriented Rank IC, HAC covariance, leakage audit
│   ├── portfolio/                # Paper portfolios, costs, settlement, scoreboard
│   ├── knowledge/                 # ADRs, proposals, review budget, lessons
│   ├── triage/                    # Evidence triage: scan, features, rules, pack, validate (proposes only)
│   ├── migrate/                    # Read-only legacy migration
│   └── commands/                   # CLI command handlers
│
├── scripts/
│   ├── check.sh                 # check_spec.py + full pytest, interpreter-resolved
│   ├── signoff.sh                # Phased sign-off runner (S01-S15); see --help
│   ├── backup_state.sh           # Snapshot of the state db, price store and archives, with INVENTORY.json
│   ├── capture_routine.sh        # Idempotent month-end routine: prices | fundamentals | status
│   ├── resolve_python.sh         # The one rule for which Python runs the engine (shared venv)
│   ├── bootstrap_env.sh          # Creates or repairs the shared venv from requirements.lock
│   ├── q.sh                      # Engine CLI with this checkout's code against the data root
│   └── routine_env.sh            # Parser for the optional local settings file routine.env
│
├── monthly_cron.sh              # Scheduled runner: lock, no --push, logs to data/logs/
├── daily_cron.sh                # Legacy pipeline runner
│
├── docs/
│   ├── spec/                     # Canonical specification, contracts, golden cases
│   └── analysis/                  # Red-team reviews and audit documentation
│
└── ui/
    ├── index.html                # 8-tab dashboard markup (offline)
    ├── app.js                    # Frontend controller
    ├── style.css                  # Layout & status badges
    ├── vendor/
    │   ├── chart.umd.js           # Vendored Chart.js
    │   └── VERSION                # Vendored asset version
    ├── data.js                   # Exported ranking payload
    ├── data_learning.js           # Exported evaluation curves
    ├── data_scoreboard.js         # Exported paper portfolios & orders
    ├── data_factors.js            # Exported factor registry
    └── data_kb.js                 # Exported ADRs & governance
```

---

## 🛠️ Agent Operational Playbooks

### Playbook A: Verification & Regression Checks

```bash
# One Python environment for every agent and every checkout: venv/ in the main working directory,
# pinned by requirements.lock. scripts/resolve_python.sh finds it ($QUANT_PYTHON if set, else this
# checkout's venv, else the main working directory's venv, else python3), so a git worktree needs no
# environment of its own. Create or repair it with: scripts/bootstrap_env.sh

# 1. Run specification checks & the full test suite
./scripts/check.sh

# 2. Run the phased sign-off (engineering checks only by default)
./scripts/signoff.sh --phase engineering

# 3. Full sign-off, including operational/longitudinal rows that DEFER without
#    a real database, live network access, or matured live cohorts
./scripts/signoff.sh --phase all --output /tmp/signoff.json
```

### Playbook B: Running the Monthly Orchestration

```bash
# Bootstrap first (required once, before the first intended cutoff):
python3 -m quant data capture

# Execute monthly run (dry run)
python3 -m quant run monthly --dry-run

# Execute the full monthly run (never pushes; logs to data/logs/, which is git-ignored)
./monthly_cron.sh
```

### Playbook B2: Month-end sequence with late price admission

Full runbook: [docs/operations/late_price_admission.md](docs/operations/late_price_admission.md). Needs the one-time `kb decide` decision.

```bash
python3 -m quant universe capture && python3 -m quant data capture   # last week; ~60-75 min
python3 -m quant prices update --through 2026-10-30                   # as_of evening ~20:30 IST (exits 1 without --through); repeat 22:00, 23:30 if coverage < 98%
python3 -m quant triage list --as-of 2026-10-30                       # prints as-of bar coverage; resolve suspected actions before 23:59 IST
python3 -m quant run monthly                                          # next day or later
```

The cohort is clean only if at least 98% of the as_of closes are recorded before 23:59:59 IST that day; otherwise it is published flagged scored late (`is_clean = 0`). `kb decide` also flags an older cohort whose as_of closes were recorded after its cutoff, and `prices manifest --verify` understands late cohorts.

### Playbook C: Triage a suspected corporate action (cohort evening)

Full runbook with worked examples: [docs/operations/triage.md](docs/operations/triage.md).

```bash
python3 -m quant triage list    --as-of 2026-10-30                      # read-only
python3 -m quant triage collect --as-of 2026-10-30 --all --actor-kind human --by human:<you>
python3 -m quant triage attach  --as-of 2026-10-30 --isin I --ex-date X --title T --url U \
    --published-at DATE --source-class regulator --relevance sector --excerpt "..." --actor-kind human --by human:<you>
python3 -m quant triage build   --as-of 2026-10-30 --all                # no network; idempotent
python3 -m quant triage show    --isin I --ex-date X                    # read-only; prints the resolve command
# a human, never an agent, resolves:
python3 -m quant data actions-resolve ... --proposal-id ID --confirm SHA8 --actor-kind human --by human:<you>
```

**Agents:** the `--actor-kind human --by human:<you>` labels above are for the human operator. An agent always uses
`--actor-kind llm --by llm:<model id>`, may run only `triage list`, `show`, `submit` and `attach --url`, and
never runs `triage build` (refused for llm actors), `data actions-resolve`, `triage reject` or `kb reject`. The label is not authentication; the contract is in
`docs/operations/triage_agent_contract.md`.

### Playbook D: Rebuild and back up state

Full note: [docs/operations/storage_and_backup.md](docs/operations/storage_and_backup.md).

```bash
python3 -m quant db rebuild --output quant.db     # fresh clone: replay data/ledger into a database
python3 -m quant db verify                        # export, rebuild a temp copy, compare every table
python3 -m quant db size                          # prints compressed journal bytes; WARN above 45 MiB
scripts/backup_state.sh [--dry-run] [DEST_DIR]    # state db + price store + archives; KEEP=6 snapshots
```

The backup folder must be outside the repository; copying it off the laptop is the owner's choice.
