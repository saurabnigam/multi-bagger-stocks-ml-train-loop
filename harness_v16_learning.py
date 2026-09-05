"""
Nifty 500 data acquisition + scoring harness.

Red-team fixes (Sep 2026) — see docs/analysis/red_team_review.md:
  * DB path is repo-relative (was a hard-coded private scratch directory).
  * yfinance `dividendYield` is already a percent -> was multiplied by 100 again
    (HEROMOTOCO showed 349% yield and every dividend payer maxed Cap-Alloc).
  * Missing ROE no longer counts as ROE < 5% in the value-trap score.
  * The value-trap score receives the REAL profit growth. It used to receive a
    number floored at +1%, so the "profit declining" penalty could never fire.
  * Missing growth data is flagged instead of being imputed as +15%.
  * DCF uses 3-year average FCF, not a single (often spiky) year.
  * EBIT and EPS growth are read from the statements when present, instead of
    both being silently proxied by net-profit growth.
  * Universe fallback is the last full snapshot's ticker list, not Nifty 50.
  * A partial run can no longer delete an existing full snapshot for today.
  * Sector/industry both feed the WACC and strategic-risk lookups.
  * base_score (pre-multiplier composite) is stored for alpha attribution.
"""
import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import datetime
import time
import urllib.request
import csv

from config import DB_PATH, FULL_UNIVERSE_MIN
from db_setup import ensure_schema
from quant_math import *
from concall_analyzer import analyze_sentiment

REQUEST_SLEEP_SECONDS = 0.5  # Prime directive: never remove (HTTP 429 protection)


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
        "TECHM.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS", "IOC.NS",
    ]


def get_last_full_universe():
    """Ticker list from the most recent full snapshot already in the DB."""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute('''
            SELECT date FROM daily_predictions GROUP BY date
            HAVING count(*) >= ? ORDER BY date DESC LIMIT 1
        ''', (FULL_UNIVERSE_MIN,)).fetchone()
        if not row:
            return []
        return [r[0] for r in conn.execute(
            'SELECT ticker FROM daily_predictions WHERE date = ? ORDER BY ticker', (row[0],))]
    finally:
        conn.close()


def get_nifty_500_tickers():
    url = 'https://niftyindices.com/IndexConstituent/ind_nifty500list.csv'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        response = urllib.request.urlopen(req, timeout=10)
        lines = [l.decode('utf-8') for l in response.readlines()]
        reader = csv.DictReader(lines)
        tickers = [row['Symbol'] + '.NS' for row in reader if row.get('Symbol')]
        if len(tickers) >= FULL_UNIVERSE_MIN:
            return tickers
        raise ValueError(f"constituent file had only {len(tickers)} rows")
    except Exception as e:
        print(f"WARNING: Failed to fetch Nifty 500 constituents: {e}")
        prev = get_last_full_universe()
        if prev:
            print(f"         Falling back to the {len(prev)} tickers of the last full snapshot.")
            return prev
        print("         No prior snapshot in DB; falling back to Nifty 50 (partial universe).")
        return get_nifty_50_tickers()


def get_active_weights():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM active_weights ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return {k: row[k] for k in FACTOR_WEIGHT_KEYS}
    return dict(DEFAULT_WEIGHTS)


def save_predictions_to_db(results, run_date=None):
    """
    Replace today's snapshot. Refuses to overwrite an existing full-universe
    snapshot with a partial one (a failed run used to wipe the day's data).
    """
    today = run_date or datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    cursor = conn.cursor()

    existing = cursor.execute('SELECT count(*) FROM daily_predictions WHERE date = ?', (today,)).fetchone()[0]
    if existing >= FULL_UNIVERSE_MIN and len(results) < FULL_UNIVERSE_MIN:
        conn.close()
        raise RuntimeError(
            f"Refusing to replace full snapshot for {today} ({existing} rows) with a partial run ({len(results)} rows)")

    try:
        cursor.execute('BEGIN')
        cursor.execute('DELETE FROM daily_predictions WHERE date = ?', (today,))
        for r in results:
            cursor.execute('''
                INSERT INTO daily_predictions (
                    date, ticker, price, quality_score, valuation_score, growth_score,
                    moat_score, risk_score, bs_score, cap_alloc_score, smart_money_score,
                    trap_score, momentum_multiplier, final_score, latest_catalyst, news_link, raw_json,
                    inst_flow_delta, concall_sentiment_score, concall_summary, base_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                today, r['Ticker'], r['Price'], r['Quality_Score'], r['Valuation_Score'],
                r['Growth_Score'], r['Moat_Score'], r['Risk_Score'], r['BalanceSheet_Score'],
                r['CapAlloc_Score'], r['Smart_Money_Score'], r['Value_Trap_Risk'],
                r['Momentum_Multiplier_Raw'], r['Final_V16_Score'], r['Latest_Catalyst'], r['Latest_News_Link'],
                json.dumps(r), r['Inst_Flow_Delta'], r['Concall_Sentiment_Score'], r['Concall_Summary'],
                r['Base_Score'],
            ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_historical_inst_holdings(ticker):
    """Institutional holding % from at least 7 days ago, to compute the flow delta."""
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


def _row_series(frame, label, n=4):
    """Newest-first list of floats for a statement row, None where missing."""
    if frame is None or frame.empty or label not in frame.index:
        return []
    out = []
    for col in frame.columns[:n]:
        v = frame.loc[label, col]
        out.append(float(v) if pd.notna(v) else None)
    return out


def _fcf_series(cf, n=4):
    ocf = _row_series(cf, 'Operating Cash Flow', n)
    capex = _row_series(cf, 'Capital Expenditure', n)
    out = []
    for i in range(max(len(ocf), len(capex))):
        o = ocf[i] if i < len(ocf) else None
        c = capex[i] if i < len(capex) else None
        if o is None:
            out.append(None)
            continue
        c = c or 0.0
        out.append(o + c if c < 0 else o - c)
    return out, ocf


def extract_financials(ticker, weights):
    flags = []
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or info.get('quoteType') == 'NONE': return None

        current_price = info.get('currentPrice', info.get('regularMarketPrice', 0)) or 0
        if current_price == 0: return None

        mcap = (info.get('marketCap', 0) or 0) / 1e7
        if mcap < 100: return None

        sma50 = info.get('fiftyDayAverage', 0) or 0
        sma200 = info.get('twoHundredDayAverage', 0) or 0
        momentum_status, momentum_multiplier = get_momentum_status(current_price, sma50, sma200)

        # --- News (fetched once, shared with the sentiment scorer) ---
        latest_news = "No recent catalyst."
        news_link = "#"
        news = []
        try:
            news = stock.news or []
        except Exception:
            news = []
        if news:
            news_item = news[0]
            if 'content' in news_item:
                content = news_item['content'] or {}
                latest_news = content.get('title', latest_news)
                if content.get('clickThroughUrl'):
                    news_link = content['clickThroughUrl'].get('url', '#')
                elif content.get('canonicalUrl'):
                    news_link = content['canonicalUrl'].get('url', '#')
            elif 'title' in news_item:
                latest_news = news_item['title']
                news_link = news_item.get('link', '#')

        inst_raw = info.get('heldPercentInstitutions')
        if inst_raw is None:
            inst_holdings = 0.20
            flags.append('inst_holdings_imputed')
        else:
            inst_holdings = float(inst_raw)

        shares = info.get('sharesOutstanding') or ((mcap * 1e7) / current_price)
        sector = info.get('sector', 'UNKNOWN') or 'UNKNOWN'
        industry = info.get('industry', '') or ''
        sector_key = f"{sector} {industry}"
        trailing_pe = info.get('trailingPE', 0) or 0

        de_ratio = info.get('debtToEquity', 0) / 100.0 if info.get('debtToEquity') else 0
        if info.get('debtToEquity') is None: flags.append('de_missing')
        current_ratio = info.get('currentRatio')
        if current_ratio is None:
            current_ratio = 1.0
            flags.append('current_ratio_missing')

        roe_raw = info.get('returnOnEquity')
        roe = roe_raw * 100 if roe_raw is not None else None  # percent or None

        # --- Statements ---
        fin = stock.financials
        bs = stock.balance_sheet
        cf = stock.cashflow

        profits = _row_series(fin, 'Net Income')
        revs = _row_series(fin, 'Total Revenue')
        ebits = _row_series(fin, 'EBIT')
        eps_series = _row_series(fin, 'Diluted EPS') or _row_series(fin, 'Basic EPS')
        fcf_hist, ocf_hist = _fcf_series(cf)

        net_income = profits[0] if profits and profits[0] is not None else 0.0
        fcf = fcf_hist[0] if fcf_hist and fcf_hist[0] is not None else 0.0

        # ROE fallback from statements when Yahoo has none
        book_value = info.get('bookValue', 0) or 0
        if roe is None and book_value > 0 and shares > 0 and net_income:
            roe = net_income / (book_value * shares) * 100
            flags.append('roe_derived')
        elif roe is None:
            flags.append('roe_missing')

        # ROCE = EBIT / (Total Assets - Current Liabilities); fall back to ROE
        roce = None
        try:
            ebit0 = ebits[0] if ebits else None
            assets = _row_series(bs, 'Total Assets', 1)
            curr_liab = _row_series(bs, 'Current Liabilities', 1)
            if ebit0 and assets and assets[0] and curr_liab and curr_liab[0]:
                cap_emp = assets[0] - curr_liab[0]
                if cap_emp > 0:
                    roce = (ebit0 / cap_emp) * 100
        except Exception:
            pass
        if roce is None:
            roce = roe if roe is not None else 0.0
            flags.append('roce_proxied_by_roe' if roe is not None else 'roce_missing')

        # dividendRate is rupees per share -> unambiguous; dividendYield's units changed between yfinance versions
        div_rate = info.get('dividendRate')
        if div_rate is not None and div_rate >= 0 and current_price > 0:
            div_yield = min(float(div_rate) / current_price, 0.25)
        else:
            div_yield = normalize_yield(info.get('dividendYield'))
            if info.get('dividendYield') is not None: flags.append('div_yield_from_heuristic_units')
        payout_ratio = info.get('payoutRatio', 0) or 0

        # --- Growth (flagged, not imputed) ---
        profit_cagr, pflag = estimate_growth(profits)
        rev_cagr, rflag = estimate_growth(revs)
        ebit_cagr, eflag = estimate_growth(ebits)
        eps_cagr, epsflag = estimate_growth(eps_series)
        fcf_cagr, fflag = estimate_growth(fcf_hist)

        if profit_cagr is None:
            profit_cagr = 0.05; flags.append('profit_growth_insufficient')
        elif pflag != 'ok':
            flags.append(f'profit_growth_{pflag}')
        if rev_cagr is None:
            rev_cagr = 0.05; flags.append('rev_growth_insufficient')
        elif rflag != 'ok':
            flags.append(f'rev_growth_{rflag}')
        if ebit_cagr is None or eflag != 'ok':
            ebit_cagr = profit_cagr; flags.append('ebit_growth_proxied')
        if eps_cagr is None or epsflag != 'ok':
            eps_cagr = profit_cagr; flags.append('eps_growth_proxied')
        if fcf_cagr is None or fflag != 'ok':
            fcf_cagr = profit_cagr; flags.append('fcf_growth_proxied')

        wacc, term_growth = get_sector_wacc_and_terminal(sector_key, mcap)
        comp_growth = calc_composite_growth(rev_cagr, ebit_cagr, fcf_cagr, eps_cagr)

        if sector == "Financial Services":
            iv = calculate_bank_intrinsic_value(book_value, (roe or 0) / 100.0, wacc, term_growth)
            fcf_for_dcf = 0.0
        else:
            fcf_for_dcf = normalized_fcf(fcf_hist)
            iv = calculate_dcf(fcf_for_dcf, comp_growth, shares, wacc, term_growth)

        mos = calculate_margin_of_safety(iv, current_price)
        peg = calculate_peg(trailing_pe, comp_growth * 100)

        trap_score = get_value_trap_score(fcf, net_income, roe, de_ratio, profit_cagr)
        strat_risk = score_strategic_risk_v9(ticker, sector_key)
        moat = score_competitive_moat(ticker)
        cap_alloc = score_capital_allocation(div_yield, payout_ratio)
        q_score = score_quality(roce, fcf, net_income)
        bs_score = score_balance_sheet(de_ratio, current_ratio)
        val_score = score_valuation(mos, peg)
        g_score = score_growth(comp_growth)

        historical_inst = get_historical_inst_holdings(ticker)
        inst_flow_delta = inst_holdings - historical_inst if historical_inst is not None else 0.0
        if historical_inst is None: flags.append('inst_flow_no_history')
        sm_score = score_smart_money(inst_holdings, inst_flow_delta)

        concall_sentiment_score, concall_summary = analyze_sentiment(ticker, news=news)

        base_score = calc_base_score(
            q_score, val_score, moat, strat_risk, cap_alloc,
            g_score, bs_score, sm_score, weights, concall_sentiment_score
        )
        final_score = calc_final_v16_score(
            q_score, val_score, moat, strat_risk, cap_alloc,
            g_score, bs_score, sm_score, trap_score, momentum_multiplier, weights, concall_sentiment_score
        )

        def cr(v):  # rupees -> crores, rounded; None stays 0 for the UI arrays
            return round((v or 0.0) / 1e7, 2)

        # Arrays are oldest -> newest (4 slots) for the UI chart
        fcf_array = [cr(v) for v in reversed((fcf_hist + [None] * 4)[:4])]
        ocf_array = [cr(v) for v in reversed((ocf_hist + [None] * 4)[:4])]

        return {
            'Ticker': ticker,
            'Name': info.get('shortName', ticker),
            'Sector': sector,
            'Industry': industry,
            'Market_Cap_Cr': round(mcap, 0),
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
            'Base_Score': round(base_score, 1),
            'Final_V16_Score': round(final_score, 1),
            'ocf_array': json.dumps(ocf_array),
            'fcf_array': json.dumps(fcf_array),
            'Inst_Flow_Delta': round(inst_flow_delta * 100, 2),
            'Concall_Sentiment_Score': round(concall_sentiment_score, 1),
            'Concall_Summary': concall_summary,
            'Trailing_PE': round(trailing_pe, 2) if trailing_pe else 0.0,
            'ROCE_%': round(roce, 1),
            'ROE_%': round(roe, 1) if roe is not None else None,
            'Composite_Growth_%': round(comp_growth * 100, 1),
            'Profit_CAGR_%': round(profit_cagr * 100, 1),
            'Revenue_CAGR_%': round(rev_cagr * 100, 1),
            'WACC_%': round(wacc * 100, 1),
            'DCF_FCF_Cr': round(fcf_for_dcf / 1e7, 2),
            'SMA_50': round(sma50, 2),
            'SMA_200': round(sma200, 2),
            'Div_Yield_%': round(div_yield * 100, 2),
            'FCF_Yield_%': round((fcf / (mcap * 1e7)) * 100, 2) if mcap > 0 else 0.0,
            'Data_Flags': flags,
        }
    except Exception as e:
        print(f"  Warning: {ticker} failed: {e}")
        return None


def main():
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    conn.close()

    tickers = get_nifty_500_tickers()
    weights = get_active_weights()
    results = []

    print(f"\n{'='*80}")
    print(f"RUNNING V16 SELF-LEARNING HARNESS ON {len(tickers)} STOCKS")
    print(f"DB: {DB_PATH}")
    print(f"Current Weights: {weights}")
    print(f"{'='*80}")

    for i, t in enumerate(tickers):
        if (i+1) % 10 == 0: print(f"  Processed {i+1}/{len(tickers)} ...")
        res = extract_financials(t, weights)
        if res: results.append(res)
        time.sleep(REQUEST_SLEEP_SECONDS)  # Rate limit protection

    df = pd.DataFrame(results)
    if df.empty:
        print("No data fetched.")
        return

    if len(results) < FULL_UNIVERSE_MIN:
        print(f"WARNING: only {len(results)} stocks scored; this will NOT count as a full snapshot.")

    df_sorted = df.sort_values('Final_V16_Score', ascending=False)
    save_predictions_to_db(results)

    flag_counts = pd.Series([f for r in results for f in r['Data_Flags']]).value_counts()
    print("\nData-quality flags this run:")
    print(flag_counts.to_string() if not flag_counts.empty else "  none")

    print(f"\n✅ DATA SAVED TO DB. TOP COMPOUNDERS (V16):")
    cols = ['Ticker', 'Final_V16_Score', 'Base_Score', 'Growth_Score', 'Debt_to_Equity', 'Inst_Holdings_%']
    print(df_sorted.head(10)[cols].to_string())


if __name__ == '__main__':
    main()
