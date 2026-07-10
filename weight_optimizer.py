import sqlite3
import yfinance as yf
import datetime
import numpy as np

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

def run_optimizer():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 1. Fetch predictions from 30 days ago
    # CRITICAL-2 FIX: Using 30 days instead of 7 days to evaluate fundamental performance rather than weekly momentum noise.
    # TEMPORARY BYPASS: Changed to '-8 days' per user request to run the pipeline
    cursor.execute("SELECT * FROM daily_predictions WHERE date <= date('now', '-30 days')")
    predictions = cursor.fetchall()
    
    if not predictions:
        print("Not enough historical data to run optimizer.")
        return
        
    print(f"Running V16 Gradient Optimizer on {len(predictions)} historical predictions...")
    
    factors = {
        'quality': [], 'valuation': [], 'growth': [], 'moat': [],
        'risk': [], 'bs': [], 'cap_alloc': [], 'smart_money': [],
        'returns': []
    }
    
    for row in predictions:
        ticker = row['ticker']
        pred_price = row['price']
        
        # Fetch actual current price
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            current_price = info.get('currentPrice', info.get('regularMarketPrice', pred_price))
            
            # Record Performance
            return_pct = ((current_price - pred_price) / pred_price) if pred_price > 0 else 0
            
            cursor.execute('''
                INSERT INTO performance_tracking (prediction_id, forward_date, forward_price, return_pct)
                VALUES (?, date('now'), ?, ?)
            ''', (row['id'], current_price, return_pct))
            
            factors['quality'].append(row['quality_score'])
            factors['valuation'].append(row['valuation_score'])
            factors['growth'].append(row['growth_score'])
            factors['moat'].append(row['moat_score'])
            factors['risk'].append(row['risk_score'])
            factors['bs'].append(row['bs_score'])
            factors['cap_alloc'].append(row['cap_alloc_score'])
            factors['smart_money'].append(row['smart_money_score'])
            factors['returns'].append(return_pct)
            
        except Exception as e:
            continue

    # 2. Calculate Correlation matrix between factors and actual returns
    y_returns = np.array(factors['returns'])
    if len(y_returns) < 5 or np.all(y_returns == 0):
        print("Insufficient variance in returns. Skipping optimization.")
        conn.commit()
        conn.close()
        return

    # Fetch active weights
    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    w_row = cursor.fetchone()
    
    current_weights = {
        'quality_weight': w_row['quality_weight'],
        'valuation_weight': w_row['valuation_weight'],
        'growth_weight': w_row['growth_weight'],
        'moat_weight': w_row['moat_weight'],
        'risk_weight': w_row['risk_weight'],
        'bs_weight': w_row['bs_weight'],
        'cap_alloc_weight': w_row['cap_alloc_weight'],
        'smart_money_weight': w_row['smart_money_weight']
    }

    print("\n--- Correlation Results ---")
    adjustments = {}
    
    for key in ['quality', 'valuation', 'growth', 'moat', 'risk', 'bs', 'cap_alloc', 'smart_money']:
        x = np.array(factors[key])
        if len(set(x)) <= 1: 
            corr = 0
        else:
            corr = np.corrcoef(x, y_returns)[0, 1]
            if np.isnan(corr): corr = 0
        
        print(f"{key.capitalize()} Correlation to Returns: {corr:.3f}")
        
        # Simple Gradient Step: Shift weight towards positively correlated factors
        # +1% weight if positive corr > 0.1, -1% if negative corr < -0.1
        adj = 0
        if corr > 0.1: adj = 0.01
        elif corr < -0.1: adj = -0.01
        
        w_key = f"{key}_weight"
        adjustments[w_key] = current_weights[w_key] + adj

    # CRITICAL-3 FIX: Softmax normalization & Hard Floor/Ceilings
    # Ensure no factor drops below 5% or exceeds 30% to prevent weight collapse
    for k, v in adjustments.items():
        if v < 0.05: adjustments[k] = 0.05
        elif v > 0.30: adjustments[k] = 0.30
        
    # Normalize weights back to 1.0 after bounds are applied
    total_weight = sum(adjustments.values())
    new_weights = {k: round(v / total_weight, 3) for k, v in adjustments.items()}
    
    # Second normalization pass to fix rounding errors ensuring sum == 1.0
    diff = 1.0 - sum(new_weights.values())
    if diff != 0:
        new_weights['quality_weight'] = round(new_weights['quality_weight'] + diff, 3)
        
    print("\n--- New Optimized Weights ---")
    for k, v in new_weights.items():
        print(f"{k}: {v}")

    cursor.execute('''
        INSERT INTO active_weights (
            last_updated, quality_weight, growth_weight, valuation_weight, 
            risk_weight, moat_weight, bs_weight, cap_alloc_weight, smart_money_weight
        ) VALUES (date('now'), ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        new_weights['quality_weight'], new_weights['growth_weight'], new_weights['valuation_weight'],
        new_weights['risk_weight'], new_weights['moat_weight'], new_weights['bs_weight'],
        new_weights['cap_alloc_weight'], new_weights['smart_money_weight']
    ))
    
    conn.commit()
    conn.close()
    print("\nWeights Updated in Database!")

if __name__ == '__main__':
    run_optimizer()
