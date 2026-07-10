import yfinance as yf
import pandas as pd
import os
import json
from quant_math import *

OUTPUT_DIR = '/Users/saurabhnigam/.gemini/antigravity/brain/4ca10147-d4d2-4287-957e-cfadc0b4954e/scratch'

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
        "TECHM.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS"
    ]

def extract_financials(ticker):
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
        
        div_yield = info.get('dividendYield', 0) if info.get('dividendYield') else 0
        payout_ratio = info.get('payoutRatio', 0) if info.get('payoutRatio') else 0
        
        profit_cagr = 0.15
        rev_cagr = 0.15
        fcf = 0
        net_income = 0
        
        # We need historical arrays for the UI Chart
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

            # Extract last 4 years for the UI Chart
            num_cols = min(4, len(cf.columns))
            for i in range(num_cols):
                col = cf.columns[i]
                c_ocf = float(cf.loc['Operating Cash Flow', col]) if 'Operating Cash Flow' in cf.index and pd.notna(cf.loc['Operating Cash Flow', col]) else 0
                c_cap = float(cf.loc['Capital Expenditure', col]) if 'Capital Expenditure' in cf.index and pd.notna(cf.loc['Capital Expenditure', col]) else 0
                c_fcf = c_ocf + c_cap if c_cap < 0 else c_ocf - c_cap
                # Fill backwards so oldest is index 0
                idx = 3 - i
                ocf_array[idx] = round(c_ocf / 1e7, 2)
                fcf_array[idx] = round(c_fcf / 1e7, 2)

        if profit_cagr <= 0: profit_cagr = 0.05
        
        wacc, term_growth = get_sector_wacc_and_terminal(sector, mcap)
        comp_growth = calc_composite_growth(rev_cagr, profit_cagr, profit_cagr, profit_cagr)
        
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
        sm_score = score_smart_money(inst_holdings)
        
        final_score = calc_final_v15_score(
            q_score, val_score, moat, strat_risk, cap_alloc, 
            g_score, bs_score, sm_score, trap_score, momentum_multiplier
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
            'Growth_Score': round(g_score, 1),
            'Smart_Money_Score': round(sm_score, 1),
            'Debt_to_Equity': round(de_ratio, 2),
            'Inst_Holdings_%': round(inst_holdings * 100, 1),
            'Value_Trap_Risk': round(trap_score, 1),
            'Momentum_Status': momentum_status,
            'Latest_Catalyst': latest_news,
            'Latest_News_Link': news_link,
            'Final_V15_Score': round(final_score, 1),
            # Keep array raw string so we can easily parse it in JS generator
            'ocf_array': json.dumps(ocf_array),
            'fcf_array': json.dumps(fcf_array)
        }
    except Exception as e:
        return None

def main():
    tickers = get_nifty_50_tickers()
    results = []
    print(f"\n{'='*80}")
    print(f"RUNNING V15 INSTITUTIONAL HARNESS ON {len(tickers)} STOCKS")
    print(f"{'='*80}")
    
    for i, t in enumerate(tickers):
        if (i+1) % 5 == 0: print(f"  Processed {i+1}/{len(tickers)} ...")
        res = extract_financials(t)
        if res: results.append(res)
        
    df = pd.DataFrame(results)
    if df.empty:
        print("No data fetched.")
        return
        
    df_sorted = df.sort_values('Final_V15_Score', ascending=False)
    df_sorted.to_csv(os.path.join(OUTPUT_DIR, 'v15_nifty50_top.csv'), index=False)
    
    print(f"\n✅ TOP INSTITUTIONAL COMPOUNDERS (V15):")
    cols = ['Ticker', 'Final_V15_Score', 'Growth_Score', 'Debt_to_Equity', 'Inst_Holdings_%']
    print(df_sorted.head(10)[cols].to_string())

if __name__ == '__main__':
    main()
