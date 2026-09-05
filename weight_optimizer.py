"""
V18 Multi-Period Panel Optimizer (red-teamed edition).

What changed and why — see docs/analysis/red_team_review.md:
  * IDEMPOTENT. The old optimizer re-applied the same gradient every time it
    ran (daily via cron) on the same three periods, so weights drifted with no
    new information (two runs on 2026-09-03 moved Growth 28.2% -> 30.0%).
    Now each transition is learned from exactly once, tracked via
    active_weights.trained_through. Re-running is a no-op.
  * Unadjusted corporate actions are excluded from returns. A 6:1 split in
    ZFCVINDIA produced a "-84%" return that moved a quintile mean by 0.9 pts.
  * Alpha attribution: reports the IC of the momentum filter and of the
    fundamental composite separately, plus the killed (score=0) bucket. The
    headline "composite IC" was mostly the death-cross filter.
  * t-statistics per period and an honest IR (n=3 is not a distribution).
  * Pure functions (build_transitions, period_stats, project_weights, ...) so
    the maths is unit-testable without a database.
"""
import argparse
import datetime
import sqlite3
import numpy as np
import pandas as pd

from config import DB_PATH, FULL_UNIVERSE_MIN, CORPORATE_ACTION_ABS_RETURN
from db_setup import ensure_schema
from quant_math import FACTOR_WEIGHT_KEYS, DEFAULT_WEIGHTS, trap_penalty_multiplier

FACTOR_MAP = {
    'quality': ('quality_score', 'quality_weight'),
    'growth': ('growth_score', 'growth_weight'),
    'valuation': ('valuation_score', 'valuation_weight'),
    'risk': ('risk_score', 'risk_weight'),
    'moat': ('moat_score', 'moat_weight'),
    'bs': ('bs_score', 'bs_weight'),
    'cap_alloc': ('cap_alloc_score', 'cap_alloc_weight'),
    'smart_money': ('smart_money_score', 'smart_money_weight'),
}
FLOOR, CEIL = 0.05, 0.30
LEARNING_RATE = 0.45
GRAD_SCALE = 5.0
GRAD_CLIP = 0.4
MIN_TRANSITION_DAYS = 7


# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #
def build_transitions(snapshot_dates, min_days=MIN_TRANSITION_DAYS):
    """Consecutive snapshot pairs at least `min_days` apart -> [(d1, d2, days)]."""
    dates = sorted(snapshot_dates)
    out = []
    for d1, d2 in zip(dates, dates[1:]):
        days = (pd.to_datetime(d2) - pd.to_datetime(d1)).days
        if days >= min_days:
            out.append((d1, d2, days))
    return out


def forward_returns(preds, price_matrix, start_d, end_d, ca_threshold=CORPORATE_ACTION_ABS_RETURN):
    """
    Attach fwd_return to the start_d cross-section using prices already in the
    DB (never re-fetched). Rows with |return| > ca_threshold are dropped as
    suspected unadjusted corporate actions and returned separately.
    """
    sub = preds[preds['date'] == start_d].copy()
    sub['p_start'] = sub['ticker'].map(price_matrix[start_d])
    sub['p_end'] = sub['ticker'].map(price_matrix[end_d])
    sub['fwd_return'] = (sub['p_end'] - sub['p_start']) / sub['p_start']
    sub = sub.dropna(subset=['fwd_return'])
    suspect = sub[sub['fwd_return'].abs() > ca_threshold]
    clean = sub[sub['fwd_return'].abs() <= ca_threshold].copy()
    return clean, suspect


def rank_ic(x, y):
    if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
        return 0.0
    v = x.corr(y, method='spearman')
    return 0.0 if np.isnan(v) else float(v)


def ic_tstat(ic, n):
    """t-stat of a Spearman correlation under independence. Cross-sectional
    returns share market/sector factors, so the effective n is smaller and this
    overstates significance. Use as an upper bound."""
    if n < 3 or abs(ic) >= 1:
        return 0.0
    return ic * np.sqrt((n - 2) / (1 - ic ** 2))


def composite(sub, weights):
    return sum(sub[col] * weights[wkey] for col, wkey in FACTOR_MAP.values())


def period_stats(sub, weights_in_force):
    """
    All the per-period diagnostics from a cross-section that already has
    fwd_return. `weights_in_force` are the active weights when the snapshot
    was scored (so the fundamental composite matches what was stored).
    """
    n = len(sub)
    r = sub['fwd_return']
    ic_dict = {f: rank_ic(sub[col], r) for f, (col, _) in FACTOR_MAP.items()}

    base = composite(sub, weights_in_force)
    eq_w = {k: 1.0 / len(FACTOR_WEIGHT_KEYS) for k in FACTOR_WEIGHT_KEYS}
    eq_base = composite(sub, eq_w)
    mult = sub['trap_score'].map(trap_penalty_multiplier) * sub['momentum_multiplier']

    attribution = {
        'final_score (stored)': rank_ic(sub['final_score'], r),
        'fundamental composite (no multipliers)': rank_ic(base, r),
        'equal-weight composite (no multipliers)': rank_ic(eq_base, r),
        'equal-weight composite x multipliers': rank_ic(eq_base * mult, r),
        'momentum multiplier alone': rank_ic(sub['momentum_multiplier'], r),
        'trap score alone (sign flipped)': rank_ic(-sub['trap_score'], r),
    }
    if 'concall_sentiment_score' in sub.columns:
        attribution['headline sentiment alone'] = rank_ic(sub['concall_sentiment_score'], r)

    killed = sub[sub['final_score'] <= 0]
    alive = sub[sub['final_score'] > 0].copy()
    quintiles = {}
    if len(alive) >= 25:
        alive['quintile'] = pd.qcut(alive['final_score'].rank(method='first'), 5, labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'])
        quintiles = (alive.groupby('quintile', observed=False)['fwd_return'].mean() * 100).to_dict()

    return {
        'n_stocks': n,
        'market_mean_return': r.mean() * 100,
        'market_median_return': r.median() * 100,
        'ic_dict': ic_dict,
        'final_rank_ic': attribution['final_score (stored)'],
        'final_tstat': ic_tstat(attribution['final_score (stored)'], n),
        'attribution': attribution,
        'killed_n': len(killed),
        'killed_mean': killed['fwd_return'].mean() * 100 if len(killed) else float('nan'),
        'alive_n': len(alive),
        'alive_mean': alive['fwd_return'].mean() * 100 if len(alive) else float('nan'),
        'quintiles': quintiles,
        'top_bottom_spread': (quintiles.get('Q5', np.nan) - quintiles.get('Q1', np.nan)) if quintiles else float('nan'),
    }


def exp_gradient_step(current_weights, agg_ic, lr=LEARNING_RATE):
    """w_new = w_old * exp(clip(lr * 5 * IC)). Multiplicative-weights update."""
    out = {}
    for factor, (_, w_key) in FACTOR_MAP.items():
        multiplier = np.exp(np.clip(lr * agg_ic[factor] * GRAD_SCALE, -GRAD_CLIP, GRAD_CLIP))
        out[w_key] = current_weights[w_key] * multiplier
    return out


def project_weights(raw, floor=FLOOR, ceil=CEIL, best_key=None):
    """
    Project onto {sum == 1, floor <= w <= ceil}, then round to 3 dp with the
    rounding residue assigned to `best_key` so the sum is exactly 1.000.
    """
    adj = dict(raw)
    for _ in range(50):
        tot = sum(adj.values())
        adj = {k: v / tot for k, v in adj.items()}
        clamped = False
        for k in adj:
            if adj[k] < floor:
                adj[k] = floor; clamped = True
            elif adj[k] > ceil:
                adj[k] = ceil; clamped = True
        if not clamped:
            break
    # Final exact projection: distribute residue over unclamped keys
    free = [k for k in adj if floor < adj[k] < ceil]
    residue = 1.0 - sum(adj.values())
    if free and abs(residue) > 1e-12:
        share = residue / len(free)
        for k in free:
            adj[k] += share
    new = {k: round(v, 3) for k, v in adj.items()}
    remainder = round(1.0 - sum(new.values()), 3)
    if best_key is None or best_key not in new:
        best_key = max(new, key=new.get)
    new[best_key] = round(new[best_key] + remainder, 3)
    assert abs(sum(new.values()) - 1.0) < 1e-9, new
    return new


def weights_in_force(weights_df, snapshot_date):
    """Latest active_weights row with last_updated <= snapshot_date (else the first row)."""
    prior = weights_df[weights_df['last_updated'] <= snapshot_date]
    row = prior.iloc[-1] if len(prior) else weights_df.iloc[0]
    return {k: float(row[k]) for k in FACTOR_WEIGHT_KEYS}


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def load_panel(conn):
    preds = pd.read_sql_query('''
        SELECT id, date, ticker, price, quality_score, valuation_score, growth_score,
               moat_score, risk_score, bs_score, cap_alloc_score, smart_money_score,
               trap_score, momentum_multiplier, final_score, concall_sentiment_score
        FROM daily_predictions
    ''', conn)
    counts = preds.groupby('date').size()
    snapshot_dates = sorted(counts[counts >= FULL_UNIVERSE_MIN].index.tolist())
    price_matrix = preds.pivot_table(index='ticker', columns='date', values='price')
    weights_df = pd.read_sql_query('SELECT * FROM active_weights ORDER BY id', conn)
    return preds, snapshot_dates, price_matrix, weights_df


def evaluate_transitions(preds, price_matrix, weights_df, transitions, conn=None, verbose=True):
    """Return list of period result dicts; optionally write performance_tracking."""
    results = []
    for start_d, end_d, days in transitions:
        clean, suspect = forward_returns(preds, price_matrix, start_d, end_d)
        if len(clean) < 30:
            continue
        if verbose and len(suspect):
            for _, s in suspect.iterrows():
                print(f"  ⚠️  Excluding {s['ticker']} {start_d}➔{end_d}: {s['fwd_return']*100:+.1f}% "
                      f"(₹{s['p_start']:.2f} ➔ ₹{s['p_end']:.2f}) — suspected unadjusted split/bonus")
        if conn is not None:
            cur = conn.cursor()
            cur.executemany('''
                INSERT OR REPLACE INTO performance_tracking (prediction_id, forward_date, forward_price, return_pct)
                VALUES (?, ?, ?, ?)
            ''', [(int(r['id']), end_d, float(r['p_end']), float(r['fwd_return'])) for _, r in clean.iterrows()])
        stats = period_stats(clean, weights_in_force(weights_df, start_d))
        stats.update({'start_date': start_d, 'end_date': end_d, 'days': days, 'n_excluded': len(suspect)})
        results.append(stats)
    if conn is not None:
        conn.commit()
    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_period_matrix(period_results):
    print("\n" + "=" * 80)
    print("📊 MULTI-PERIOD FACTOR REGIME AUDIT (OUT-OF-SAMPLE RANK IC)")
    print("=" * 80)
    header = f"{'Period':<26} | {'Days':<4} | {'Mkt Ret':<8} | " + " | ".join([f"{f[:5].upper():>6}" for f in FACTOR_MAP]) + " | Final IC (t)   | Spread (Q5-Q1, alive)"
    print(header)
    print("-" * len(header))
    for p in period_results:
        label = f"{p['start_date']} ➔ {p['end_date']}"
        facs = " | ".join(f"{p['ic_dict'][f]:+0.3f}" for f in FACTOR_MAP)
        print(f"{label:<26} | {p['days']:<4} | {p['market_mean_return']:+6.2f}% | {facs} | "
              f"{p['final_rank_ic']:+0.3f} ({p['final_tstat']:+.1f}) | {p['top_bottom_spread']:+6.2f}%")

    print("\n--- Alpha attribution: where does the composite IC come from? ---")
    keys = list(period_results[0]['attribution'].keys())
    print(f"{'Signal':<42} | " + " | ".join(f"{p['start_date'][5:]}➔{p['end_date'][5:]}" for p in period_results) + " |  mean")
    for k in keys:
        vals = [p['attribution'][k] for p in period_results]
        print(f"{k:<42} | " + " | ".join(f"{v:+0.3f}     " for v in vals) + f" | {np.mean(vals):+0.3f}")

    print("\n--- Death-cross hard kill: return of stocks scored 0 vs the rest ---")
    for p in period_results:
        print(f"  {p['start_date']} ➔ {p['end_date']}: killed n={p['killed_n']:<4} mean={p['killed_mean']:+.2f}%   "
              f"| alive n={p['alive_n']:<4} mean={p['alive_mean']:+.2f}%   "
              f"| alive quintiles: " + ", ".join(f"{q}={v:+.2f}%" for q, v in p['quintiles'].items()))


def factor_summary(period_results):
    n = len(period_results)
    print("\n--- Factor Information Summary (Multi-Period Panel) ---")
    print(f"{'Factor':<15} | {'Mean Rank IC':<12} | {'IC Std':<8} | {'IR (n=%d)' % n:<10} | {'# periods > 0'}")
    print("-" * 70)
    for f in FACTOR_MAP:
        s = np.array([p['ic_dict'][f] for p in period_results])
        ir = s.mean() / (s.std(ddof=1) + 0.01) if n > 1 else float('nan')
        print(f"{f.capitalize():<15} | {s.mean():+11.4f} | {s.std(ddof=1) if n>1 else 0:7.4f}  | {ir:+9.3f}  | {(s > 0).sum()}/{n}")
    if n < 8:
        print(f"  NOTE: an IR from {n} periods is not a stable estimate; the ranking above can flip with one more month.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run_optimizer(force=False, dry_run=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    cursor = conn.cursor()

    print("=" * 80)
    print("🚀 RUNNING V18 INSTITUTIONAL MULTI-PERIOD PANEL OPTIMIZER")
    print(f"DB: {DB_PATH}")
    print("=" * 80)

    preds, snapshot_dates, price_matrix, weights_df = load_panel(conn)
    if len(snapshot_dates) < 2:
        print("⚠️ Not enough distinct historical snapshots (need at least 2). Skipping.")
        conn.close()
        return

    print(f"Discovered {len(snapshot_dates)} full snapshots: {', '.join(snapshot_dates)}")
    transitions = build_transitions(snapshot_dates)
    if not transitions:
        print("⚠️ No valid multi-week transitions found. Skipping.")
        conn.close()
        return
    print(f"Identified {len(transitions)} forward-holding periods:")
    for d1, d2, days in transitions:
        print(f"  • {d1} ➔ {d2} ({days} calendar days)")

    period_results = evaluate_transitions(preds, price_matrix, weights_df, transitions, conn=None if dry_run else conn)
    if not period_results:
        print("⚠️ No period had enough matched returns. Skipping.")
        conn.close()
        return

    print_period_matrix(period_results)
    factor_summary(period_results)

    # ---- What is new since the last learning step? ----
    w_row = cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1").fetchone()
    current_weights = {k: (float(w_row[k]) if w_row else DEFAULT_WEIGHTS[k]) for k in FACTOR_WEIGHT_KEYS}
    trained_through = w_row['trained_through'] if w_row and 'trained_through' in w_row.keys() else None

    if force:
        new_periods = period_results
    else:
        new_periods = [p for p in period_results if trained_through is None or p['end_date'] > trained_through]

    print("\n" + "=" * 60)
    if not new_periods:
        print(f"⏸  No new holding period since trained_through={trained_through}. Weights unchanged (idempotent).")
        print("   Use --force to re-learn from all periods.")
        print("=" * 60)
        conn.close()
        return

    # Exponential time-decay across the periods being learned from (recent first)
    k = len(new_periods)
    decay = 0.70
    raw_pw = [decay ** (k - 1 - i) for i in range(k)]
    pw = [w / sum(raw_pw) for w in raw_pw]
    print(f"Learning from {k} period(s)" + (" [FORCED: all periods]" if force else " not seen before") + ":")
    for w, p in zip(pw, new_periods):
        print(f"  {p['start_date']} ➔ {p['end_date']}  weight={w*100:.1f}%")

    agg_ic = {f: sum(w * p['ic_dict'][f] for w, p in zip(pw, new_periods)) for f in FACTOR_MAP}
    unconstrained = exp_gradient_step(current_weights, agg_ic)
    best_w_key = FACTOR_MAP[max(agg_ic, key=agg_ic.get)][1]
    new_weights = project_weights(unconstrained, best_key=best_w_key)

    print("\n🏆 OPTIMIZED FACTOR WEIGHTS")
    print(f"{'Factor':<15} | {'Previous':<10} | {'Optimized':<10} | {'Shift':<8} | Agg IC")
    print("-" * 62)
    for factor, (_, w_key) in FACTOR_MAP.items():
        old_w, new_w = current_weights[w_key], new_weights[w_key]
        delta = new_w - old_w
        arrow = "▲" if delta > 0.0005 else "▼" if delta < -0.0005 else "■"
        print(f"{factor.capitalize():<15} | {old_w*100:6.1f}%    | {new_w*100:6.1f}%    | {arrow} {delta*100:+5.1f}% | {agg_ic[factor]:+.3f}")
    print("-" * 62)
    print(f"{'Total Sum':<15} | {sum(current_weights.values())*100:6.1f}%    | {sum(new_weights.values())*100:6.1f}%")
    pinned = [f for f, (_, wk) in FACTOR_MAP.items() if new_weights[wk] in (FLOOR, CEIL)]
    if pinned:
        print(f"  NOTE: at bounds: {', '.join(pinned)} — the optimizer can no longer express a stronger view on these.")

    if dry_run:
        print("\n(dry run — nothing written)")
        conn.close()
        return

    new_trained_through = max(p['end_date'] for p in new_periods)
    cursor.execute('''
        INSERT INTO active_weights (
            last_updated, quality_weight, growth_weight, valuation_weight,
            risk_weight, moat_weight, bs_weight, cap_alloc_weight, smart_money_weight,
            trained_through, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.date.today().isoformat(),
        new_weights['quality_weight'], new_weights['growth_weight'], new_weights['valuation_weight'],
        new_weights['risk_weight'], new_weights['moat_weight'], new_weights['bs_weight'],
        new_weights['cap_alloc_weight'], new_weights['smart_money_weight'],
        new_trained_through,
        f"EG step on {k} period(s) ending {new_trained_through}" + (" (forced)" if force else ""),
    ))
    conn.commit()
    conn.close()
    print(f"\n✅ New weights saved (trained_through={new_trained_through}).")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='re-learn from all periods even if already trained')
    ap.add_argument('--dry-run', action='store_true', help='print everything, write nothing')
    args = ap.parse_args()
    run_optimizer(force=args.force, dry_run=args.dry_run)
