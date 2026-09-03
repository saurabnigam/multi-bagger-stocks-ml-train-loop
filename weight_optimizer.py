import sqlite3
import datetime
import numpy as np
import pandas as pd
import yfinance as yf

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

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

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def run_optimizer():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    print("================================================================================")
    print("🚀 RUNNING V18 INSTITUTIONAL MULTI-PERIOD PANEL OPTIMIZER")
    print("================================================================================")
    
    # 1. Discover all full-universe historical snapshot dates
    cursor.execute('''
        SELECT date, count(*) as count 
        FROM daily_predictions 
        GROUP BY date 
        HAVING count >= 100 
        ORDER BY date ASC
    ''')
    rows = cursor.fetchall()
    
    if len(rows) < 2:
        print("⚠️ Not enough distinct historical snapshots (need at least 2). Skipping.")
        conn.close()
        return
        
    snapshot_dates = [r['date'] for r in rows]
    print(f"Discovered {len(snapshot_dates)} historical snapshots: {', '.join(snapshot_dates)}")
    
    # 2. Build multi-period transitions (minimum 7 days spacing to filter intraday/weekend duplicates)
    transitions = []
    for i in range(len(snapshot_dates) - 1):
        d1 = snapshot_dates[i]
        d2 = snapshot_dates[i + 1]
        days = (pd.to_datetime(d2) - pd.to_datetime(d1)).days
        if days >= 7:
            transitions.append((d1, d2, days))
            
    if not transitions:
        print("⚠️ No valid multi-week transitions found. Skipping.")
        conn.close()
        return

    print(f"Identified {len(transitions)} distinct forward-holding regime periods:")
    for d1, d2, days in transitions:
        print(f"  • Period: {d1} ➔ {d2} ({days} calendar days)")
        
    # 3. Load prediction and price data for transitions
    query = '''
        SELECT id, date, ticker, price, quality_score, valuation_score, growth_score,
               moat_score, risk_score, bs_score, cap_alloc_score, smart_money_score, final_score
        FROM daily_predictions
    '''
    all_preds = pd.read_sql_query(query, conn)
    
    # Price pivot matrix (ticker x date)
    price_matrix = all_preds.pivot(index='ticker', columns='date', values='price')
    
    # 4. Evaluate each transition period independently
    period_results = []
    
    for start_d, end_d, days in transitions:
        sub = all_preds[all_preds['date'] == start_d].copy()
        
        # Calculate forward return using prices already recorded in SQLite
        p_start = sub['ticker'].map(price_matrix[start_d])
        p_end = sub['ticker'].map(price_matrix[end_d])
        
        sub['p_start'] = p_start
        sub['p_end'] = p_end
        sub['fwd_return'] = (p_end - p_start) / p_start
        sub = sub.dropna(subset=['fwd_return'])
        
        if len(sub) < 30:
            continue
            
        # Update performance_tracking in SQLite without duplicates
        tracking_records = []
        for _, row in sub.iterrows():
            tracking_records.append((int(row['id']), end_d, float(row['p_end']), float(row['fwd_return'])))
            
        cursor.executemany('''
            INSERT OR REPLACE INTO performance_tracking (prediction_id, forward_date, forward_price, return_pct)
            VALUES (?, ?, ?, ?)
        ''', tracking_records)
        
        # Compute Rank IC (Spearman correlation) and Pearson IC for each factor
        ic_dict = {}
        for factor, (col, _) in FACTOR_MAP.items():
            rank_ic = sub[col].corr(sub['fwd_return'], method='spearman')
            if np.isnan(rank_ic): rank_ic = 0.0
            ic_dict[factor] = rank_ic
            
        final_rank_ic = sub['final_score'].corr(sub['fwd_return'], method='spearman')
        if np.isnan(final_rank_ic): final_rank_ic = 0.0
        
        # Quintile spread (Top 20% vs Bottom 20%)
        sub['quintile'] = pd.qcut(sub['final_score'].rank(method='first'), 5, labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'])
        q_perf = sub.groupby('quintile', observed=False)['fwd_return'].mean() * 100
        spread = q_perf.iloc[-1] - q_perf.iloc[0]
        
        period_results.append({
            'start_date': start_d,
            'end_date': end_d,
            'days': days,
            'n_stocks': len(sub),
            'market_mean_return': sub['fwd_return'].mean() * 100,
            'ic_dict': ic_dict,
            'final_rank_ic': final_rank_ic,
            'quintiles': q_perf.to_dict(),
            'top_bottom_spread': spread
        })

    conn.commit()

    # 5. Display Institutional Multi-Period Performance Matrix
    print("\n" + "=" * 80)
    print("📊 MULTI-PERIOD FACTOR REGIME AUDIT (OUT-OF-SAMPLE RANK IC)")
    print("=" * 80)
    
    header = f"{'Period':<26} | {'Days':<4} | {'Mkt Ret':<8} | " + " | ".join([f"{f[:5].upper():>5}" for f in FACTOR_MAP.keys()]) + " | Final IC | Spread (Q5-Q1)"
    print(header)
    print("-" * len(header))
    
    for p in period_results:
        period_label = f"{p['start_date']} ➔ {p['end_date']}"
        factor_strs = [f"{p['ic_dict'][f]:+0.3f}" for f in FACTOR_MAP.keys()]
        row_str = f"{period_label:<26} | {p['days']:<4} | {p['market_mean_return']:+6.2f}% | " + " | ".join(factor_strs) + f" | {p['final_rank_ic']:+0.3f}   | {p['top_bottom_spread']:+6.2f}%"
        print(row_str)
        
    # 6. Multi-Period Exponentiated Gradient Descent Optimization
    # Apply exponential time-decay weighting (recent regimes matter more, but historical regimes prevent overfitting)
    num_periods = len(period_results)
    decay_rate = 0.70  # Half-life weight decay
    raw_weights = [decay_rate ** (num_periods - 1 - idx) for idx in range(num_periods)]
    norm_period_weights = [w / sum(raw_weights) for w in raw_weights]
    
    print("\n--- Regime Weighting per Historical Period ---")
    for idx, p in enumerate(period_results):
        print(f"  Period {p['start_date']} ➔ {p['end_date']}: Weight = {norm_period_weights[idx]*100:.1f}%")
        
    # Compute Aggregate Information Coefficient and Information Ratio
    agg_ic = {f: 0.0 for f in FACTOR_MAP.keys()}
    factor_ic_series = {f: [] for f in FACTOR_MAP.keys()}
    
    for idx, p in enumerate(period_results):
        for f in FACTOR_MAP.keys():
            ic_val = p['ic_dict'][f]
            agg_ic[f] += norm_period_weights[idx] * ic_val
            factor_ic_series[f].append(ic_val)
            
    print("\n--- Factor Information Summary (Multi-Period Panel) ---")
    print(f"{'Factor':<15} | {'Agg Rank IC':<12} | {'IC Volatility':<14} | {'Information Ratio':<18}")
    print("-" * 65)
    for f in FACTOR_MAP.keys():
        mean_ic = np.mean(factor_ic_series[f])
        std_ic = np.std(factor_ic_series[f])
        ir = mean_ic / (std_ic + 0.01)
        print(f"{f.capitalize():<15} | {agg_ic[f]:+11.4f} | {std_ic:13.4f}  | {ir:+17.3f}")

    # Fetch currently active weights
    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    w_row = cursor.fetchone()
    
    current_weights = {
        w_key: w_row[w_key] if w_row else 0.125
        for _, (_, w_key) in FACTOR_MAP.items()
    }
    
    # Exponentiated Gradient Step (Multiplicative Weights Algorithm):
    # w_new = w_old * exp(learning_rate * agg_ic)
    # Continuous adaptation: strong ICs gain weight, negative ICs shed weight smoothly
    LEARNING_RATE = 0.45
    unconstrained_weights = {}
    
    for factor, (_, w_key) in FACTOR_MAP.items():
        grad = agg_ic[factor]
        # Bounded gradient multiplier
        multiplier = np.exp(np.clip(LEARNING_RATE * grad * 5.0, -0.4, 0.4))
        unconstrained_weights[w_key] = current_weights[w_key] * multiplier
        
    # Projected Simplex with Hard Bounds [0.05, 0.30]
    FLOOR = 0.05
    CEIL = 0.30
    
    # Iterative projection onto simplex with box constraints
    adj_weights = unconstrained_weights.copy()
    for _ in range(20):
        tot = sum(adj_weights.values())
        adj_weights = {k: v / tot for k, v in adj_weights.items()}
        clamped = False
        for k in adj_weights:
            if adj_weights[k] < FLOOR:
                adj_weights[k] = FLOOR
                clamped = True
            elif adj_weights[k] > CEIL:
                adj_weights[k] = CEIL
                clamped = True
        if not clamped:
            break
            
    # Final normalization & rounding to 3 decimals summing strictly to 1.000
    tot = sum(adj_weights.values())
    new_weights = {k: round(v / tot, 3) for k, v in adj_weights.items()}
    remainder = round(1.0 - sum(new_weights.values()), 3)
    
    # Distribute any rounding residue into highest performing factor
    best_w_key = FACTOR_MAP[max(agg_ic, key=agg_ic.get)][1]
    new_weights[best_w_key] = round(new_weights[best_w_key] + remainder, 3)

    print("\n" + "=" * 60)
    print("🏆 OPTIMIZED FACTOR WEIGHTS (V18 MULTI-PERIOD ML BRAIN)")
    print("=" * 60)
    print(f"{'Factor':<15} | {'Previous':<10} | {'Optimized':<10} | {'Shift'}")
    print("-" * 50)
    
    for factor, (_, w_key) in FACTOR_MAP.items():
        old_w = current_weights[w_key]
        new_w = new_weights[w_key]
        delta = new_w - old_w
        arrow = "▲" if delta > 0.005 else "▼" if delta < -0.005 else "■"
        print(f"{factor.capitalize():<15} | {old_w*100:6.1f}%    | {new_w*100:6.1f}%    | {arrow} {delta*100:+5.1f}%")
        
    print("-" * 50)
    print(f"{'Total Sum':<15} | {sum(current_weights.values())*100:6.1f}%    | {sum(new_weights.values())*100:6.1f}%    | 100.0%")

    # 7. Record in active_weights table
    today_str = datetime.date.today().isoformat()
    cursor.execute('''
        INSERT INTO active_weights (
            last_updated, quality_weight, growth_weight, valuation_weight, 
            risk_weight, moat_weight, bs_weight, cap_alloc_weight, smart_money_weight
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        today_str,
        new_weights['quality_weight'], new_weights['growth_weight'], new_weights['valuation_weight'],
        new_weights['risk_weight'], new_weights['moat_weight'], new_weights['bs_weight'],
        new_weights['cap_alloc_weight'], new_weights['smart_money_weight']
    ))
    
    conn.commit()
    conn.close()
    print("\n✅ New Optimized Weights Successfully Saved into SQLite Database!")

if __name__ == '__main__':
    run_optimizer()
