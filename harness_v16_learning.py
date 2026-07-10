import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import datetime
import time
import urllib.request
import csv
from quant_math import *
from concall_analyzer import analyze_sentiment

DB_PATH = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch/quant_engine.db'

def get_nifty_50_tickers():
    return [
        "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS", "AXISBANK.NS", 
        "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "BPCL.NS", "BHARTIARTL.NS", 
        "BRITANNIA.NS", "CIPLA.NS", "COALINDIA.NS", "DIVISLAB.NS", "DRREDDY.NS", 
        "EICHERMOT.NS", "GRASIM.NS", "HCLTECH.NS", "HDFCBANK.NS", "HDFCLIFE.NS", 
        "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS", "ICICIBANK.NS", "ITC.NS", 
        "INDUSINDBK.NS", "INFY.NS", "JSWSTEEL.NS", "KOTAKBANK.NS", "LT.NS", 
        "LTIM.NS", "M&M.NS", "MARUTI.NS", "NTPC.NS", "NESTLEIND.NS", 
        "ONGC.NS", "POWERGRID.NS", "RELIANCE.NS", "SBILIFE.NS", "SBIN.NS", 
        "SUNPHARMA.NS", "TCS.NS", "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS", 
        "TECHM.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS", "IOC.NS", "ARIS.NS"
    ]

def get_nifty_500_tickers():
    url = 'https://niftyindices.com/IndexConstituent/ind_nifty500list.csv'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        response = urllib.request.urlopen(req, timeout=10)
        lines = [l.decode('utf-8') for l in response.readlines()]
        reader = csv.DictReader(lines)
        return [row['Symbol'] + '.NS' for row in reader]
    except Exception as e:
        print(f"Failed to fetch Nifty 500: {e}. Falling back to Nifty 50.")
        return get_nifty_50_tickers()

def get_active_weights():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    else:
        return {
            'quality_weight': 0.20, 'growth_weight': 0.20, 'valuation_weight': 0.15,
            'risk_weight': 0.15, 'moat_weight': 0.10, 'bs_weight': 0.10,
            'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05
        }

def save_predictions_to_db(results):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = datetime.date.today().isoformat()
    
    cursor.execute('DELETE FROM daily_predictions WHERE date = ?', (today,))

    for r in results:
        raw_json = json.dumps(r)
        cursor.execute('''
            INSERT INTO daily_predictions (
                date, ticker, price, quality_score, valuation_score, growth_score,
                moat_score, risk_score, bs_score, cap_alloc_score, smart_money_score,
                trap_score, momentum_multiplier, final_score, latest_catalyst, news_link, raw_json,
                inst_flow_delta, concall_sentiment_score, concall_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            today, r['Ticker'], r['Price'], r['Quality_Score'], r['Valuation_Score'],
            r['Growth_Score'], r['Moat_Score'], r['Risk_Score'], r['BalanceSheet_Score'],
            r['CapAlloc_Score'], r['Smart_Money_Score'], r['Value_Trap_Risk'],
            r['Momentum_Multiplier_Raw'], r['Final_V16_Score'], r['Latest_Catalyst'], r['Latest_News_Link'], raw_json,
            r['Inst_Flow_Delta'], r['Concall_Sentiment_Score'], r['Concall_Summary']
        ))
    
    conn.commit()
    conn.close()

def get_historical_inst_holdings(ticker):
    """Fetches the institutional holding % from 7 days ago to calculate delta."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT json_extract(raw_json, "$.Inst_Holdings_%") / 100.0
        FROM daily_predictions 
        WHERE ticker = ? AND date <= date('now', '-7 days')
        ORDER BY date DESC LIMIT 1
    ''', (ticker,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0] is not None: return float(row[0])
    return None

def extract_financials(ticker, weights):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or info.get('quoteType') == 'NONE': return None
        
        current_price = info.get('currentPrice', info.get('regularMarketPrice', 0))
        if current_price == 0: return None
        
        mcap = info.get('marketCap', 0) / 1e7
        if mcap < 100: return None
        
        sma50 = info.get('fiftyDayAverage', 0)
        sma200 = info.get('twoHundredDayAverage', 0)
        momentum_status, momentum_multiplier = get_momentum_status(current_price, sma50, sma200)
        
        latest_news = "No recent catalyst."
        news_link = "#"
        if stock.news and len(stock.news) > 0:
            news_item = stock.news[0]
            if 'content' in news_item:
                if 'title' in news_item['content']:
                    latest_news = news_item['content']['title']
                
                # Extract URL
                if news_item['content'].get('clickThroughUrl'):
                    news_link = news_item['content']['clickThroughUrl'].get('url', '#')
                elif news_item['content'].get('canonicalUrl'):
                    news_link = news_item['content']['canonicalUrl'].get('url', '#')
            elif 'title' in news_item:
                latest_news = news_item['title']
                news_link = news_item.get('link', '#')
            
        inst_holdings = info.get('heldPercentInstitutions', 0.20)
        
        shares = info.get('sharesOutstanding', (mcap * 1e7) / current_price)
        sector = info.get('sector', 'UNKNOWN')
        trailing_pe = info.get('trailingPE', 0)
        
        de_ratio = info.get('debtToEquity', 0) / 100.0 if info.get('debtToEquity') else 0
        current_ratio = info.get('currentRatio', 1.0)
        
        roe = info.get('returnOnEquity', 0) * 100 if info.get('returnOnEquity') else 0
        
        roce = roe 
        try:
            bs = stock.balance_sheet
            fin_t = stock.financials
            if not bs.empty and not fin_t.empty:
                ebit = float(fin_t.loc['EBIT'].iloc[0]) if 'EBIT' in fin_t.index else None
                assets = float(bs.loc['Total Assets'].iloc[0]) if 'Total Assets' in bs.index else None
                curr_liab = float(bs.loc['Current Liabilities'].iloc[0]) if 'Current Liabilities' in bs.index else None
                if ebit and assets and curr_liab:
                    cap_emp = assets - curr_liab
                    if cap_emp > 0:
                        roce = (ebit / cap_emp) * 100
        except Exception:
            pass
        
        div_yield = info.get('dividendYield', 0) if info.get('dividendYield') else 0
        payout_ratio = info.get('payoutRatio', 0) if info.get('payoutRatio') else 0
        
        profit_cagr = 0.15
        rev_cagr = 0.15
        fcf = 0
        net_income = 0
        
        fcf_array = [0, 0, 0, 0]
        ocf_array = [0, 0, 0, 0]
        
        fin = stock.financials
        if not fin.empty and len(fin.columns) >= 3:
            profits = []
            revs = []
            for col in fin.columns[:4]:
                if 'Net Income' in fin.index and pd.notna(fin.loc['Net Income', col]):
                    profits.append(float(fin.loc['Net Income', col]))
                if 'Total Revenue' in fin.index and pd.notna(fin.loc['Total Revenue', col]):
                    revs.append(float(fin.loc['Total Revenue', col]))
            
            if len(profits) >= 3 and profits[2] > 0 and profits[0] > 0:
                profit_cagr = calculate_cagr(profits[2], profits[0], 2)
                net_income = profits[0]
            if len(revs) >= 3 and revs[2] > 0 and revs[0] > 0:
                rev_cagr = calculate_cagr(revs[2], revs[0], 2)
                
        cf = stock.cashflow
        if not cf.empty:
            ocf = 0
            capex = 0
            latest_cf = cf.iloc[:, 0]
            if 'Operating Cash Flow' in latest_cf.index and pd.notna(latest_cf['Operating Cash Flow']):
                ocf = float(latest_cf['Operating Cash Flow'])
            if 'Capital Expenditure' in latest_cf.index and pd.notna(latest_cf['Capital Expenditure']):
                capex = float(latest_cf['Capital Expenditure'])
            fcf = ocf + capex if capex < 0 else ocf - capex

            num_cols = min(4, len(cf.columns))
            for i in range(num_cols):
                col = cf.columns[i]
                c_ocf = float(cf.loc['Operating Cash Flow', col]) if 'Operating Cash Flow' in cf.index and pd.notna(cf.loc['Operating Cash Flow', col]) else 0
                c_cap = float(cf.loc['Capital Expenditure', col]) if 'Capital Expenditure' in cf.index and pd.notna(cf.loc['Capital Expenditure', col]) else 0
                c_fcf = c_ocf + c_cap if c_cap < 0 else c_ocf - c_cap
                idx = 3 - i
                ocf_array[idx] = round(c_ocf / 1e7, 2)
                fcf_array[idx] = round(c_fcf / 1e7, 2)

        if profit_cagr <= 0: profit_cagr = 0.01
        
        fcf_cagr = profit_cagr
        if fcf_array[1] > 0 and fcf_array[3] > 0:
            fcf_cagr = calculate_cagr(fcf_array[1], fcf_array[3], 2)
        
        wacc, term_growth = get_sector_wacc_and_terminal(sector, mcap)
        comp_growth = calc_composite_growth(rev_cagr, profit_cagr, fcf_cagr, profit_cagr)
        
        if sector == "Financial Services":
            book_value = info.get('bookValue', 0)
            iv = calculate_bank_intrinsic_value(book_value, roe / 100.0, wacc, term_growth)
        else:
            iv = calculate_dcf(fcf, comp_growth, shares, wacc, term_growth)
            
        mos = calculate_margin_of_safety(iv, current_price)
        peg = calculate_peg(trailing_pe, comp_growth * 100)
        
        trap_score = get_value_trap_score(fcf, net_income, roe, de_ratio, profit_cagr)
        strat_risk = score_strategic_risk_v9(ticker, sector)
        moat = score_competitive_moat(ticker)
        cap_alloc = score_capital_allocation(div_yield, payout_ratio)
        q_score = score_quality(roce, fcf, net_income)
        bs_score = score_balance_sheet(de_ratio, current_ratio)
        val_score = score_valuation(mos, peg)
        g_score = score_growth(comp_growth)
        
        historical_inst = get_historical_inst_holdings(ticker)
        inst_flow_delta = inst_holdings - historical_inst if historical_inst is not None else 0.0
        sm_score = score_smart_money(inst_holdings, inst_flow_delta)
        
        concall_sentiment_score, concall_summary = analyze_sentiment(ticker)
        
        final_score = calc_final_v16_score(
            q_score, val_score, moat, strat_risk, cap_alloc, 
            g_score, bs_score, sm_score, trap_score, momentum_multiplier, weights, concall_sentiment_score
        )
        
        return {
            'Ticker': ticker,
            'Name': info.get('shortName', ticker),
            'Sector': sector,
            'Price': round(current_price, 2),
            'Intrinsic_Value': round(iv, 2),
            'Margin_Of_Safety_%': round(mos, 1),
            'Quality_Score': round(q_score, 1),
            'Valuation_Score': round(val_score, 1),
            'Moat_Score': round(moat, 1),
            'Risk_Score': round(strat_risk, 1),
            'CapAlloc_Score': round(cap_alloc, 1),
            'BalanceSheet_Score': round(bs_score, 1),
            'Growth_Score': round(g_score, 1),
            'Smart_Money_Score': round(sm_score, 1),
            'Debt_to_Equity': round(de_ratio, 2),
            'Inst_Holdings_%': round(inst_holdings * 100, 1),
            'Value_Trap_Risk': round(trap_score, 1),
            'Momentum_Status': momentum_status,
            'Momentum_Multiplier_Raw': momentum_multiplier,
            'Latest_Catalyst': latest_news,
            'Latest_News_Link': news_link,
            'Final_V16_Score': round(final_score, 1),
            'ocf_array': json.dumps(ocf_array),
            'fcf_array': json.dumps(fcf_array),
            'Inst_Flow_Delta': round(inst_flow_delta * 100, 2),
            'Concall_Sentiment_Score': round(concall_sentiment_score, 1),
            'Concall_Summary': concall_summary,
            'Trailing_PE': round(trailing_pe, 2) if trailing_pe else 0.0,
            'ROCE_%': round(roce, 1),
            'SMA_50': round(sma50, 2),
            'SMA_200': round(sma200, 2),
            'Div_Yield_%': round(div_yield * 100, 2),
            'FCF_Yield_%': round((fcf / mcap) * 100, 2) if mcap > 0 else 0.0
        }
    except Exception as e:
        print(f"  Warning: {ticker} failed: {e}")
        return None

def main():
    tickers = get_nifty_500_tickers()
    weights = get_active_weights()
    results = []
    
    print(f"\n{'='*80}")
    print(f"RUNNING V16 SELF-LEARNING HARNESS ON {len(tickers)} STOCKS")
    print(f"Current Weights: {weights}")
    print(f"{'='*80}")
    
    for i, t in enumerate(tickers):
        if (i+1) % 10 == 0: print(f"  Processed {i+1}/{len(tickers)} ...")
        res = extract_financials(t, weights)
        if res: results.append(res)
        time.sleep(0.5) # Rate limit protection
        
    df = pd.DataFrame(results)
    if df.empty:
        print("No data fetched.")
        return
        
    df_sorted = df.sort_values('Final_V16_Score', ascending=False)
    
    # Save to SQLite DB for persistent learning
    save_predictions_to_db(results)
    
    # Keep CSV for legacy/debugging
    # df_sorted.to_csv(os.path.join('/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch', 'v16_nifty50_top.csv'), index=False)
    
    print(f"\n✅ DATA SAVED TO DB. TOP COMPOUNDERS (V16):")
    cols = ['Ticker', 'Final_V16_Score', 'Growth_Score', 'Debt_to_Equity', 'Inst_Holdings_%']
    print(df_sorted.head(10)[cols].to_string())

if __name__ == '__main__':
    main()
