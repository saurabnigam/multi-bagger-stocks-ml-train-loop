import sqlite3
import json
import sys

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

def run_evals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get the latest date
    cursor.execute("SELECT MAX(date) FROM daily_predictions")
    latest_date = cursor.fetchone()[0]
    
    if not latest_date:
        print("❌ EVAL FAILED: No DB data found.")
        sys.exit(1)
        
    cursor.execute("SELECT raw_json FROM daily_predictions WHERE date = ?", (latest_date,))
    rows = cursor.fetchall()
    conn.close()

    errors = 0
    
    print("Running V16 ML Engine Evals...\n")
    
    mos_passed = True
    tech_passed = True
    death_cross_passed = True
    
    for r in rows:
        data = json.loads(r['raw_json'])
        
        # 1. Valuation Clipping
        mos = data['Margin_Of_Safety_%']
        if mos < -100 or mos > 100:
            print(f"❌ EVAL FAILED: Unbounded Margin of Safety detected in {data['Ticker']}. DCF Clipping failed.")
            errors += 1
            mos_passed = False
            
        # 2. AI Disruption Penalty Misfires
        sector = str(data['Sector']).upper()
        if 'TECH' in sector or 'IT' in sector or 'SOFTWARE' in sector:
            if data['Risk_Score'] > 60:
                print(f"❌ EVAL FAILED: {data['Ticker']} avoided AI Disruption penalties (Risk={data['Risk_Score']}).")
                errors += 1
                tech_passed = False
                
        # 3. Death Cross Multiplier Override
        if "Death Cross" in data['Momentum_Status'] and data['Final_V16_Score'] > 0:
            print(f"❌ EVAL FAILED: {data['Ticker']} had a Death Cross but scored > 0 ({data['Final_V16_Score']}). Override logic broken.")
            errors += 1
            death_cross_passed = False

    if mos_passed: print("✅ EVAL PASSED: All Margin of Safety values strictly bounded (-100 to +100).")
    if tech_passed: print("✅ EVAL PASSED: All Technology stocks correctly absorbed AI Disruption penalties.")
    if death_cross_passed: print("✅ EVAL PASSED: All Death Crosses correctly zeroed out Final Scores.")

    if errors > 0:
        print(f"\n🚨 {errors} EVALS FAILED. Halting deployment.")
        sys.exit(1)
    else:
        print("\n🏆 ALL EVALS PASSED. Portfolio structurally verified.")

if __name__ == '__main__':
    run_evals()
