"""
Institutional health & out-of-sample audit suite (red-teamed edition).

The previous version could not fail on any of the problems the project
actually had: it accepted 349% dividend yields, a risk factor that was
constant for 85% of the universe, and never asked whether the learned weights
beat equal weights out of sample. This version:

  1. Structural audit of the latest snapshot — bounds, hard-kill, unit sanity,
     near-constant factors, data-quality flags, final == base x multipliers.
  2. Active-weight constraints (exact bounds, sum == 1.000, provenance).
  3. Multi-period audit with alpha attribution and corporate-action filtering
     (shared code with weight_optimizer.py, so the numbers match).
  4. Walk-forward test of the LEARNING RULE: for each period, weights learned
     only from earlier periods vs equal weights vs the weights actually used.

Exit code 1 on errors. Warnings are printed but do not fail the run.
"""
import json
import sqlite3
import sys
import numpy as np
import pandas as pd

from config import DB_PATH
from db_setup import ensure_schema
from quant_math import FACTOR_WEIGHT_KEYS, DEFAULT_WEIGHTS, trap_penalty_multiplier
from weight_optimizer import (
    FACTOR_MAP, FLOOR, CEIL, load_panel, build_transitions, evaluate_transitions,
    print_period_matrix, factor_summary, forward_returns, rank_ic, composite,
    weights_in_force, exp_gradient_step, project_weights, ic_tstat,
)

errors = []
warnings = []


def err(msg):
    errors.append(msg); print(f"  ❌ {msg}")


def warn(msg):
    warnings.append(msg); print(f"  ⚠️  {msg}")


def ok(msg):
    print(f"  ✅ {msg}")


# --------------------------------------------------------------------------- #
def structural_audit(conn):
    cursor = conn.cursor()
    latest_date = cursor.execute("SELECT MAX(date) FROM daily_predictions").fetchone()[0]
    if not latest_date:
        err("No DB data found."); return None

    print(f"1️⃣  STRUCTURAL INTEGRITY AUDIT (Snapshot Date: {latest_date})")
    print("-" * 70)
    rows = pd.read_sql_query("SELECT * FROM daily_predictions WHERE date = ?", conn, params=(latest_date,))
    raw = [json.loads(r) for r in rows['raw_json']]
    n = len(rows)
    print(f"  Universe: {n} stocks")

    # Bounds & hard-kill
    mos = np.array([d['Margin_Of_Safety_%'] for d in raw])
    if ((mos < -99.9) | (mos > 99.9)).any():
        err(f"Margin of Safety outside [-99.9, 99.9] for {int(((mos < -99.9) | (mos > 99.9)).sum())} stocks")
    else:
        ok("Margin of Safety strictly bounded [-99.9%, +99.9%]")

    dc = rows[rows['momentum_multiplier'] == 0]
    if (dc['final_score'] > 0).any():
        err(f"{int((dc['final_score'] > 0).sum())} Death-Cross stocks have final_score > 0")
    else:
        ok(f"Death-Cross hard kill verified ({len(dc)} stocks zeroed = {len(dc)/n*100:.0f}% of universe)")

    # final == base x multipliers (only for rows that carry base_score)
    if 'base_score' in rows.columns and rows['base_score'].notna().any():
        b = rows.dropna(subset=['base_score'])
        recon = b['base_score'] * b['trap_score'].map(trap_penalty_multiplier) * b['momentum_multiplier']
        bad = (recon - b['final_score']).abs() > 0.15
        if bad.any():
            err(f"final_score != base_score x multipliers for {int(bad.sum())} rows")
        else:
            ok("final_score reconciles to base_score x trap x momentum")
    else:
        warn("Snapshot predates base_score column; alpha attribution uses a reconstructed composite")

    # Unit sanity (the 349% dividend-yield bug lived here undetected)
    dy = np.array([d.get('Div_Yield_%', 0) or 0 for d in raw])
    fy = np.array([d.get('FCF_Yield_%', 0) or 0 for d in raw])
    ih = np.array([d.get('Inst_Holdings_%', 0) or 0 for d in raw])
    if (dy > 25).any():
        err(f"Dividend yield > 25% for {int((dy > 25).sum())} stocks (max {dy.max():.0f}%) — yfinance unit bug; re-run harness")
    else:
        ok(f"Dividend yields plausible (max {dy.max():.2f}%)")
    if (np.abs(fy) > 200).any():
        err(f"FCF yield outside ±200% for {int((np.abs(fy) > 200).sum())} stocks — crore/rupee unit bug; re-run harness")
    else:
        ok(f"FCF yields plausible (range {fy.min():.1f}% .. {fy.max():.1f}%)")
    if ((ih < 0) | (ih > 100)).any():
        err("Institutional holdings outside [0, 100]%")

    # Near-constant factors
    w = dict(cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1").fetchone() or {})
    print("\n  Factor dispersion (share of universe sitting on the modal value):")
    for f, (col, wkey) in FACTOR_MAP.items():
        vc = rows[col].value_counts(normalize=True)
        modal_share, modal_val = vc.iloc[0], vc.index[0]
        weight = w.get(wkey, DEFAULT_WEIGHTS[wkey])
        line = f"    {f:<12} weight={weight*100:4.1f}%  distinct={rows[col].nunique():>3}  modal={modal_val:>5} ({modal_share*100:4.1f}% of stocks)"
        if modal_share >= 0.80:
            warn(line + "  ← near-constant; its IC is noise from the few exceptions")
        else:
            print(line)
    fs_zero = (rows['final_score'] == 0).mean()
    if fs_zero > 0.25:
        warn(f"{fs_zero*100:.0f}% of the universe has final_score == 0; ranking information for those stocks is destroyed")

    # Data-quality flags
    flags = pd.Series([f for d in raw for f in d.get('Data_Flags', [])])
    if len(flags):
        print("\n  Data-quality flags (share of universe):")
        for k, v in (flags.value_counts() / n).items():
            print(f"    {k:<32} {v*100:5.1f}%")
    else:
        warn("Snapshot has no Data_Flags (pre-review harness): imputed values are indistinguishable from real ones")
    return latest_date


# --------------------------------------------------------------------------- #
def weights_audit(conn):
    print("\n2️⃣  ACTIVE WEIGHT CONSTRAINTS AUDIT")
    print("-" * 70)
    row = conn.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        err("No active_weights row"); return
    w = dict(row)
    total = sum(w[k] for k in FACTOR_WEIGHT_KEYS)
    bad = [k for k in FACTOR_WEIGHT_KEYS if w[k] < FLOOR - 1e-9 or w[k] > CEIL + 1e-9]
    if bad:
        err(f"Weight bound violation: {', '.join(f'{k}={w[k]:.3f}' for k in bad)} (must be in [{FLOOR}, {CEIL}])")
    else:
        ok(f"All weights within [{FLOOR*100:.0f}%, {CEIL*100:.0f}%]")
    if abs(total - 1.0) > 0.0005:
        err(f"Weights sum to {total:.4f}, not 1.000")
    else:
        ok(f"Weights sum to {total:.3f}")
    pinned = [k for k in FACTOR_WEIGHT_KEYS if abs(w[k] - CEIL) < 1e-9 or abs(w[k] - FLOOR) < 1e-9]
    if pinned:
        warn(f"At bounds: {', '.join(pinned)} — optimizer saturated on these")
    tt = w.get('trained_through')
    if tt:
        ok(f"Provenance: trained_through={tt} ({w.get('note') or 'no note'})")
    else:
        warn("Latest weights carry no trained_through; optimizer idempotency cannot be verified for this row")


# --------------------------------------------------------------------------- #
def walk_forward_learning_test(preds, price_matrix, weights_df, transitions):
    """
    Does the learning rule add value? For period k, use weights learned ONLY
    from periods < k (starting from the seed weights), and compare the IC of the
    resulting composite with equal weights and with the weights actually used.
    """
    print("\n4️⃣  WALK-FORWARD TEST OF THE LEARNING RULE (strictly out-of-sample)")
    print("-" * 70)
    seed = {k: float(weights_df.iloc[0][k]) for k in FACTOR_WEIGHT_KEYS} if len(weights_df) else dict(DEFAULT_WEIGHTS)
    eq = {k: 1.0 / len(FACTOR_WEIGHT_KEYS) for k in FACTOR_WEIGHT_KEYS}
    learned = dict(seed)
    table = []
    for start_d, end_d, days in transitions:
        clean, _ = forward_returns(preds, price_matrix, start_d, end_d)
        if len(clean) < 30:
            continue
        r = clean['fwd_return']
        mult = clean['trap_score'].map(trap_penalty_multiplier) * clean['momentum_multiplier']
        inforce = weights_in_force(weights_df, start_d)
        row = {
            'period': f"{start_d} ➔ {end_d}",
            'n': len(clean),
            'learned_fund': rank_ic(composite(clean, learned), r),
            'equal_fund': rank_ic(composite(clean, eq), r),
            'inforce_fund': rank_ic(composite(clean, inforce), r),
            'learned_full': rank_ic(composite(clean, learned) * mult, r),
            'equal_full': rank_ic(composite(clean, eq) * mult, r),
            'inforce_full': rank_ic(composite(clean, inforce) * mult, r),
        }
        table.append(row)
        # learn from this period, then move on
        ic_dict = {f: rank_ic(clean[col], r) for f, (col, _) in FACTOR_MAP.items()}
        learned = project_weights(exp_gradient_step(learned, ic_dict))

    if not table:
        warn("Not enough periods for a walk-forward test"); return
    df = pd.DataFrame(table)
    print("  Rank IC of composite built with ...              fundamentals only        | with trap & momentum multipliers")
    print(f"  {'period':<26} {'n':>4} | {'learned':>8} {'equal':>8} {'in-force':>9} | {'learned':>8} {'equal':>8} {'in-force':>9}")
    for _, x in df.iterrows():
        print(f"  {x['period']:<26} {x['n']:>4} | {x['learned_fund']:+8.3f} {x['equal_fund']:+8.3f} {x['inforce_fund']:+9.3f} | {x['learned_full']:+8.3f} {x['equal_full']:+8.3f} {x['inforce_full']:+9.3f}")
    m = df.mean(numeric_only=True)
    print(f"  {'MEAN':<26} {'':>4} | {m['learned_fund']:+8.3f} {m['equal_fund']:+8.3f} {m['inforce_fund']:+9.3f} | {m['learned_full']:+8.3f} {m['equal_full']:+8.3f} {m['inforce_full']:+9.3f}")
    print(f"  Learned weights after all periods: " + ", ".join(f"{k.replace('_weight','')}={v:.3f}" for k, v in learned.items()))
    edge = m['learned_fund'] - m['equal_fund']
    if len(df) < 6:
        warn(f"Only {len(df)} out-of-sample periods; learned-vs-equal edge of {edge:+.3f} IC is not distinguishable from noise")
    elif edge <= 0:
        warn(f"Learned weights do NOT beat equal weights out of sample (edge {edge:+.3f} IC)")
    else:
        ok(f"Learned weights beat equal weights out of sample by {edge:+.3f} IC over {len(df)} periods")


# --------------------------------------------------------------------------- #
def run_evals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    print("=" * 80)
    print("🔬 RUNNING V18 INSTITUTIONAL QUANTITATIVE VALIDATION & HEALTH SUITE")
    print(f"DB: {DB_PATH}")
    print("=" * 80 + "\n")

    structural_audit(conn)
    weights_audit(conn)

    print("\n3️⃣  MULTI-PERIOD OUT-OF-SAMPLE PREDICTIVE AUDIT")
    print("-" * 70)
    preds, snapshot_dates, price_matrix, weights_df = load_panel(conn)
    transitions = build_transitions(snapshot_dates)
    if transitions:
        results = evaluate_transitions(preds, price_matrix, weights_df, transitions, conn=None)
        if results:
            print_period_matrix(results)
            factor_summary(results)
            mean_final = np.mean([p['final_rank_ic'] for p in results])
            print(f"\n  Mean composite Rank IC across {len(results)} periods: {mean_final:+.3f} "
                  f"(per-period t-stats: {', '.join(f'{p['final_tstat']:+.1f}' for p in results)})")
            mean_mom = np.mean([p['attribution']['momentum multiplier alone'] for p in results])
            mean_fund = np.mean([p['attribution']['fundamental composite (no multipliers)'] for p in results])
            print(f"  Of which: momentum filter alone {mean_mom:+.3f}, fundamental composite alone {mean_fund:+.3f}")
        walk_forward_learning_test(preds, price_matrix, weights_df, transitions)
    else:
        warn("Fewer than two full snapshots; no out-of-sample audit possible")

    conn.close()
    print("\n" + "=" * 70)
    print(f"Summary: {len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        print(f"🚨 {len(errors)} EVALUATION FAILURES DETECTED:")
        for e in errors: print(f"   - {e}")
        sys.exit(1)
    print("🏆 ALL HARD QUANTITATIVE HEALTH CHECKS PASSED" + (" (with warnings — read them)" if warnings else "."))
    print("=" * 70)


if __name__ == '__main__':
    run_evals()
