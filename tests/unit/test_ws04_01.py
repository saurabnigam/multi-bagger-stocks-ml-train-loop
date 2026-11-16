"""Acceptance tests for WS04.01: Field bounds, masking in copies, and PSI drift."""
import numpy as np
import pandas as pd
import pytest

from quant.config import Config, load
from quant.data.contracts import check, check_drift, field_contracts, psi
from quant.types import Check


@pytest.fixture
def cfg():
    return load()


def test_field_contracts_loaded(cfg):
    """field_contracts loads dictionary of field definitions from config."""
    contracts = field_contracts(cfg)
    assert isinstance(contracts, dict)
    assert "dividend_yield_frac" in contracts
    assert "debt_to_equity_x" in contracts
    assert "market_cap_inr" in contracts
    assert "close_inr" in contracts

    fc = contracts["dividend_yield_frac"]
    assert fc["field"] == "dividend_yield_frac"
    assert fc["unit"] == "frac"
    assert fc["min_value"] == 0.0
    assert fc["max_value"] == 0.25
    assert fc["contract_version"] >= 1


def test_invalid_yield_masked_only_in_copy(cfg):
    """Invalid yield (e.g. 3.49 == 349%) is masked only in copy; original source is untouched."""
    contracts = field_contracts(cfg)
    contract = contracts["dividend_yield_frac"]

    raw_series = pd.Series([0.02, 3.49, 0.05], index=[1, 2, 3], name="dividend_yield_frac")
    raw_copy = raw_series.copy()

    masked, chk = check(raw_series, contract)

    # 1. Original series is strictly unchanged
    pd.testing.assert_series_equal(raw_series, raw_copy)
    assert raw_series.loc[2] == 3.49

    # 2. Masked copy has the violator set to NaN
    assert np.isnan(masked.loc[2])
    assert masked.loc[1] == 0.02
    assert masked.loc[3] == 0.05

    # 3. Check object reflects violation
    assert isinstance(chk, Check)
    assert chk.id == "contract_dividend_yield_frac"


def test_check_excess_violators_blocks(cfg):
    """When > 5 violators exceed bounds, check returns FAIL with blocking=True."""
    contracts = field_contracts(cfg)
    contract = contracts["dividend_yield_frac"]

    # 6 violators out of 50
    vals = [3.49] * 6 + [0.03] * 44
    raw_series = pd.Series(vals, name="dividend_yield_frac")

    masked, chk = check(raw_series, contract)
    assert chk.status == "FAIL"
    assert chk.blocking is True
    assert masked.isna().sum() == 6


def test_psi_identical_distribution_is_zero():
    """Identical distributions produce PSI == 0.0."""
    ref = pd.Series(np.linspace(0.01, 0.20, 100))
    cur = pd.Series(np.linspace(0.01, 0.20, 100))
    edges = [0.05, 0.10, 0.15]

    score = psi(cur, ref, edges)
    assert score is not None
    assert pytest.approx(score, abs=1e-5) == 0.0


def test_psi_handles_zero_bins_with_documented_epsilon():
    """Fixed-bin PSI handles zero/empty bins using documented epsilon 1e-6."""
    ref = pd.Series([0.01, 0.02, 0.03, 0.12, 0.14, 0.18])
    # cur has no values in the middle bin (0.05 to 0.10)
    cur = pd.Series([0.01, 0.02, 0.03, 0.18, 0.19, 0.20])
    edges = [0.05, 0.10, 0.15]

    score = psi(cur, ref, edges)
    assert score is not None
    assert np.isfinite(score)
    assert score > 0.0


def test_psi_reports_none_on_missing_or_empty():
    """When current or reference is missing/empty, psi reports None."""
    cur = pd.Series([0.01, 0.02])
    edges = [0.05, 0.10]

    assert psi(cur, pd.Series([], dtype=float), edges) is None
    assert psi(pd.Series([], dtype=float), cur, edges) is None
    assert psi(pd.Series([np.nan, np.nan]), cur, edges) is None


def test_first_baseline_drift_is_deferred():
    """First baseline drift check is DEFERRED, not fabricated history."""
    cur = pd.Series([0.01, 0.02, 0.03])
    edges = [0.05, 0.10]

    chk = check_drift(field="dividend_yield_frac", current=cur, reference=None, edges=edges)
    assert isinstance(chk, Check)
    assert chk.status == "DEFERRED"
    assert chk.blocking is False
    assert "baseline" in chk.reason.lower()
