def get_sector_wacc_and_terminal(sector, market_cap_cr=5000):
    wacc = 0.12 # Default
    term_growth = 0.03
    
    sector = str(sector).upper()
    sectors = sector.split()
    if any(s in ['IT', 'SOFTWARE', 'TECH', 'TECHNOLOGY'] for s in sectors):
        wacc = 0.10; term_growth = 0.04
    elif any(s in ['CONSUMER', 'RETAIL', 'FMCG'] for s in sectors):
        wacc = 0.10; term_growth = 0.04
    elif any(s in ['PHARMA', 'HEALTH', 'HEALTHCARE'] for s in sectors):
        wacc = 0.11; term_growth = 0.035
    elif any(s in ['CAPITAL', 'INFRA', 'DEFENSE', 'POWER', 'GOODS'] for s in sectors):
        wacc = 0.12; term_growth = 0.03
    elif any(s in ['CHEM', 'CHEMICALS'] for s in sectors):
        wacc = 0.12; term_growth = 0.03
    elif any(s in ['MANUFACTURING', 'AUTO', 'EMS'] for s in sectors):
        wacc = 0.13; term_growth = 0.03
    elif any(s in ['COMMODITY', 'METAL', 'METALS'] for s in sectors):
        wacc = 0.14; term_growth = 0.02
        
    if market_cap_cr < 1000: wacc += 0.03
    elif market_cap_cr < 5000: wacc += 0.01
        
    return wacc, term_growth

def calculate_cagr(start_value, end_value, periods):
    if periods <= 0: return 0.0
    if start_value <= 0 or end_value <= 0: return 0.0
    return ((end_value / start_value) ** (1 / periods)) - 1

def calc_composite_growth(rev_cagr, ebit_cagr, fcf_cagr, eps_cagr):
    return (rev_cagr * 0.30) + (ebit_cagr * 0.30) + (fcf_cagr * 0.25) + (eps_cagr * 0.15)

def calculate_dcf(fcf, growth_rate, shares_outstanding, wacc, terminal_growth, years=5):
    if fcf <= 0 or shares_outstanding <= 0: return 0.0
    safe_growth = min(max(growth_rate, 0.02), 0.18) 
    
    pv_fcf = 0
    projected_fcf = fcf
    for year in range(1, years + 1):
        projected_fcf *= (1 + safe_growth)
        pv_fcf += projected_fcf / ((1 + wacc) ** year)
        
    terminal_value = (projected_fcf * (1 + terminal_growth)) / (wacc - terminal_growth)
    pv_tv = terminal_value / ((1 + wacc) ** years)
    
    return (pv_fcf + pv_tv) / shares_outstanding

def calculate_bank_intrinsic_value(book_value_per_share, roe, cost_of_equity, terminal_growth):
    """
    Price-to-Book (Justified P/B) Valuation Model for Banks.
    Intrinsic Value = Book Value * (ROE - g) / (Ke - g)
    """
    if book_value_per_share <= 0 or roe <= 0: return 0.0
    safe_roe = min(max(roe, 0.05), 0.25)
    
    # CRITICAL-1 FIX: Enforce minimum spread to prevent P/B explosion
    spread = cost_of_equity - terminal_growth
    if spread < 0.03:
        spread = 0.03
    
    # Ensure g <= Ke - 0.03 for stability
    safe_growth = min(terminal_growth, cost_of_equity - 0.03)
        
    justified_pb = (safe_roe - safe_growth) / spread
    if justified_pb < 0.2: justified_pb = 0.2 # Floor
    
    return book_value_per_share * justified_pb

def calculate_margin_of_safety(intrinsic_value, current_price):
    if intrinsic_value <= 0 or current_price <= 0: return -99.9
    raw_mos = ((intrinsic_value - current_price) / intrinsic_value) * 100
    return min(max(raw_mos, -99.9), 99.9)

def calculate_peg(trailing_pe, growth_rate_pct):
    if growth_rate_pct <= 0: return 999.0
    if trailing_pe <= 0: return 0.0
    return trailing_pe / growth_rate_pct

def get_value_trap_score(fcf, net_income, roe, de_ratio, profit_cagr):
    score = 0
    if fcf <= 0 and net_income > 0: score += 40
    if de_ratio > 1.5: score += 20
    if de_ratio > 3.0: score += 20
    if roe < 10: score += 10
    if roe < 5: score += 10
    if profit_cagr < 0: score += 20
    return min(score, 100)

def score_strategic_risk_v9(ticker, sector):
    ticker = str(ticker).replace('.NS', '').upper()
    sector = str(sector).upper()
    
    reg_risk = 5   
    dis_risk = 5   
    esg_risk = 5   
    exe_risk = 5   
    cus_risk = 5   

    if ticker in ['IEX', 'MCX', 'IRCTC']: reg_risk = 10
    if ticker in ['BHARTIARTL', 'RELIANCE', 'ITC']: reg_risk = 8
    if 'PHARMA' in sector: reg_risk = 7
    
    if 'IT' in sector or 'SOFTWARE' in sector or 'TECH' in sector or 'TECHNOLOGY' in sector: 
        dis_risk = 9 
        if ticker in ['TCS', 'INFY', 'HCLTECH']: dis_risk = 7 
    if ticker in ['ZOMATO', 'PAYTM']: dis_risk = 9
    if ticker in ['ASIANPAINT', 'BRITANNIA', 'NESTLEIND']: dis_risk = 1
    
    if ticker in ['COALINDIA', 'ONGC', 'NTPC']: esg_risk = 10
    if 'AUTO' in sector: esg_risk = 7
    if 'IT' in sector or 'TECH' in sector or 'TECHNOLOGY' in sector: esg_risk = 1
    
    if 'CAPITAL' in sector or 'INFRA' in sector: exe_risk = 8
    if ticker in ['BHEL', 'TEXRAIL']: exe_risk = 9
    
    if ('IT' in sector or 'TECH' in sector or 'TECHNOLOGY' in sector) and ticker not in ['TCS', 'INFY', 'HCLTECH']: cus_risk = 8
    if ticker in ['NEWGEN', 'SONATSOFTW']: cus_risk = 8

    total_risk_penalty = (reg_risk + dis_risk + esg_risk + exe_risk + cus_risk) 
    safety_score = 100 - (total_risk_penalty * 2)
    return min(max(safety_score, 0), 100)

def score_competitive_moat(ticker):
    ticker = str(ticker).replace('.NS', '').upper()
    if ticker in ['MCX', 'BSE', 'CDSL', 'IEX', 'CAMS']: return 100 
    if ticker in ['ASIANPAINT', 'BRITANNIA', 'NESTLEIND', 'ITC']: return 90 
    if ticker in ['BHARTIARTL', 'RELIANCE']: return 85 
    if ticker in ['KFINTECH', 'SONATSOFTW']: return 80 
    if ticker in ['TCS', 'INFY']: return 75 
    if ticker in ['LUPIN', 'SUNPHARMA', 'DIVISLAB']: return 70 
    return 50

def score_capital_allocation(div_yield, payout_ratio):
    score = 50 
    if div_yield > 0.05: score += 40
    elif div_yield > 0.02: score += 30
    elif div_yield > 0.01: score += 15
    
    if 0.20 <= payout_ratio <= 0.60: score += 20 
    elif payout_ratio > 0.80: score -= 20 
    elif payout_ratio < 0: score -= 40 
    return min(max(score, 0), 100)

def score_quality(roce, fcf, net_income):
    score = 0
    if roce > 20: score += 50
    elif roce > 15: score += 30
    elif roce > 10: score += 10
    
    fcf_conv = 0
    if net_income > 0: 
        fcf_conv = fcf / net_income
        if fcf_conv > 0.8: score += 50
        elif fcf_conv > 0.5: score += 25
        elif fcf_conv < 0: score = max(0, score - 30)
    else:
        # Turnaround / loss-making companies
        if fcf > 0:
            score += 25 # Positive FCF despite losses is good
        elif fcf < 0:
            score = max(0, score - 30) # Bleeding cash

    return min(100, max(0, score))

def score_balance_sheet(de_ratio, current_ratio):
    score = 100
    if de_ratio > 1.0: score -= 30
    if de_ratio > 2.0: score -= 40
    if current_ratio < 1.0: score -= 30
    return max(0, score)

def score_valuation(margin_of_safety, peg):
    score = 0
    if margin_of_safety > 50: score = 100
    elif margin_of_safety >= 0: score = 80
    elif margin_of_safety >= -25: score = 60
    elif margin_of_safety >= -50: score = 40
    else: score = 0
    
    if peg < 1.0: score += 20
    elif peg > 3.0: score -= 20
    
    return min(max(score, 0), 100)

def score_growth(comp_growth):
    score = 0
    growth_pct = comp_growth * 100
    if growth_pct >= 20: score = 100
    elif growth_pct >= 15: score = 80
    elif growth_pct >= 10: score = 50
    elif growth_pct >= 5: score = 20
    return score

def score_smart_money(inst_holdings_pct, inst_flow_delta):
    """
    Factor 9: Big Money Conviction (FII / DII)
    Now incorporates historical delta to detect net buying/selling.
    """
    score = 50
    if inst_holdings_pct > 0.50: score += 20
    elif inst_holdings_pct > 0.30: score += 10
    elif inst_holdings_pct < 0.15: score -= 20
    
    # Delta (Net Flow) is the primary driver
    if inst_flow_delta > 0.02: score += 40 # > 2% net buying
    elif inst_flow_delta > 0.005: score += 20 # Mild net buying
    elif inst_flow_delta < -0.02: score -= 40 # > 2% net selling
    elif inst_flow_delta < -0.005: score -= 20 # Mild net selling
    
    return min(max(score, 0), 100)

def get_momentum_status(price, sma50, sma200):
    if sma50 <= 0 or sma200 <= 0 or price <= 0:
        return "Unknown", 1.0
    if price < sma50 and sma50 < sma200:
        return "Death Cross (Falling Knife)", 0.0
    elif price < sma50:
        return "Bearish Short-Term", 0.8
    elif price > sma50 and sma50 > sma200:
        return "Golden Cross (Bullish)", 1.0
    elif price > sma50:
        return "Bullish Short-Term", 1.0
    return "Neutral", 1.0

def calc_final_v16_score(quality, val, moat, strat_risk, cap_alloc, growth, bs, smart_money, trap_score, momentum_multiplier, weights, concall_sentiment=0):
    """
    V16 Self-Learning Architecture + V17 NLP Sentiment
    """
    base_score = (
        (quality * weights.get('quality_weight', 0.20)) + 
        (growth * weights.get('growth_weight', 0.20)) + 
        (val * weights.get('valuation_weight', 0.15)) + 
        (strat_risk * weights.get('risk_weight', 0.15)) + 
        (moat * weights.get('moat_weight', 0.10)) + 
        (bs * weights.get('bs_weight', 0.10)) + 
        (cap_alloc * weights.get('cap_alloc_weight', 0.05)) + 
        (smart_money * weights.get('smart_money_weight', 0.05))
    )
    
    # Add NLP sentiment (+/- 10 impact)
    base_score += (concall_sentiment / 2)
    
    penalty_multiplier = 1.0
    if trap_score >= 75: penalty_multiplier = 0.2
    elif trap_score >= 50: penalty_multiplier = 0.5
    elif trap_score >= 25: penalty_multiplier = 0.8
    
    final = base_score * penalty_multiplier * momentum_multiplier
    return min(max(final, 0.0), 100.0)
