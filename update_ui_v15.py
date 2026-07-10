import pandas as pd
import json
import os

OUTPUT_DIR = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch'

df = pd.read_csv(os.path.join(OUTPUT_DIR, 'v15_nifty50_top.csv'))
accepted = []
rejected = []

for idx, row in df.iterrows():
    
    score = row['Final_V15_Score']
    mos = row['Margin_Of_Safety_%']
    iv = row['Intrinsic_Value']
    price = row['Price']
    q_score = row['Quality_Score']
    risk = row['Risk_Score']
    v_trap = row['Value_Trap_Risk']
    moat = row['Moat_Score']
    g_score = row['Growth_Score']
    sm_score = row['Smart_Money_Score']
    de_ratio = row['Debt_to_Equity']
    inst_holdings = row['Inst_Holdings_%']
    momentum = row['Momentum_Status']
    news = row['Latest_Catalyst']
    news_link = row.get('Latest_News_Link', '#')
    
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

    # Balance Sheet & Smart Money
    bs_text = f"<b>Debt-to-Equity Ratio: {de_ratio}x</b><br>High debt spirals will trigger Value Trap penalties."
    fii_text = f"<b>Institutional Holding: {inst_holdings}%</b><br>Smart Money Score: {sm_score}/100"

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
        rejection_reason = "REJECTED: Technicals were safe, but fundamental valuation or quality was too weak to crack the Top 10."

    if v_trap >= 50:
        rejection_reason = f"REJECTED: Fatal Value Trap. Trap Penalty Multiplier triggered due to failing ROE or massive debt."
    if score == 0:
        rejection_reason = "REJECTED: Fatal Multiplier applied. Score manually zeroed."

    try:
        fcf_arr = json.loads(row['fcf_array'])
        ocf_arr = json.loads(row['ocf_array'])
    except:
        fcf_arr = [0,0,0,0]; ocf_arr = [0,0,0,0]

    stock_obj = {
        'id': str(row['Ticker']).lower().replace('.ns', ''),
        'ticker': row['Ticker'],
        'name': str(row['Ticker']).replace('.NS', ''),
        'sector': str(row['Sector']),
        'mcap': 'V15 Engine',
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
            'news': news,
            'news_link': news_link
        },
        'bullCase': f"<b>FINAL V15 SCORE: {score}/100</b><br><br>Why the AI ranked it here: <br><br><b>Growth Focus:</b> {growth_text}<br><b>Quality:</b> {qual_text}",
        'bearRisk': { 
            'title': 'FACTORIZED RISK AUDIT', 
            'description': f"Risk Score: {risk}/100", 
            'level': 'High' if risk < 40 else 'Medium' if risk < 70 else 'Low'
        }
    }

    if idx < 10 and score > 0:
        accepted.append(stock_obj)
    else:
        rejected.append(stock_obj)

js_content = 'const acceptedStocks = ' + json.dumps(accepted, indent=2) + ';\n'
js_content += 'const rejectedStocks = ' + json.dumps(rejected, indent=2) + ';'

with open(os.path.join(OUTPUT_DIR, 'ui', 'data.js'), 'w') as f:
    f.write(js_content)
print('UI Data updated for V15 Math Engine!')
