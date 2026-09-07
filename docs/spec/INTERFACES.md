# Canonical V2 public interfaces — revision 2

These are signature and shape contracts, not implementation stubs. Workstream documents reference the contract IDs below. Internal helpers may be added without changing these public signatures. Every timestamp is UTC per MASTER_SPEC §2.1. Every security index is integer security_id; deterministic ordering is ascending ID. Series values are float64 with NaN for missing; persisted NaN becomes SQL NULL. No interface uses a global current database implicitly except configuration loading.

## C00 — Common types and state (WS00)

Implement shared dataclasses in `quant/types.py`. `Config` exposes nested attributes matching every key in contracts/config.toml plus `.policy_sha256`; paths are pathlib.Path. `Clock.now()` returns an aware UTC datetime. `SystemClock` reads the system clock; `FrozenClock` is supplied only in tests. `Actor(kind: Literal['human','llm','system'], name: str)` is explicit, with `.by` equal to kind:name. It is an audit identity, not proof of authentication.

```
Result(status: str, counts: dict[str,int], details: dict[str,object])
Check(id: str, status: Literal['PASS','FAIL','DEFERRED'], observed: object,
      expected: object, reason: str, blocking: bool)
CheckReport(checks: list[Check])       # .passed: no FAIL on blocking checks; .deferred: list of deferred IDs
HacResult(mean: float|None, se: float|None, t: float|None,
          ci_lo: float|None, ci_hi: float|None, n: int, n_eff: float, status: str)
CriteriaCheck(subject_id: str, checks: list[Check], eligible: bool,
              evidence_ids: list[int], next_review: str|None)
World(db_path: Path, prices_db_path: Path, cfg: Config, sessions: DataFrame,
      months: list[str], security_ids: list[int], events: dict[str,object])
Draft(cohort_id: str, as_of: str, track: str, knowledge_cutoff: str,
      definition_hash: str, members: DataFrame, groups: Series,
      source_refs: dict, factor_values: DataFrame, model_weights: DataFrame, scores: DataFrame)
```

Draft tables use canonical schema columns but remain in memory until publish. It is valid for factor_values/model_weights/scores to be empty while a stage is incomplete. `members` columns: security_id, isin, symbol, company_name, nse_sector, series; index security_id. Source references include capture IDs and content hashes. A draft is never counted as a published cohort.

```
quant.config.load(path: str|None = None) -> Config
quant.db.core.connect(path: str, *, readonly: bool = False) -> sqlite3.Connection
quant.db.core.apply_schema(conn: Connection, *, kind: Literal['state','prices']='state') -> None
quant.db.core.append_rows(ctx: RunContext, table: str, rows: DataFrame, keys: list[str]) -> int
quant.db.core.update_control(ctx: RunContext, table: str, key: dict, changes: dict) -> int
quant.db.core.table_hash(conn: Connection, table: str) -> str
quant.db.ledger.export(conn: Connection, ledger_dir: Path) -> list[Path]
quant.db.ledger.rebuild(ledger_dir: Path, output_db: Path) -> None
quant.db.ledger.verify(conn: Connection, ledger_dir: Path) -> CheckReport
quant.db.ledger.size(cfg: Config) -> dict[str,int]
quant.cli.main(argv: list[str]|None = None) -> int
quant.cli.register(group: str, name: str, handler: Callable, help: str) -> None
```

Connection uses sqlite3.Row, FK ON, busy_timeout5000; writable state uses WAL, read-only uses URI mode=ro and never PRAGMA journal_mode=WAL. apply_schema initializes DDL only and validates its hash; registered state bootstrap is C09, not an unrecorded human decision. append_rows permits an equal-key/equal-value no-op; a mismatch raises ImmutableConflict. update_control has an explicit allowlist (runs, proposals, decisions lifecycle, orders status, registry mirrors, securities/symbol/sector validity and model role/valid_to, portfolios.inception on first fill, data_quality_events.resolved_by). Every actual write is journaled in its transaction. Dynamic identifiers are checked against the canonical schema allowlist.

```
quant.run.RunContext(as_of: str, kind: str, track: str, cfg: Config,
                     clock: Clock, actor: Actor)
```

Context manager exposing .conn, .store (None before prices initialized), .cfg, .clock, .actor, .run_id, .as_of, .track, .git_sha. On enter append attempt row; on exit finalize status/finished_at after rolling back any failed staging transaction. .status may be set blocked/partial; exceptions map through quant.errors. Monthly's published-date precheck occurs before entering this context. Reads/tests use separate temporary contexts. No function creates nested monthly attempts.

RunContext also exposes `.checks: dict[str, Callable[[RunContext,Draft],Check]]`, initially empty. WS11 injects G9/G10 callbacks before calling post-compute gates. Tests use explicit callbacks. If an applicable gate has no callback, raise an implementation error; no silent passing fallback. Foundations may leave .store=None until PriceStore exists; neither core nor ledger imports future data/evaluation providers. Use TYPE_CHECKING for forward type references.

Errors in quant/errors.py: Blocked(code,detail), Refused(code,detail), LookaheadError(detail), ImmutableConflict(table,key), SourceChanged(detail). CLI maps blocked2, refused3, other errors1; missing help arguments2 are argparse usage errors and described separately.

## C01 — Calendar, universe and identities (WS00, WS01)

```
quant.data.calendar.Calendar(sessions: DataFrame, timezone: str='Asia/Kolkata')
  .last_session_on_or_before(date: str) -> str
  .month_ends(start: str, end: str) -> list[str]
  .add_sessions(date: str, count: int) -> str
  .next_session_after(date: str) -> str
  .cutoff(as_of: str) -> str
  .first_exec_after(generated_at: str) -> str
quant.data.universe.fetch_list(name: str, cfg: Config, clock: Clock) -> tuple[bytes,dict]
quant.data.universe.parse_list(content: bytes) -> DataFrame
quant.data.universe.capture(ctx: RunContext) -> Result
quant.data.universe.members_at(conn: Connection, cutoff: str, index_name: str='NIFTY500') -> DataFrame
quant.data.identity.upsert_security(ctx: RunContext, isin: str, name: str, symbol: str, observed_at: str) -> int
quant.data.identity.resolve_security_id(conn: Connection, *, isin: str|None=None,
                                       symbol: str|None=None, cutoff: str) -> int|None
quant.data.identity.yahoo_ticker(conn: Connection, security_id: int, cutoff: str) -> str
quant.data.identity.tracked_securities(conn: Connection, cutoff: str, horizons: list[int]) -> list[int]
quant.sectors.taxonomy.assign_groups(members: DataFrame, rules: DataFrame,
                                    yahoo_industry: Series, cfg: Config) -> DataFrame
quant.sectors.taxonomy.capture(ctx: RunContext, members: DataFrame) -> Result
quant.sectors.taxonomy.groups_at(conn: Connection, cutoff: str, security_ids: list[int]) -> Series
quant.sectors.crosswalk.yahoo_to_nse(sector: str, industry: str|None, rules: DataFrame) -> tuple[str,float]
```

Session columns: date ISO, close_at UTC. A missing requested past/future session range raises Blocked(calendar_missing); never treat latest downloaded date as the next session. first_exec_after returns the next session's close timestamp strictly after generated_at's IST date. fetch_list metadata: url,captured_at,source_version,sha256; transport failure raises an error for caller fallback, never returns stale bytes labeled fresh. parse_list output matches Draft.members. assign_groups output: security_id,nse_sector,sector_group,macro_sector,merged_from; unclassified is explicit.

## C02 — Yahoo captures and PIT reads (WS03)

```
RawBundle(ticker: str, fetched_at: str, info: dict,
          statements: dict[str,DataFrame], earnings_dates: DataFrame, errors: list[str])
quant.data.yahoo.YahooClient(cfg: Config, clock: Clock, sleep: Callable[[float],None])
  .bundle(ticker: str) -> RawBundle
  .download_batch(tickers: list[str], start: str, end: str) -> DataFrame
  .archive(bundles: list[RawBundle], ctx: RunContext) -> str
quant.data.yahoo.normalize_info(info: dict, close: float|None) -> tuple[dict,list[str]]
quant.data.fundamentals.available_from(period_end: str, freq: str, fetched_at: str,
                                      earnings_dates: DataFrame, calendar: Calendar) -> tuple[str,str]
quant.data.fundamentals.ingest(ctx: RunContext, bundles: dict[int,RawBundle]) -> Result
quant.data.fundamentals.pit_frame(conn: Connection, cutoff: str, statement: str, field: str,
                                 freq: str, n_periods: int, security_ids: list[int]) -> DataFrame
quant.data.fundamentals.ttm(conn: Connection, cutoff: str, field: str,
                           security_ids: list[int], offset_quarters: int=0) -> tuple[Series,Series]
quant.data.holdings.capture(ctx: RunContext, bundles: dict[int,RawBundle]) -> Result
quant.data.holdings.series(conn: Connection, cutoff: str, lag_runs: int, security_ids: list[int]) -> Series
quant.data.attributes.capture(ctx: RunContext, bundles: dict[int,RawBundle]) -> Result
quant.data.attributes.at(conn: Connection, cutoff: str, field: str, security_ids: list[int]) -> Series
quant.data.capture.run(ctx: RunContext, client: YahooClient) -> Result
```

download_batch columns use MultiIndex(ticker, source_field); sessions are ISO-indexed and source adjustment metadata is carried in DataFrame.attrs before archiving. archive returns capture_id. pit_frame rows are security_id, columns integer period_rank0..n-1, newest admissible fiscal period first; attrs contains aligned period_end and source primary-key references. Annual and quarterly frames are always separate. ttm returns(values,flags), with comma-separated flag strings; it requires consecutive fiscal quarters and exact offset. normalize_info returns canonical numeric/text keys used by security_attributes plus inst_pct/insider_pct and debt_to_equity for checks; no dividendYield input. Capture persists actual time from bundles, never ctx.as_of. The capture orchestrator also archives the raw price windows via download_batch; normalized ingestion is supplied by C03 after that provider exists. WS03 validates archiving with a fake transport and does not claim the integrated capture command accepted until WS11.

## C03 — Prices, actions and benchmarks (WS02)

```
quant.data.prices.PriceStore(path: Path, state_conn: Connection)
  .ingest(ctx: RunContext, source: DataFrame, metadata: dict) -> Result
  .backfill(ctx: RunContext, client: YahooClient, tickers: dict[int,str], start: str, end: str) -> Result
  .update(ctx: RunContext, client: YahooClient, security_ids: list[int], through: str) -> Result
  .reconcile(ctx: RunContext, normalized: DataFrame) -> Result
  .close_raw(security_ids: list[int], start: str, end: str, vintage_at: str) -> DataFrame
  .close_split(security_ids: list[int], start: str, end: str, vintage_at: str) -> DataFrame
  .tri(security_ids: list[int], start: str, end: str, vintage_at: str) -> DataFrame
  .volume(security_ids: list[int], start: str, end: str, vintage_at: str) -> DataFrame
  .adv_inr(security_ids: list[int], as_of: str, vintage_at: str, window: int=63) -> DataFrame
  .manifest_write(output: Path, vintage_at: str) -> str
  .manifest_verify(manifest: Path) -> CheckReport
quant.data.prices.normalize_source(source: DataFrame, metadata: dict) -> DataFrame
quant.data.prices.monthly_panel(ctx: RunContext, draft: Draft) -> DataFrame
quant.data.actions.detect(ctx: RunContext, security_ids: list[int], since: str) -> Result
quant.data.actions.add(ctx: RunContext, isin: str, ex_date: str, kind: str,
                       factor: float, decision_id: str) -> Result
quant.data.actions.clear(ctx: RunContext, event_id: int, decision_id: str) -> Result
quant.data.actions.accept_revision(ctx: RunContext, capture_id: str, decision_id: str) -> Result
quant.data.benchmarks.update(ctx: RunContext, through: str) -> Result
quant.data.benchmarks.series(conn: Connection, benchmark_id: str, start: str, end: str, known_at: str) -> Series
```

Read frames have date index, security_id columns; no forward fill. adv_inr returns security-indexed adv_63_inr,n_days_63. normalized frame contains the price_schema.sql fields plus source adjustment metadata in its archive. TRI reads approved actions from state_conn and selects only accepted price observations available by vintage_at. For live replay, vintage_at comes from frozen input_refs, not wall-clock now. Close_split is rebased to end with only that version's splits. monthly_panel returns state-schema columns in memory, publication-owned. Data decision APIs validate actual decision authority through stored rows (C09 implementation is integrated before real mutation commands run); WS02 tests create explicit synthetic decisions.

## C04 — Data quality (WS04)

```
quant.data.contracts.field_contracts(cfg: Config) -> dict[str,dict]
quant.data.contracts.check(values: Series, contract: dict) -> tuple[Series,Check]
quant.data.contracts.psi(current: Series, reference: Series, edges: list[float]) -> float|None
quant.data.gates.record_event(ctx: RunContext, code: str, severity: str, detail: dict,
                              security_id: int|None=None, field: str|None=None) -> int
quant.data.gates.run(ctx: RunContext, draft: Draft, phase: Literal['pre','post'],
                     *, strict: bool=True) -> CheckReport
```

check returns a masked COPY plus Check; raw input rows stay unchanged. pre implements G1-G7; post consumes actual draft.factor_values and scores for G8-G10. Deterministic absent dependencies are implementation errors, not DEFERRED. Lack of historical cohorts is a declared DEFERRED. strict raises Blocked only after every applicable gate result is persisted. G9 replay and G10 evaluation hooks are dependency-injected callbacks from WS11, tested with fixtures before those providers exist.

## C05 — Factors (WS05)

```
FactorSpec(name: str, version: int, family: str, direction: int, horizon_m: int,
           hypothesis: str, formula: str, inputs: tuple[str,...], lookback_days: int,
           applies_to_financials: bool, level: Literal['stock','sector'], backfillable: bool,
           min_coverage: float, evidence: str, hypothesis_id: str)   # .factor_id = name@version
Factor.compute(inputs: FactorInputs) -> Series
quant.factors.inputs.build(ctx: RunContext, draft: Draft) -> FactorInputs
FactorInputs.as_of: str; .cutoff: str; .members: Index; .sector_group: Series
FactorInputs.tri(lookback_days: int) -> DataFrame
FactorInputs.close_split(lookback_days: int) -> DataFrame
FactorInputs.close_raw(lookback_days: int) -> DataFrame
FactorInputs.volume(lookback_days: int) -> DataFrame
FactorInputs.attribute(field: str) -> Series
FactorInputs.fundamental(statement: str, field: str, freq: str, n_periods: int) -> DataFrame
FactorInputs.ttm(field: str, offset_quarters: int=0) -> Series
FactorInputs.holdings(lag_runs: int=0) -> Series
FactorInputs.adv_inr() -> Series
FactorInputs.benchmark_tri(symbol: str, lookback_days: int) -> Series
FactorInputs.provenance() -> dict
quant.factors.standardise.transform(raw: Series, groups: Series, direction: int, cfg: Config) -> DataFrame
quant.factors.registry.sync(ctx: RunContext, definitions: list[FactorSpec]) -> Result
quant.factors.registry.compute_all(ctx: RunContext, draft: Draft) -> DataFrame
quant.factors.registry.values_frame(conn: Connection, cohort_id: str, factor_ids: list[str]) -> DataFrame
quant.factors.sector.compute(inputs: FactorInputs, cfg: Config) -> DataFrame
```

FactorInputs contains pre-filtered copies and source references. It exposes no labels, connection or network client. Undeclared field/future data requests raise LookaheadError. A factor cannot request more than its registered lookback/inputs; excessive lookback returns NaN if source history is legitimately short but refuses undeclared access. transform returns raw,winsor,z,flags indexed security_id. It implements MASTER_SPEC §5.2 including ties/constants, not unit-variance z-scores. compute_all returns state-schema factor rows without publishing. values_frame returns a security x factor_id z matrix with flags/provenance in attrs. sector.compute returns sector_group,feature_id,value,n_members. A family module can contain multiple Factor objects; its dependency hashes are pinned per affected version.

## C06 — Models and scores (WS06)

```
quant.model.learn.allocate_units(target: dict[str,float], floor_mult: float=.5,
                                 cap_mult: float=2., total: int=10000) -> dict[str,int]
quant.model.learn.fit_family_weights(ic_hist: DataFrame, cfg: Config) -> tuple[dict[str,int],dict]
quant.model.composite.compose(z: DataFrame, definitions: DataFrame, units: dict[str,int],
                              groups: Series, cfg: Config, *, mode: str='hierarchical',
                              sleeve: Series|None=None, sleeve_weight: float=0.) -> DataFrame
quant.model.screens.apply(ctx: RunContext, draft: Draft, scores: DataFrame, model_id: str) -> DataFrame
quant.model.models.definition_at(conn: Connection, model_id: str, as_of: str) -> dict
quant.model.models.seed(ctx: RunContext, bootstrap_decision_id: str) -> Result
quant.model.models.score_all(ctx: RunContext, draft: Draft, family_ic_history: DataFrame) -> tuple[DataFrame,DataFrame]
quant.model.models.check(conn: Connection) -> CheckReport
```

ic_hist is date-indexed with one column per included family and only preselected admissible observations from C07; fit drops incomplete common dates and reports n_months,n_eff,alpha,gate,means,omitted_dates. No database access in fitting. definitions frame: factor_id,family,status_weight,applicable boolean matrix metadata; use version-frozen definitions. compose returns family_scores_json,composite,composite_neutral,sector_tilt,final,scored,n_factors_used and ranking columns. mode flat averages factor values directly; momentum has its registered coverage exception. score_all returns (scores,model_weights) in canonical state shapes. It receives evidence explicitly; no guarded import of future evaluation modules. Model criteria/review/version changes belong to C09, eliminating the model->evaluation cycle.

## C07 — Labels, statistics and evaluations (WS07)

```
quant.evaluation.labels.mature(ctx: RunContext, through: str) -> Result
quant.evaluation.labels.frame(conn: Connection, cohort_id: str, horizon_m: int,
                              known_at: str, *, scope: str='all', model_id: str='EW_HIER_v1') -> DataFrame
quant.evaluation.stats.hac_mean_test(x: list[float], lag: int) -> HacResult
quant.evaluation.stats.block_bootstrap_ci(x: list[float], block: int, n: int=1000,
                                         q: float=.90, seed: int=0) -> tuple[float|None,float|None]
quant.evaluation.stats.t_crit(m: int, looks: int=3, alpha: float=.05, floor: float=2.) -> float
quant.evaluation.stats.wilson(k: int, n: int, z: float=1.645) -> tuple[float|None,float|None]
quant.evaluation.metrics.rank_ic(score: Series, label: Series) -> tuple[float|None,int,str]
quant.evaluation.metrics.quintiles(score: Series, returns: Series, groups: Series) -> DataFrame
quant.evaluation.metrics.partial_ic(candidate: Series, active: DataFrame, label: Series) -> tuple[float|None,int,str]
quant.evaluation.walkforward.family_ic_history(conn: Connection, model_version: dict,
                                              as_of: str, known_at: str) -> DataFrame
quant.evaluation.evaluate.run(ctx: RunContext, through: str, track: str) -> Result
quant.evaluation.evaluate.ic_series(conn: Connection, subject_kind: str, subject_id: str,
                                    subject_version: str, horizon_m: int, scope: str,
                                    track: str, through: str, known_at: str) -> Series
quant.evaluation.leakage.run(ctx: RunContext, draft: Draft|None) -> CheckReport
quant.evaluation.curves.update(ctx: RunContext, through: str) -> Result
quant.evaluation.backfill.replay(ctx: RunContext, start: str, end: str) -> Result
```

labels.frame columns security_id,l_rel,r_log,r_arith,status,sector_group,eligible,revision,evidence_hash. Select latest revision known_at per key before applying status filters: an excluded revision cannot resurrect a superseded valid one. quintiles output q,n,mean,median,trimmed_mean pooled after within-group assignment. Metric undefined results use None with status, not zero. family_ic_history uses stored model/factor versions, clean live cohorts, completed endpoints and contemporaneously known evaluation revisions. It cannot read backfill or current active sets. Curves never refit published history. Evaluation of net paper spread is an optional explicit input from C08; absent book evidence yields N/A, not zero cost.

## C08 — Portfolios (WS08)

```
quant.portfolio.costs.bucket(adv_inr: float, cfg: Config) -> str
quant.portfolio.costs.cost_bps_one_way(bucket: str, cfg: Config, stress: bool=False) -> float
quant.portfolio.construct.rebalance(previous: DataFrame, ranks: Series, eligible: Series,
                                    groups: Series, buckets: Series, cfg: Config,
                                    rule: str='top30_buffer') -> tuple[DataFrame,DataFrame]
quant.portfolio.paper.plan(ctx: RunContext, cohort_id: str) -> Result
quant.portfolio.paper.settle(ctx: RunContext, through: str) -> Result
quant.portfolio.paper.roll_forward(ctx: RunContext, through: str) -> Result
quant.portfolio.paper.net_selection_spread(conn: Connection, cohort_id: str,
                                          subject_kind: str, subject_id: str,
                                          subject_version: str, horizon_m: int) -> dict
quant.portfolio.scoreboard.compute(conn: Connection, through: str, cfg: Config) -> DataFrame
```

previous frame: security_id,weight,entry_as_of,group; cash has security_id0 in calculation only (not persisted as a security). Return positions(security_id,target_weight,entry_as_of) and deltas(security_id,weight_delta,side); unsettled orders are not positions. Rules top30_buffer,decile,ew_universe,tranche0..2. Bucket C cap and cash residual are explicit. paper.plan stores pending orders with actual created_at and earliest_exec_at; settle alone writes unique fills. Roll-forward derives NAV from dated fills and actions, not previous month-end target weights. net_selection_spread returns value,n_cost_events,status,execution_start,execution_end,evidence_refs; absent simulation status unavailable. Scoreboard returns portfolio_id,window_start,window_end,n_months,ret_net,excess_net,ir,hac_t,ci_lo,ci_hi,turnover,cost_drag,max_drawdown,verdict,evidence_refs; unavailable numeric values are NULL and verdict insufficient.

paper.plan also creates the independent factor/model TOP_Q20 and MATCHED_EW attribution books in MASTER_SPEC §8, using rule names cohort_top_quintile/cohort_matched_ew. For factor books model_id is NULL and subject_kind/subject_id/subject_version identify the factor. Entry/exit orders pin cohort, book and actual intended timestamps; no subsequent ranking can rebalance that fixed cohort book. Pending pair evidence is explicitly unavailable to review.factor.

Order purpose is rebalance, entry or exit and belongs to its natural key. A cohort book may have one entry and one exit per security; a retry cannot add a second of either. Exits have target_weight=0 and operate on the quantity actually held, after corporate actions. Never settle an exit for an unfilled/cancelled entry; an expired pair cancels both pending legs. Rolling books use purpose=rebalance. Order status changes are journaled; keys, original targets and intended timestamps are immutable.

## C09 — Knowledge and bootstrap (WS09)

```
quant.knowledge.bootstrap.seed(ctx: RunContext, spec_sha256: str) -> Result
quant.knowledge.registry.new_hypothesis(ctx: RunContext, fields: dict) -> str
quant.knowledge.registry.budget_status(conn: Connection, year: int, horizon_m: int) -> dict
quant.knowledge.review.factor(conn: Connection, factor_id: str, as_of: str, cfg: Config) -> CriteriaCheck
quant.knowledge.review.model(conn: Connection, model_id: str, as_of: str, cfg: Config) -> CriteriaCheck
quant.knowledge.proposals.draft(ctx: RunContext, as_of: str) -> list[str]
quant.knowledge.proposals.approve(ctx: RunContext, proposal_id: str, note: str) -> str
quant.knowledge.proposals.reject(ctx: RunContext, proposal_id: str, note: str) -> str
quant.knowledge.proposals.ratify(ctx: RunContext, decision_id: str, note: str) -> Result
quant.knowledge.proposals.apply(ctx: RunContext, as_of: str) -> Result
quant.knowledge.proposals.authorize(conn: Connection, actor: Actor, decision_id: str,
                                    kind: str, subject_id: str, now: str) -> None
quant.knowledge.adr.write(conn: Connection, decision_id: str, output_dir: Path) -> Path
quant.knowledge.adr.check(conn: Connection, knowledge_dir: Path) -> CheckReport
quant.knowledge.report.render(conn: Connection, as_of: str, cfg: Config,
                              *, snapshot: dict|None=None) -> Path
quant.knowledge.report.render_backfill(conn: Connection, as_of: str, cfg: Config,
                                       *, snapshot: dict|None=None) -> Path
```

Hypothesis required fields are all NOT NULL columns in canonical hypotheses table except generated ID/sequence/md_path; family, review_opportunities_json and formula_sha256 are stored in the hypothesis row and mirrored into its markdown. Compare first_oos_as_of using Calendar.cutoff(first_oos_as_of) against the actual registered_on timestamp, not a date-string comparison. Bootstrap seeds only the exact master launch set, ledgered with system actor and spec hash. No unrestricted Tier2 power is delegated. Approval reads actor from ctx, not a second contradictory by argument. apply stores prospective versions and actual evidence; expires provisional authority on real UTC time, appending reversion records. Tier1 data revisions may be used now but old outputs persist. Report numbers come from stored evaluation/portfolio views, not handwritten prose.

Report snapshot shape and content-addressed paths are MASTER_SPEC §9.5. With snapshot=None, select and freeze the current persisted evidence/control-state view; known_at is the latest included recorded timestamp, not an unpinned call to wall time. With a supplied snapshot, use exactly its references and renderer version or fail reproducibility. Report rendering writes immutable markdown/manifest artifacts and an optional navigation index; it never mutates the database. verify.report checks every archived snapshot for the requested month. Never overwrite a different artifact under the same report_id.

## C10 — Legacy migration (WS10)

```
quant.migrate.legacy.run(ctx: RunContext, legacy_db_path: Path, *, dry_run: bool=False) -> Result
quant.migrate.legacy.build_sample(source: Path, output: Path, tickers: list[str]) -> Result
quant.migrate.legacy.reconcile(conn: Connection, legacy_db_path: Path) -> DataFrame
```

Second migration returns counts0,status unchanged. Source hashes/counts recorded before/after; dry_run never writes to either DB, filesystem or Git. reconcile output: transition,metric,legacy_expected,recomputed_original,adjusted_descriptive,difference,status. Compare original attribution within.01 only on the full frozen source; a20-security subset is not expected to reproduce the full-universe statistic. It also prints actual counts and uncertainty limitations. Stored monthly legacy labels use normal month-end horizons, not irregular adjacent snapshots.

## C11 — Orchestration and UI (WS11)

```
quant.run.monthly(cfg: Config, clock: Clock, actor: Actor, *, as_of: str|None=None,
                  skip_capture: bool=False, stop_after: str|None=None,
                  dry_run: bool=False, commit: bool=False, push: bool=False) -> int
quant.ui_export.export(conn: Connection, cfg: Config) -> list[Path]
quant.verify.report(conn: Connection, as_of: str, cfg: Config) -> CheckReport
quant.verify.pit(conn: Connection, months: int, cfg: Config) -> CheckReport
quant.status.read(conn: Connection, cfg: Config) -> dict
```

UI files: data.js ranking, data_learning.js curves, data_scoreboard.js portfolios, data_factors.js diagnostics, data_kb.js decisions. Inferential records contain value,n,n_eff,method,ci_lo,ci_hi,uncertainty_status,track,subject_version,evidence_hash; unavailable bands are permitted only with the documented status and explicit visible label. Export raises on a claimed estimable interval whose endpoints are absent. verify.report re-renders from the exact report evidence references, not whatever revisions are newest today. status includes last publication, last capture, blocked reason, pending orders/proposals, overdue ratifications, next possible maturities and source archive availability.
