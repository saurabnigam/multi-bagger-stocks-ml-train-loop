-- V2 specification revision 2. Canonical state DDL.
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS securities (
  security_id   INTEGER PRIMARY KEY,
  isin          TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  listing_date  TEXT,                       -- optional, from EQUITY_L.csv adapter
  first_seen    TEXT NOT NULL, last_seen TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('listed','delisted','suspended','unknown'))
);

CREATE TABLE IF NOT EXISTS symbol_history (
  security_id INTEGER NOT NULL REFERENCES securities,
  nse_symbol  TEXT NOT NULL, yahoo_ticker TEXT NOT NULL,     -- yahoo_ticker = nse_symbol || '.NS' unless overridden
  valid_from  TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL,
  PRIMARY KEY (security_id, valid_from)
);

CREATE TABLE IF NOT EXISTS universe_membership (
  as_of TEXT NOT NULL, observed_at TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  index_name TEXT NOT NULL,                                     -- 'NIFTY500' | 'NIFTY200MOM30' | 'NIFTY200QUAL30' | 'NIFTYTOTALMARKET'
  nse_symbol TEXT NOT NULL, nse_sector TEXT, series TEXT,
  source TEXT NOT NULL CHECK (source IN ('nse_csv','nse_csv_stale','legacy_snapshot','current_backfill')),
  source_sha256 TEXT, PRIMARY KEY (as_of, index_name, security_id, observed_at)
);

CREATE TABLE IF NOT EXISTS sector_group_def (
  version INTEGER NOT NULL, nse_sector TEXT NOT NULL, yahoo_industry_pattern TEXT NOT NULL DEFAULT '',   -- empty string = any; regex otherwise
  sector_group TEXT NOT NULL, macro_sector TEXT NOT NULL, merge_into TEXT, min_group_size INTEGER NOT NULL DEFAULT 8,
  registered_on TEXT NOT NULL, decision_id TEXT, PRIMARY KEY (version, nse_sector, yahoo_industry_pattern)
);

CREATE TABLE IF NOT EXISTS sector_map (
  security_id INTEGER NOT NULL REFERENCES securities,
  observed_at TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,                 -- inclusive / exclusive; NULL = current
  nse_sector TEXT, yahoo_sector TEXT, yahoo_industry TEXT,
  sector_group TEXT NOT NULL, group_def_version INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('nse_csv','nse_csv_prior','yahoo_crosswalk','manual','legacy_backfill','current_backfill')),
  confidence REAL NOT NULL,                                -- 1.0 nse_csv; 0.9 prior; crosswalk share; 0.5 manual/backfill
  PRIMARY KEY (security_id, valid_from)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
  security_id INTEGER NOT NULL REFERENCES securities, ex_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('split','bonus','dividend','rights','demerger','scheme','manual_adj','suspected')),
  ratio REAL, amount_inr REAL, adj_factor REAL,           -- split/bonus: new per old; dividend: INR/share; manual: multiplicative price factor
  source TEXT NOT NULL CHECK (source IN ('yahoo_actions','reconcile','manual','inferred')),
  observed_at TEXT NOT NULL, decision_id TEXT, note TEXT,
  PRIMARY KEY (security_id, ex_date, kind, observed_at)
);

CREATE TABLE IF NOT EXISTS prices_monthly (
  cohort_id TEXT NOT NULL REFERENCES cohorts, as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  close_raw REAL, tri REAL, adv_63_inr REAL, n_days_63 INTEGER, mcap_inr REAL, shares_out REAL,
  quote_legacy REAL, source TEXT NOT NULL, price_manifest_sha TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY(cohort_id,security_id)
);

CREATE TABLE IF NOT EXISTS fundamentals (
  security_id INTEGER NOT NULL REFERENCES securities,
  statement TEXT NOT NULL CHECK (statement IN ('income','balance','cashflow','info')),
  freq TEXT NOT NULL CHECK (freq IN ('A','Q','P')),         -- annual, quarterly, point value from info
  period_end TEXT NOT NULL,                                  -- '' for point values
  field TEXT NOT NULL,                                       -- canonical name from config/field_contracts_v1.json
  value REAL, unit TEXT NOT NULL CHECK (unit IN ('inr','frac','x','shares','count')),
  available_from TEXT NOT NULL,
  available_from_basis TEXT NOT NULL CHECK (available_from_basis IN ('earnings_date','lodr_45d','lodr_60d','first_fetch','run_date')),
  fetched_at TEXT NOT NULL, source TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY (security_id, statement, freq, period_end, field, fetched_at)
);

CREATE INDEX IF NOT EXISTS ix_fund_pit ON fundamentals (security_id, field, available_from);

CREATE TABLE IF NOT EXISTS holdings (
  security_id INTEGER NOT NULL REFERENCES securities, captured_at TEXT NOT NULL,
  inst_pct REAL, insider_pct REAL, shares_out REAL, source TEXT NOT NULL,
  PRIMARY KEY (security_id, captured_at)
);

CREATE TABLE IF NOT EXISTS security_attributes (
  captured_at TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  mcap_inr REAL, shares_out REAL, float_shares REAL, ev_inr REAL, trailing_pe REAL, price_to_book REAL,
  dividend_rate_inr REAL, beta REAL, yahoo_sector TEXT, yahoo_industry TEXT, source_sha256 TEXT NOT NULL,
  PRIMARY KEY(security_id,captured_at)
);

CREATE TABLE IF NOT EXISTS data_quality_events (
  event_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('INFO','WARN','BLOCK')),
  code TEXT NOT NULL, security_id INTEGER, field TEXT, detail_json TEXT, resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS dq_runs (
  run_id INTEGER NOT NULL REFERENCES runs, gate TEXT NOT NULL, phase TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PASS','FAIL','DEFERRED')),
  observed_json TEXT NOT NULL, expected_json TEXT NOT NULL, reason TEXT NOT NULL,
  blocking INTEGER NOT NULL CHECK(blocking IN (0,1)), PRIMARY KEY(run_id,gate,phase)
);

CREATE TABLE IF NOT EXISTS factor_registry (
  factor_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, family TEXT NOT NULL,
  direction INTEGER NOT NULL CHECK (direction IN (-1,1)), horizon_m INTEGER NOT NULL, level TEXT NOT NULL,
  hypothesis TEXT NOT NULL, formula TEXT NOT NULL, inputs_json TEXT NOT NULL, lookback_days INTEGER NOT NULL,
  applies_to_financials INTEGER NOT NULL, backfillable INTEGER NOT NULL, min_coverage REAL NOT NULL, evidence TEXT,
  hypothesis_id TEXT REFERENCES hypotheses(hypothesis_id), code_sha256 TEXT NOT NULL, module_path TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('registered','shadow','active','probation','retired','quarantined')),
  registered_on TEXT NOT NULL, first_live_as_of TEXT, status_changed_on TEXT NOT NULL, status_decision_id TEXT
);

CREATE TABLE IF NOT EXISTS factor_values (
  cohort_id TEXT NOT NULL REFERENCES cohorts, as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  factor_id TEXT NOT NULL REFERENCES factor_registry, raw REAL, winsor REAL, z REAL, sector_group TEXT NOT NULL,
  flags TEXT NOT NULL DEFAULT '', input_refs_json TEXT NOT NULL, track TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY(cohort_id,security_id,factor_id),
  FOREIGN KEY(cohort_id,as_of,track) REFERENCES cohorts(cohort_id,as_of,track)
);

CREATE INDEX IF NOT EXISTS ix_fv_factor_asof ON factor_values (factor_id, as_of);

CREATE TABLE IF NOT EXISTS sector_features (
  cohort_id TEXT NOT NULL REFERENCES cohorts, as_of TEXT NOT NULL, track TEXT NOT NULL,
  sector_group TEXT NOT NULL, feature_id TEXT NOT NULL, value REAL, n_members INTEGER,
  PRIMARY KEY(cohort_id,sector_group,feature_id),
  FOREIGN KEY(cohort_id,as_of,track) REFERENCES cohorts(cohort_id,as_of,track)
);

CREATE TABLE IF NOT EXISTS models (
  model_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('equal','shrink','overlay','reference','legacy')),
  role TEXT NOT NULL CHECK (role IN ('champion','challenger','reference','legacy','retired')),
  description TEXT NOT NULL, params_json TEXT NOT NULL, hypothesis_id TEXT, registered_on TEXT NOT NULL, decision_id TEXT
);

CREATE TABLE IF NOT EXISTS model_versions (
  model_id TEXT NOT NULL REFERENCES models, version INTEGER NOT NULL,
  factor_set_json TEXT NOT NULL,              -- [{"factor_id":"mom_12_1@1","family":"momentum"}, ...] active at valid_from
  weights_json TEXT NOT NULL,                 -- {"family": {"momentum": 0.2, ...}, "sleeve": 0.0}
  valid_from TEXT NOT NULL, valid_to TEXT, decision_id TEXT, note TEXT, PRIMARY KEY (model_id, version)
);

CREATE TABLE IF NOT EXISTS model_weights (
  cohort_id TEXT NOT NULL REFERENCES cohorts, model_id TEXT NOT NULL REFERENCES models, model_version INTEGER NOT NULL,
  as_of TEXT NOT NULL, family TEXT NOT NULL, weight_units INTEGER NOT NULL CHECK(weight_units BETWEEN 0 AND 10000),
  n_eff REAL, alpha REAL, gate TEXT NOT NULL, evidence_hash TEXT NOT NULL, run_id INTEGER NOT NULL,
  PRIMARY KEY(cohort_id,model_id,family)
);

CREATE TABLE IF NOT EXISTS scores (
  cohort_id TEXT NOT NULL REFERENCES cohorts, as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  model_id TEXT NOT NULL, model_version INTEGER NOT NULL, sector_group TEXT NOT NULL, group_def_version INTEGER NOT NULL,
  family_scores_json TEXT NOT NULL, composite REAL, composite_neutral REAL, sector_tilt REAL, final REAL,
  rank_all INTEGER, rank INTEGER, rank_group INTEGER, decile INTEGER, quintile INTEGER,
  scored INTEGER NOT NULL, eligible INTEGER NOT NULL, exclusion_reason TEXT, liquidity_bucket TEXT,
  n_factors_used INTEGER NOT NULL, dc_flag INTEGER, input_hash TEXT NOT NULL, generated_at TEXT NOT NULL,
  track TEXT NOT NULL, run_id INTEGER NOT NULL, PRIMARY KEY(cohort_id,security_id,model_id),
  FOREIGN KEY(model_id,model_version) REFERENCES model_versions(model_id,version),
  FOREIGN KEY(cohort_id,as_of,track) REFERENCES cohorts(cohort_id,as_of,track)
);

CREATE TABLE IF NOT EXISTS labels (
  cohort_id TEXT NOT NULL REFERENCES cohorts, as_of TEXT NOT NULL, security_id INTEGER NOT NULL REFERENCES securities,
  horizon_m INTEGER NOT NULL, end_date TEXT NOT NULL, track TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1), evidence_hash TEXT NOT NULL, computed_at TEXT NOT NULL,
  r_log REAL, r_arith REAL, r_group_median REAL, l_rel REAL, r_uni REAL, sector_group TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ok','delisted_partial','suspended','excluded_ca','missing')),
  mb36 INTEGER, mb36_touch INTEGER, price_manifest_sha TEXT NOT NULL, computed_run_id INTEGER NOT NULL,
  decision_id TEXT, supersedes_revision INTEGER, PRIMARY KEY(cohort_id,security_id,horizon_m,revision),
  UNIQUE(cohort_id,security_id,horizon_m,evidence_hash),
  FOREIGN KEY(cohort_id,as_of,track) REFERENCES cohorts(cohort_id,as_of,track)
);

CREATE TABLE IF NOT EXISTS evaluations (
  eval_id INTEGER PRIMARY KEY, computed_run_id INTEGER NOT NULL, computed_at TEXT NOT NULL,
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, subject_version TEXT NOT NULL,
  as_of TEXT NOT NULL, horizon_m INTEGER NOT NULL, scope TEXT NOT NULL, track TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL, n INTEGER NOT NULL, n_eff REAL, se REAL, ci90_lo REAL, ci90_hi REAL, status TEXT NOT NULL,
  method TEXT NOT NULL, window_start TEXT NOT NULL DEFAULT '', window_end TEXT NOT NULL DEFAULT '',
  evidence_hash TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), supersedes_eval_id INTEGER REFERENCES evaluations,
  UNIQUE(subject_kind,subject_id,subject_version,as_of,horizon_m,scope,track,metric,method,window_start,window_end,revision),
  UNIQUE(subject_kind,subject_id,subject_version,as_of,horizon_m,scope,track,metric,method,window_start,window_end,evidence_hash)
);

CREATE TABLE IF NOT EXISTS evaluations_log (
  log_id INTEGER PRIMARY KEY, old_eval_id INTEGER NOT NULL REFERENCES evaluations,
  new_eval_id INTEGER NOT NULL UNIQUE REFERENCES evaluations, reason TEXT NOT NULL, decision_id TEXT, changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_curve (
  computed_at TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, subject_version TEXT NOT NULL,
  horizon_m INTEGER NOT NULL, track TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  months_clean INTEGER NOT NULL, n_labelled INTEGER NOT NULL, n_eff REAL NOT NULL,
  ic_cum_mean REAL, ic_hac_se REAL, ic_hac_t REAL, ci90_lo REAL, ci90_hi REAL, status TEXT NOT NULL,
  cusum_ic REAL, spread_net_cum REAL, slope_24 REAL,
  PRIMARY KEY(subject_kind,subject_id,subject_version,horizon_m,track,evidence_hash)
);

CREATE TABLE IF NOT EXISTS learning_curve_points (
  model_id TEXT NOT NULL, model_version INTEGER NOT NULL, cohort_id TEXT NOT NULL REFERENCES cohorts,
  horizon_m INTEGER NOT NULL, track TEXT NOT NULL, k INTEGER NOT NULL, train_end TEXT NOT NULL,
  test_as_of TEXT NOT NULL, realised_as_of TEXT NOT NULL, weights_json TEXT NOT NULL,
  oos_ic REAL, ew_oos_ic REAL, n INTEGER NOT NULL, evidence_hash TEXT NOT NULL, computed_run_id INTEGER NOT NULL,
  PRIMARY KEY(model_id,model_version,cohort_id,horizon_m,evidence_hash)
);

CREATE TABLE IF NOT EXISTS portfolios (portfolio_id TEXT PRIMARY KEY, model_id TEXT REFERENCES models,
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, subject_version TEXT NOT NULL,
  cohort_id TEXT REFERENCES cohorts, rule TEXT NOT NULL, cadence TEXT NOT NULL, inception TEXT, rule_version TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS portfolio_positions (portfolio_id TEXT NOT NULL, as_of TEXT NOT NULL, security_id INTEGER NOT NULL, weight REAL NOT NULL,
  entry_as_of TEXT NOT NULL, rank_at_entry INTEGER, liquidity_bucket TEXT, PRIMARY KEY (portfolio_id, as_of, security_id));

CREATE TABLE IF NOT EXISTS portfolio_trades (
  trade_id INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES portfolio_orders,
  portfolio_id TEXT NOT NULL REFERENCES portfolios, cohort_id TEXT NOT NULL REFERENCES cohorts,
  exec_at TEXT NOT NULL, security_id INTEGER NOT NULL, side TEXT NOT NULL, weight_delta REAL NOT NULL,
  fill_price REAL NOT NULL, cost_bps REAL NOT NULL, liquidity_bucket TEXT NOT NULL, price_manifest_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_returns (portfolio_id TEXT NOT NULL, month_end TEXT NOT NULL, revision INTEGER NOT NULL, evidence_hash TEXT NOT NULL, computed_at TEXT NOT NULL, ret_gross REAL, turnover_one_way REAL, cost REAL, ret_net REAL,
  ret_net_stress REAL, bm_ew REAL, bm_ew_sector REAL, bm_cw REAL, bm_index REAL, n_positions INTEGER, cost_model_version TEXT NOT NULL,
  PRIMARY KEY (portfolio_id, month_end, revision), UNIQUE(portfolio_id,month_end,evidence_hash));

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY, as_of TEXT NOT NULL, kind TEXT NOT NULL, track TEXT NOT NULL DEFAULT 'live',
  attempt INTEGER NOT NULL CHECK(attempt>=1), started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','ok','partial','blocked','failed','refused')),
  dq_status TEXT CHECK(dq_status IN ('passed','passed_with_warnings','blocked','legacy_defects')),
  git_sha TEXT NOT NULL, code_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL, registry_sha256 TEXT NOT NULL,
  yfinance_version TEXT, python_version TEXT, n_universe INTEGER, n_scored INTEGER, n_eligible INTEGER,
  http_calls INTEGER, http_429s INTEGER, override_decision_id TEXT, is_clean INTEGER NOT NULL DEFAULT 0, notes_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_asof_kind_track ON runs (as_of, kind, track, attempt);

CREATE TABLE IF NOT EXISTS hypotheses (
  family TEXT NOT NULL, review_opportunities_json TEXT NOT NULL, formula_sha256 TEXT NOT NULL,
  hypothesis_id TEXT PRIMARY KEY,                    -- 'H-2026-001'
  kind TEXT NOT NULL CHECK (kind IN ('factor','model','rule','sector_feature','data','cost_model')),
  subject_id TEXT NOT NULL, title TEXT NOT NULL, statement TEXT NOT NULL,
  expected_sign INTEGER CHECK (expected_sign IN (-1,1)), horizon_m INTEGER, primary_metric TEXT NOT NULL,
  success_criterion TEXT NOT NULL, failure_criterion TEXT NOT NULL,
  registered_on TEXT NOT NULL, registered_by TEXT NOT NULL,   -- 'human:<name>' | 'llm:<model>' | 'system'
  first_oos_as_of TEXT NOT NULL, code_sha TEXT, budget_year INTEGER NOT NULL, sequence_in_year INTEGER NOT NULL,
  counts_toward_budget INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('open','supported','rejected','withdrawn','inconclusive')),
  n_periods_at_eval INTEGER, t_hac_at_eval REAL, t_crit_at_eval REAL, m_tests_at_eval INTEGER,
  resolved_on TEXT, resolution TEXT, decision_id TEXT, md_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,                    -- 'X-2027-004'
  hypothesis_id TEXT REFERENCES hypotheses, run_id INTEGER REFERENCES runs,
  kind TEXT NOT NULL CHECK (kind IN ('shadow_eval','walk_forward','ablation','leakage','cost_calib','rescoring','backfill_dev','backfill_holdout')),
  config_json TEXT NOT NULL, code_sha TEXT NOT NULL, track TEXT NOT NULL, window_start TEXT, window_end TEXT,
  started_on TEXT NOT NULL, finished_on TEXT, result_json TEXT, verdict TEXT CHECK (verdict IN ('pass','fail','inconclusive')),
  counts_toward_budget INTEGER NOT NULL DEFAULT 0, md_path TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,                      -- 'P-2026-11-01'
  created_run_id INTEGER NOT NULL REFERENCES runs, as_of TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('promote_factor','probation','retire_factor','quarantine','release_quarantine','promote_model','demote_model',
                                     'register_hypothesis','rule_change','cost_model','taxonomy','data_fix','clear_ca_flag','accept_revision','gate_override','other')),
  subject_id TEXT NOT NULL, payload_json TEXT NOT NULL, evidence_json TEXT NOT NULL, rule_id TEXT NOT NULL,
  criteria_check_json TEXT, proposed_by TEXT NOT NULL, llm_review TEXT,
  status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','expired','superseded')),
  decided_on TEXT, decided_by TEXT, decision_id TEXT, md_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (                             -- decision content frozen; lifecycle fields are journaled control updates
  decision_id TEXT PRIMARY KEY,                      -- 'D-2026-11-01'
  proposal_id TEXT REFERENCES proposals, kind TEXT NOT NULL, tier INTEGER NOT NULL CHECK (tier IN (0,1,2)),
  subject_id TEXT NOT NULL, title TEXT NOT NULL, context TEXT NOT NULL, options_json TEXT NOT NULL, decision TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL, criteria_check_json TEXT,
  decided_on TEXT NOT NULL, decided_by TEXT NOT NULL, approver_kind TEXT NOT NULL CHECK (approver_kind IN ('human','llm','system')),
  ratified_by TEXT, ratified_on TEXT,                -- human co-signature required for LLM Tier-1 decisions within 60 days
  status TEXT NOT NULL CHECK (status IN ('approved','rejected','provisional','applied','superseded','reverted')),
  effective_from TEXT, applied_on TEXT, adr_path TEXT NOT NULL, supersedes TEXT, reverted_by TEXT, git_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (lesson_id INTEGER PRIMARY KEY, recorded_on TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, evidence_refs_json TEXT, decision_id TEXT, tags TEXT);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT);

CREATE TABLE IF NOT EXISTS benchmarks_monthly (
  month_end TEXT NOT NULL, benchmark_id TEXT NOT NULL, tri REAL, source TEXT NOT NULL,
  observed_at TEXT NOT NULL, evidence_hash TEXT NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY(month_end,benchmark_id,observed_at), UNIQUE(month_end,benchmark_id,evidence_hash)
);

CREATE TABLE IF NOT EXISTS field_contracts (field TEXT PRIMARY KEY, unit TEXT NOT NULL, min_value REAL, max_value REAL, max_null_rate REAL NOT NULL, source TEXT NOT NULL, notes TEXT, contract_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS legacy_snapshot_map (legacy_date TEXT PRIMARY KEY, as_of TEXT NOT NULL, is_full INTEGER NOT NULL, superseded_by TEXT, defects_json TEXT NOT NULL, migrated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS legacy_defects (defect_id TEXT PRIMARY KEY, snapshot_date TEXT NOT NULL, scope TEXT NOT NULL, field TEXT NOT NULL DEFAULT '', ticker TEXT NOT NULL DEFAULT '', defect_code TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE(snapshot_date,scope,field,ticker,defect_code));

CREATE INDEX IF NOT EXISTS ix_scores_model_asof ON scores (model_id, as_of);

CREATE INDEX IF NOT EXISTS ix_labels_asof_h ON labels (as_of, horizon_m);

CREATE INDEX IF NOT EXISTS ix_eval_subject ON evaluations (subject_kind, subject_id, horizon_m, metric);

CREATE TABLE IF NOT EXISTS cohorts (
  cohort_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, track TEXT NOT NULL CHECK(track IN ('live','backfill','legacy','counterfactual')),
  knowledge_cutoff TEXT NOT NULL, definition_hash TEXT NOT NULL, membership_hash TEXT NOT NULL,
  source_refs_json TEXT NOT NULL, published_at TEXT NOT NULL, generated_at TEXT NOT NULL,
  is_clean INTEGER NOT NULL CHECK(is_clean IN (0,1)), run_id INTEGER NOT NULL REFERENCES runs,
  UNIQUE(cohort_id,as_of,track), UNIQUE(as_of,track,definition_hash)
);

CREATE TABLE IF NOT EXISTS captures (
  capture_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, kind TEXT NOT NULL,
  archive_path TEXT NOT NULL, sha256 TEXT NOT NULL, source_version TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs
);

CREATE TABLE IF NOT EXISTS factor_status_history (
  factor_id TEXT NOT NULL REFERENCES factor_registry, effective_from TEXT NOT NULL, status TEXT NOT NULL,
  decision_id TEXT NOT NULL REFERENCES decisions, PRIMARY KEY(factor_id,effective_from)
);

CREATE TABLE IF NOT EXISTS portfolio_orders (
  order_id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL REFERENCES portfolios, cohort_id TEXT NOT NULL REFERENCES cohorts,
  security_id INTEGER NOT NULL REFERENCES securities, created_at TEXT NOT NULL, earliest_exec_at TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('rebalance','entry','exit')),
  side TEXT NOT NULL, target_weight REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','filled','cancelled')),
  liquidity_bucket TEXT NOT NULL, decision_id TEXT, UNIQUE(portfolio_id,cohort_id,security_id,purpose)
);

CREATE TABLE IF NOT EXISTS ledger_events (
  seq INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs, recorded_at TEXT NOT NULL,
  table_name TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('insert','update','delete')),
  key_json TEXT NOT NULL, before_sha256 TEXT NOT NULL, after_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
  sha256 TEXT PRIMARY KEY, effective_from TEXT NOT NULL, content_toml TEXT NOT NULL, decision_id TEXT NOT NULL REFERENCES decisions
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_live_cohort ON cohorts(as_of) WHERE track='live';
CREATE TRIGGER IF NOT EXISTS immutable_cohorts_update BEFORE UPDATE ON cohorts BEGIN SELECT RAISE(ABORT, 'immutable cohorts'); END;

CREATE TRIGGER IF NOT EXISTS immutable_cohorts_delete BEFORE DELETE ON cohorts BEGIN SELECT RAISE(ABORT, 'immutable cohorts'); END;

CREATE TRIGGER IF NOT EXISTS immutable_fundamentals_update BEFORE UPDATE ON fundamentals BEGIN SELECT RAISE(ABORT, 'immutable fundamentals'); END;

CREATE TRIGGER IF NOT EXISTS immutable_fundamentals_delete BEFORE DELETE ON fundamentals BEGIN SELECT RAISE(ABORT, 'immutable fundamentals'); END;

CREATE TRIGGER IF NOT EXISTS immutable_holdings_update BEFORE UPDATE ON holdings BEGIN SELECT RAISE(ABORT, 'immutable holdings'); END;

CREATE TRIGGER IF NOT EXISTS immutable_holdings_delete BEFORE DELETE ON holdings BEGIN SELECT RAISE(ABORT, 'immutable holdings'); END;

CREATE TRIGGER IF NOT EXISTS immutable_security_attributes_update BEFORE UPDATE ON security_attributes BEGIN SELECT RAISE(ABORT, 'immutable security_attributes'); END;

CREATE TRIGGER IF NOT EXISTS immutable_security_attributes_delete BEFORE DELETE ON security_attributes BEGIN SELECT RAISE(ABORT, 'immutable security_attributes'); END;

CREATE TRIGGER IF NOT EXISTS immutable_factor_values_update BEFORE UPDATE ON factor_values BEGIN SELECT RAISE(ABORT, 'immutable factor_values'); END;

CREATE TRIGGER IF NOT EXISTS immutable_factor_values_delete BEFORE DELETE ON factor_values BEGIN SELECT RAISE(ABORT, 'immutable factor_values'); END;

CREATE TRIGGER IF NOT EXISTS immutable_model_weights_update BEFORE UPDATE ON model_weights BEGIN SELECT RAISE(ABORT, 'immutable model_weights'); END;

CREATE TRIGGER IF NOT EXISTS immutable_model_weights_delete BEFORE DELETE ON model_weights BEGIN SELECT RAISE(ABORT, 'immutable model_weights'); END;

CREATE TRIGGER IF NOT EXISTS immutable_scores_update BEFORE UPDATE ON scores BEGIN SELECT RAISE(ABORT, 'immutable scores'); END;

CREATE TRIGGER IF NOT EXISTS immutable_scores_delete BEFORE DELETE ON scores BEGIN SELECT RAISE(ABORT, 'immutable scores'); END;

CREATE TRIGGER IF NOT EXISTS immutable_labels_update BEFORE UPDATE ON labels BEGIN SELECT RAISE(ABORT, 'immutable labels'); END;

CREATE TRIGGER IF NOT EXISTS immutable_labels_delete BEFORE DELETE ON labels BEGIN SELECT RAISE(ABORT, 'immutable labels'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evaluations_update BEFORE UPDATE ON evaluations BEGIN SELECT RAISE(ABORT, 'immutable evaluations'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evaluations_delete BEFORE DELETE ON evaluations BEGIN SELECT RAISE(ABORT, 'immutable evaluations'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evaluations_log_update BEFORE UPDATE ON evaluations_log BEGIN SELECT RAISE(ABORT, 'immutable evaluations_log'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evaluations_log_delete BEFORE DELETE ON evaluations_log BEGIN SELECT RAISE(ABORT, 'immutable evaluations_log'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evidence_curve_update BEFORE UPDATE ON evidence_curve BEGIN SELECT RAISE(ABORT, 'immutable evidence_curve'); END;

CREATE TRIGGER IF NOT EXISTS immutable_evidence_curve_delete BEFORE DELETE ON evidence_curve BEGIN SELECT RAISE(ABORT, 'immutable evidence_curve'); END;

CREATE TRIGGER IF NOT EXISTS immutable_learning_curve_points_update BEFORE UPDATE ON learning_curve_points BEGIN SELECT RAISE(ABORT, 'immutable learning_curve_points'); END;

CREATE TRIGGER IF NOT EXISTS immutable_learning_curve_points_delete BEFORE DELETE ON learning_curve_points BEGIN SELECT RAISE(ABORT, 'immutable learning_curve_points'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_trades_update BEFORE UPDATE ON portfolio_trades BEGIN SELECT RAISE(ABORT, 'immutable portfolio_trades'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_trades_delete BEFORE DELETE ON portfolio_trades BEGIN SELECT RAISE(ABORT, 'immutable portfolio_trades'); END;

CREATE TRIGGER IF NOT EXISTS immutable_captures_update BEFORE UPDATE ON captures BEGIN SELECT RAISE(ABORT, 'immutable captures'); END;

CREATE TRIGGER IF NOT EXISTS immutable_captures_delete BEFORE DELETE ON captures BEGIN SELECT RAISE(ABORT, 'immutable captures'); END;

CREATE TRIGGER IF NOT EXISTS immutable_factor_status_history_update BEFORE UPDATE ON factor_status_history BEGIN SELECT RAISE(ABORT, 'immutable factor_status_history'); END;

CREATE TRIGGER IF NOT EXISTS immutable_factor_status_history_delete BEFORE DELETE ON factor_status_history BEGIN SELECT RAISE(ABORT, 'immutable factor_status_history'); END;

CREATE TRIGGER IF NOT EXISTS immutable_ledger_events_update BEFORE UPDATE ON ledger_events BEGIN SELECT RAISE(ABORT, 'immutable ledger_events'); END;

CREATE TRIGGER IF NOT EXISTS immutable_ledger_events_delete BEFORE DELETE ON ledger_events BEGIN SELECT RAISE(ABORT, 'immutable ledger_events'); END;

CREATE TRIGGER IF NOT EXISTS immutable_config_versions_update BEFORE UPDATE ON config_versions BEGIN SELECT RAISE(ABORT, 'immutable config_versions'); END;

CREATE TRIGGER IF NOT EXISTS immutable_config_versions_delete BEFORE DELETE ON config_versions BEGIN SELECT RAISE(ABORT, 'immutable config_versions'); END;

CREATE TRIGGER IF NOT EXISTS immutable_corporate_actions_update BEFORE UPDATE ON corporate_actions BEGIN SELECT RAISE(ABORT, 'immutable corporate_actions'); END;

CREATE TRIGGER IF NOT EXISTS immutable_corporate_actions_delete BEFORE DELETE ON corporate_actions BEGIN SELECT RAISE(ABORT, 'immutable corporate_actions'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_returns_update BEFORE UPDATE ON portfolio_returns BEGIN SELECT RAISE(ABORT, 'immutable portfolio_returns'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_returns_delete BEFORE DELETE ON portfolio_returns BEGIN SELECT RAISE(ABORT, 'immutable portfolio_returns'); END;

CREATE TRIGGER IF NOT EXISTS immutable_legacy_defects_update BEFORE UPDATE ON legacy_defects BEGIN SELECT RAISE(ABORT, 'immutable legacy_defects'); END;

CREATE TRIGGER IF NOT EXISTS immutable_legacy_defects_delete BEFORE DELETE ON legacy_defects BEGIN SELECT RAISE(ABORT, 'immutable legacy_defects'); END;

CREATE TRIGGER IF NOT EXISTS immutable_sector_features_update BEFORE UPDATE ON sector_features BEGIN SELECT RAISE(ABORT, 'immutable sector_features'); END;

CREATE TRIGGER IF NOT EXISTS immutable_sector_features_delete BEFORE DELETE ON sector_features BEGIN SELECT RAISE(ABORT, 'immutable sector_features'); END;

CREATE TRIGGER IF NOT EXISTS immutable_benchmarks_monthly_update BEFORE UPDATE ON benchmarks_monthly BEGIN SELECT RAISE(ABORT, 'immutable benchmarks_monthly'); END;

CREATE TRIGGER IF NOT EXISTS immutable_benchmarks_monthly_delete BEFORE DELETE ON benchmarks_monthly BEGIN SELECT RAISE(ABORT, 'immutable benchmarks_monthly'); END;

CREATE TRIGGER IF NOT EXISTS immutable_prices_monthly_update BEFORE UPDATE ON prices_monthly BEGIN SELECT RAISE(ABORT, 'immutable prices_monthly'); END;

CREATE TRIGGER IF NOT EXISTS immutable_prices_monthly_delete BEFORE DELETE ON prices_monthly BEGIN SELECT RAISE(ABORT, 'immutable prices_monthly'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_positions_update BEFORE UPDATE ON portfolio_positions BEGIN SELECT RAISE(ABORT, 'immutable portfolio_positions'); END;

CREATE TRIGGER IF NOT EXISTS immutable_portfolio_positions_delete BEFORE DELETE ON portfolio_positions BEGIN SELECT RAISE(ABORT, 'immutable portfolio_positions'); END;
