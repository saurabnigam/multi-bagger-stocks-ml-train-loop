import sqlite3
import json
import os

from config import DB_PATH, UI_DIR

TOP_N = 25


def _display_div_yield(v):
    """Legacy snapshots stored yfinance's percent x100 (e.g. 349.0). Normalise for display."""
    v = v or 0.0
    return round(v / 100.0, 2) if v > 25 else v


def _display_fcf_yield(v):
    """Legacy snapshots divided rupees by crores (off by 1e7)."""
    v = v or 0.0
    return round(v / 1e7, 2) if abs(v) > 1000 else v


def generate_ui_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

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
    rank = 0

    for row in rows:
        r = json.loads(row['raw_json'])

        score = r['Final_V16_Score']
        base_score = r.get('Base_Score')
        mos = r['Margin_Of_Safety_%']
        iv = r['Intrinsic_Value']
        price = r['Price']
        q_score = r['Quality_Score']
        risk = r['Risk_Score']
        v_trap = r['Value_Trap_Risk']
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
        flags = r.get('Data_Flags')  # None => snapshot predates data-quality flags

        if q_score >= 80: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Exceptional Business: Highly efficient at turning profits into actual cash in the bank."
        elif q_score >= 50: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Solid Business: Good profitability, though it requires some capital to keep growing."
        else: qual_text = f"<b>Raw Score: {q_score}/100</b><br>Capital Intensive: Needs to spend a lot of money to maintain its operations."

        if iv <= 0:
            val_text = f"<b>Margin of Safety: n/a</b><br>No intrinsic value could be computed (negative average FCF or missing book value), so the valuation factor scores 0."
        elif mos > 10: val_text = f"<b>Margin of Safety: {mos}%</b><br>Discounted: Trading below its intrinsic value of ₹{iv} (Current Price: ₹{price})."
        elif mos > -10: val_text = f"<b>Margin of Safety: {mos}%</b><br>Fairly Priced: Trading almost exactly at its true intrinsic value of ₹{iv}."
        else: val_text = f"<b>Margin of Safety: {mos}%</b><br>Expensive: You are overpaying by {abs(mos)}% (Intrinsic Value: ₹{iv} vs Price: ₹{price})."

        growth_math = f"<b>Growth Score: {g_score}/100</b>"
        if r.get('Composite_Growth_%') is not None:
            growth_math += f" (composite growth {r['Composite_Growth_%']}%/yr)"
        growth_math += "<br>"
        if g_score >= 80: growth_text = growth_math + "Explosive fundamental earnings and cash flow growth."
        elif g_score >= 50: growth_text = growth_math + "Steady, reliable fundamental growth."
        else: growth_text = growth_math + "Stagnant or declining fundamental growth."

        bs_text = f"<b>Debt-to-Equity Ratio: {de_ratio}x</b><br>High debt spirals will trigger Value Trap penalties."

        fii_flow_text = f"Net Buying (+{inst_flow_delta}%)" if inst_flow_delta > 0 else f"Net Selling ({inst_flow_delta}%)" if inst_flow_delta < 0 else "Neutral"
        fii_text = f"<b>Institutional Holding: {inst_holdings}%</b><br>Smart Money Score: {sm_score}/100<br><i>Recent Flow: {fii_flow_text}</i>"

        concall_text = f"<b>Headline Sentiment Score: {concall_sentiment}</b> <i>(diagnostic only — not part of the ranking)</i><br><i>{concall_summary}</i>"

        if "Death Cross" in momentum:
            mom_text = f"<span style='color: #ff3b30; font-weight: bold;'>FATAL MULTIPLIER (0.0x): {momentum}</span>"
            rejection_reason = "REJECTED: Stock is in a Falling Knife Death Spiral."
        elif "Bearish" in momentum:
            mom_text = f"<span style='color: #ff9500; font-weight: bold;'>WARNING MULTIPLIER (0.8x): {momentum}</span>"
            rejection_reason = "REJECTED: Bearish technical momentum dragged down the final score."
        else:
            mom_text = f"<span style='color: #34c759; font-weight: bold;'>SAFE MULTIPLIER (1.0x): {momentum}</span>"
            rejection_reason = f"REJECTED: Technicals were safe, but the fundamental composite was too weak to crack the Top {TOP_N}."

        if v_trap >= 50:
            rejection_reason = "REJECTED: Fatal Value Trap. Trap Penalty Multiplier triggered due to failing ROE, negative FCF or massive debt."
        if score == 0:
            rejection_reason = "REJECTED: Fatal Multiplier applied. Score zeroed."

        try:
            fcf_arr = json.loads(r['fcf_array'])
            ocf_arr = json.loads(r['ocf_array'])
        except Exception:
            fcf_arr = [0, 0, 0, 0]; ocf_arr = [0, 0, 0, 0]

        is_turnaround = False
        fcf_burn_raw = 0
        if r.get('Sector', '') != 'Financial Services' and g_score >= 80:
            if len(fcf_arr) > 0 and fcf_arr[-1] < 0:
                is_turnaround = True
                fcf_burn_raw = fcf_arr[-1]

        if flags is None:
            data_quality = "Data-quality flags were not recorded for this snapshot (pre-review harness); imputed inputs cannot be distinguished from real ones."
        elif not flags:
            data_quality = "No imputed inputs."
        else:
            data_quality = "Imputed / proxied inputs: " + ", ".join(flags)
        mcap_cr = r.get('Market_Cap_Cr')
        mcap_text = f"₹{mcap_cr:,.0f} Cr" if mcap_cr else "Mkt cap n/a"

        stock_obj = {
            'id': str(r['Ticker']).lower().replace('.ns', ''),
            'ticker': r['Ticker'],
            'name': str(r['Ticker']).replace('.NS', ''),
            'sector': str(r['Sector']),
            'mcap': mcap_text,
            'score': score,
            'base_score': base_score,
            'rejection_reason': rejection_reason,
            'cashflows': {'ocf': ocf_arr, 'fcf': fcf_arr},
            'plainEnglish': {
                'quality': qual_text,
                'valuation': val_text,
                'growth': growth_text,
                'momentum': mom_text,
                'balance_sheet': bs_text,
                'fii': fii_text,
                'concall': concall_text,
                'news': news,
                'news_link': news_link,
                'data_quality': data_quality,
            },
            'bullCase': (f"<b>FINAL V16 SCORE: {score}/100</b>"
                         + (f" (fundamental composite {base_score}/100 before multipliers)" if base_score is not None else "")
                         + f"<br><br>Why the AI ranked it here: <br><br><b>Growth Focus:</b> {growth_text}<br><b>Quality:</b> {qual_text}"),
            'bearRisk': {
                'title': 'FACTORIZED RISK AUDIT',
                'description': f"Risk Score: {risk}/100",
                'level': 'High' if risk < 40 else 'Medium' if risk < 70 else 'Low'
            },
            'quantTickers': {
                'pe': r.get('Trailing_PE', 0.0),
                'roce': r.get('ROCE_%', 0.0),
                'fcf_yield': _display_fcf_yield(r.get('FCF_Yield_%', 0.0)),
                'div_yield': _display_div_yield(r.get('Div_Yield_%', 0.0)),
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

        rank += 1
        if rank <= TOP_N and score > 0:
            stock_obj['rejection_reason'] = ''
            accepted.append(stock_obj)
        else:
            rejected.append(stock_obj)

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
    meta = {'snapshot_date': latest_date, 'universe': len(rows), 'top_n': TOP_N}

    js_content = 'const aiWeights = ' + json.dumps(weights_dict, indent=2) + ';\n'
    js_content += 'const snapshotMeta = ' + json.dumps(meta) + ';\n'
    js_content += 'const acceptedStocks = ' + json.dumps(accepted, indent=2) + ';\n'
    js_content += 'const rejectedStocks = ' + json.dumps(rejected, indent=2) + ';\n'
    js_content += 'const turnaroundStocks = ' + json.dumps(turnarounds, indent=2) + ';'

    out_path = os.path.join(UI_DIR, 'data.js')
    with open(out_path, 'w') as f:
        f.write(js_content)
    print(f'UI data written to {out_path} (snapshot {latest_date}: {len(accepted)} accepted, {len(rejected)} rejected, {len(turnarounds)} turnarounds)')


if __name__ == '__main__':
    generate_ui_data()
