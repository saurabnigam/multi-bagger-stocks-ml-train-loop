# WS08 — Paper portfolios, liquidity buckets, cost model, alpha scoreboard, crowding monitors

## 1. Mission
Turn rankings into a paper track record that pays realistic Indian trading costs, trades at T+1, holds with a buffer, and is compared to the right nulls. "Alpha" gets a definition, a sample-size guard and a verdict word; nobody sizes real money on 18 months of paper.

## 2. Read first
1. MASTER_SPEC §8 (all), §7.4 (benchmarks), §7.6 (net spreads), §10.4 `[portfolio]` `[costs]`, §13 D16/D17, §15 Q1/Q14
2. WS06 (`scores` columns), WS07 (`evaluations`, `benchmarks`), WS02 (`tri`, `adv_inr`) docs §6

## 3. Scope
In: cost buckets and stack, portfolio construction (top-30 buffer, decile, quarterly tranches, EW benchmark portfolio), monthly roll-forward with T+1 execution and dividends, `portfolio_*` tables, scoreboard rows + verdict + years-to-significance, crowding monitors, `spread_net` cost inputs for WS07, `portfolio rebalance|scoreboard` commands.
Out: report prose (WS09), UI (WS11).

## 4. Dependencies
WS06 (scores with `rank`, `eligible`, `liquidity_bucket`, `sector_group`), WS07 (`benchmarks_monthly`, `evaluations`), WS02 (`tri`). Verify the synthetic world has scores for all months.

## 5. Interfaces you consume
`scores` table, `PriceStore.tri/close_raw/adv_inr`, `calendar.add_trading_days`, `benchmarks.series/ew_tri`, `universe.members_at`, `evaluation.stats.hac_mean_test`, `gates.record_event`.

## 6. Interfaces you provide
```python
# quant/portfolio/costs.py
def bucket(adv_inr: float, cfg) -> str                                            # 'A'|'B'|'C'|'D' from cfg.costs.bucket_adv_inr (A>=5e8, B>=1e8, C>=2e7, else D)
def cost_bps_one_way(bucket: str, cfg, stress: bool = False) -> float             # fixed 12 + impact ; x1.5 when stress ; D -> inf (not tradable)
def trade_cost(weight_delta: float, bucket: str, cfg, stress=False) -> float
# quant/portfolio/construct.py
def rebalance(prev: pd.DataFrame, ranks: pd.Series, eligible: pd.Series, groups: pd.Series, cfg, rule: str = 'top30_buffer') -> tuple[pd.DataFrame, pd.DataFrame]
    # rules: 'top30_buffer' (enter rank<=30, keep while rank<=60, sector cap 6, EW), 'decile' (top 10% eligible, EW, no buffer), 'ew_universe' (all eligible EW) ; returns (positions[security_id, weight, entry_as_of], trades[security_id, side, weight_delta])
def tranche_rebalance(prev_tranches: list[pd.DataFrame], ranks, eligible, groups, cfg, month_index: int) -> ...   # quarterly variant: 3 tranches, one refreshed per month
# quant/portfolio/paper.py
PORTFOLIO_RULES = {'TOP30': 'top30_buffer', 'DEC10': 'decile', 'TOP30_Q': 'tranches', 'BM_EW': 'ew_universe'}
def ensure_portfolios(conn, cfg) -> None                                           # one PF_<model>_<rule> per non-retired model + PF_BM_EW ; portfolios rows
def rebalance_all(conn, store, as_of: str, cfg) -> PaperReport                     # for each portfolio: positions at as_of from scores(as_of) ; trades executed at calendar.add_trading_days(as_of, 1) close ; costs by bucket at trade time ; writes portfolio_positions, portfolio_trades
def roll_forward(conn, store, portfolio_id: str, month_end: str, cfg) -> dict      # returns for the month ending month_end from positions at the prior as_of using TRI (dividends in) ; drift ; ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_* ; writes portfolio_returns
def returns(conn, portfolio_id: str, start: str, end: str) -> pd.DataFrame
def cost_drag_for_quintile(conn, store, as_of: str, model_id: str, horizon_m: int, cfg) -> float   # used by WS07 spread_net: measured turnover into the top quintile over h x round-trip cost
# quant/portfolio/scoreboard.py
def compute(conn, as_of: str, cfg) -> pd.DataFrame                                 # §8.4 rows per portfolio x window ('since_inception','rolling_12','rolling_36') ; excess_vs_ew, _ew_sector, _cw, _mom30, _qual30 ; te, ir, hac_t, max_dd, hit_rate, turnover_1y, cost_drag_1y, n_months, verdict, years_to_significance
def verdict(ir: float, t: float, n_months: int) -> str                             # 'insufficient' (<24) | 'negative' | 'weak positive' | 'positive' (ir>0.5 and t>=2)
def years_to_significance(ir: float) -> float                                      # (2/ir)^2 ; inf for ir<=0
def alpha_word_allowed(n_months: int, t: float) -> bool                            # n>=24 and t>=2.0
def crowding(conn, as_of: str, cfg) -> dict                                        # overlap with MOM30/QUAL30 lists ; top-decile median earnings yield vs universe ; group weights vs universe ; top10 concentration ; share of sells held < 12 months
```

## 7. Deliverables
`quant/portfolio/{__init__,costs,construct,paper,scoreboard}.py`, `config/costs_v1.toml`, `quant/commands/portfolio.py` (`portfolio rebalance --as-of D`, `portfolio scoreboard [--as-of D]`), `tests/unit/test_costs.py`, `tests/unit/test_construct.py`, `tests/unit/test_paper.py`, `tests/unit/test_scoreboard.py`, PROGRESS entry.

## 8. Implementation plan
1. `costs_v1.toml`: the §8.3 table with `version`, `source`, `as_of` per line; loader merges with `cfg.costs`.
2. `construct.rebalance` for the three rules; determinism (ties by security_id); sector cap enforced at entry (skip to next-ranked name); buffer logic; positions carry `entry_as_of`.
3. `paper.rebalance_all`: portfolios per model; positions at `as_of`; execution date `as_of + 1 td`; trade cost via bucket of the traded name at `as_of`; write tables.
4. `roll_forward`: gross return = Σ w_prev × (TRI_end/TRI_start − 1) with weights drifted from the prior rebalance; turnover one-way = 0.5 Σ|Δw|; cost = Σ|Δw| × one-way bps; net; stress; benchmark columns from `benchmarks_monthly` (BM_EW built from the same eligible set at the prior as_of; BM_EW_SECTOR from per-group EW series weighted by the portfolio's group weights); write `portfolio_returns` with `cost_model_version`.
5. `scoreboard.compute`: windows; excess series; TE, IR (n ≥ 24 else NaN), HAC t (lag 0), max drawdown on net NAV, hit rate, turnover_1y, cost_drag_1y, verdict, years_to_significance; always include `PF_EW_HIER_v1_TOP30` row (honesty row).
6. `crowding`: read `universe_membership` index lists; `security_attributes` earnings yield proxy via `ttm(EBIT)/ev` from WS05 values or `attributes.at('trailing_pe')`; group weights; concentration; STCG share from `portfolio_trades` entry dates.
7. `cost_drag_for_quintile` for WS07: quintile membership from `scores.quintile` at `s` and `s+h`; turnover × round trip by bucket mix.
8. Commands with summary lines (`[9 portfolio] PF_EW_HIER_v1_TOP30: 30 names, turnover 6.7% one-way, cost 2.5 bp; PF_BM_EW 471 names`).
9. Tests; PROGRESS; commit `WS08: portfolios, costs, scoreboard`.

## 9. Tests you must write
```
tests/unit/test_costs.py::test_bucket_boundaries_inclusive_exclusive              5e8 -> A ; 4.99e8 -> B ; 2e7 -> C ; 1.99e7 -> D
tests/unit/test_costs.py::test_round_trip_a_44_b_74_c_124_bp_and_stress_1_5x
tests/unit/test_costs.py::test_bucket_d_not_tradable
tests/unit/test_construct.py::test_top30_buffer_keeps_rank_45_drops_rank_61
tests/unit/test_construct.py::test_sector_cap_6_enforced_skips_to_next_rank
tests/unit/test_construct.py::test_decile_rule_no_buffer
tests/unit/test_construct.py::test_ew_universe_rule_all_eligible
tests/unit/test_construct.py::test_ineligible_holding_is_sold
tests/unit/test_construct.py::test_deterministic_on_ties
tests/unit/test_paper.py::test_execution_at_t_plus_1_close_not_as_of
tests/unit/test_paper.py::test_turnover_half_sum_abs_and_cost_uses_bucket_at_trade_time
tests/unit/test_paper.py::test_dividends_and_split_in_gross_return              synthetic world events
tests/unit/test_paper.py::test_drift_between_rebalances
tests/unit/test_paper.py::test_ew_universe_portfolio_alpha_vs_bm_ew_is_zero      within rounding
tests/unit/test_paper.py::test_cost_drag_for_quintile_measured_not_assumed
tests/unit/test_scoreboard.py::test_ir_hidden_below_24_months_and_verdict_words
tests/unit/test_scoreboard.py::test_years_to_significance
tests/unit/test_scoreboard.py::test_alpha_word_rule
tests/unit/test_scoreboard.py::test_honesty_row_always_present
tests/unit/test_scoreboard.py::test_crowding_overlap_and_stcg_share
```

## 10. Verification checklist
- Synthetic world scored for 48 months: `for m in <months>; do python -m quant portfolio rebalance --as-of $m --db /tmp/w/quant.db; done` then `python -m quant portfolio scoreboard --db /tmp/w/quant.db` prints rows for every model with `verdict` and `years_to_significance`; `PF_BM_EW` excess vs BM_EW ≈ 0.
- `select portfolio_id, avg(turnover_one_way), avg(cost) from portfolio_returns group by 1` shows TOP30 turnover well below DEC10.
- `python -m pytest tests/unit/test_costs.py tests/unit/test_construct.py tests/unit/test_paper.py tests/unit/test_scoreboard.py -q` green.

## 11. Definition of done
- [ ] Cost table versioned; buckets tested at boundaries
- [ ] Three rules + tranches; buffer and sector cap proven
- [ ] T+1 execution; dividends; drift; stress; benchmark columns
- [ ] Scoreboard with verdict, years-to-significance, alpha-word rule, honesty row, crowding
- [ ] PROGRESS; commit

## 12. Handoff notes
- WS07 calls `cost_drag_for_quintile` for `spread_net`; WS09 report reads `scoreboard.compute` and `alpha_word_allowed`; WS11 renders NAV series from `portfolio_returns`.
- Changing `costs_v1.toml` is a decision; WS09 checks the ±25% tier boundary.

## 13. Risks, gotchas
- Same-bar execution is the classic paper-portfolio flattery; T+1 is mandatory.
- BM_EW must use the same eligible set and the same cost model as the portfolio or "alpha" is a construction artefact.
- Never annualise 6 months of returns into an IR; the n ≥ 24 guard is in code.
