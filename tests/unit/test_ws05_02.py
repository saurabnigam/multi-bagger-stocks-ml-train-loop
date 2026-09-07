"""Acceptance tests for WS05.02: Centered bounded ranks standardisation."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.factors.standardise import transform


@pytest.fixture
def cfg():
    return load()


@pytest.fixture
def golden_cases():
    path = Path("docs/spec/contracts/golden_cases.json")
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("cases", data)


def test_rank_ties_golden_case(cfg, golden_cases):
    """Match every golden z within 1e-12 on rank_ties test case."""
    case = golden_cases["rank_ties"]
    raw = pd.Series(case["raw"], index=[1, 2, 3, 4, 5])
    groups = pd.Series(case["groups"], index=[1, 2, 3, 4, 5])
    direction = case["direction"]

    res = transform(raw, groups, direction, cfg)
    assert isinstance(res, pd.DataFrame)
    assert set(res.columns) == {"raw", "winsor", "z", "flags"}

    expected_z = case["expected_z"]
    for i, exp in enumerate(expected_z, start=1):
        assert abs(res.loc[i, "z"] - exp) < 1e-12

    # Verify tied values stay tied
    assert res.loc[1, "z"] == res.loc[2, "z"] == res.loc[3, "z"] == res.loc[4, "z"]
    # Verify mean is 0 within numerical tolerance
    assert abs(res["z"].mean() - 0.0) < 1e-12
    # Verify abs(z) <= 3
    assert (res["z"].abs() <= 3.0).all()


def test_constant_rank_golden_case(cfg, golden_cases):
    """Constant group produces all NaN z-scores and constant_group flag."""
    case = golden_cases["constant_rank"]
    raw = pd.Series(case["raw"], index=list(range(len(case["raw"]))))
    groups = pd.Series(["Technology"] * len(raw), index=raw.index)

    res = transform(raw, groups, 1, cfg)
    assert res["z"].isna().all()
    assert (res["flags"] == "constant_group").all()


def test_negative_direction_applied_once(cfg, golden_cases):
    """Negative direction inverts the rank ordering."""
    case = golden_cases["negative_direction"]
    raw = pd.Series(case["raw"], index=[1, 2, 3, 4, 5])
    groups = pd.Series(["Tech"] * 5, index=[1, 2, 3, 4, 5])
    direction = case["direction"]  # -1

    res = transform(raw, groups, direction, cfg)
    # With direction = -1: raw 1 has highest z, raw 5 has lowest z
    assert res.loc[1, "z"] > res.loc[2, "z"] > res.loc[3, "z"] > res.loc[4, "z"] > res.loc[5, "z"]
    assert abs(res["z"].mean()) < 1e-12
    assert (res["z"].abs() <= 3.0).all()


def test_small_group_below_five_is_nan(cfg):
    """Groups with fewer than 5 finite observations produce NaN with small_group flag."""
    raw = pd.Series([10.0, 20.0, 30.0], index=[1, 2, 3])
    groups = pd.Series(["TinySector"] * 3, index=[1, 2, 3])

    res = transform(raw, groups, 1, cfg)
    assert res["z"].isna().all()
    assert (res["flags"] == "small_group").all()
