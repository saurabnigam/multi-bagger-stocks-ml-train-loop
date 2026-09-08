# AGENTS.md — Autonomous Coding Agent Operating Manual

Welcome, Agent. This document is your primary context and operational guide for maintaining, extending, and running the **Multi-Bagger Stocks ML Train Loop & Quant Engine (V2 Institutional)**.

---

## 🧭 Repository Mission
This repository is an institutional-grade Quantitative Machine Learning platform that identifies, scores, and tracks multi-bagger compounder stocks across the **Nifty 500** Indian equity universe.

It combines:
1. **Modular Quantitative Core (`quant/`)**: PIT data ingestion, quality gates (G1–G10), factor registry, exponentiated gradient models, and HAC-adjusted evaluation.
2. **Paper Portfolios & Scoreboard (`quant/portfolio/`)**: Simulated execution, realistic cost models, benchmark attribution, and next-session order settlement.
3. **Governance & Decision Ledger (`quant/knowledge/`)**: Architecture Decision Records (ADRs), proposals, review budget, and human co-sign ratifications.
4. **Offline Zero-Dependency UI (`ui/`)**: 8-tab institutional dashboard with locally vendored assets (`ui/vendor/chart.umd.js`) and system font stacks.
5. **Frozen Historical Baseline (`quant_engine.db`)**: Frozen reference database preserved byte-for-byte with SHA256 `03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8`.

---

## 📋 Prime Directives for Agents

1. **NEVER Modify Frozen Legacy Files or Database:**
   * `quant_engine.db` has frozen SHA256 `03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8`. It is read-only.
   * Legacy scripts (`quant_math.py`, `weight_optimizer.py`, `harness_v16_learning.py`, etc.) are preserved for baseline reference.
2. **NO Automatic Remote Pushes:**
   * Never execute `git push` from automated scripts, cron runners, or tool calls without explicit human authorization.
3. **Preserve Mathematical Invariants:**
   * **Active Weights Bounds:** Every factor weight in active models must be $\ge 0.05$ (5%) and $\le 0.30$ (30%).
   * **Active Weights Normalization:** The sum of active model factor weights must equal **strictly 1.000**.
   * **Death Cross Multiplier:** Enforces a `0.0x` hard-kill multiplier.
4. **Enforce Temporal Integrity & Quality Gates (G1–G10):**
   * Capture timestamps must be strictly $\le$ observation cutoff (`knowledge_cutoff`).
   * Publication timestamps must be $\ge$ observation cutoff.
   * No future endpoints, backfill data, or unadjusted corporate actions may leak into live inference.
5. **Keep Frontend Zero-Dependency & Fully Offline:**
   * The `ui/` directory must remain browser-native Vanilla HTML/JS/CSS with ZERO external CDN or Google Font requests.
   * Chart.js is loaded from local `ui/vendor/chart.umd.js`.
6. **Never Hard-Code Paths:**
   * All modules resolve paths relative to the repository via `quant.config.Config`.
7. **Adhere Strictly to TDD & Specification Checks:**
   * Run `python3 docs/spec/check_spec.py` and `./scripts/check.sh` before any commit.

---

## 🗂️ Codebase Architecture & File Map

```
.
├── AGENTS.md                   # This operating manual
├── README.md                   # Public repository documentation
├── requirements.txt            # Python dependencies
├── config.py                   # Legacy path/threshold config
├── quant_engine.db             # FROZEN SQLite reference database (SHA256: 03fe228b8...)
│
├── quant/                      # V2 Modular Quant Package
│   ├── config.py               # Immutable Config & paths
│   ├── types.py                # Core dataclasses & contracts
│   ├── run.py                  # Monthly pipeline orchestration & recovery
│   ├── ui_export.py            # Offline multi-tab payload generator
│   ├── verify.py               # Report & PIT verification
│   ├── status.py               # Operational status & governance reporter
│   ├── universe/               # Constituents, ISINs, corporate actions
│   ├── data/                   # Captures, manifests, and quality gates G1-G10
│   ├── factors/                # Pure factor calculations & registry
│   ├── model/                  # Exponentiated gradient & weights
│   ├── evaluation/             # Oriented Rank IC, HAC covariance, leakage audit
│   ├── portfolio/              # Paper portfolios, costs, settlement, scoreboard
│   ├── knowledge/              # ADRs, proposals, review budget, lessons
│   ├── migrate/                # Read-only legacy migration
│   └── commands/               # CLI command handlers
│
├── scripts/
│   ├── check.sh                # Specification + full test suite runner
│   └── signoff.sh              # 3-stage phased sign-off runner
│
├── monthly_cron.sh             # Scheduled runner (no auto-push)
├── daily_cron.sh               # Legacy pipeline runner
│
├── docs/
│   ├── spec/                   # Canonical specification, contracts, golden cases
│   └── analysis/               # Red-team reviews and audit documentation
│
└── ui/
    ├── index.html              # 8-tab dashboard markup (offline)
    ├── app.js                  # Frontend controller
    ├── style.css               # Modern layout & status badges
    ├── vendor/
    │   ├── chart.umd.js        # Vendored Chart.js (v4.4.1)
    │   └── VERSION             # Vendored asset version
    ├── data.js                 # Exported ranking payload
    ├── data_learning.js        # Exported evaluation curves
    ├── data_scoreboard.js      # Exported paper portfolios & orders
    ├── data_factors.js         # Exported factor registry
    └── data_kb.js              # Exported ADRs & governance
```

---

## 🛠️ Agent Operational Playbooks

### Playbook A: Verification & Regression Checks
```bash
# 1. Activate environment
source venv/bin/activate

# 2. Run specification checks & full test suite
./scripts/check.sh

# 3. Run phased sign-off audit
./scripts/signoff.sh
```

### Playbook B: Running the Monthly Orchestration
```bash
# Execute monthly run (dry run)
python3 -m quant run monthly --dry-run

# Execute full monthly run
./monthly_cron.sh
```
