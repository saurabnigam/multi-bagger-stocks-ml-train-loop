# Knowledge Base & Empirical Governance

This directory contains the immutable quantitative knowledge base, architecture decision records (ADRs), pre-registered hypotheses, monthly audit reports, and lessons learned.

---

## 🗂️ Directory Structure

- `decisions/`: Architecture Decision Records (ADRs) documenting every formal decision (bootstrap, factor promotion, model changes, automated reversions).
  - Format: `ADR-<decision_id>.md`.
  - Audited via `quant kb check` / `adr.check`.
- `hypotheses/`: Pre-registered hypotheses for factors and models.
  - Registered prior to observing any out-of-sample data (`first_oos_as_of`).
  - Strict annual budget limit (6) and per-family limit (3) enforced.
- `reports/`: Content-addressed monthly reports and companion JSON manifests.
  - Path: `reports/YYYY-MM/<report_id>.md` and `.json`.
  - `report_id` is SHA256 of the pinned evidence manifest.
- `lessons.md`: Append-only chronological ledger of quantitative and data lessons.
  - Synchronized from the `lessons` SQLite table via `quant.knowledge.lessons`.

---

## 🛡️ Governance & Approval Tiers

1. **Tier 0 (System Bootstrap):**
   - Autonomous system bootstrap (`DEC_BOOTSTRAP`) recording initial launch factor and model specs referencing the exact spec hash.
2. **Tier 1 (Factors, Data Fixes, Revisions):**
   - Autonomous LLM review approval is **provisional** only.
   - Requires human co-signature (ratification) within 60 calendar days.
   - Unratified decisions automatically expire, creating an automated reversion decision, returning factor to shadow status, and appending a prospective model version.
3. **Tier 2 (Model Architecture & Strategy Promotion):**
   - Requires human authorization. Autonomous LLM approvals of Tier 2 are strictly refused (exit code 3).
   - Impersonation of human actors by LLM agents is strictly refused (exit code 3).

---

## ⚖️ Empirical Invariants

- **Persisted Evidence Only:** Reports render solely from stored evaluation rows, cohorts, orders, and ledger tables. No online model execution or fabricated metrics.
- **Uncertainty Status:** Finite confidence intervals (`[ci_lo, ci_hi]`) are rendered ONLY when `uncertainty_status == 'estimable'`. For `'insufficient'` or `'not_applicable'`, finite bands are never fabricated.
- **Track Separation:** Live and backfill tracks are rendered into distinct reports.
- **Mandatory Disclaimer:** "Small or dependent samples may not distinguish skill from noise."
