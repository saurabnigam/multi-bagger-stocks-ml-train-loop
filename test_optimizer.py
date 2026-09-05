"""Unit tests for the optimizer's pure functions (no database, no network)."""
import numpy as np
import pandas as pd
import pytest

from weight_optimizer import (
    build_transitions, forward_returns, project_weights, exp_gradient_step,
    period_stats, rank_ic, ic_tstat, FACTOR_MAP, FLOOR, CEIL,
)
from quant_math import FACTOR_WEIGHT_KEYS, DEFAULT_WEIGHTS


def test_build_transitions_skips_short_gaps():
    t = build_transitions(['2026-06-12', '2026-06-14', '2026-07-11', '2026-08-14'])
    assert t == [('2026-06-14', '2026-07-11', 27), ('2026-07-11', '2026-08-14', 34)]


def _panel():
    rng = np.random.default_rng(0)
    tickers = [f"T{i}.NS" for i in range(60)]
    rows = []
    for d in ['2026-06-14', '2026-07-11']:
        for i, t in enumerate(tickers):
            rows.append({
                'id': len(rows) + 1, 'date': d, 'ticker': t,
                'price': 100.0 * (1.1 if d == '2026-07-11' else 1.0) * (1 + 0.01 * i),
                'quality_score': rng.choice([0, 50, 100]), 'growth_score': rng.choice([0, 20, 50, 80, 100]),
                'valuation_score': rng.choice([0, 20, 80]), 'risk_score': 50, 'moat_score': 50,
                'bs_score': rng.choice([30, 70, 100]), 'cap_alloc_score': rng.choice([50, 90]),
                'smart_money_score': rng.choice([30, 50, 70]), 'trap_score': rng.choice([0, 20, 60]),
                'momentum_multiplier': rng.choice([0.0, 0.8, 1.0]), 'concall_sentiment_score': 0,
            })
    df = pd.DataFrame(rows)
    df['final_score'] = df['growth_score'] * 0.5 + df['quality_score'] * 0.5
    df.loc[df.momentum_multiplier == 0, 'final_score'] = 0
    return df


def test_forward_returns_excludes_corporate_actions():
    df = _panel()
    # inject a 6:1 split
    df.loc[(df.ticker == 'T5.NS') & (df.date == '2026-07-11'), 'price'] /= 6
    pm = df.pivot_table(index='ticker', columns='date', values='price')
    clean, suspect = forward_returns(df, pm, '2026-06-14', '2026-07-11')
    assert list(suspect.ticker) == ['T5.NS']
    assert len(clean) == 59
    assert clean['fwd_return'].abs().max() < 0.6


def test_project_weights_respects_bounds_and_sums_to_one():
    raw = {k: v for k, v in zip(FACTOR_WEIGHT_KEYS, [0.9, 0.001, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05])}
    w = project_weights(raw)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(FLOOR - 1e-9 <= v <= CEIL + 1e-9 for v in w.values())
    assert w['quality_weight'] == CEIL
    # mass clipped from quality is redistributed, so the others end up equal
    assert w['growth_weight'] == pytest.approx(0.10, abs=0.001)


def test_project_weights_floor_binds_when_many_are_tiny():
    raw = {k: v for k, v in zip(FACTOR_WEIGHT_KEYS, [0.30, 0.30, 0.30, 0.001, 0.001, 0.001, 0.001, 0.001])}
    w = project_weights(raw)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v >= FLOOR - 1e-9 for v in w.values())
    assert w['risk_weight'] == FLOOR


def test_project_weights_rounding_residue_goes_to_best_key():
    raw = {k: 1 / 8 + (0.0004 if i == 0 else 0) for i, k in enumerate(FACTOR_WEIGHT_KEYS)}
    w = project_weights(raw, best_key='smart_money_weight')
    assert round(sum(w.values()), 3) == 1.0
    assert all(round(v, 3) == v for v in w.values())


def test_exp_gradient_step_direction_and_clip():
    ic = {f: 0.0 for f in FACTOR_MAP}
    ic['growth'] = 0.5     # clipped: exp(0.4)
    ic['valuation'] = -0.5  # clipped: exp(-0.4)
    out = exp_gradient_step(dict(DEFAULT_WEIGHTS), ic)
    assert out['growth_weight'] == pytest.approx(0.20 * np.exp(0.4))
    assert out['valuation_weight'] == pytest.approx(0.15 * np.exp(-0.4))
    assert out['quality_weight'] == pytest.approx(0.20)


def test_step_is_zero_when_ic_is_zero():
    """Idempotency at the maths level: no information -> no movement."""
    ic = {f: 0.0 for f in FACTOR_MAP}
    w = project_weights(exp_gradient_step(dict(DEFAULT_WEIGHTS), ic))
    assert w == DEFAULT_WEIGHTS


def test_period_stats_separates_killed_bucket():
    df = _panel()
    pm = df.pivot_table(index='ticker', columns='date', values='price')
    clean, _ = forward_returns(df, pm, '2026-06-14', '2026-07-11')
    st = period_stats(clean, dict(DEFAULT_WEIGHTS))
    assert st['killed_n'] + st['alive_n'] == st['n_stocks']
    assert set(st['attribution']) >= {'final_score (stored)', 'momentum multiplier alone',
                                      'fundamental composite (no multipliers)'}
    assert set(st['ic_dict']) == set(FACTOR_MAP)


def test_rank_ic_handles_constant_input():
    x = pd.Series([50.0] * 10)
    y = pd.Series(np.arange(10, dtype=float))
    assert rank_ic(x, y) == 0.0


def test_tstat_scale():
    assert ic_tstat(0.1, 500) == pytest.approx(2.24, abs=0.02)
    assert ic_tstat(0.0, 500) == 0.0
