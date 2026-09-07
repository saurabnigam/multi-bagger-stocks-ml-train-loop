# V2 Quant Engine — implementation specification, revision 2

Revised 2026-09-07 after the implementation-readiness review. This is a specification, not a claim that V2 is implemented or profitable. The owner authorized this documentation correction pass; the frozen legacy engine remains governed by the root AGENTS.md.

## 0. Authority, scope and implementation procedure

Build the complete V2 research engine in this repository with Python 3.11+, SQLite, pandas, numpy, scipy and yfinance. Keep the browser UI vanilla HTML/JS/CSS; vendor the existing Chart.js dependency, remove remote fonts, and add no frontend build tool. No new runtime dependencies. Use repository-relative paths and environment overrides. Read `docs/analysis/red_team_review.md` for the historical evidence caveats.

The normative package has four sources with distinct responsibilities:

1. This document owns behavior, formulas, policies and defaults.
2. `docs/spec/contracts/schema.sql`, `price_schema.sql`, and `config.toml` own exact DDL and configuration. WS00 copies them to the production paths without changing semantics.
3. `docs/spec/INTERFACES.md` owns exact public signatures and return shapes. Workstreams reference contract IDs; they do not redefine signatures.
4. `docs/spec/TEST_AND_VERIFICATION_PLAN.md` and `contracts/golden_cases.json` own acceptance procedures and fixed examples.

`subagents/_workstreams.json` owns task order, dependencies, file ownership and task IDs. The workstream documents explain implementation and tests. `00_context_brief.md`, `docs/analysis/` and `docs/spec/drafts/` provide historical context, not alternative contracts. Superseded prose and old progress entries do not override revision 2.

If these normative sources disagree, record the smallest reproducible contradiction and stop only the affected task. Do not guess a new model policy, silently edit an acceptance expectation, or claim a dependency is complete because a stub imports. Ordinary implementation choices within these contracts require no further approval. The build/resume prompt is in HANDOFF.md.

This pass changes V2 design rules only. Leave all legacy source files and quant_engine.db byte-identical; do not move the legacy modules during this build. New code may import tested pure legacy helpers, but must never execute the legacy acquisition loop. WS11 updates the root operating manual to distinguish V2 from legacy invariants, not to rewrite historical evidence.

## 1. Objective, evidence and success

### 1.1 Objective

Rank the current Nifty 500 universe monthly using continuous factors standardized within sector groups. Store enough provenance to reproduce each published ranking. Evaluate future total returns and change definitions only through recorded decisions. Learning is an empirical question; no rising learning curve or positive return is guaranteed.

The learning target is 3-month sector-relative log total return. The thesis target is 12-month sector-relative log total return. Track 1, 3, 6, 12, 24 and 36 months. The slow KPI is 36-month doubling lift. No trading orders go to a broker; portfolios are paper portfolios only.

### 1.2 Targets and counts

Targets, never build-pass conditions: cumulative live 3M IC +0.02 by 24 clean calendar months; +0.03 by 36, with uncertainty reported. The 12M target is +0.04 by 36 months. Net top-30 return should exceed the matched EW universe; it may fail. Missing active inputs should be below 15% at the first annual review and below 10% thereafter. Measure gate pass rates and source coverage rather than promising them.

Let the first published clean live cohort be month 1. With no missing months, its 3M label first appears in month 4, its 12M label in month 13, and its 36M label in month 37. There are 12 matured 3M cohorts at month 15. These are earliest calendar opportunities, not scheduled promotions or guaranteed changes in weights. Failed captures, missing labels, family coverage and publication delays postpone eligibility.

`n_eff_overlap = N / horizon_m` is a conservative overlap convention, not an estimate of the actual number of independent observations. Output uses the field `n_eff` with this definition printed. It ignores additional persistence and regime dependence; HAC sensitivity and bootstrap results accompany eligible statistics.

### 1.3 Falsification and honest outcomes

At month 12, repeated leakage failures stop new live publication until corrected. At month 24, nonpositive 3M and 6M IC triggers a proposal to freeze new factor registrations for 12 months. At month 36, a 90% IC band wholly below +0.01 together with net top-30 underperformance triggers a review of the fundamental thesis. At month 48, no improvement by the challenger over at least 24 paired eligible cohorts triggers a proposal to retire the weight-learning rule. These policy changes require decisions, not automatic edits to thresholds.

A negative backfill or live result is a valid research result. Investigate unexpected results using independent reconciliation, but never tune the system to make a sign-off statistic positive. No historical or synthetic observation is promoted to live evidence.

### 1.4 Two curves

An evidence curve accumulates results for a fixed subject version and track. A learning curve uses weights actually stored for a historical live cohort and its subsequent realized label, alongside EW on the same dates. Never refit historical live weights with current knowledge to populate this curve. Counterfactual refits use a separate track. Model and factor version changes are annotated; a mixed-version series is explicitly labeled an operational strategy series, not a fixed-model test.

## 2. Time, labels and execution

### 2.1 Clock contract

Dates are ISO dates; all observed/created/published timestamps are UTC strings `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Inject a clock in tests. Preserve actual capture timestamps; a CLI `--as-of` flag never changes them.

`as_of` is the last completed NSE session of the target month. `knowledge_cutoff` is that date at 23:59:59.999999 Asia/Kolkata converted to UTC. Thus a late-night archive capture after market close can be used, but next-month captures cannot be backdated. Future as_of values are refused. Daily closes must be from completed sessions.

The calendar reads an explicit session table/DataFrame supplied to its constructor; fetching is outside the calendar. Fallback weekday/holiday data is versioned and flagged. Missing holiday years block live publication until the session calendar is verified; synthetic tests use their declared weekday calendar. Future order dates use the verified calendar, not the last date in an already downloaded price series.

### 2.2 Bootstrap and monthly timing

Run `python -m quant data capture` at least once before the intended first cutoff, preferably during the last trading week and again after the last session closes. It archives constituent lists, attributes, holdings, statements and the price input windows at the actual capture time. This requires no earlier scoring cohort. Registration of launch hypotheses and the bootstrap decision must also precede the first live cutoff.

The monthly runner executes in the following month. It archives fresh observations for the NEXT cutoff, updates price histories, and selects only admissible pre-cutoff captures for the target cohort. Fundamentals, holdings, attributes, sector classifications and membership all enforce observed time <= cutoff. A first run without sufficient earlier captures is `BLOCKED: BOOTSTRAP_REQUIRED`, records diagnostics and creates no live score cohort. Capture now and target a later month; do not fill the gap from current statements.

The earliest first live date is the first month-end after bootstrap with sufficient verified inputs; September 2026 is an example, not a hard-coded acceptance date. Migration is never a substitute for clean V2 captures.

### 2.3 Labels

For cohort c at t, horizon h, endpoint is the last NSE session of month t+h. Freeze the cohort's universe and groups at publication:

```
r_log(i,t,h) = log(TRI(i,end) / TRI(i,t))
r_arith = exp(r_log) - 1
r_group_median = median(r_log of the cohort members in the same frozen group)
l_rel = r_log - r_group_median
r_uni = r_log - median(r_log of all cohort members)
```

Mature only after the endpoint session has completed. Follow every original member, including unscored/ineligible and later-dropped names. The expected label universe is the cohort membership, not today's members or only names with finite scores. Missing/CA exclusions remain explicit rows; the IC sample is the finite intersection and counts exclusions by reason.

Confirmed delisting/permanent halt uses last available TRI as `delisted_partial`; publish a separate -50% terminal-return sensitivity. A missing quote alone is not proof of delisting. Unconfirmed suspension is `suspended` and excluded until resolved. An unresolved corporate action yields `excluded_ca`; never assume zero return. Missing data yields `missing`.

Labels carry cohort_id, track and revision. `backfill` and `live` at the same date cannot share a label key because their membership and groups may differ. The evidence hash includes all group members' price versions, statuses, endpoint and label-policy version: changing one return can revise other members' group medians too. Append new label revisions for the complete affected group and regenerate dependent evaluations atomically. Earlier revisions remain queryable.

Legacy adjacent-snapshot returns have irregular durations. Calculate them only in the migration reconciliation table; do not store them as monthly h=1 labels. Legacy monthly labels use the same calendar horizon rule as other tracks.

### 2.4 Paper execution

Each score batch records `generated_at`. An order may execute only at the close of the first verified session strictly AFTER the IST date on which that batch was generated. This intentionally gives a full session of decision latency. A ranking generated on the fifth of the following month cannot receive a fill on the first.

An order whose execution close is unavailable stays pending. Do not move its date backward or write a synthetic fill. `portfolio settle --through DATE` processes available completed sessions later; the next monthly run also settles pending orders. Trades have a unique order_id, so settlement is idempotent. Returns begin at actual fills and include prior holdings/cash through the intervening sessions. No new portfolio gets a pre-inception return.

## 3. Universe, identity and sectors

### 3.1 Captures

Fetch the Nifty 500 CSV and optional Nifty200 Momentum 30 / Quality 30 constituent files from the configured Nifty Indices endpoints. Archive exact bytes with UTC capture time, source URL and SHA256. Parse the five named headers: Company Name, Industry, Symbol, Series, ISIN Code. Require unique ISIN and symbol, and at least 480 Nifty 500 rows for live capture acceptance. Optional benchmark failures are reported, not substituted silently.

Use the newest validated capture observed by the cutoff. Fallback is the previous admissible capture, WARN after one missed monthly refresh and BLOCK after 62 days. An unchanged checksum is not proof of staleness: an index review may legitimately leave a list unchanged. Report source timestamp and elapsed days instead. Never save a newly fetched file under a fabricated earlier capture date.

### 3.2 Identity

Key securities by ISIN and retain symbol history with effective dates. A rename with the same ISIN opens a symbol version. A changed ISIN is a new security until an explicit corporate-action relationship is approved; do not infer equivalence from a similar name. Accept symbols containing ampersands and hyphens. Name files by capture ID or ISIN, not raw symbols.

Tracked securities are current members UNION members of any published cohort with at least one configured unmatured horizon UNION open orders/positions. Derive open horizons from cohorts even when no labels row exists yet. Do not prune dropped names after only one horizon matures.

### 3.3 Taxonomy and groups

Use the constituent CSV Industry text as the canonical NSE sector field for this adapter version; archive the observed distinct values rather than asserting there are always exactly twenty. Preserve Yahoo sector/industry as captured attributes and maintain a versioned crosswalk for diagnostics.

Launch with Financial Services as one group. Implement an optional later split: Bank -> FS_BANKS; Credit Services/Mortgage/Financial Conglomerates/Insurance -> FS_LENDERS; remaining Financial Services -> FS_MARKETS. Enable it only through a human Tier-2 taxonomy decision with before/after coverage, not automatically in month 3.

Merge Textiles with Consumer Durables, Media Entertainment & Publication with Consumer Services, and Diversified with Services. Telecommunication stays separate at >=8 members, otherwise joins Services & Diversified. Any other group below 8 uses its configured merge target or OTHER. Merge decisions depend only on membership at the cutoff. An unknown NSE sector is UNCLASSIFIED until mapped; it does not silently map to OTHER. More than 1% unclassified live members blocks publication.

Fallback classification for a tracked dropped name is its last admissible NSE mapping up to 24 months old, then a versioned Yahoo crosswalk (flagged, not clean), then UNCLASSIFIED. Store definition version and actual observation timestamp. Published cohorts freeze their group map. Replay never uses current classifications for historical live scores.

### 3.4 Sector features

Compute sector_mom_6m = EW member log TR over t-6m to t-1m; sector_breadth_200 = share above SMA200; sector_flow_proxy = median three-capture holdings change; sector_val_spread = median earnings yield minus universe median; sector_dispersion = SD of member 3M returns. All are diagnostic/shadow at launch. Flow requires four eligible captures. Any overlay is a separately registered challenger with sleeve 0.10; champion sleeve remains zero until human promotion. Upper sleeve bound 0.20.

## 4. Storage, prices, provenance and quality

### 4.1 Canonical schema and write policy

The state DDL is `contracts/schema.sql`; the separate price-cache DDL is `contracts/price_schema.sql`. All schema changes go through these sources and the matching production migration; no embedded DDL in PriceStore. Table counts are derived from those files, never hard-coded as 39.

Published cohorts, raw captures, fundamentals, holdings, attributes, factor values, scores, weights, labels, evaluations, curves and fills are append-only. SQL triggers prevent UPDATE/DELETE on protected tables. Mutable control state (run status, proposal status, order status, registry current-status mirror, validity-range closing) changes through a logged control-update helper. Historical factor status is separately appended to factor_status_history; model_versions freeze composition and weights policy.

`append_rows` returns zero for a byte-equivalent existing key, but raises ImmutableConflict if that key has different values. No INSERT OR REPLACE on historical tables. No generic replace_as_of API. An unpublished task may recompute in memory or connection-local staging tables. Cohort rows, all factor rows and all score batches are published in one SQLite transaction only after post-compute gates pass. Failed staging leaves no partial live cohort.

Every attempt gets a runs row, including blocked/error attempts. A second monthly call for an already published live date exits 0 before creating a new run or capture. Retries of failed attempts may add operational events, but cannot duplicate published evidence. Acquire a repository lock before checking/starting a monthly run; concurrent attempts cannot both publish. No `--force` overwrite of a passed live cohort. Corrections to published formulas use a new factor version for future cohorts and optionally a `counterfactual` cohort for old dates.

### 4.2 Ledger and recovery

Commit quant_engine.db unchanged, quant.db, raw capture archives, knowledge records, UI data and the canonical ledger. Daily cache files are git-ignored. The ledger is `data/ledger/YYYY-MM/events.jsonl`, ordered by globally increasing seq; each operation includes table, primary key, operation, prior-row hash, complete after-row JSON, recorded_at and run_id. No floating-point rounding on export: use JSON's round-trip representation; encode SQL NULL as JSON null and reject NaN/Infinity. Ledger export uses atomic file replacement and deterministic ordering.

The state mutation and ledger_events insertion occur in the same transaction. ledger_events does not recursively journal itself. Export events only after the run's final status is persisted. Rebuild creates the exact schema and replays operations by seq, verifying before hashes and restoring ledger_events rows without generating new events. `db verify` compares every state table's sorted rows with a rebuilt temporary database. Full initial state/seed operations must be journaled too. Test mutations to undated registry/decision rows, not just dated facts.

Raw price captures are archived with their vintage and manifest. Rebuild the daily cache from those retained archives where available. Re-downloading a vendor series is not guaranteed to reproduce historical bytes; a mismatch against a committed manifest is `SOURCE_CHANGED`, never success or a reason to rewrite the manifest. Backfill archives may be large: record measured size and retain them in the owner-configured archive directory with a committed inventory; do not claim fresh-clone recovery without those bytes. Commit the live factor input price windows and label endpoint evidence needed to reproduce published live results.

### 4.3 Price adapter and total returns

Keep source OHLC, adjusted close, volume, dividends and splits unchanged in the archive. Normalize into an explicit unadjusted quoted-share basis for calculation: close_raw, volume_raw, dividend_raw per post-event share, and split_ratio = new shares per old share (1 when none).

For Yahoo responses whose recorded fixture shows split-adjusted Close, undo future splits over the complete adjustment horizon: raw_close[d] = delivered_close[d] * product(split_ratio[e], e>d); raw_dividend[d] uses the same factor if dividends are split-adjusted; raw_volume[d] divides by that factor if volumes are adjusted, otherwise stays as delivered. The adapter version must state each column's basis, verify it against captured event examples and preserve source metadata. Fetch the action history through the response's adjustment end, even if the requested price window ends earlier. Unknown basis, incomplete split history or a failed fixture blocks affected data. Never infer correctness from the auto_adjust flag alone.

```
TRI[0] = 100
TRI[d] = TRI[d-1] * split_ratio[d] * (close_raw[d] + dividend_raw[d]) / close_raw[d-1]
```

For a same-day split/dividend, dividend_raw is paid per new share. A split from 600 to 100 at ratio 6 gives zero return. A dividend 10 with close dropping from 100 to 90 gives zero total return. A manual rights/demerger adjustment is a separately approved value-transfer factor applied to the day's gross return; it is not mixed with a split twice.

Signals based on SMA/highs use split-consistent prices rebased to the cohort cutoff. Liquidity uses the contemporaneous quoted price times actual share volume. Store the adjustment manifest in input_refs; historical replay selects that vintage. Changed vendor bars are appended with observed_at; unexplained revisions go to quarantine until a recorded data decision accepts them. A corrected vintage never silently changes published factor values.

Flag an unexplained daily move outside the symmetric ratio interval [1/1.40,1.40] with no applicable action; a -40% drop is included. Do not apply the legacy monthly +/-60% exclusion in V2. Compare engine TR with the source adjusted-close return as a diagnostic, with a 30bp tolerance and observed discrepancy details; a disagreement must be investigated, not automatically repaired to match Yahoo.

### 4.4 Fundamentals, attributes and holdings

The fundamentals identity is `(security_id, statement, freq, period_end, field, fetched_at)`: annual and Q4 facts coexist. Always partition PIT queries by frequency. Statement fiscal ends remain dates; unknown legacy FY positions live only in legacy data/defects, never masquerade as calendar dates.

Store annual and quarterly income/balance data, annual cashflow, and numeric info observations. Canonical fields: Total Revenue, EBIT, EBITDA, Net Income, Diluted EPS, Basic EPS, Interest Expense; Total Assets, Current Liabilities, Total Debt, Cash And Cash Equivalents, Stockholders Equity, Ordinary Shares Number; Operating Cash Flow, Capital Expenditure, Free Cash Flow. Units are rupees, shares, ratios or fractions. Store sector/industry text only in security_attributes. Never put them into a numeric fundamentals value column.

Estimated publication date is earnings report date + one trading day when a matching reported-EPS event exists; otherwise quarterly period end +45 calendar days followed by the next session, annual/Q4 +60 followed by the next session. These lags are conservative modeling assumptions, not evidence that a particular issuer filed on time. Live availability is always `max(estimated_publication, actual fetched_at)`; thus the engine cannot use a value it had not captured. An unchanged subsequent fetch need not duplicate the fact; a changed value inserts a version with its new timestamp.

PIT requires available_from <= cutoff AND fetched_at <= cutoff; choose the latest such version for each full fact identity including freq. Info, membership and attributes use actual observation time too. No live fundamental backfill from today's statements. `ttm(field, offset_quarters=0)` sums four consecutive fiscal quarters; offset 4 compares against the prior year's four quarters. Missing quarters return NaN. An explicit latest-annual fallback is allowed only at offset 0, flagged ttm_from_annual; never reuse that annual for both sides of earnings momentum.

Dividend yield is dividendRate / admissible close; debtToEquity 357 normalizes to 3.57; heldPercent fields are fractions in [0,1]; None remains NaN. Never use dividendYield or impute growth, ROE, yields, assets, or missing prices. Use actual recorded fixtures with capture dates; do not manufacture an old Yahoo response with previously quoted values.

Holdings captured in the following month are invisible to the preceding cutoff. `holdings(lag_runs=3)` means the fourth eligible distinct monthly capture counting latest as lag 0. Choose at most one last capture per IST calendar month. Flows join only with sufficient actual captures and coverage, not automatically on the third run. Exclude legacy holdings from live factor inputs.

### 4.5 Evidence tracks

Live means a published cohort based on pre-cutoff observations and pre-registered definitions. Only clean live cohorts feed promotions and weight fitting. Backfill means historical price/volume signals using today's constituents and taxonomy, explicitly survivorship-biased. Legacy means migrated snapshots with defect flags, never clean. Counterfactual means a retrospective changed definition or corrected factor recomputation, never promotion evidence.

Backfill runs through the last calendar endpoint available, with lookback warmup excluded. A 2016 start needs 2015 bars for a 12-1 signal at the first cohort. Do not promise a fixed number of backfill points; report requested range, warmup, excluded cohorts and realized horizon counts. Attributes such as current market cap are not backfillable price factors.

### 4.6 Gates

Gates return PASS, FAIL or DEFERRED with observed value, threshold, reason and applicability. DEFERRED is permitted only for evidence-history checks whose prerequisites are absent; it is never a substitute for missing current inputs.

| Gate | Condition | Failure |
|---|---|---|
| G1 | Nifty500 >=480 unique valid members | BLOCK |
| G2 | admissible universe capture <=62 days old | BLOCK; unchanged content is allowed |
| G3 | completed as_of close coverage >=98% | BLOCK |
| G4 | same close as prior month for <5% of common names | BLOCK; first month DEFERRED |
| G5 | unexplained price revisions <=2% of universe | BLOCK; affected rows stay quarantined |
| G6 | unit bounds: yield <=.25, D/E <=50, abs(PE)<1000, holdings [0,1], positive mcap/assets | invalid inputs masked in staging; BLOCK if >5 violators per field |
| G7 | known sector coverage >=99% | BLOCK |
| G8 | actual computed factor coverage: price >=95%, other >=70% of applicable members | exclude factor; BLOCK if >=3 active factors excluded |
| G9 | prior published cohort reproduced from pinned inputs, code and definitions | BLOCK on mismatch; first month DEFERRED |
| G10 | prescribed leakage checks with sufficient evidence | BLOCK on a deterministic invariant failure; absent evidence DEFERRED |

G1-G7 run before computation. G8 checks actual computed results and per-security score coverage; do not substitute raw-field availability. G9 and applicable G10 run before atomic publication against staging/prior cohorts. Missing fundamentals on a fresh install are BOOTSTRAP_REQUIRED, not G8 DEFERRED.

Warnings: statement staleness >15 months, missing factor share >20%, modal raw share >=80%, action flags >5, changed source package version, PSI >.25. Three consecutive near-constant months create a quarantine event/decision. PSI uses fixed bins derived from the first baseline capture and a pooled prior-three-month reference; >=3 drifting fields blocks pending source investigation. A first baseline is DEFERRED. Record missing/invalid values without rewriting raw observations. Clean means passed all applicable blocking gates, no override, acceptable capture provenance; benign warnings and lack of matured labels alone do not make a cohort unclean.

## 5. Factor library

### 5.1 Contract

Every FactorSpec has a versioned ID, family, fixed raw direction, horizon, formula, allowed input names, lookback, financial applicability, coverage threshold, provenance and pre-registration ID. Factor.compute receives only FactorInputs and returns raw values indexed by every member; it cannot perform I/O. FactorInputs owns all PIT selection and logs source row/capture references. Pin code plus helper dependency hashes and archive source used for each factor version; a shared helper change requires version bumps for affected factors. Registry status is not inferred from Python module contents.

### 5.2 Standardisation

For applicable finite raw values: winsorize at cross-section 1st/99th percentiles; within frozen sector group require >=5 finite observations and >1 distinct value; rank using average ties; compute `v = direction * NormalPPF((rank-.5)/n)`; subtract mean(v); divide by `max(1, max(abs(v-mean(v)))/3)` to keep centered values within [-3,3]. Missing remains NaN. Constants return NaN with constant_group. Coverage denominator excludes structural non-applicability (e.g. financials for ROCE).

Required invariants: mean z = 0 within numerical tolerance for valid groups, abs(z)<=3, order matches the registered direction, ties stay tied, and NaN stays NaN. Unit variance is NOT required: ties reduce dispersion. The explicit tie example in golden_cases.json is authoritative. The same transform without a direction multiplier neutralizes the composite.

### 5.3 Launch set and formulas

All entries below receive frozen hypotheses at bootstrap. Launch exemptions cover this exact set and the initial reference/shrink models, not future variants. Controls/diagnostics are lifecycle status shadow, identified by family and excluded from weights; `control`/`candidate` are not lifecycle enum values.

| ID (version 1 unless legacy) | Family; raw direction; horizon | Launch role | Raw formula |
|---|---|---|---|
| mom_12_1 | momentum; +1; 3 | active | log(TRI at trading offset -21 / TRI at -252); requires both endpoints and >=253 bars |
| trend_200 | momentum; +1; 3 | active | split-consistent close / trailing 200-close mean - 1 |
| vol_252 | low_risk; -1; 12 | active | population SD of 252 daily log TRI returns * sqrt(252); requires 253 bars |
| roce | quality; +1; 12 | active, nonfinancial | TTM EBIT / (latest annual Assets - Current Liabilities); denominator >0 |
| accruals | quality; -1; 12 | active, nonfinancial | (annual Net Income - annual OCF) / same-period Assets; denominator >0 |
| cash_conversion_3y | quality; +1; 12 | active, nonfinancial | sum same three FY OCF / sum Net Income; denominator >0 |
| earnings_yield | value; +1; 12 | active | nonfinancial TTM EBIT / EV; financial TTM NI / mcap; denominator >0 |
| book_to_price | value; +1; 12 | active | annual Equity / mcap; positive mcap; negative equity remains negative |
| eps_growth_3y | growth; +1; 12 | active | log(EPS latest / EPS three FY earlier)/3; both endpoints positive |
| earn_mom | growth; +1; 3 | active | (TTM NI offset0 - TTM NI offset4) / abs(TTM NI offset4); eight quarters, nonzero denominator |
| inst_hold_chg_3m | flows; +1; 3 | active when covered | eligible holdings lag 0 - lag 3; four monthly captures |
| mom_6_1 | momentum; +1; 3 | shadow | log(TRI[-21]/TRI[-126]) |
| dist_52w_high | momentum; +1; 3 | shadow | split-consistent close / max(last 252 closes) - 1 |
| rev_1m | momentum; -1; 1 | shadow diagnostic | log(TRI[0]/TRI[-21]) |
| max_ret_21 | low_risk; -1; 1 | shadow diagnostic | max(last 21 arithmetic daily TRI returns) |
| leverage | quality; -1; 12 | shadow, nonfinancial | (annual Debt-Cash)/TTM EBITDA; EBITDA positive |
| roe_stability_3y | quality; +1; 12 | shadow | mean(three annual NI/Equity) / population SD; Equity positive; zero SD -> NaN |
| fcf_yield | value; +1; 12 | shadow, nonfinancial | mean(three annual OCF+signed CapEx)/EV; EV positive |
| div_yield | value; +1; 12 | shadow | admissible dividend_rate / raw quoted close |
| rev_growth_3y | growth; +1; 12 | shadow | log(Revenue latest/three FY earlier)/3; endpoints positive |
| size | control; +1; 1 | diagnostic, never weighted | log(admissible mcap); no backfill |
| liq | control; +1; 1 | diagnostic, never weighted | log(ADV63), positive only |
| beta_252 | control; +1; 1 | diagnostic, never weighted | OLS beta of 252 daily TRI returns against index, intercept included |
| dc_flag | legacy; +1; 1 | diagnostic, never weighted | 1 if split-consistent close < SMA50 < SMA200 else 0 |

Sector features are specified in §3.4. A factor with horizon 1 remains diagnostic and cannot activate under a 3M promotion rule without a newly registered version. Raw direction is applied exactly once, during standardisation. All decisive ICs are computed on oriented z; positive always means good. Raw IC may be reported as a separately named diagnostic.

### 5.4 Registration and lifecycle

Every factor/model version records formula, inputs, direction, horizon, code hash, registered_on (an actual UTC timestamp) and first_oos_as_of. First OOS cutoff must be after registration. Initial bootstrap creates one system Tier-0 decision referencing the authorized spec/config hashes and exact seed set; this is not a human signature or a reusable authority for later Tier-2 changes. Launch hypotheses have counts_toward_budget=0 but remain included in reported trial counts.

Lifecycle: registered -> shadow after the first eligible computation; shadow -> active by a decision meeting §9 criteria; active -> probation by decision; probation -> active/retired by decision; any -> quarantined automatically on a deterministic input failure with a system decision; release by decision. Retired factors continue diagnostics for 24 months if inputs permit. Active cap14, shadow cap12 excluding control/legacy diagnostics. Status changes append history and create future model_versions; historical input sets are immutable.

## 6. Scoring and learning

### 6.1 Composite and screens

Within each family, average finite factors using status weights 1 active, .5 probation, 0 otherwise. Across present families, weighted mean with weights renormalized over those present. Apply §5.2 neutralization to that composite; final = (1-sleeve)*neutral_composite + sleeve*sector_tilt. Store all intermediate values.

EW_HIER uses equal weights across globally covered active/probation families. Flows is absent until coverage passes; adding it changes model version effective for a future cohort after an explicit coverage-admission decision. EW_FLAT averages all finite active/probation factors directly; do not try to simulate this by giving equal family weights. MOM_ONLY uses the momentum family and has model-specific minimum coverage of one family and one of its two factors. Other models require >=3 present families and >=60% of their applicable active/probation factor set. Financial non-applicability is excluded from the denominator. Store unscored rows with reason coverage.

Eligibility additionally requires EQ series, known sector, ADV63>=20,000,000 INR and >=54 positive-volume sessions of the last 63. Short histories produce NaN only for the factors requiring them. Rank all scored names and separately eligible names descending, ties by security_id. Assign quantiles using integer formula `min(q, 1+floor((rank_ascending-1)*q/n))`; label top performance quantile Q5/D10. No trap or momentum multiplier. The turnaround view is high-growth top-quintile and negative FCF, not a separate scoring path.

### 6.2 Models and versioning

EW_HIER_v1 is the initial champion and permanent reference after replacement. EW_FLAT_v1 and MOM_ONLY_v1 are references. IC_SHRUNK_v1 is the initial challenger with its own paper portfolio from the first clean cohort. Register SECTOR_OVERLAY_v1 only through a later hypothesis decision; no empty placeholder registration pretending it is approved.

model_versions freezes factor IDs, inclusion rules, sleeve and parameter version. Store monthly family weight_units separately. Registry status at replay time never overrides the version's frozen definition. Champion is a role, not a permanent assertion that every future champion equals EW. Test that unpromoted challengers cannot affect the selected live ranking; promotion switches roles prospectively while EW remains visible.

### 6.3 Shrunk family weights

Fit from clean live family IC evaluations known at ranking generation time, using only cohorts whose endpoints have completed by as_of. This learning-evidence timestamp differs from the earlier feature-input knowledge_cutoff: a completed historical endpoint may be evaluated during the current run before fitting. Record both cutoffs and the selected evaluation IDs. Use a common finite date intersection across included families; report each family's omitted dates. Never count NULL ICs as evidence or convert undefined correlations to zero.

```
F = number of included families; N = count of common valid matured 3M rows
n_eff = N / 3
alpha = n_eff / (n_eff + 24)
raw[k] = max(mean(IC_z[k]),0); normalize raw; if its sum is zero use EW
if n_eff < 4: target = exact EW policy
else: target[k] = (1-alpha)/F + alpha*raw[k]
```

Project by Euclidean capped-simplex projection `w[k]=clip(target[k]-lambda, .5/F, 2/F)`, solving lambda by bisection to sum1. Store integer units out of 10000, with lower bound ceil(10000*.5/F) and upper floor(10000*2/F). Allocate the remaining units by largest fractional remainder, ties by family name, without crossing bounds; if rounding down crosses a lower bound start from that lower bound and distribute/remove by residual distance. Sum must equal10000 exactly. Public weight = units/10000. EW at F=6 necessarily differs by one unit across families; equality means identical deterministic allocation, not six exactly representable 1/6 decimals.

For ordered families a,b,c,d,e,f and mean IC [.06,.02,.03,-.01,0,.04] with N12: units [2000,1619,1714,1429,1429,1809], alpha1/7. All nonpositive family means produce EW even after the gate opens. Idempotency is keyed by cohort/model/definition/evidence hash, never by re-applying an update to previous weights.

### 6.4 Invariants and promotion

Sum family units10000; bounds hold; within-family status weights normalize; excluded factors contribute zero; sleeve in[0,.20]; source provenance and all version IDs are present; published history never changes. A promoted model may differ from EW; before promotion the challenger cannot determine the champion ranking.

Model promotion requires a human Tier-2 decision, >=24 paired live matured 3M cohorts under the registered comparison, paired-difference HAC t >= adjusted threshold, net paper performance >= champion over the same realized interval, 12M sign check when >=3 paired points exist, turnover <=1.5 times champion, and no overrides in the comparison window. Insufficient paper or 12M data is reported explicitly; no fabricated values. Track the previous champion indefinitely. Demotion is also human Tier-2.

## 7. Evaluation and statistics

### 7.1 Subjects, revisions and undefined results

Evaluate oriented factor z, versioned family scores, model final scores, sector features and diagnostic cohorts for each track and eligible/all scopes. Label groups use all members; the IC sample uses finite score/label pairs. Report per-date Spearman IC, universe-relative IC diagnostic, within-group quintile/decile arithmetic means/medians/5%-trimmed means, top-decile hit rate, screened cohort returns, correlations, residual partial IC, and Fama-MacBeth slopes with size control.

Spearman returns undefined for <3 pairs or either constant vector; n is recorded, status insufficient/constant, and value NULL. It never returns zero to mean missing. Compute partial IC by residualizing the candidate z on the contemporaneous active-z matrix plus intercept using least squares on complete rows; correlate residuals with labels, and report dropped coverage.

Monthly evaluation windows use empty strings, not NULL. The natural key includes subject_version, track, scope, metric, method, window bounds and revision. evidence_hash covers ordered selected source revisions and code/config. An unchanged recomputation inserts zero rows. Changed evidence appends a new revision and evaluations_log old/new references. Queries select exactly one latest applicable revision per natural key, known by the decision timestamp. Prior decisions keep their evidence references. Reports explicitly state whether they show originally published or revised evidence.

### 7.2 Temporal validation

Training uses only labels with endpoint <= as_of and revision computed_at <= ranking generation time. First assess/mature earlier cohorts, then fit weights, then score the new cohort. This prevents a one-month accidental lag caused by scoring before processing the newly matured evidence. Walk-forward learning points read stored weights/scores; counterfactual recomputation never feeds a live decision. Backfill labels are descriptive only.

### 7.3 Statistics

For a contiguous finite monthly IC series of length N and horizon h, lag L=h-1:

```
gamma[j] = sum((x[t]-mean)*(x[t-j]-mean), t=j..N-1) / N
S = gamma[0] + 2*sum((1-j/(L+1))*gamma[j], j=1..L)
se = sqrt(max(S,0)/N); t = mean/se; CI90 = mean +/- 1.645*se
```

If N<=L+1 or variance is zero, store NULL se/t/band with status insufficient/constant. Do not invent wide bands or print a missing band as zero. Cumulative mean remains descriptive, and the UI displays uncertainty unavailable plus n/n_eff. Monthly individual IC has no time-series confidence band; label its band_status not_applicable rather than applying the old cross-sectional t formula.

Use the longest contiguous block ending at the latest valid cohort for HAC/rolling tests; disclose excluded earlier blocks and calendar gaps. Do not compress missing months into artificial adjacent observations. All-valid cumulative descriptive means may be shown separately. Circular block bootstrap uses length h, seed 0 and 1000 resamples when N>=3h. Report sensitivity at lag 2h-1 when estimable. Neither h-1 nor N/h guarantees independence. ICIR is unavailable below n_eff6.

The promotion threshold is `max(2, NormalPPF(1 - .05/(3*m)))`. Count m as every hypothesis version registered since this engine's bootstrap that could enter the same decisive 3M comparison, including unsuccessful/withdrawn versions, launch exemptions and the subject under review; m is at least 1. Do not let old trials disappear from this count after a rolling window. This is a conservative review budget using an asymptotic normal approximation, not a guarantee of a finite-sample error rate.

Factor promotion opportunities are fixed at registration at 12, 24 and 36 eligible labeled 3M cohorts. Model promotion has a 24-cohort minimum, so its opportunities are 24, 36 and 48 paired cohorts. Each opportunity is evaluated once on the first run reaching that count, even if ancillary criteria are unmet; use exactly the first specified number of eligible cohorts, and store the look and evidence IDs. Later revisions do not create another look. Maximum three looks per hypothesis; a new variant requires a new registration and budget slot. All decisive promotions use 3M IC; the factor table's horizon describes its thesis diagnostic, and the 12M sign is a supporting check when available. Horizon-1 diagnostics need a new promotable version. Annual BH FDR10% is diagnostic and creates a proposal; it never silently changes status.

The Bartlett estimator is downward weighted at finite bandwidth: an h-month moving sum does not have estimated variance exactly h times white noise under this finite lag. Test the formula against golden HAC examples and a known MA covariance calculation, not a guessed sqrt(h) requirement.

### 7.4 Benchmarks and nulls

Primary portfolio benchmark PF_BM_EW holds the same eligible cohort universe at the same execution schedule and cost assumptions. BM_EW is its gross-return counterpart; net excess comparisons use the net benchmark. BM_EW_SECTOR matches portfolio sector weights using cohort groups. BM_CW is a captured-mcap-weighted universe proxy, explicitly not an official index replication. ^CRSLDX and ^NSEI are external price-index checks. Any +1.2% annual dividend accrual is labeled an assumed proxy, never total-return ground truth.

Replicated Momentum30 and Quality30 constituent portfolios use only captured membership, equal weights and the same engine TR basis; disclose that published index weighting rules differ. ETFs are independent adjusted-close cross-checks with limited history. Missing benchmark histories are N/A. Backfill compares only survivorship-labeled EW and available price-index checks.

NULL_RANDOM evaluates1000 seeded Dirichlet family-weight composites on the same admissible factor matrix/labels, descriptive only. NULL_BEST is the best single active factor measured over that same interval; it is selected ex post and is not an investable baseline claim.

### 7.5 Leakage and adversarial tests

T1 shuffle: use200 fixed permutations within cohort and sector, summarize the null distribution and its centered mean with a Monte Carlo error tolerance. Never block because one random permutation happens to exceed2SE. T2 planted: a deterministic synthetic rank permutation has an exact expected Spearman; the production FactorInputs must reject access to future labels. A randomized48-month world is a stress fixture, not an oracle demanding a realized IC in a narrow interval.

T3 replay: rebuild published factors from archived inputs/code/status versions; compare values and provenance. T4 boundary: every referenced observation <= cutoff, except historical price vintages explicitly marked backfill/legacy. T5 embargo: a future endpoint or later revision cannot enter fitting. T6 availability shift: moving a claimed publication date earlier must NOT bypass actual fetched_at; deliberately leaky test code is rejected by this stronger boundary. Increased IC after intentionally adding information is not itself proof of an existing leak. T7 action invariance: use consistent economic split/dividend fixtures. T8 holdings: current run's captures cannot leak. T9 survivorship: count original cohort members through every horizon. T10 sector-only predictor: balanced synthetic sector-independent returns give no stock-selection signal; do not assert all real sector-dummy ICs must be exactly zero after median adjustment.

Run deterministic checks at every publication. Real-data statistical checks run when their prerequisites exist, otherwise DEFERRED with required cohort count. Synthetic injection always operates on a temporary copy, never live stored values.

### 7.6 Spreads and curves

Gross Q5-Q1 is a research spread, not a realizable long-short strategy. Net selection spread compares a top-quintile paper cohort book against the matched EW paper book with identical actual execution and endpoint dates. Sum actual turnover/cost events within the interval, not just endpoint membership differences. If that book/interval was never simulated, net spread is N/A; do not substitute zero cost. Evaluate cost multipliers .5/1/2 and declare sensitivity.

Evidence curves select latest permitted revisions and retain the original evidence hash. Learning curves compare stored challenger/EW scores on the same cohorts and labels, with dates and omitted rows shown. Report bands only when estimable and label track/version on every plotted statistic. Three-month live evidence starts no earlier than month 4; no future points are drawn.

## 8. Paper portfolios and costs

Create TOP30 buffered, DEC10 unbuffered, quarterly TOP30 tranches and matched EW books for champion/challenger/reference models. TOP30 retains eligible holdings with rank<=60; fill vacancies by best remaining rank, enforcing at most6 names per sector. This may select an entrant ranked below 30 when sector constraints require it. If fewer than 30 feasible names exist, hold residual cash and report capacity; do not violate caps.

Also create independent 3M cohort paper books for each promotable factor version and scored model: TOP_Q20 selects the highest quintile of eligible names and MATCHED_EW holds that same subject's finite eligible universe. Start both at the cohort's actual execution session, liquidate at the label endpoint session, and include both entry and exit costs. They are attribution books, separate from the rolling TOP30 portfolio. Missing execution prices keep the pair pending/unavailable; if the intended entry is no earlier than the endpoint, mark expired without a spread. Apply the liquidity bucket C cap and cash fallback to both books. Do not impose the rolling TOP30 six-name sector cap on a broad EW attribution book. Paper book IDs include subject kind/version, cohort ID and rule. These books supply the required net selection evidence; without them a factor cannot meet promotion criteria merely because its gross IC is favorable.

Equal weight at entry, weights drift between fills. A rebalance computes target weight minus actual pre-trade drifted weight, including cash. Bucket C has a2% portfolio target cap; redistribute remaining target across eligible A/B names under the sector limit, else cash. Never claim30 equal3.33% weights and simultaneously enforce2% for C. Quarterly books have three separately tracked tranches, one reviewed each month. Fixture tests must cover sector/cap infeasibility.

ADV63 buckets: A>=500m INR; B>=100m; C>=20m; D below 20m excluded. Cost assumptions per traded side: fixed12bp plus impact A10/B25/C50; D infinity; stress1.5x. These are modeling assumptions, not a current statutory-fee quotation. Version the assumptions and record their provenance; changing them requires governance. Cost = sum(abs(delta_weight)*bps/10000). Turnover = .5*sum(abs(delta_weight)) including cash; report initial deployment separately. Dividends reinvest within the paying stock for TR comparability; splits alter shares without economic gain.

Gross NAV is reconciled across actual fill/event dates. Net deducts simulated trading costs; no tax-return advice or modeled capital-gains tax. Compare benchmark net to portfolio net. Compute drawdown, turnover, concentration, factor-index membership overlap, sector exposures and net excess series. IR/annualized inference is unavailable before 24 completed portfolio months. Use HAC lag 3 and lag 0 sensitivity for monthly excess returns; lack of overlap does not guarantee no autocorrelation. Print alpha only with >=24 months, positive excess and primary HAC t>=2; describe it as evidence of paper excess under the stated cost assumptions, never proven future skill.

## 9. Knowledge, decisions and monthly flow

### 9.1 Monthly sequence

1. Lock and reject future dates; exit0 immediately if that live cohort is already published.
2. Start a new attempt; process already authorized expiry/reversion effects before selecting definitions. Archive current captures for future use and update completed price sessions. Select target inputs strictly by cutoff.
3. Settle pending paper orders for completed sessions, mature/revise old labels, evaluate prior published cohorts; preserve these results even if new scoring is blocked.
4. Run G1-G7 pre-compute gates; stage factors and derive actual factor coverage/exclusions; fit weights from matured admissible evidence; stage all model scores. Run the post-compute gate call G8-G10 and score invariants against this complete draft. A failed draft cannot publish even if fitting already ran in memory.
5. If valid, atomically append cohort/membership panel, factors, model weights and scores. If invalid, discard staging, set blocked and continue report/export only.
6. Create future-dated paper orders from published scores; never assume they filled. Compute available portfolio returns and scoreboards.
7. Review criteria; draft deduplicated proposals only. Do not create new discretionary approvals.
8. Write report/UI from persisted data, finalize run status, export ledger/knowledge, verify rebuild, checkpoint/VACUUM outside transactions. Commit only intended data/report files when --commit is supplied; push only when --push is explicitly supplied.

A blocked data capture may still advance earlier labels or fill prior orders if those inputs are valid. Preserve all diagnostics. The runner returns0 successful/previously done,1 implementation/source error,2 blocked current publication,3 governance refusal. `--dry-run` performs offline planning from stored inputs without writes, network, files or Git changes.

### 9.2 Knowledge records

Use hypotheses, experiments, proposals, decisions, lessons, factor_status_history and model_versions plus generated markdown and JSONL mirrors. Reports are views of tables, never inputs to program state. Every applied decision has an ADR with evidence IDs, criteria output, actor, time, effective cutoff, intended consequence and reversion. The initial seed decision is system bootstrap referencing the exact spec hash; migration defect annotations are system factual records and do not fabricate approved model-change decisions.

### 9.3 Approval authority

Tier0: deterministic computation, logging, prescribed quarantine and initial fixed bootstrap. Tier1: criteria-satisfying factor transitions, in-budget registrations, documented data fixes, revision acceptance and cost recalibration within 25%. An LLM may provisionally approve these, recorded as llm and ratified by a human within 60 calendar days. Tier2: model promotion/demotion, rule/threshold/taxonomy changes, budget overrides, schema migrations after live launch, and blocked-gate overrides; human only.

Agent commands use `--actor-kind llm --by llm:MODEL`. The CLI requires actor kind and by prefix to agree and refuses Tier2 to llm. `--by` text is audit metadata, not authentication: local operators with filesystem access can bypass any in-process CLI. Do not claim a security boundary. An agent must never invoke human mode or invent an approval based on a placeholder name. Human operation uses a separately reviewed decision command. Record actual authorization context in the note/ADR.

Provisional factor/model effects have a stored reversion plan. On expiry, append the prescribed reversion decision and future model version; past scores remain intact. If reversion cannot be constructed, block the next affected publication and ask for human action. A decision recorded after a target cutoff cannot be backdated to select that cohort's definition. If the selected definition depends on authority that has expired by generation time, block the affected publication and use the next admissible cutoff; do not publish using expired authority. Applying at month-end is prospective; operational data resolution may take effect immediately in the revision view, but never rewrites a published value.

### 9.4 Budget and criteria

At most6 new hypotheses per calendar year and 3 per family; withdrawn/failed variants still count. Initial fixed launch set is explicitly exempt from the registration budget only. At most3 live challengers. Register review opportunities and trial counts before looking at outcomes; record all attempted variants and actual thresholds. Diagnostics and corrections that do not change a hypothesis are separately categorized, never used to conceal a new formula trial.

Promotion uses oriented-z IC: mean>=.02, positive in>=60% of eligible months, HAC t above the budgeted threshold, >=12 labeled3M months, positive12M sign when at least 3 exist, residual partial IC t>=1.5, absolute same-family correlation<=.70, coverage>=80% of applicable members over 3 runs, net3M selection spread>0 where a matching paper record exists, and counterfactual ablation degradation no worse than.005. Missing required cost/ablation evidence makes the criterion unmet. Counterfactual ablation is supporting evidence, never a replacement live track.

Probation proposal: 24-month oriented IC mean<0 with HAC t<=-1, coverage<80% for 3 consecutive runs, or same-family correlation>.85 for 6 runs. Recovery: nonnegative24-month HAC t for 6 consecutive scheduled reviews. Retire after 6 months probation without recovery, or36 months shadow without support. No automatic sign reversal. Formula/input changes create new factor versions and hypotheses, leaving prior history untouched.

### 9.5 Reports

One page of prose plus tables: verdict and up to 3 required actions; gates and data provenance; matured labels/status exclusions; live statistics and separate backfill/legacy/counterfactual tables; null comparisons; curves; spreads/cost sensitivity; portfolios and execution delays/pending orders; cohorts; criteria/proposals; budget and ratifications; run/code/config/source hashes and reproduction commands. Never render an inferential number without n, n_eff, method and either a finite band or an explicit insufficient/not_applicable uncertainty status. Footer: "Small or dependent samples may not distinguish skill from noise."

Preserve reports as `knowledge/reports/YYYY-MM/<report_id>.md` plus a JSON manifest with pinned row references and rendered control-state values. report_id is SHA256 of the canonical manifest body, excluding report_id and output hashes. It includes as_of, view, known_at, run IDs, selected evaluation/label/portfolio revision keys, source/definition/config hashes, renderer code hash, and frozen proposal/gate/decision summaries. Rendering the same snapshot is a no-op; changed evidence creates a different report_id. `YYYY-MM.md` may be a replaceable navigation index only. Verification rebuilds a historical report from its manifest and archived renderer/input versions; it cannot select today's latest revisions. Missing retained inputs or renderer code is a failed reproducibility check, never a reason to replace the old report.

## 10. Architecture, CLI and operation

### 10.1 Package

Production modules and public APIs are listed in INTERFACES.md; each belongs to one task. Layers: config/types -> db/calendar -> universe/client/captures/prices -> PIT and gates -> factors -> model -> evaluation -> portfolio -> knowledge -> migration -> orchestration/UI. Cross-layer review logic belongs to knowledge, not model, avoiding a model/evaluation/portfolio dependency cycle.

Keep legacy modules at root unchanged. `quant/` owns V2 modules; tests live under tests/unit, tests/property, tests/leakage and tests/integration. V2 does not mutate legacy DB even during migration acceptance. UI has8 tabs: Ranking, Learning, Scoreboard, Factors, Sectors, Data, Knowledge, Legacy. The original8-factor invariants remain for legacy only; V2 uses §6.

### 10.2 Schema and config

Copy canonical SQL to quant/db/schema.sql and quant/db/price_schema.sql; copy contracts/config.toml to config/quant.toml. WS00 implements migrations for subsequent versions but no change of initial canonical DDL without a spec correction. Parse TOML using tomllib. Use CSV/JSON for field contracts and holidays to avoid an undeclared YAML dependency.

Config overrides: QUANT_DB_PATH, QUANT_PRICES_DB_PATH, QUANT_DATA_DIR, QUANT_UI_DIR, QUANT_LEGACY_DB_PATH, QUANT_KNOWLEDGE_DIR and QUANT_ARCHIVE_DIR. Resolve relative values from repository root, never current working directory. Record a hash of effective resolved policy values excluding machine-specific absolute path strings. A policy change after bootstrap requires a matching decision/config version; path overrides for isolated tests do not.

### 10.3 CLI

Every command has --help; globals --db, --config, --actor-kind, --by are accepted before or after the subcommand through explicit argparse parent definitions. Most commands accept --as-of only where meaningful. No fake run rows for read-only status/help/verify; mutating commands report run_id, as_of, git SHA and actual row counts.

```
db init | export | rebuild --output PATH | verify | size | migrate-legacy --dry-run
universe capture | sectors show --as-of DATE
data capture | ca detect | ca add --isin I --ex-date D --kind K --factor F --decision-id ID
             | ca clear-flag --event-id ID --decision-id ID
             | accept-revision --capture-id ID --decision-id ID
prices backfill --start DATE | update --through DATE | manifest --verify
fundamentals show --as-of DATE
factors sync | list | test ID | compute --as-of DATE --track TRACK
score --as-of DATE --track TRACK
labels mature --through DATE
evaluate --as-of DATE --track TRACK
portfolio plan --as-of DATE | settle --through DATE | scoreboard
model list | check
kb hypothesis new --file PATH | queue | review --as-of DATE
kb approve ID --actor-kind KIND --by ACTOR --note TEXT | reject ID --actor-kind KIND --by ACTOR --note TEXT
kb ratify ID --actor-kind human --by human:NAME --note TEXT
kb apply --as-of DATE | report --as-of DATE | decision new --file PATH
verify leakage --as-of DATE | pit --months N | report --as-of DATE
ui export
status
run monthly [--as-of DATE] [--skip-capture] [--stop-after STAGE] [--dry-run] [--commit] [--push]
run backfill-track --start DATE --end DATE
```

`score` and `factors compute` on live data create staging only; publication belongs to run monthly. `--skip-capture` uses existing captured data, never implies permission to fill missing historical facts. `--stop-after` names capture, mature, gates, stage, publish or record; only publish commits a new cohort. Manual data commands require a matching applied/provisional Tier1 or approved Tier2 decision and evidence; they cannot change stored scores.

### 10.4 Acquisition and budgets

All yfinance calls go through YahooClient; serialize calls and enforce >=0.5s between external accessor invocations, with batches<=25, threads=False and>=1s between batches. Where one library call performs multiple requests, document the transport limitation and apply a shared request limiter if supported; never claim per-request throttling based only on sleeping once per ticker. On429 wait120s then retry up to 5 times with checkpointing. Unit tests use an injected sleeper and never really wait. Capture partial failures explicitly; fewer than 90% successful required captures blocks live scoring.

Record actual Python/package versions in fixture manifests and run metadata. Generate an exact requirements lock after the first tested install; do not assume unbounded requirements.txt reproduces identical responses. Record source fields/bases, not fabricated historic values. Network integration is opt-in; offline tests patch all network entrypoints.

Budget targets: offline suite<90s; real monthly<60min; skip-capture<10min; initial history fetch<30min; backfill replay<30min; UI payload<1.5MB. Measure DB and Git growth, warnings at state file50MB and repo800MB; optimize or propose revised storage when exceeded. Archive availability and actual measured size are acceptance evidence, not unverified promises that ten years of data always fits a guessed budget.

### 10.5 Scheduling and recovery

monthly_cron.sh uses repo-relative discovery, the configured interpreter, a lock and IST-aware target resolution. Run manually first. Schedule capture before cutoff and monthly processing during the next week's first available session; an owner may also schedule portfolio settlement. Creating a schedule is an explicit deployment action, not part of documentation sign-off. Push requires an explicit flag and successful ledger verification; never stage unrelated user changes or caches.

### 10.6 Legacy migration

Open quant_engine.db with mode=ro and sqlite3.Row, record SHA256 before/after. Source expectations: daily_predictions2543, active_weights12, performance_tracking4773; dates 2026-06-04(47),06-12(499),06-14(499),07-11(499),08-14(499),09-03(500). Verify against the checked-in file rather than rewriting it to match.

Keep six legacy_snapshot_map rows; June12 duplicate is superseded by June14 with mapped trading date June12; July11 maps July10. Publish four full legacy cohorts (499+499+499+500=1997 rows/model), each with its frozen legacy membership/current-backfill group map and defects. Import8 factors plus trap, momentum multiplier and dc_flag as legacy_*@0. Two models LEGACY_V18 and LEGACY_V18_BASE preserve original final and pre-multiplier reconstruction respectively. Migrate weights as model versions; do not apply V2 bounds to them.

Store corrupt raw inputs only as archived evidence/legacy_defects. Do not import unknown FY endpoints, current market cap or legacy holdings as clean live fundamentals. Preserve base_score where present; otherwise reconstruct using weights actually in force, excluding headline sentiment and marking reconstructed. Irregular legacy-quote attribution uses original raw scores/quotes and the legacy guard; do not sector-transform it before comparing to the red-team table.

Migration records factual system decisions and defect ADRs, never Tier2 approved placeholders. It runs after knowledge support exists. Second migration returns unchanged, zero inserts; --dry-run is read-only. Legacy results remain visibly flagged; their uncertainty may be unavailable with only three irregular periods.

### 10.7 UI and documentation

Export persisted ranking, learning, scoreboard, factor and knowledge payloads. Exporter validates uncertainty status but calculates no statistics. Display pending orders, as_of, generated_at, source cutoff, freshness and track. Use explicit empty states for no live evidence, no matured labels and unavailable bands. A backfill curve is dashed; legacy markers are hollow; show only realized labeled points, so four legacy snapshots do not imply four forward-return observations.

Keep the turnaround saved view and stock-level factor explanations. A DCF explainer is optional and diagnostic only, computed in a separate analysis function from captured inputs; do not reintroduce it into ranking. No added network dependency in the page. Browser smoke tests verify all 8 tabs, empty states, no console errors and no network requests after local assets load.

## 11. Build plan and acceptance stages

Build order is WS00, WS01, WS03, WS02, WS04, WS05, WS06, WS07, WS08, WS09, WS10, WS11. Task granularity, files and commands are in subagents/. Implement every workstream in this build; activation dates and evidence maturity remain operational state, not reasons to postpone implementing the UI/governance modules.

Specification acceptance is `python3 docs/spec/check_spec.py`: DDL, config, examples, dependency graph, task completeness and document references. Engineering acceptance runs offline implementation tests and deterministic end-to-end/recovery checks. Operational acceptance uses recorded/live source checks, read-only real migration and an actual admissible monthly cohort. Longitudinal acceptance waits for real history. Each is reported separately as PASS/FAIL/DEFERRED. A deferred operational or longitudinal check does not become an engineering pass, and does not prevent completing all code that can be tested now.

## 12. Risks and limits

Yahoo is a single mutable source, annual statement coverage and filing dates vary, snapshots can be missed, historical constituents are incomplete, and demergers/delistings need evidence. Expose these limitations rather than impute facts. Cold-start and data gaps delay the live record. Proxy benchmarks and guessed costs limit investment conclusions. Repeated testing, model turnover and dependent cohorts can flatter inference despite controls. No probability of future profitability is assigned by this spec.

## 13. Correction decisions

R01: actual observation cutoff plus pre-month bootstrap replaces retrospective live capture. R02: next-session-after-generation paper fills replaces backdated T+1. R03: frequency is part of the fundamental identity. R04: append-only labeled/evaluation revisions replace contradictory overwrite rules. R05: oriented IC is the only decisive sign convention. R06: integer allocation replaces rounded weights claiming an exact binary-float sum. R07: centered bounded ranks explicitly allow tied-score variance. R08: source/client before price consumers, knowledge before migration, no fake approval placeholders. R09: statistical outcomes are research observations, not build oracles. R10: one canonical API/DDL/config with executable examples and task manifests. R11: honest pending/history states replace calendar-date promises. R12: artifact-vintage recovery replaces a promise that mutable vendor re-downloads reproduce hashes.

## 14. Glossary

Cohort: immutable membership, classifications and published model scores for one as_of/track/definition. Capture: raw bytes at their real observation time. Cutoff: latest admissible knowledge timestamp. Revision: appended correction with prior reference, leaving original rows intact. Track: live/backfill/legacy/counterfactual. Oriented IC: Spearman of sign-adjusted standardized factor against future label. Clean: admissible provenance and all applicable blocking checks passed without override. DEFERRED: not evaluated because a declared prerequisite does not yet exist. ADR: a generated decision record with actual actor and evidence. Paper excess: simulated net return minus a matched simulated benchmark, not a profitability promise.

## 15. Defaults already decided

Paper notional5m INR; monthly buffered headline with quarterly comparison; first live date derived from bootstrap/cutoff gates; Financial Services initially unsplit; live factor history never filled from legacy/current statements; optional NSE bhavcopy/NSDL/AMFI adapters documented interfaces only and excluded from build acceptance; state DB plus ledger committed; source archive retention explicit; no automatic pushes; LLM approvals Tier1 provisional only; human ratification60days; no autonomous model promotion; no real trades. No external paid service is required. Implementation in another LLM is expected; none of these documents requires a Codex-only skill, plugin or agent runtime.
