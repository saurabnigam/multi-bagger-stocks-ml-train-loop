# WS09 — Knowledge base, criteria engine, proposals, tiered approval, decisions and ADRs, monthly report, lessons

## 1. Mission
Make the system remember why it is the way it is, and make change slow, evidenced and reversible. Hypotheses are registered before evidence; a yearly budget and a deflated threshold control multiple testing; the rules propose, humans (or a provisional LLM) dispose through one CLI that refuses what the tiers forbid; every decision produces an ADR; the monthly report is generated from tables and may only use the word "alpha" when the numbers earn it.

## 2. Read first
1. MASTER_SPEC §9 (all), §5.4–§5.5, §6.5 (promotion), §7.3 (`t_crit`), §8.4 (alpha word), §13 D18, §15 Q2/Q15
2. WS06 (`review`, `bump_version`), WS07 (`ic_series`, cumulative rows), WS08 (`scoreboard`), WS04 (`events`) docs §6
3. `docs/analysis/red_team_review.md` (the first entries of `lessons.md` come from it)

## 3. Scope
In: `hypotheses`/`experiments` registry with budget and sequence numbers; criteria engine (§9.5 factor rules, §6.5 model rules via WS06 `review`); proposals drafting; approve/reject/apply with tiers, provisional LLM approvals and 60-day ratification; ADR writer + checker; monthly report renderer; lessons; JSONL mirror; `kb` commands; seed ADRs D-2026-09-01..07 (legacy retirements) and ADR-001 (launch set) templates; `knowledge/README.md`.
Out: the numbers themselves (WS07/WS08), UI (WS11).

## 4. Dependencies
WS06, WS07, WS08 merged; synthetic world evaluated through month 48 so criteria have data.

## 5. Interfaces you consume
`evaluation.evaluate.ic_series`, cumulative `evaluations` rows, `evaluation.stats.t_crit/hac_mean_test`, `evaluation.metrics.corr_matrix/partial_ic`, `models.review/bump_version/definition_at`, `factors.registry.statuses`, `portfolio.scoreboard.compute/alpha_word_allowed/crowding`, `gates.events`, `data.actions.clear_flag/add_ca`, `db.core.upsert`, `RunContext`.

## 6. Interfaces you provide
```python
# quant/knowledge/registry.py
def new_hypothesis(conn, cfg, *, kind, subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_by, first_oos_as_of, counts_toward_budget=1, code_sha=None) -> str
    # assigns H-YYYY-NNN and sequence_in_year ; raises Refused when budget_status(year)['remaining'] == 0 (or per-family cap) and counts_toward_budget ; writes knowledge/hypotheses/H-....md from the template
def budget_status(conn, year: int, cfg) -> dict                                   # used, remaining, per_family, m_trailing_24 (for t_crit)
def list_hypotheses(conn, status=None) -> pd.DataFrame
def record_experiment(conn, hypothesis_id, kind, config: dict, result: dict, verdict: str, track: str, run_id: int, counts_toward_budget=0) -> str
def export_jsonl(conn, knowledge_dir: Path) -> list[Path]                          # knowledge/db/<table>.jsonl for hypotheses, experiments, proposals, decisions, lessons, factor_registry, model_versions
# quant/knowledge/review.py
@dataclass CriteriaCheck: subject_id, kind, as_of, checks: dict[str, bool], values: dict, all_true: bool, next_review: str | None
def criteria_for_factor(conn, factor_id: str, as_of: str, cfg) -> CriteriaCheck   # §9.5 shadow->active, active->probation, probation->active/retired, shadow->retired, near-constant quarantine hint
def criteria_for_model(conn, model_id: str, as_of: str, cfg) -> CriteriaCheck     # wraps models.review P1–P5 with t_crit(m_models)
def review_all(conn, as_of: str, cfg) -> list[CriteriaCheck]
# quant/knowledge/proposals.py
TIERS = {0: [...auto kinds...], 1: cfg.approval.llm_allowed_kinds, 2: [...human only...]}   # MASTER_SPEC §9.6
def tier_of(kind: str, payload: dict, cfg) -> int                                  # e.g. cost_model within ±25% -> 1 else 2
def draft(conn, as_of: str, cfg) -> list[str]                                      # from review_all + events (CA flags, quarantines, near-constant x3, turnover>30% x3 -> rule hypothesis) ; status 'proposed' ; knowledge/proposals/YYYY-MM.md ; never duplicates an open proposal for the same subject/kind
def approve(conn, proposal_id: str, by: str, note: str, cfg) -> str               # by = 'human:<name>' | 'llm:<model>' ; refuses (Refused) llm on tier 2, llm on tier 1 with any false in criteria_check_json, unknown proposal ; creates decisions row (status 'approved' for human, 'provisional' for llm with ratification deadline) ; writes ADR ; returns decision_id
def reject(conn, proposal_id: str, by: str, note: str, cfg) -> str
def ratify(conn, decision_id: str, by: str) -> None                                # human co-signature for provisional decisions
def apply(conn, as_of: str, cfg) -> AppliedReport                                  # applies approved (and provisional-within-deadline) decisions EFFECTIVE FROM the next as_of: factor_registry status changes, models.bump_version, config-gated rule changes recorded (the config file edit is the human's; apply verifies config_sha256 matches the decision), CA flags cleared ; expires overdue provisional decisions (status 'reverted') ; sets applied_on
def expire_stale(conn, as_of: str, cfg) -> int                                      # proposals older than 3 months without decision -> 'expired'
# quant/knowledge/adr.py
def write(conn, decision_id: str) -> Path                                          # knowledge/decisions/D-YYYY-MM-NN-<slug>.md from the §9.3 template ; idempotent
def check_all_have_adr(conn) -> list[str]
# quant/knowledge/report.py
def render(conn, as_of: str, cfg) -> Path                                          # knowledge/reports/YYYY-MM.md per §9.7 ; verdict line ; every IC row carries n, n_eff, CI ; alpha word only if alpha_word_allowed ; footer ; writes learning_curve_YYYY-MM.json
def render_backfill_block(conn, cfg) -> str                                        # separate table with the survivorship caveat
# quant/knowledge/lessons.py
def add(conn, text: str, refs: list[str] | None = None, decision_id: str | None = None, source: str = 'review', tags: str = '') -> int   # + append to knowledge/lessons.md
```

## 7. Deliverables
`quant/knowledge/{__init__,registry,review,proposals,adr,report,lessons,templates.py}`, `quant/commands/kb.py` (`kb hypothesis new|list`, `kb propose --as-of D`, `kb queue`, `kb approve P --by A [--note]`, `kb reject P --by A --note`, `kb ratify D --by A`, `kb apply --as-of D`, `kb report --as-of D`, `kb lesson add TEXT`, `kb decision new ...` for manual Tier-2 decisions), `knowledge/README.md`, `knowledge/lessons.md` (seeded with 8–10 lessons from the red-team review, each linked to its section), `knowledge/decisions/ADR-TEMPLATE.md`, `knowledge/decisions/D-2026-09-01..07-*.md` (legacy retirements; WS10 references them), `knowledge/decisions/D-2026-09-08-launch-factor-set.md` (ADR-001: launch set exempt from the 2026 budget), `tests/unit/test_knowledge.py`, `tests/unit/test_review.py`, `tests/unit/test_proposals.py`, `tests/unit/test_report.py`, PROGRESS entry.

## 8. Implementation plan
1. Templates module with the hypothesis, ADR, proposal and report skeletons as Python strings (no Jinja).
2. `registry.new_hypothesis`: id allocation by year; budget check (`counts_toward_budget`, per-family cap); `first_oos_as_of >= registered_on` enforced; markdown written.
3. `review.criteria_for_factor`: read cumulative rows (`mean_ic`, `hac_t`, `n`, `n_eff`, `hit_rate`) at `horizon_m = 3` for `window_end = as_of` from `evaluations`; 12M sign check from the 12M cumulative row when `n >= 3`; partial IC from per-month rows; correlation from `metrics.corr_matrix` on the latest z frame; coverage from `factor_values.flags` over the last 3 runs; ablation via a helper that recomputes the champion's cumulative 3M IC with the candidate added (compose on stored z + stored labels); `t_crit(budget_status(...)['m_trailing_24'])`; returns booleans per criterion and `next_review` (the month the count criterion would first be satisfiable).
4. `proposals.draft`: promotion/probation/retirement from criteria; `quarantine` from W3×3 events; `clear_ca_flag` per open `SUSPECTED_UNRECORDED_CA`; `rule_change` hint when TOP30 turnover > 30% one-way for 3 months; `register_hypothesis` drafts recommended by rules (never auto-registered); dedupe; write the month's proposals markdown with evidence tables and an empty "LLM first-reader checklist" section.
5. `approve/reject/ratify/apply` per contract; `apply` writes `effective_from = next as_of` and calls `bump_version` where needed; `provisional` past `llm_ratification_days` → `reverted` and the underlying status change undone at the next apply.
6. `adr.write` from the decision row; `check_all_have_adr` for CI.
7. `report.render`: assemble sections 1–12 from tables; verdict line; IC formatting helper that refuses to print an IC without `n` and CI; the alpha-word rule; footer; JSON for the learning curve.
8. Seed files: lessons from the red-team review (units, momentum attribution, idempotency, near-constant factors, split contamination, sentiment outside budget, 5-level buckets, 1M objective mismatch); legacy retirement ADRs with context sections quoting the review numbers; ADR-001 launch set.
9. Commands; tests; PROGRESS; commit `WS09: knowledge base and governance`.

## 9. Tests you must write
```
tests/unit/test_knowledge.py::test_hypothesis_id_and_sequence_allocation
tests/unit/test_knowledge.py::test_budget_refuses_seventh_and_per_family_cap
tests/unit/test_knowledge.py::test_withdrawn_keeps_sequence_number
tests/unit/test_knowledge.py::test_first_oos_not_before_registration
tests/unit/test_knowledge.py::test_jsonl_mirror_roundtrip
tests/unit/test_review.py::test_factor_promotion_criteria_on_synthetic            planted factor passes after >= 12 labelled months ; noise factor does not
tests/unit/test_review.py::test_t_crit_uses_trailing_24_month_count
tests/unit/test_review.py::test_probation_and_retirement_rules
tests/unit/test_review.py::test_correlation_and_coverage_rules
tests/unit/test_review.py::test_model_criteria_wrap_review_p1_p5
tests/unit/test_proposals.py::test_draft_dedupes_and_writes_markdown
tests/unit/test_proposals.py::test_llm_cannot_approve_tier2                       Refused
tests/unit/test_proposals.py::test_llm_tier1_refused_when_any_criterion_false
tests/unit/test_proposals.py::test_provisional_then_ratified_or_reverted_after_60_days
tests/unit/test_proposals.py::test_apply_effective_next_as_of_never_retroactive
tests/unit/test_proposals.py::test_apply_bumps_model_version_on_activation
tests/unit/test_proposals.py::test_every_decision_has_adr
tests/unit/test_proposals.py::test_cost_model_tier_boundary_25pct
tests/unit/test_report.py::test_report_renders_all_sections_from_synthetic_db
tests/unit/test_report.py::test_no_ic_without_n_and_ci
tests/unit/test_report.py::test_alpha_word_only_when_allowed
tests/unit/test_report.py::test_verdict_line_present_and_footer
```

## 10. Verification checklist
- Synthetic world through month 48: `python -m quant kb propose --as-of <m48> --db /tmp/w/quant.db` → at least one `promote_factor` proposal for the planted factor with `criteria_check_json` all true; `kb queue` lists it.
- `python -m quant kb approve P-... --by llm:test --note ok` → decision `provisional`; `kb approve <tier-2 kind> --by llm:test` → exit 3 with a one-line refusal.
- `python -m quant kb apply --as-of <m48>` → factor status `active` effective next as_of; `model_versions` has a new EW_HIER_v1 row; ADR file exists; `kb report --as-of <m48>` renders with the verdict line and no IC lacking `n_eff`.
- `python -m quant kb hypothesis new ...` seven times in one year → seventh exits 3.
- `python -m pytest tests/unit/test_knowledge.py tests/unit/test_review.py tests/unit/test_proposals.py tests/unit/test_report.py -q` green.

## 11. Definition of done
- [ ] Registry, budget, sequence numbers, JSONL mirror
- [ ] Criteria engine matches §9.5/§6.5; t_crit from trailing-24 count
- [ ] Proposals, tiers, provisional/ratify/revert, apply-next-as_of, ADRs
- [ ] Report renders from tables with the alpha-word rule and the footer
- [ ] Seed lessons and legacy ADRs written
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS11's `run monthly` calls `draft`, `render`, `export_jsonl` in steps 11–12 and `apply` only in the manual step 14.
- WS10's migration references the seed ADR ids D-2026-09-01..07; write them before WS10 runs on the real DB (WS10 builds first but uses placeholder ids until this lands — see its doc).
- WS04's `--override-gate` needs an approved Tier-2 decision id; `tier_of('gate_override')` is 2.

## 13. Risks, gotchas
- An LLM approver will find reasons to approve; the code path (criteria all true, provisional, ratification) is the guard — do not soften it.
- Proposals must fire only at review months determined by counts, never because a human saw a good month.
- The report generator must never compute statistics itself; it reads `evaluations` so the numbers in the report equal the numbers in the DB.
