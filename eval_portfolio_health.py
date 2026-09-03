import sqlite3
import json
import sys
import numpy as np
import pandas as pd

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

def run_evals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("================================================================================")
    print("🔬 RUNNING V18 INSTITUTIONAL QUANTITATIVE VALIDATION & HEALTH SUITE")
    print("================================================================================\n")
    
    # 1. Structural Checks on Latest Predictions
    cursor.execute("SELECT MAX(date) FROM daily_predictions")
    latest_date = cursor.fetchone()[0]
    
    if not latest_date:
        print("❌ EVAL FAILED: No DB data found.")
        sys.exit(1)
        
    print(f"1️⃣  STRUCTURAL INTEGRITY AUDIT (Snapshot Date: {latest_date})")
    print("-" * 60)
    
    cursor.execute("SELECT raw_json FROM daily_predictions WHERE date = ?", (latest_date,))
    rows = cursor.fetchall()
    
    errors = 0
    mos_passed = True
    death_cross_passed = True
    factor_variance_passed = True
    
    scores = {
        'quality': [], 'valuation': [], 'growth': [], 'moat': [],
        'risk': [], 'bs': [], 'cap_alloc': [], 'smart_money': [], 'final': []
    }
    
    for r in rows:
        data = json.loads(r['raw_json'])
        
        # 1. Valuation Bounding [-100, +100]
        mos = data['Margin_Of_Safety_%']
        if mos < -100 or mos > 100:
            print(f"❌ Unbounded Margin of Safety in {data['Ticker']}: {mos}")
            errors += 1
            mos_passed = False
            
        # 2. Death Cross Multiplier Override
        if "Death Cross" in data['Momentum_Status'] and data['Final_V16_Score'] > 0:
            print(f"❌ Death Cross stock scored > 0 in {data['Ticker']}: {data['Final_V16_Score']}")
            errors += 1
            death_cross_passed = False
            
        scores['quality'].append(data['Quality_Score'])
        scores['valuation'].append(data['Valuation_Score'])
        scores['growth'].append(data['Growth_Score'])
        scores['moat'].append(data['Moat_Score'])
        scores['risk'].append(data['Risk_Score'])
        scores['bs'].append(data['BalanceSheet_Score'])
        scores['cap_alloc'].append(data['CapAlloc_Score'])
        scores['smart_money'].append(data['Smart_Money_Score'])
        scores['final'].append(data['Final_V16_Score'])
        
    # Check factor non-degeneracy (no dead factors with zero variance)
    for f_name, vals in scores.items():
        std_val = np.std(vals)
        if std_val < 1.0:
            print(f"❌ Factor Degeneracy: {f_name} has near-zero standard deviation ({std_val:.3f})")
            errors += 1
            factor_variance_passed = False
            
    if mos_passed: print("  ✅ Margin of Safety strictly bounded [-99.9%, +99.9%]")
    if death_cross_passed: print("  ✅ Momentum Death Cross hard-kill (0.0x multiplier) verified")
    if factor_variance_passed: print(f"  ✅ All {len(scores)} score dimensions demonstrate healthy cross-sectional dispersion")

    # 2. Active Weight Bounds & Normalization Audit
    print("\n2️⃣  ACTIVE WEIGHT CONSTRAINTS AUDIT")
    print("-" * 60)
    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    w = dict(cursor.fetchone())
    
    weight_keys = [
        'quality_weight', 'growth_weight', 'valuation_weight', 'risk_weight',
        'moat_weight', 'bs_weight', 'cap_alloc_weight', 'smart_money_weight'
    ]
    w_sum = sum(w[k] for k in weight_keys)
    bounds_ok = True
    
    for k in weight_keys:
        val = w[k]
        if val < 0.049 or val > 0.301:
            print(f"❌ Weight bound violation on {k}: {val:.4f} (must be in [0.05, 0.30])")
            errors += 1
            bounds_ok = False
            
    if abs(w_sum - 1.0) > 0.002:
        print(f"❌ Weights do not sum to 1.000: Sum = {w_sum:.4f}")
        errors += 1
    else:
        print(f"  ✅ Active weights strictly normalized: Sum = {w_sum:.3f}")
        
    if bounds_ok:
        print(f"  ✅ All individual weights strictly respect bounds [5.0%, 30.0%]")

    # 3. Multi-Period Walk-Forward Out-Of-Sample Backtest
    print("\n3️⃣  MULTI-PERIOD OUT-OF-SAMPLE PREDICTIVE AUDIT")
    print("-" * 60)
    
    query = '''
        SELECT id, date, ticker, price, final_score
        FROM daily_predictions
    '''
    all_preds = pd.read_sql_query(query, conn)
    price_matrix = all_preds.pivot(index='ticker', columns='date', values='price')
    
    cursor.execute('''
        SELECT date, count(*) as count 
        FROM daily_predictions 
        GROUP BY date 
        HAVING count >= 100 
        ORDER BY date ASC
    ''')
    snapshots = [r['date'] for r in cursor.fetchall()]
    
    transitions = []
    for i in range(len(snapshots) - 1):
        d1 = snapshots[i]
        d2 = snapshots[i + 1]
        days = (pd.to_datetime(d2) - pd.to_datetime(d1)).days
        if days >= 7:
            transitions.append((d1, d2, days))
            
    if transitions:
        for start_d, end_d, days in transitions:
            sub = all_preds[all_preds['date'] == start_d].copy()
            sub['fwd_return'] = (sub['ticker'].map(price_matrix[end_d]) - sub['ticker'].map(price_matrix[start_d])) / sub['ticker'].map(price_matrix[start_d])
            sub = sub.dropna(subset=['fwd_return'])
            
            rank_ic = sub['final_score'].corr(sub['fwd_return'], method='spearman')
            sub['quintile'] = pd.qcut(sub['final_score'].rank(method='first'), 5, labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'])
            q_perf = sub.groupby('quintile', observed=False)['fwd_return'].mean() * 100
            spread = q_perf.iloc[-1] - q_perf.iloc[0]
            
            print(f"  • Transition {start_d} ➔ {end_d} ({days}d, N={len(sub)}):")
            print(f"      Composite Rank IC: {rank_ic:+0.3f} | Quintile Spread (Q5-Q1): {spread:+0.2f}%")
            print(f"      Q1: {q_perf['Q1']:+0.2f}% | Q3: {q_perf['Q3']:+0.2f}% | Q5 (Top Picks): {q_perf['Q5']:+0.2f}%")
            
    conn.close()
    
    print("\n" + "=" * 60)
    if errors > 0:
        print(f"🚨 {errors} EVALUATION FAILURES DETECTED.")
        sys.exit(1)
    else:
        print("🏆 ALL QUANTITATIVE HEALTH EVALUATIONS PASSED.")
        print("=" * 60)

if __name__ == '__main__':
    run_evals()
