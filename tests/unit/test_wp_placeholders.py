"""Regression tests for work package `placeholders` (task T9 / decision D10).

NSE's Nifty 500 constituent file lists placeholder rows during demergers, e.g.
"Dummy HEG Ltd." under symbol DUMMYHEG with a syntactically well-formed but
check-digit-invalid ISIN (DUM545A01024). Before this fix the engine excluded such
rows for "coverage" (no prices) -- indistinguishable from a genuinely thin name.
Separately, `quant.factors.standardise` flagged nonfinancial-only factors on
Financial Services names as "small_group", which is wrong: those values are
structurally not applicable, not merely undersampled.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from quant.data.universe import capture, is_index_placeholder, validate_isin
from quant.factors.standardise import transform
from quant.model.screens import apply as apply_screens
from quant.run import RunContext
from quant.types import Draft


# --------------------------------------------------------------------------- ISIN validation


def test_valid_ine_isins_pass():
    """Real, correctly check-digited ISINs validate (Reliance, Infosys)."""
    assert validate_isin("INE002A01018") is True
    assert validate_isin("INE009A01021") is True


def test_dummy_placeholder_isin_fails_check_digit():
    """NSE's real placeholder ISIN for the DUMMYHEG row fails the Luhn-style check
    digit despite being a syntactically well-formed 12-character code."""
    assert validate_isin("DUM545A01024") is False


def test_isin_validation_rejects_malformed_shapes():
    assert validate_isin("INE002A0101") is False  # 11 chars, too short
    assert validate_isin("1NE002A01018") is False  # country code not alphabetic
    assert validate_isin(None) is False
    assert validate_isin(float("nan")) is False


def test_is_index_placeholder_signal():
    # DUMMY-prefixed symbol is a placeholder regardless of ISIN shape.
    assert is_index_placeholder("DUMMYHEG", "DUM545A01024") is True
    # A present, malformed/invalid ISIN alone is a placeholder signal even without
    # a DUMMY-prefixed symbol.
    assert is_index_placeholder("SOMESYM", "DUM545A01024") is True
    # A missing ISIN is not itself placeholder evidence.
    assert is_index_placeholder("HEG", None) is False
    assert is_index_placeholder("HEG", float("nan")) is False
    # A normal symbol with a genuinely valid ISIN is not a placeholder.
    assert is_index_placeholder("RELIANCE", "INE002A01018") is False


# --------------------------------------------------------------------------- screens.py


def _isin_with_check_digit(base11: str) -> str:
    """Test helper: append the correct ISIN check digit to an 11-char body."""
    numeral_chars = []
    for ch in base11.upper():
        if ch.isdigit():
            numeral_chars.append(ch)
        else:
            numeral_chars.append(str(ord(ch) - ord("A") + 10))
    numeral = "".join(numeral_chars)
    for d in range(10):
        candidate = numeral + str(d)
        total = 0
        for i, ch in enumerate(candidate[::-1]):
            v = int(ch)
            if i % 2 == 1:
                v *= 2
                if v > 9:
                    v -= 9
            total += v
        if total % 10 == 0:
            return base11 + str(d)
    raise AssertionError("no check digit found")


def _base_scores(sids, finals):
    return pd.DataFrame(
        {
            "security_id": sids,
            "composite": finals,
            "composite_neutral": finals,
            "sector_tilt": [0.0] * len(sids),
            "final": finals,
            "scored": [0] * len(sids),  # no factor coverage -- mirrors the DUMMYHEG case
            "n_factors_used": [0] * len(sids),
            "exclusion_reason": [None] * len(sids),
        },
        index=sids,
    )


def test_placeholder_excluded_with_index_placeholder(cfg):
    """A DUMMY-prefixed member with an invalid ISIN and no factor coverage gets
    exclusion_reason index_placeholder -- not 'coverage' -- taking precedence over
    every other reason (MASTER_SPEC 6.1)."""
    sids = [1, 2]
    groups = pd.Series(["Sector A"] * 2, index=sids)
    members = pd.DataFrame(
        {
            "security_id": sids,
            "symbol": ["DUMMYHEG", "HEG"],
            "isin": ["DUM545A01024", _isin_with_check_digit("INE545A0102")],
            "series": ["EQ", "EQ"],
            "adv_63_inr": [np.nan, 50_000_000.0],
            "pos_sessions_63": [np.nan, 60],
        },
        index=sids,
    )
    scores_initial = _base_scores(sids, [np.nan, 0.5])

    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=scores_initial,
    )
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)

    out = apply_screens(ctx, draft, scores_initial, "EW_HIER_v1")

    assert out.loc[1, "eligible"] == 0
    assert out.loc[1, "exclusion_reason"] == "index_placeholder"

    # HEG itself is a normal, valid-ISIN member and is unaffected by the sibling
    # placeholder row -- it is still excluded for its own real reason (no coverage).
    assert out.loc[2, "exclusion_reason"] == "coverage"


def test_non_placeholder_member_keeps_its_own_reason(cfg):
    """A member with a valid symbol/ISIN never gets index_placeholder, even when it
    fails other screens (illiquid here)."""
    sids = [1]
    groups = pd.Series(["Sector A"], index=sids)
    members = pd.DataFrame(
        {
            "security_id": sids,
            "symbol": ["ABCLTD"],
            "isin": [_isin_with_check_digit("INE001A0100")],
            "series": ["EQ"],
            "adv_63_inr": [5_000_000.0],  # below min_adv
            "pos_sessions_63": [63],
        },
        index=sids,
    )
    scores_initial = pd.DataFrame(
        {
            "security_id": sids,
            "composite": [0.5],
            "composite_neutral": [0.5],
            "sector_tilt": [0.0],
            "final": [0.5],
            "scored": [1],
            "n_factors_used": [5],
            "exclusion_reason": [None],
        },
        index=sids,
    )
    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=scores_initial,
    )
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)

    out = apply_screens(ctx, draft, scores_initial, "EW_HIER_v1")
    assert out.loc[1, "exclusion_reason"] == "illiquid"


# --------------------------------------------------------------------------- universe.py capture


def make_universe_csv_with_placeholder(n: int = 500) -> bytes:
    """A synthetic Nifty 500 CSV containing a real member (HEG) and NSE's
    placeholder row for its demerger spin-off (DUMMYHEG / DUM545A01024), padded to
    the minimum live-capture row count with otherwise-valid, uniquely check-digited
    filler ISINs."""
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    rows.append("HEG Ltd.,Diversified,HEG," + "BE," + _isin_with_check_digit("INE545A0102"))
    rows.append("Dummy HEG Ltd.,Diversified,DUMMYHEG,EQ,DUM545A01024")

    for i in range(3, n + 1):
        isin = _isin_with_check_digit(f"INE{i:08d}")
        rows.append(f"Company {i},Financial Services,SYM{i},EQ,{isin}")

    return "\n".join(rows).encode("utf-8")


def test_capture_records_index_placeholder_event_naming_parent(ctx, monkeypatch):
    """capture() records one WARN data_quality_events row, code INDEX_PLACEHOLDER,
    for the DUMMYHEG row, naming HEG as the parent it most likely refers to."""
    csv_bytes = make_universe_csv_with_placeholder(500)
    capture_time = "2026-09-30T12:00:00.000000Z"

    def mock_fetch(name, cfg, clock):
        return csv_bytes, {
            "url": "https://niftyindices.com/test.csv",
            "captured_at": capture_time,
            "source_version": "nifty_v1",
            "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        }

    monkeypatch.setattr("quant.data.universe.fetch_list", mock_fetch)

    res = capture(ctx)
    assert res.status == "ok"

    rows = ctx.conn.execute(
        "SELECT security_id, detail_json FROM data_quality_events WHERE code = 'INDEX_PLACEHOLDER'"
    ).fetchall()
    assert len(rows) == 1

    import json as json_mod

    detail = json_mod.loads(rows[0]["detail_json"])
    assert detail["placeholder_symbol"] == "DUMMYHEG"
    assert detail["placeholder_isin"] == "DUM545A01024"
    assert detail["parent_symbol"] == "HEG"

    parent_security_id = ctx.conn.execute(
        "SELECT security_id FROM securities WHERE isin = ?", (_isin_with_check_digit("INE545A0102"),)
    ).fetchone()["security_id"]
    assert rows[0]["security_id"] == parent_security_id


# --------------------------------------------------------------------------- standardise.py


def test_financial_group_not_applicable_vs_genuine_small_group(cfg):
    """A nonfinancial-only factor's structural exclusion on Financial Services names
    gets flagged not_applicable, never small_group -- while a separate, genuinely
    undersized sector group (< 5 finite observations, all applicable) still gets
    small_group unchanged."""
    # Group "Financial Services": 6 members, factor does not apply to any of them
    # (raw is NaN for all 6, mirroring quant.factors.value.FcfYield.compute()).
    fin_sids = [1, 2, 3, 4, 5, 6]
    # Group "TinySector": 3 members, factor DOES apply, but too few observations.
    tiny_sids = [7, 8, 9]

    sids = fin_sids + tiny_sids
    raw = pd.Series(
        [np.nan] * 6 + [10.0, 20.0, 30.0],
        index=sids,
    )
    groups = pd.Series(
        ["Financial Services"] * 6 + ["TinySector"] * 3,
        index=sids,
    )
    applicable = pd.Series(
        [False] * 6 + [True] * 3,
        index=sids,
    )

    res = transform(raw, groups, direction=1, cfg=cfg, applicable=applicable)

    for sid in fin_sids:
        assert res.loc[sid, "flags"] == "not_applicable"
        assert pd.isna(res.loc[sid, "z"])

    for sid in tiny_sids:
        assert res.loc[sid, "flags"] == "small_group"
        assert pd.isna(res.loc[sid, "z"])


def test_transform_default_applicable_matches_prior_behaviour(cfg):
    """Omitting `applicable` (the default) reproduces the exact prior small_group
    behaviour -- this is a pure additive change."""
    raw = pd.Series([10.0, 20.0, 30.0], index=[1, 2, 3])
    groups = pd.Series(["TinySector"] * 3, index=[1, 2, 3])

    res = transform(raw, groups, 1, cfg)
    assert res["z"].isna().all()
    assert (res["flags"] == "small_group").all()
