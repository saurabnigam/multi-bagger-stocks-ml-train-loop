"""
Core quantitative scoring formulas.

Red-team fixes (Sep 2026) — see docs/analysis/red_team_review.md:
  * Sector matching is token-based. The old substring test made 'UTILITIES'
    match 'IT', so every utility was scored as a disruptable tech company.
  * WACC/terminal-growth lookup understands the Yahoo Finance sector taxonomy
    ("Basic Materials", "Industrials", "Consumer Cyclical", ...). Before, most
    of the universe silently fell through to the default.
  * Headline-keyword "sentiment" no longer adds up to +10 points outside the
    8-factor weight budget. It is recorded, and its Rank IC is reported, but
    SENTIMENT_SCALE defaults to 0.0 until it earns a place.
  * Helpers for yfinance unit normalisation, FCF normalisation for the DCF, and
    growth estimation that distinguishes "no data" from "15% growth".
"""
import re

# Contribution of the headline sentiment term to the base score, in points per
# sentiment unit. 0.5 reproduces the pre-review behaviour (+/-10 points).
SENTIMENT_SCALE = 0.0

FACTOR_WEIGHT_KEYS = [
    'quality_weight', 'growth_weight', 'valuation_weight', 'risk_weight',
    'moat_weight', 'bs_weight', 'cap_alloc_weight', 'smart_money_weight',
]
DEFAULT_WEIGHTS = {
    'quality_weight': 0.20, 'growth_weight': 0.20, 'valuation_weight': 0.15,
    'risk_weight': 0.15, 'moat_weight': 0.10, 'bs_weight': 0.10,
    'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05,
}


def sector_tokens(sector):
    """'Consumer Cyclical / Auto Manufacturers' -> {'CONSUMER','CYCLICAL','AUTO','MANUFACTURERS'}"""
    return {t for t in re.split(r'[^A-Z]+', str(sector).upper()) if t}


def get_sector_wacc_and_terminal(sector, market_cap_cr=5000):
    wacc = 0.12  # Default
    term_growth = 0.03

    toks = sector_tokens(sector)

    def has(*words):
        return any(w in toks for w in words)

    if has('IT', 'SOFTWARE', 'TECH', 'TECHNOLOGY'):
        wacc = 0.10; term_growth = 0.04
    elif has('PHARMA', 'HEALTH', 'HEALTHCARE'):
        wacc = 0.11; term_growth = 0.035
    elif has('MANUFACTURING', 'AUTO', 'EMS'):
        wacc = 0.13; term_growth = 0.03
    elif has('CYCLICAL'):                                  # Yahoo: "Consumer Cyclical"
        wacc = 0.12; term_growth = 0.03
    elif has('CONSUMER', 'RETAIL', 'FMCG', 'STAPLES', 'DEFENSIVE'):
        wacc = 0.10; term_growth = 0.04
    elif has('CAPITAL', 'INFRA', 'DEFENSE', 'POWER', 'GOODS', 'INDUSTRIALS'):
        wacc = 0.12; term_growth = 0.03
    elif has('CHEM', 'CHEMICALS'):
        wacc = 0.12; term_growth = 0.03
    elif has('COMMODITY', 'METAL', 'METALS', 'MATERIALS', 'MINING'):  # Yahoo: "Basic Materials"
        wacc = 0.14; term_growth = 0.02
    elif has('UTILITIES', 'UTILITY'):
        wacc = 0.11; term_growth = 0.03
    elif has('ENERGY', 'OIL', 'GAS'):
        wacc = 0.13; term_growth = 0.025
    elif has('REAL', 'REALTY', 'ESTATE'):
        wacc = 0.13; term_growth = 0.03
    elif has('COMMUNICATION', 'TELECOM'):
        wacc = 0.11; term_growth = 0.035
    elif has('FINANCIAL', 'FINANCE', 'BANK', 'BANKS'):
        wacc = 0.12; term_growth = 0.03

    if market_cap_cr < 1000: wacc += 0.03
    elif market_cap_cr < 5000: wacc += 0.01

    return wacc, term_growth


def calculate_cagr(start_value, end_value, periods):
    if periods <= 0: return 0.0
    if start_value <= 0 or end_value <= 0: return 0.0
    return ((end_value / start_value) ** (1 / periods)) - 1


def estimate_growth(series_newest_first, periods=2):
    """
    Growth estimate from an annual series ordered newest -> oldest.

    Returns (growth_rate, flag). The flag explains how the number was obtained
    so downstream code can distinguish real growth from an imputed value:
      'ok'               both endpoints positive, plain CAGR
      'loss_to_profit'   start <= 0 < end : turnaround, imputed +15%
      'profit_to_loss'   start > 0 >= end : collapse, imputed -25%
      'undefined'        both endpoints <= 0, treated as 0%
      'insufficient'     fewer than periods+1 data points, returns None
    The old code imputed +15% for every one of these cases.
    """
    vals = list(series_newest_first)
    if len(vals) < periods + 1 or vals[0] is None or vals[periods] is None:
        return None, 'insufficient'
    end, start = float(vals[0]), float(vals[periods])
    if start > 0 and end > 0:
        return calculate_cagr(start, end, periods), 'ok'
    if start <= 0 < end:
        return 0.15, 'loss_to_profit'
    if start > 0 >= end:
        return -0.25, 'profit_to_loss'
    return 0.0, 'undefined'


def calc_composite_growth(rev_cagr, ebit_cagr, fcf_cagr, eps_cagr):
    return (rev_cagr * 0.30) + (ebit_cagr * 0.30) + (fcf_cagr * 0.25) + (eps_cagr * 0.15)


def normalized_fcf(fcf_newest_first, years=3):
    """
    FCF to feed the DCF. A single year's FCF is dominated by working-capital
    swings (HEROMOTOCO's FY26 FCF was 2.1x FY25, which the old code extrapolated
    at 18% for five years). Uses the mean of the last `years` reported years.
    Returns 0.0 (=> DCF yields no intrinsic value) when the average is <= 0.
    """
    vals = [float(v) for v in fcf_newest_first[:years] if v is not None]
    if not vals:
        return 0.0
    avg = sum(vals) / len(vals)
    return avg if avg > 0 else 0.0


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


def normalize_yield(value):
    """
    yfinance >= 1.x returns `dividendYield` already in percent (3.48 means
    3.48%), while older versions returned a fraction (0.0348). The encodings
    overlap below 1.0, so the rule is: anything above 0.25 is a percent (no
    Nifty 500 stock yields 25%). Prefer dividendRate / price in the harness,
    which is unambiguous; this is the fallback. Returns a fraction. None -> 0.0.
    """
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    if v > 0.25:
        v = v / 100.0
    return max(v, 0.0)


def get_value_trap_score(fcf, net_income, roe, de_ratio, profit_cagr):
    """
    roe may be None when the data source has no figure. Missing data must not
    count as ROE < 5% (it did: 288/500 stocks carried a phantom +20).
    """
    score = 0
    if fcf <= 0 and net_income > 0: score += 40
    if de_ratio > 1.5: score += 20
    if de_ratio > 3.0: score += 20
    if roe is not None:
        if roe < 10: score += 10
        if roe < 5: score += 10
    if profit_cagr < 0: score += 20
    return min(score, 100)


def score_strategic_risk_v9(ticker, sector):
    ticker = str(ticker).replace('.NS', '').upper()
    toks = sector_tokens(sector)
    is_tech = bool(toks & {'IT', 'SOFTWARE', 'TECH', 'TECHNOLOGY'})

    reg_risk = 5
    dis_risk = 5
    esg_risk = 5
    exe_risk = 5
    cus_risk = 5

    if ticker in ['IEX', 'MCX', 'IRCTC']: reg_risk = 10
    if ticker in ['BHARTIARTL', 'RELIANCE', 'ITC']: reg_risk = 8
    if 'PHARMA' in toks: reg_risk = 7

    if is_tech:
        dis_risk = 9
        if ticker in ['TCS', 'INFY', 'HCLTECH']: dis_risk = 7
    if ticker in ['ZOMATO', 'PAYTM']: dis_risk = 9
    if ticker in ['ASIANPAINT', 'BRITANNIA', 'NESTLEIND']: dis_risk = 1

    if ticker in ['COALINDIA', 'ONGC', 'NTPC']: esg_risk = 10
    if 'AUTO' in toks: esg_risk = 7
    if is_tech: esg_risk = 1

    if toks & {'CAPITAL', 'INFRA'}: exe_risk = 8
    if ticker in ['BHEL', 'TEXRAIL']: exe_risk = 9

    if is_tech and ticker not in ['TCS', 'INFY', 'HCLTECH']: cus_risk = 8
    if ticker in ['NEWGEN', 'SONATSOFTW']: cus_risk = 8

    total_risk_penalty = (reg_risk + dis_risk + esg_risk + exe_risk + cus_risk)
    safety_score = 100 - (total_risk_penalty * 2)
    return min(max(safety_score, 0), 100)


def score_competitive_moat(ticker):
    """
    NOTE: a hand-picked ticker list. 482/500 stocks receive the default 50, so
    this factor is close to a constant and its 'Rank IC' is driven by 18 names
    chosen with hindsight. Treat with suspicion; see red_team_review.md.
    """
    ticker = str(ticker).replace('.NS', '').upper()
    if ticker in ['MCX', 'BSE', 'CDSL', 'IEX', 'CAMS']: return 100
    if ticker in ['ASIANPAINT', 'BRITANNIA', 'NESTLEIND', 'ITC']: return 90
    if ticker in ['BHARTIARTL', 'RELIANCE']: return 85
    if ticker in ['KFINTECH', 'SONATSOFTW']: return 80
    if ticker in ['TCS', 'INFY']: return 75
    if ticker in ['LUPIN', 'SUNPHARMA', 'DIVISLAB']: return 70
    return 50


def score_capital_allocation(div_yield, payout_ratio):
    """div_yield and payout_ratio are FRACTIONS (0.03 == 3%)."""
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


def trap_penalty_multiplier(trap_score):
    if trap_score >= 75: return 0.2
    if trap_score >= 50: return 0.5
    if trap_score >= 25: return 0.8
    return 1.0


def calc_base_score(quality, val, moat, strat_risk, cap_alloc, growth, bs, smart_money, weights, concall_sentiment=0):
    """
    Weighted fundamental composite BEFORE the trap and momentum multipliers.
    Stored separately so the optimizer can tell fundamental alpha apart from
    the trend filter (which turned out to explain most of the composite IC).
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
    base_score += concall_sentiment * SENTIMENT_SCALE
    return min(max(base_score, 0.0), 100.0)


def calc_final_v16_score(quality, val, moat, strat_risk, cap_alloc, growth, bs, smart_money, trap_score, momentum_multiplier, weights, concall_sentiment=0):
    """
    V16 Self-Learning Architecture. final = base x trap_multiplier x momentum_multiplier
    """
    base_score = calc_base_score(quality, val, moat, strat_risk, cap_alloc, growth, bs, smart_money, weights, concall_sentiment)
    final = base_score * trap_penalty_multiplier(trap_score) * momentum_multiplier
    return min(max(final, 0.0), 100.0)
