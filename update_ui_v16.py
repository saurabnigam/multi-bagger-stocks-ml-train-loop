import sqlite3
import json
import os

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'
OUTPUT_DIR = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch'

def generate_ui_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get the latest date
    cursor.execute("SELECT MAX(date) FROM daily_predictions")
    latest_date = cursor.fetchone()[0]
    
    if not latest_date:
        print("No data in DB yet.")
        return
        
    cursor.execute("SELECT raw_json FROM daily_predictions WHERE date = ? ORDER BY final_score DESC", (latest_date,))
    rows = cursor.fetchall()

    accepted = []
    rejected = []
    turnarounds = []

    for idx, row in enumerate(rows):
        r = json.loads(row['raw_json'])
        
        score = r['Final_V16_Score']
        mos = r['Margin_Of_Safety_%']
        iv = r['Intrinsic_Value']
        price = r['Price']
        q_score = r['Quality_Score']
        risk = r['Risk_Score']
        v_trap = r['Value_Trap_Risk']
        moat = r['Moat_Score']
        g_score = r['Growth_Score']
        sm_score = r['Smart_Money_Score']
        de_ratio = r['Debt_to_Equity']
        inst_holdings = r['Inst_Holdings_%']
        momentum = r['Momentum_Status']
        news = r['Latest_Catalyst']
        news_link = r.get('Latest_News_Link', '#')
        inst_flow_delta = r.get('Inst_Flow_Delta', 0.0)
        concall_sentiment = r.get('Concall_Sentiment_Score', 0.0)
        concall_summary = r.get('Concall_Summary', "No summary available")
        
        # Translate Quality
        if q_score >= 80: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Exceptional Business: Highly efficient at turning profits into actual cash in the bank."
        elif q_score >= 50: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Solid Business: Good profitability, though it requires some capital to keep growing."
        else: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Capital Intensive: Needs to spend a lot of money to maintain its operations."
        
        # Translate Valuation (Bounded)
        if mos > 10: val_text = f"<b>Margin of Safety: {mos}%</b><br>Discounted: Trading below its intrinsic value of ₹{iv} (Current Price: ₹{price})."
        elif mos > -10: val_text = f"<b>Margin of Safety: {mos}%</b><br>Fairly Priced: Trading almost exactly at its true intrinsic value of ₹{iv}."
        else: val_text = f"<b>Margin of Safety: {mos}%</b><br>Expensive: You are overpaying by {abs(mos)}% (Intrinsic Value: ₹{iv} vs Price: ₹{price})."
        
        # Growth
        growth_math = f"<b>Growth Score: {g_score}/100</b><br>"
        if g_score >= 80: growth_text = growth_math + "Explosive fundamental earnings and cash flow growth."
        elif g_score >= 50: growth_text = growth_math + "Steady, reliable fundamental growth."
        else: growth_text = growth_math + "Stagnant or declining fundamental growth."

        bs_text = f"<b>Debt-to-Equity Ratio: {de_ratio}x</b><br>High debt spirals will trigger Value Trap penalties."
        
        fii_flow_text = f"Net Buying (+{inst_flow_delta}%)" if inst_flow_delta > 0 else f"Net Selling ({inst_flow_delta}%)" if inst_flow_delta < 0 else "Neutral"
        fii_text = f"<b>Institutional Holding: {inst_holdings}%</b><br>Smart Money Score: {sm_score}/100<br><i>Recent Flow: {fii_flow_text}</i>"
        
        concall_text = f"<b>Concall Sentiment Score: {concall_sentiment}</b><br><i>{concall_summary}</i>"

        # Momentum Text
        if "Death Cross" in momentum: 
            mom_color = "#ff3b30"
            mom_text = f"<span style='color: {mom_color}; font-weight: bold;'>FATAL MULTIPLIER (0.0x): {momentum}</span>"
            rejection_reason = "REJECTED: Stock is in a Falling Knife Death Spiral."
        elif "Bearish" in momentum: 
            mom_color = "#ff9500"
            mom_text = f"<span style='color: {mom_color}; font-weight: bold;'>WARNING MULTIPLIER (0.8x): {momentum}</span>"
            rejection_reason = "REJECTED: Bearish technical momentum dragged down the final score."
        else: 
            mom_color = "#34c759"
            mom_text = f"<span style='color: {mom_color}; font-weight: bold;'>SAFE MULTIPLIER (1.0x): {momentum}</span>"
            rejection_reason = "REJECTED: Technicals were safe, but fundamental valuation or quality was too weak to crack the Top 25."

        if v_trap >= 50:
            rejection_reason = f"REJECTED: Fatal Value Trap. Trap Penalty Multiplier triggered due to failing ROE or massive debt."
        if score == 0:
            rejection_reason = "REJECTED: Fatal Multiplier applied. Score manually zeroed."

        try:
            fcf_arr = json.loads(r['fcf_array'])
            ocf_arr = json.loads(r['ocf_array'])
        except:
            fcf_arr = [0,0,0,0]; ocf_arr = [0,0,0,0]

        is_turnaround = False
        fcf_burn_raw = 0
        if r.get('Sector', '') != 'Financial Services' and g_score >= 80:
            if len(fcf_arr) > 0 and fcf_arr[-1] < 0:
                is_turnaround = True
                fcf_burn_raw = fcf_arr[-1]

        stock_obj = {
            'id': str(r['Ticker']).lower().replace('.ns', ''),
            'ticker': r['Ticker'],
            'name': str(r['Ticker']).replace('.NS', ''),
            'sector': str(r['Sector']),
            'mcap': 'V16 Database Engine',
            'score': score,
            'rejection_reason': rejection_reason,
            'cashflows': {
                'ocf': ocf_arr,
                'fcf': fcf_arr
            },
            'plainEnglish': {
                'quality': qual_text,
                'valuation': val_text,
                'growth': growth_text,
                'momentum': mom_text,
                'balance_sheet': bs_text,
                'fii': fii_text,
                'concall': concall_text,
                'news': news,
                'news_link': news_link
            },
            'bullCase': f"<b>FINAL V16 SCORE: {score}/100</b><br><br>Why the AI ranked it here: <br><br><b>Growth Focus:</b> {growth_text}<br><b>Quality:</b> {qual_text}",
            'bearRisk': { 
                'title': 'FACTORIZED RISK AUDIT', 
                'description': f"Risk Score: {risk}/100", 
                'level': 'High' if risk < 40 else 'Medium' if risk < 70 else 'Low'
            },
            'quantTickers': {
                'pe': r.get('Trailing_PE', 0.0),
                'roce': r.get('ROCE_%', 0.0),
                'fcf_yield': r.get('FCF_Yield_%', 0.0),
                'div_yield': r.get('Div_Yield_%', 0.0),
                'sma50': r.get('SMA_50', 0.0),
                'sma200': r.get('SMA_200', 0.0),
                'debt_to_equity': r.get('Debt_to_Equity', 0.0),
                'inst_holdings': r.get('Inst_Holdings_%', 0.0)
            }
        }

        if is_turnaround:
            stock_obj['bearRisk']['fcf_burn_raw'] = fcf_burn_raw
            turnarounds.append(stock_obj)
            continue

        if idx < 25 and score > 0:
            accepted.append(stock_obj)
        else:
            rejected.append(stock_obj)

    # Sort turnarounds by magnitude of cash burn (most negative first)
    turnarounds.sort(key=lambda x: x['bearRisk']['fcf_burn_raw'])

    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    w_row = cursor.fetchone()
    
    weights_dict = {
        'Quality': f"{w_row['quality_weight']*100:.1f}%",
        'Growth': f"{w_row['growth_weight']*100:.1f}%",
        'Valuation': f"{w_row['valuation_weight']*100:.1f}%",
        'Risk': f"{w_row['risk_weight']*100:.1f}%",
        'Moat': f"{w_row['moat_weight']*100:.1f}%",
        'Balance Sheet': f"{w_row['bs_weight']*100:.1f}%",
        'Cap Alloc': f"{w_row['cap_alloc_weight']*100:.1f}%",
        'Smart Money': f"{w_row['smart_money_weight']*100:.1f}%"
    }

    js_content = 'const aiWeights = ' + json.dumps(weights_dict, indent=2) + ';\n'
    js_content += 'const acceptedStocks = ' + json.dumps(accepted, indent=2) + ';\n'
    js_content += 'const rejectedStocks = ' + json.dumps(rejected, indent=2) + ';\n'
    js_content += 'const turnaroundStocks = ' + json.dumps(turnarounds, indent=2) + ';'

    with open(os.path.join(OUTPUT_DIR, 'ui', 'data.js'), 'w') as f:
        f.write(js_content)
    print('UI Data updated for V16 DB Engine!')

if __name__ == '__main__':
    generate_ui_data()
