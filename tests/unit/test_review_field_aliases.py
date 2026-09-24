"""Vendor alias ties resolve by contract priority, not by SQLite row order.

Real-data motivation (2026-09-24 independent verification): one Yahoo fetch stores Diluted
EPS and Basic EPS (and EBIT and Operating Income, Cash And Cash Equivalents and Cash
Financial) for the same period with the same fetched_at. The point-in-time readers ordered
only by fetched_at, so the value a factor saw depended on the sort order SQLite happened to
use: LUPIN's eps_growth_3y used Basic EPS (116.75 / 9.46) although the factor asks for
Diluted EPS (116.44 / 9.41). On the 2026-09-11 capture 3,843 EBIT periods, 2,206 cash
periods and 1,492 EPS periods carried two aliases with different values.

Rows are inserted in both orders for two alias pairs whose alphabetical order is opposite
(EBIT < Operating Income; Basic EPS < Diluted EPS), so no accidental tie-break passes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.db.core import apply_schema, connect
from quant.factors.inputs import FactorInputs

FETCHED = "2026-09-11T09:32:08.662300Z"
AVAILABLE = "2026-06-01T00:00:00.000000Z"
CUTOFF = "2026-09-11T18:29:59.999999Z"

PRIMARY = {"Diluted EPS": 116.44, "EBIT": 10.0}
SECONDARY = {"Basic EPS": 116.75, "Operating Income": 7.0}


def _conn(tmp_path, skip_quarter=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "state.db")
    apply_schema(conn, kind="state")
    for sid in (1, 2):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, 'X', '2020-01-01', '2026-09-11', 'listed')", (sid, f"INE00000000{sid}"))
    quarters = [q.strftime("%Y-%m-%d") for q in pd.date_range("2024-09-30", "2026-06-30", freq="QE")][::-1]
    quarters = [q for q in quarters if q != skip_quarter]
    for sid, order in ((1, (PRIMARY, SECONDARY)), (2, (SECONDARY, PRIMARY))):
        for fields in order:
            for field, value in fields.items():
                for freq, periods in (("A", ["2026-03-31", "2023-03-31"]), ("Q", quarters)):
                    for period_end in periods:
                        conn.execute(
                            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
                            "available_from, available_from_basis, fetched_at, source, run_id) "
                            "VALUES (?, 'income', ?, ?, ?, ?, 'inr', ?, 'lodr_45d', ?, 'fixture', 1)",
                            (sid, freq, period_end, field, value, AVAILABLE, FETCHED))
    return conn


def test_pit_frame_prefers_contract_alias_on_same_fetch(tmp_path):
    from quant.data.fundamentals import pit_frame

    conn = _conn(tmp_path)
    eps = pit_frame(conn, CUTOFF, "income", "Diluted EPS", "A", 2, [1, 2])
    ebit = pit_frame(conn, CUTOFF, "income", "EBIT", "A", 2, [1, 2])
    assert (eps.to_numpy() == 116.44).all(), eps
    assert (ebit.to_numpy() == 10.0).all(), ebit


def test_ttm_prefers_contract_alias_quarterly_and_annual_fallback(tmp_path):
    from quant.data.fundamentals import ttm

    vals, flags = ttm(_conn(tmp_path / "full"), CUTOFF, "EBIT", [1, 2])
    assert vals.tolist() == [40.0, 40.0] and flags.tolist() == ["", ""]
    # a missing quarter breaks consecutiveness -> the latest annual value is used instead
    vals, flags = ttm(_conn(tmp_path / "gap", skip_quarter="2026-03-31"), CUTOFF, "EBIT", [1, 2])
    assert vals.tolist() == [10.0, 10.0] and flags.tolist() == ["ttm_from_annual"] * 2


def test_factor_inputs_fallback_query_prefers_contract_alias(tmp_path):
    conn = _conn(tmp_path)

    def query(sql, params):
        return conn.execute(sql, params).fetchall()

    members = pd.Index([1, 2], name="security_id")
    inputs = FactorInputs("2026-09-11", CUTOFF, members, pd.Series(["A", "A"], index=members),
                          price_store=None, state_query_fn=query, provenance_dict={})
    eps = inputs.fundamental("income", "Diluted EPS", "A", 2)
    assert np.allclose(eps.to_numpy(dtype=float), 116.44), eps


def test_newer_fetch_of_secondary_alias_still_beats_older_primary(tmp_path):
    """Priority breaks ties inside one fetch only; a later restatement still wins."""
    from quant.data.fundamentals import pit_frame

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
        "available_from, available_from_basis, fetched_at, source, run_id) "
        "VALUES (1, 'income', 'A', '2026-03-31', 'Basic EPS', 120.0, 'inr', ?, 'lodr_45d', "
        "'2026-09-11T12:00:00.000000Z', 'fixture', 1)", (AVAILABLE,))
    eps = pit_frame(conn, CUTOFF, "income", "Diluted EPS", "A", 1, [1])
    assert eps.loc[1, 0] == pytest.approx(120.0)


def test_ttm_does_not_mix_aliases_across_quarters(tmp_path):
    """Three quarters of EBIT plus one quarter carrying only Operating Income -> latest annual."""
    from quant.data.fundamentals import ttm

    conn = connect(tmp_path / "state.db")
    apply_schema(conn, kind="state")
    conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                 "VALUES (1, 'INE000000001', 'X', '2020-01-01', '2026-09-11', 'listed')")
    rows = [("Q", q, "EBIT", 10.0) for q in ("2026-06-30", "2026-03-31", "2025-12-31")]
    rows += [("Q", "2025-09-30", "Operating Income", 4.0), ("A", "2026-03-31", "EBIT", 33.0)]
    for freq, period_end, field, value in rows:
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) "
            "VALUES (1, 'income', ?, ?, ?, ?, 'inr', ?, 'lodr_45d', ?, 'fixture', 1)",
            (freq, period_end, field, value, AVAILABLE, FETCHED))
    vals, flags = ttm(conn, CUTOFF, "EBIT", [1])
    assert vals.tolist() == [33.0] and flags.tolist() == ["ttm_from_annual"]
