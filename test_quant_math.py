import pytest
from quant_math import *

# =============================================================================
# SECTOR WACC
# =============================================================================
def test_sector_wacc():
    w, t = get_sector_wacc_and_terminal("SOFTWARE", 10000)
    assert w == 0.10
    w2, t2 = get_sector_wacc_and_terminal("COMMODITY", 500)
    assert w2 == 0.17  # 0.14 + 0.03 small-cap premium

def test_sector_wacc_financial_services():
    """Financial Services falls to default WACC - verify this is documented."""
    w, t = get_sector_wacc_and_terminal("Financial Services", 50000)
    assert w == 0.12  # Default, NOT sector-specific
    assert t == 0.03

def test_sector_wacc_small_cap_premium():
    w1, _ = get_sector_wacc_and_terminal("IT", 500)   # < 1000cr -> +3%
    w2, _ = get_sector_wacc_and_terminal("IT", 3000)   # < 5000cr -> +1%
    w3, _ = get_sector_wacc_and_terminal("IT", 10000)  # no premium
    assert w1 == 0.13
    assert w2 == 0.11
    assert w3 == 0.10

# =============================================================================
# CAGR
# =============================================================================
def test_cagrs():
    assert round(calculate_cagr(100, 133.1, 3), 2) == 0.10

def test_cagr_edge_cases():
    assert calculate_cagr(0, 100, 3) == 0.0     # Start = 0
    assert calculate_cagr(100, 0, 3) == 0.0      # End = 0
    assert calculate_cagr(100, 200, 0) == 0.0     # Periods = 0
    assert calculate_cagr(-50, 100, 3) == 0.0     # Negative start
    assert calculate_cagr(100, -50, 3) == 0.0     # Negative end

# =============================================================================
# COMPOSITE GROWTH
# =============================================================================
def test_composite_growth():
    assert round(calc_composite_growth(0.1, 0.1, 0.1, 0.1), 2) == 0.10

def test_composite_growth_weights_sum_to_one():
    """The weights inside calc_composite_growth must sum to 1.0"""
    assert 0.30 + 0.30 + 0.25 + 0.15 == 1.0

# =============================================================================
# DCF
# =============================================================================
def test_dcf():
    iv = calculate_dcf(100, 0.10, 10, 0.11, 0.04, 5)
    assert iv > 0

def test_dcf_zero_fcf():
    """Negative or zero FCF must return 0 intrinsic value."""
    assert calculate_dcf(0, 0.1, 10, 0.11, 0.04, 5) == 0.0
    assert calculate_dcf(-100, 0.1, 10, 0.11, 0.04, 5) == 0.0

def test_dcf_growth_capping():
    """Growth rate is capped at 18% to prevent terminal value explosion."""
    iv_capped = calculate_dcf(100, 0.50, 10, 0.11, 0.04, 5)   # 50% growth, capped to 18%
    iv_at_cap = calculate_dcf(100, 0.18, 10, 0.11, 0.04, 5)    # exactly 18%
    assert iv_capped == iv_at_cap

def test_dcf_growth_floor():
    """Growth rate is floored at 2% to prevent negative projections."""
    iv_floored = calculate_dcf(100, -0.10, 10, 0.11, 0.04, 5)  # -10% growth, floored to 2%
    iv_at_floor = calculate_dcf(100, 0.02, 10, 0.11, 0.04, 5)  # exactly 2%
    assert iv_floored == iv_at_floor

# =============================================================================
# PEG
# =============================================================================
def test_peg():
    assert calculate_peg(20, 10) == 2.0

def test_peg_edge_cases():
    assert calculate_peg(20, 0) == 999.0     # Zero growth
    assert calculate_peg(20, -5) == 999.0     # Negative growth
    assert calculate_peg(0, 10) == 0.0        # Zero PE
    assert calculate_peg(-5, 10) == 0.0       # Negative PE

# =============================================================================
# VALUE TRAP
# =============================================================================
def test_value_trap():
    score = get_value_trap_score(-10, 10, 15, 3.5, -0.05)
    assert score == 100  # All traps triggered, capped at 100
    assert get_value_trap_score(100, 50, 25, 0.5, 0.15) == 0  # Clean company

def test_value_trap_individual_factors():
    """Test each factor in isolation."""
    # FCF negative, NI positive -> +40
    assert get_value_trap_score(-1, 1, 25, 0.5, 0.15) == 40
    # High D/E (> 1.5) -> +20
    assert get_value_trap_score(100, 50, 25, 2.0, 0.15) == 20
    # Very high D/E (> 3.0) -> +20 + 20 = 40 (stacking)
    assert get_value_trap_score(100, 50, 25, 4.0, 0.15) == 40
    # Low ROE (< 10) -> +10
    assert get_value_trap_score(100, 50, 8, 0.5, 0.15) == 10
    # Very low ROE (< 5) -> +10 + 10 = 20 (stacking)
    assert get_value_trap_score(100, 50, 3, 0.5, 0.15) == 20
    # Negative profit CAGR -> +20
    assert get_value_trap_score(100, 50, 25, 0.5, -0.05) == 20

# =============================================================================
# SCORING FUNCTIONS
# =============================================================================
def test_scores():
    assert score_quality(25, 100, 50) == 100
    assert score_balance_sheet(0.5, 1.5) == 100
    assert score_growth(0.25) == 100

def test_quality_fcf_conversion_boundary():
    """BUG: fcf_conv=0.5 gets 0 points, fcf_conv=0.51 gets 25 points."""
    q_at_boundary = score_quality(5, 50, 100)   # fcf_conv = 0.50 exactly
    q_above = score_quality(5, 51, 100)          # fcf_conv = 0.51
    assert q_at_boundary == 0   # No credit at boundary (strict >)
    assert q_above == 25        # Credit just above

def test_quality_negative_net_income_fcf_impact():
    """FIXED: When net_income < 0, positive FCF adds 25, negative subtracts 30."""
    q_good_fcf = score_quality(25, 1000000, -50)
    q_bad_fcf = score_quality(25, -1000000, -50)
    assert q_good_fcf == 75  # 50 + 25
    assert q_bad_fcf == 20   # max(0, 50 - 30)

def test_mos_clipping():
    mos_extreme_low = calculate_margin_of_safety(100, 2000)
    assert mos_extreme_low == -99.9
    mos_normal = calculate_margin_of_safety(100, 50)
    assert mos_normal == 50.0

def test_valuation_uses_peg():
    """FIXED: score_valuation correctly applies PEG bonuses and penalties."""
    assert score_valuation(60, 0.5) == 100  # min(100 + 20, 100) = 100
    assert score_valuation(60, 999.0) == 80 # 100 - 20 = 80

def test_bank_valuation_bounds():
    """FIXED: Bank valuation prevents explosion when Ke is close to g."""
    book_value = 200
    roe = 0.15
    # Extreme case where spread is only 0.001
    ke = 0.12
    g = 0.119
    iv = calculate_bank_intrinsic_value(book_value, roe, ke, g)
    
    # If unpatched, spread=0.001 -> PB = 31x -> IV = 6200
    # Patched: spread forced to 0.03, g capped to 0.09. PB = (0.15 - 0.09) / 0.03 = 2.0x -> IV = 400
    assert iv == 400.0

# =============================================================================
# STRATEGIC RISK
# =============================================================================
def test_factorized_strategic_risk():
    mcx = score_strategic_risk_v9('MCX', 'FINANCE')
    assert mcx == 40

    coal = score_strategic_risk_v9('COALINDIA', 'MINING')
    assert coal == 40

    asian = score_strategic_risk_v9('ASIANPAINT', 'CONSUMER')
    assert asian == 58

def test_tech_disruption_penalty():
    """All IT/Tech stocks must get dis_risk >= 7."""
    techm = score_strategic_risk_v9('TECHM', 'TECHNOLOGY')
    assert techm <= 60  # Must be penalized

def test_esg_penalty():
    """Coal/Oil must get ESG penalty."""
    ongc = score_strategic_risk_v9('ONGC', 'ENERGY')
    assert ongc <= 60

# =============================================================================
# COMPETITIVE MOAT
# =============================================================================
def test_competitive_moat():
    assert score_competitive_moat('MCX') == 100
    assert score_competitive_moat('ASIANPAINT') == 90
    assert score_competitive_moat('LUPIN') == 70
    assert score_competitive_moat('UNKNOWN') == 50

def test_moat_default_is_50():
    """Most stocks in the universe get default moat = 50."""
    for t in ['HDFCBANK', 'SBIN', 'AXISBANK', 'BAJFINANCE', 'CIPLA', 'WIPRO']:
        assert score_competitive_moat(t) == 50

# =============================================================================
# CAPITAL ALLOCATION
# =============================================================================
def test_capital_allocation():
    assert score_capital_allocation(0.06, 0.40) == 100
    assert score_capital_allocation(0.0, 0.90) == 30
    assert score_capital_allocation(0.03, -0.1) == 40

# =============================================================================
# MOMENTUM
# =============================================================================
def test_momentum():
    status, mult = get_momentum_status(100, 110, 120)
    assert mult == 0.0
    assert "Death Cross" in status

    status2, mult2 = get_momentum_status(150, 140, 120)
    assert mult2 == 1.0
    assert "Golden Cross" in status2

def test_momentum_zero_sma():
    """SMA=0 should return Unknown with multiplier 1.0 (safe default)."""
    status, mult = get_momentum_status(100, 0, 200)
    assert status == "Unknown"
    assert mult == 1.0

def test_momentum_bearish():
    """Price < SMA50 but SMA50 > SMA200 -> Bearish Short-Term (0.8x)."""
    status, mult = get_momentum_status(95, 100, 90)
    assert mult == 0.8
    assert "Bearish" in status

# =============================================================================
# V16 FINAL SCORE
# =============================================================================
def test_final_v16():
    weights = {
        'quality_weight': 0.20, 'growth_weight': 0.20, 'valuation_weight': 0.15,
        'risk_weight': 0.15, 'moat_weight': 0.10, 'bs_weight': 0.10,
        'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05
    }
    perfect = calc_final_v16_score(100, 100, 100, 100, 100, 100, 100, 100, 0, 1.0, weights)
    assert perfect == 100.0

    # 50% penalty (trap_score 50-74)
    penalized = calc_final_v16_score(100, 100, 100, 100, 100, 100, 100, 100, 60, 1.0, weights)
    assert penalized == 50.0

    # Death Cross -> 0.0x multiplier
    dead = calc_final_v16_score(100, 100, 100, 100, 100, 100, 100, 100, 0, 0.0, weights)
    assert dead == 0.0

    # 80% penalty (trap_score 25-49)
    mild_trap = calc_final_v16_score(100, 100, 100, 100, 100, 100, 100, 100, 30, 1.0, weights)
    assert mild_trap == 80.0

    # 20% penalty (trap_score >= 75)
    severe_trap = calc_final_v16_score(100, 100, 100, 100, 100, 100, 100, 100, 80, 1.0, weights)
    assert severe_trap == 20.0

def test_final_v16_weight_sensitivity():
    """Changing weights must change the final score."""
    w1 = {'quality_weight': 0.50, 'growth_weight': 0.10, 'valuation_weight': 0.10,
           'risk_weight': 0.10, 'moat_weight': 0.05, 'bs_weight': 0.05,
           'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05}
    w2 = {'quality_weight': 0.10, 'growth_weight': 0.50, 'valuation_weight': 0.10,
           'risk_weight': 0.10, 'moat_weight': 0.05, 'bs_weight': 0.05,
           'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05}
    # Stock with high quality (100) but low growth (0)
    s1 = calc_final_v16_score(100, 50, 50, 50, 50, 0, 50, 50, 0, 1.0, w1)
    s2 = calc_final_v16_score(100, 50, 50, 50, 50, 0, 50, 50, 0, 1.0, w2)
    assert s1 > s2  # Quality-heavy weights should favor this stock

def test_final_v16_clamping():
    """Score must be clamped between 0 and 100."""
    weights = {'quality_weight': 0.20, 'growth_weight': 0.20, 'valuation_weight': 0.15,
               'risk_weight': 0.15, 'moat_weight': 0.10, 'bs_weight': 0.10,
               'cap_alloc_weight': 0.05, 'smart_money_weight': 0.05}
    # All zeros
    zero = calc_final_v16_score(0, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, weights)
    assert zero == 0.0

# =============================================================================
# SMART MONEY
# =============================================================================
def test_smart_money():
    # Base 50 + 20 (>50%) + 40 (>2% buying) = 110 -> max 100
    assert score_smart_money(0.60, 0.03) == 100
    # Base 50 + 10 (>30%) + 20 (>0.5% buying) = 80
    assert score_smart_money(0.40, 0.01) == 80
    # Base 50 - 20 (<15%) - 40 (>2% selling) = -10 -> min 0
    assert score_smart_money(0.10, -0.03) == 0

# =============================================================================
# BALANCE SHEET
# =============================================================================
def test_balance_sheet_high_debt():
    """D/E > 2.0 should trigger both penalties (-30 and -40)."""
    bs = score_balance_sheet(2.5, 1.5)
    assert bs == 30  # 100 - 30 - 40
    
def test_balance_sheet_low_current_ratio():
    """Current ratio < 1.0 should penalize."""
    bs = score_balance_sheet(0.5, 0.5)
    assert bs == 70  # 100 - 30

# =============================================================================
# GROWTH SCORING
# =============================================================================
def test_growth_scoring_boundaries():
    """FIXED: Uses >= so score_growth(0.20) = 100."""
    assert score_growth(0.20) == 100   
    assert score_growth(0.201) == 100 

def test_growth_scoring_exact_boundaries():
    """FIXED: Exact boundary values fall to the correct bracket."""
    assert score_growth(0.15) == 80    
    assert score_growth(0.151) == 80   
