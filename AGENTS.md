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
* **Gates are computed from evidence, not asserted.** `quant.data.gates.run` derives
  G1-G10 from the actual captured/computed data for a draft cohort; it does not
  default a missing check to "passing."
* **A fresh install reports `BLOCKED: BOOTSTRAP_REQUIRED`.** Run
  `python -m quant data capture` at least once before the intended first cutoff
  (MASTER_SPEC §2.2) — there is no scoring cohort without it.
* **CLI groups** (see MASTER_SPEC §10.3 for the full command list; run any group
  with `--help` for its exact current subcommands): `db`, `status`, `universe`,
  `prices`, `data`, `factors`, `model`, `evaluate`, `portfolio`, `kb`, `run`,
  `verify`. Every command accepts `--db-path`, `--config`, `--actor-kind`, and
  `--by` before or after the subcommand.

---

## 🗂️ Codebase Architecture & File Map

```
.
├── AGENTS.md                   # This operating manual
├── README.md                   # Public repository documentation
├── requirements.txt            # Python dependencies
├── config.py                   # LEGACY path/threshold config (do not confuse with quant/config.py)
├── quant_engine.db             # FROZEN legacy SQLite reference database (SHA256: 03fe228b8...)
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
│   ├── migrate/                    # Read-only legacy migration
│   └── commands/                   # CLI command handlers
│
├── scripts/
│   ├── check.sh                 # check_spec.py + full pytest, interpreter-resolved
│   └── signoff.sh                # Phased sign-off runner (S01-S15); see --help
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
# Interpreter resolution used by every script below: $QUANT_PYTHON if set, else
# venv/bin/python if present, else python3. Antigravity sessions typically export
# QUANT_PYTHON to the scratch venv rather than creating venv/ inside the repo —
# set it if venv/bin/python does not exist on your machine.

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
