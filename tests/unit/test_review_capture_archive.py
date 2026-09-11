"""Timezone-safe availability dates and archive re-ingest (no second vendor download)."""
from __future__ import annotations

import json

import pandas as pd

from quant.data.calendar import Calendar
from quant.data.capture import ingest_archive, load_archive
from quant.data.fundamentals import available_from
from quant.data.yahoo import RawBundle
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock


def _calendar() -> Calendar:
    dates = pd.bdate_range("2025-01-01", "2027-12-31")
    return Calendar(pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "close_at": [f"{d.strftime('%Y-%m-%d')}T18:29:59.999999Z" for d in dates],
    }))


def test_available_from_handles_tz_aware_index_and_ignores_scheduled_dates():
    cal = _calendar()
    ed = pd.DataFrame(
        {"EPS Estimate": [10.0, 9.0], "Reported EPS": [None, 9.4], "Surprise(%)": [None, 4.4]},
        index=pd.DatetimeIndex(["2026-07-25 10:00", "2026-04-28 10:00"]).tz_localize("Asia/Kolkata"),
    )
    # period end 2026-03-31: the reported event on 2026-04-28 sets availability (+1 session)
    avail, basis = available_from("2026-03-31", "Q", "2026-04-01T00:00:00.000000Z", ed, cal)
    assert basis == "earnings_date" and avail.startswith("2026-04-29")
    # period end 2026-06-30: only a SCHEDULED (unreported) event exists -> fall back to +45d rule
    avail2, basis2 = available_from("2026-06-30", "Q", "2026-07-01T00:00:00.000000Z", ed, cal)
    assert basis2 == "lodr_45d" and avail2 >= "2026-08-14"


def _archive(tmp_path, ticker="ABC.NS"):
    idx = pd.DatetimeIndex(["2026-07-25 10:00", "2026-04-28 10:00"]).tz_localize("Asia/Kolkata")
    ed = pd.DataFrame({"EPS Estimate": [10.0, 9.0], "Reported EPS": [None, 9.4]}, index=idx)
    stmt = pd.DataFrame(
        {pd.Timestamp("2026-03-31"): [100.0, 20.0], pd.Timestamp("2025-03-31"): [90.0, 18.0]},
        index=["Total Revenue", "Net Income"],
    )
    payload = [{
        "ticker": ticker,
        "fetched_at": "2026-09-11T09:00:00.000000Z",
        "info": {"marketCap": 5e10, "sharesOutstanding": 1e8, "heldPercentInstitutions": 0.25,
                 "heldPercentInsiders": 0.5, "dividendRate": 2.0, "trailingPE": 20.0, "sector": "Technology",
                 "industry": "Software"},
        "statements": {"income_stmt": stmt.to_json(orient="split"), "quarterly_income_stmt": "",
                       "balance_sheet": "", "quarterly_balance_sheet": "", "cashflow": "", "quarterly_cashflow": ""},
        "earnings_dates": ed.to_json(orient="split"),
        "errors": [],
    }]
    path = tmp_path / "cap_yah_test.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_load_archive_restores_dates_and_ingest_archive_populates_tables(tmp_path):
    path = _archive(tmp_path)
    bundles = load_archive(path)
    assert len(bundles) == 1 and isinstance(bundles[0], RawBundle)
    assert list(bundles[0].statements["income_stmt"].columns) == ["2026-03-31", "2025-03-31"]
    assert bundles[0].earnings_dates.index.tz is None and len(bundles[0].earnings_dates) == 2

    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE000000001', 'ABC', '2026-01-01', '2026-09-11', 'listed')")
    conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) VALUES (1, 'ABC', 'ABC.NS', '2026-01-01', 'nifty500_csv')")
    conn.close()
    from quant.config import load
    cfg = load().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d", archive_dir=tmp_path / "a",
                            knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    with RunContext(as_of="2026-09-11", kind="capture", track="live", cfg=cfg, clock=FrozenClock("2026-09-11T10:00:00.000000Z"),
                    actor=Actor(kind="system", name="t")) as ctx:
        res = ingest_archive(ctx, path, None, prices=False)
    assert res.counts["mapped"] == 1 and res.counts["fundamental_rows"] == 4
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM captures WHERE kind = 'yahoo_bundle'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM fundamentals").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM holdings").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM security_attributes").fetchone()[0] == 1
    # availability of the FY2026 row uses the reported April event, not the scheduled July one
    row = conn.execute("SELECT available_from, available_from_basis FROM fundamentals WHERE period_end = '2026-03-31' LIMIT 1").fetchone()
    assert row[1] in ("earnings_date", "first_fetch")
    assert conn.execute("SELECT count(*) FROM ledger_events WHERE table_name = 'fundamentals'").fetchone()[0] == 4
    conn.close()
