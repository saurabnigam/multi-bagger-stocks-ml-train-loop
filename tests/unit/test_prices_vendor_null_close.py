"""X7: vendor null closes (found in a dress rehearsal on 2026-10-01).

Yahoo returned the 2026-09-30 bar with Open, High, Low and Volume but a null Close for 175 of 526 tickers, and a refetch
16.5 hours later was still null. ``normalize_source`` drops a row with no close, so the store silently had no bar and
gate G3 later blocked on coverage. ``PriceStore._acquire`` now counts those rows (per date, last 3 sessions of the raw
frame), puts the counts in the Result details, and records a WARN event ``VENDOR_NULL_CLOSE`` when the latest session
exceeds ``1 - [gates] price_coverage`` of the requested tickers. It never fills or imputes a close.

Everything here is offline: a fake batch frame, a fake client, a tmp_path state database and price store.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from quant.commands import data as data_cmd
from quant.config import load as load_config
from quant.data.prices import PriceStore, vendor_null_close_counts, vendor_null_close_verdict
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock, Result

NOW = "2026-10-01T04:00:00.000000Z"
SESSIONS = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]     # Thu, Fri, Mon, Tue, Wed
LAST3 = SESSIONS[-3:]


@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


def batch(n: int, holes: dict[str, list[int]] | None = None, *, no_volume: dict[str, list[int]] | None = None,
          all_null: dict[str, list[int]] | None = None, no_open: dict[str, list[int]] | None = None) -> pd.DataFrame:
    """A yfinance-shaped batch frame: columns (field, ticker), one row per session, n tickers T0.NS .. T{n-1}.NS.

    ``holes[date]`` are ticker numbers whose Close is null while Open, High, Low and Volume are present (the vendor bug).
    ``no_volume`` and ``no_open`` also null that field (not the vendor bug: another field is missing too), and
    ``all_null`` nulls every field (a ticker that did not trade; ``frames_from_download`` drops the row).
    """
    tickers = [f"T{i}.NS" for i in range(n)]
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"], tickers])
    base = np.tile(np.linspace(100.0, 100.0 + n - 1, n), (len(SESSIONS), 1))
    data = {}
    for field in ("Open", "High", "Low", "Close", "Adj Close"):
        data[field] = pd.DataFrame(base.copy(), index=SESSIONS, columns=tickers)
    data["Volume"] = pd.DataFrame(np.full((len(SESSIONS), n), 1e5), index=SESSIONS, columns=tickers)
    data["Dividends"] = pd.DataFrame(np.zeros((len(SESSIONS), n)), index=SESSIONS, columns=tickers)
    data["Stock Splits"] = pd.DataFrame(np.zeros((len(SESSIONS), n)), index=SESSIONS, columns=tickers)
    for day, nums in (holes or {}).items():
        for k in nums:
            data["Close"].loc[day, f"T{k}.NS"] = np.nan
            data["Adj Close"].loc[day, f"T{k}.NS"] = np.nan
    for day, nums in (no_volume or {}).items():
        for k in nums:
            data["Close"].loc[day, f"T{k}.NS"] = np.nan
            data["Volume"].loc[day, f"T{k}.NS"] = np.nan
    for day, nums in (no_open or {}).items():
        for k in nums:
            data["Close"].loc[day, f"T{k}.NS"] = np.nan
            data["Open"].loc[day, f"T{k}.NS"] = np.nan
    for day, nums in (all_null or {}).items():
        for k in nums:
            for field in data:
                data[field].loc[day, f"T{k}.NS"] = np.nan
    frame = pd.concat(data, axis=1)
    frame.index = pd.to_datetime(frame.index)
    frame.index.name = "Date"
    frame.attrs["fetched_at"] = NOW
    return frame


class FakeClient:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def download_batch(self, tickers, start, end):
        self.calls.append((list(tickers), start, end))
        return self.frame


@pytest.fixture(scope="module")
def _state_template(tmp_path_factory):
    """One state schema for the module (copied per test) with 100 securities, enough for every case here."""
    db = tmp_path_factory.mktemp("null_close_template") / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    for i in range(100):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2025-01-01', '2026-09-30', 'listed')", (i + 1, f"INE{i + 1:09d}", f"Co {i}"))
    conn.close()
    return db


@pytest.fixture
def world(tmp_path, _state_template):
    def make(n=None, *, price_coverage=None, sub=None):
        root = tmp_path / sub if sub else tmp_path
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_state_template, root / "state.db")
        cfg = load_config().with_paths(db=root / "state.db", prices_db=root / "p.db", data_dir=root / "d",
                                        archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")
        if price_coverage is not None:
            cfg.gates.price_coverage = price_coverage
        return cfg
    return make


def acquire(cfg, frame, n):
    """Run ``PriceStore._acquire`` on the fake batch and return (result, events, store)."""
    ticker_map = {i + 1: f"T{i}.NS" for i in range(n)}
    with RunContext(as_of="2026-09-30", kind="capture", track="live", cfg=cfg, clock=FrozenClock(NOW),
                    actor=Actor(kind="system", name="t")) as ctx:
        store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        res = store._acquire(ctx, FakeClient(frame), ticker_map, "2026-09-01", "2026-10-01")
    raw = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
    try:
        events = [(code, sev, json.loads(detail)) for code, sev, detail in raw.execute(
            "SELECT code, severity, detail_json FROM data_quality_events WHERE code = 'VENDOR_NULL_CLOSE' ORDER BY event_id")]
    finally:
        raw.close()
    return res, events, store


# ------------------------------------------------------------------ the counting function

def test_counts_are_per_date_for_the_last_three_sessions_only():
    frame = batch(10, holes={"2026-09-30": [0, 1, 2], "2026-09-29": [5], "2026-09-25": [6, 7]})
    counts = vendor_null_close_counts(frame, [f"T{i}.NS" for i in range(10)])
    assert counts == {"2026-09-28": [], "2026-09-29": ["T5.NS"], "2026-09-30": ["T0.NS", "T1.NS", "T2.NS"]}   # 09-25 is older


def test_a_row_missing_another_field_or_wholly_empty_is_not_a_vendor_null_close():
    frame = batch(10, holes={"2026-09-30": [0]}, no_volume={"2026-09-30": [1]}, no_open={"2026-09-30": [2]},
                  all_null={"2026-09-30": [3]})
    counts = vendor_null_close_counts(frame, [f"T{i}.NS" for i in range(10)])
    assert counts["2026-09-30"] == ["T0.NS"]


def test_a_frame_without_the_needed_fields_counts_nothing_and_does_not_raise():
    frame = batch(3)
    close_only = frame[[c for c in frame.columns if c[0] == "Close"]]
    assert vendor_null_close_counts(close_only, ["T0.NS", "T1.NS", "T2.NS"]) == {}
    assert vendor_null_close_counts(pd.DataFrame(), ["T0.NS"]) == {}


def test_a_single_ticker_flat_frame_is_read_too():
    frame = batch(1, holes={"2026-09-30": [0]})
    flat = frame.xs("T0.NS", axis=1, level=1)
    assert vendor_null_close_counts(flat, ["T0.NS"])["2026-09-30"] == ["T0.NS"]


# ------------------------------------------------------------------ _acquire: details, event, no imputation

def test_acquire_puts_the_counts_in_the_details_warns_with_a_capped_sorted_sample_and_never_imputes(world):
    """40 requested tickers: 15 null closes on the latest session, 1 on the one before (a 37.5% share against 2% allowed)."""
    cfg = world()
    res, events, store = acquire(cfg, batch(40, holes={"2026-09-30": list(range(15, 30)), "2026-09-29": [1]}), 40)
    d = res.details["vendor_null_close"]
    assert d["requested"] == 40 and d["latest_session"] == "2026-09-30"
    assert d["counts"] == {"2026-09-28": 0, "2026-09-29": 1, "2026-09-30": 15}
    assert (d["latest_count"], d["latest_share"], d["above_threshold"]) == (15, 0.375, True)
    assert d["threshold_share"] == pytest.approx(0.02)
    assert res.status == "ok" and res.details["capture_id"]                  # the existing details are untouched
    (code, severity, detail), = events
    assert (code, severity) == ("VENDOR_NULL_CLOSE", "WARN")
    assert detail["date"] == "2026-09-30" and detail["count"] == 15 and detail["share"] == pytest.approx(15 / 40)
    assert detail["sample_tickers"] == sorted(f"T{i}.NS" for i in range(15, 30))[:10]
    with store.conn() as pc:                                                 # a close is never filled or imputed
        have = {(sid, d) for sid, d in pc.execute("SELECT security_id, date FROM prices_daily")}
        nulls = pc.execute("SELECT count(*) FROM prices_daily WHERE close_raw IS NULL").fetchone()[0]
    assert (16, "2026-09-30") not in have and (30, "2026-09-30") not in have and (16, "2026-09-29") in have
    assert (31, "2026-09-30") in have and (2, "2026-09-29") not in have and nulls == 0   # ids are ticker number + 1


@pytest.mark.parametrize("count,requested,allowed,above", [
    (0, 100, 0.02, False), (1, 100, 0.02, False), (2, 100, 1 - 0.98, False),     # exactly 2% does not exceed 2%
    (3, 100, 1 - 0.98, True), (3, 10, 0.5, False), (2, 10, 0.1, True), (0, 0, 0.02, False)])
def test_the_event_threshold_is_the_share_against_one_minus_price_coverage(count, requested, allowed, above):
    share, got = vendor_null_close_verdict(count, requested, allowed)
    assert got is above and share == (count / requested if requested else 0.0)


def test_the_threshold_comes_from_the_gates_config_and_only_the_latest_session_decides(world):
    cfg = world(price_coverage=0.9)                            # 20% of 10 null on 09-30 against 10% allowed; 09-29 is also bad
    res, events, _ = acquire(cfg, batch(10, holes={"2026-09-30": [0, 1], "2026-09-29": [2, 3, 4, 5, 6]}), 10)
    d = res.details["vendor_null_close"]
    assert d["threshold_share"] == pytest.approx(0.1) and len(events) == 1 and events[0][2]["count"] == 2
    cfg = world(sub="earlier")                                 # only the earlier session is bad: no event, but it is counted
    res, events, _ = acquire(cfg, batch(10, holes={"2026-09-29": [0, 1, 2, 3, 4]}), 10)
    assert events == [] and res.details["vendor_null_close"]["counts"] == {"2026-09-28": 0, "2026-09-29": 5, "2026-09-30": 0}
    assert res.details["vendor_null_close"]["above_threshold"] is False


def test_acquire_still_returns_empty_for_an_empty_download(world):
    cfg = world()
    res, events, _ = acquire(cfg, pd.DataFrame(), 2)
    assert res.status == "empty" and "vendor_null_close" not in res.details and events == []


# ------------------------------------------------------------------ data capture prints one line

def test_the_capture_line_names_the_count_the_g3_threshold_and_that_a_rerun_may_not_fill_them():
    details = {"price_capture": {"vendor_null_close": {
        "requested": 526, "latest_session": "2026-09-30", "latest_count": 175, "latest_share": 175 / 526,
        "threshold_share": 0.02, "above_threshold": True, "counts": {"2026-09-30": 175}}}}
    line = data_cmd.vendor_null_close_line(details)
    assert "\n" not in line
    assert "175" in line and "526" in line and "2026-09-30" in line and "G3 needs 98%" in line
    assert "may or may not" in line and "re-running capture" in line


def test_the_capture_line_is_absent_when_the_share_is_within_the_allowance_or_unknown():
    below = {"price_capture": {"vendor_null_close": {"requested": 100, "latest_session": "2026-09-30", "latest_count": 2,
                                                     "latest_share": 0.02, "threshold_share": 0.02, "above_threshold": False,
                                                     "counts": {"2026-09-30": 2}}}}
    assert data_cmd.vendor_null_close_line(below) is None
    assert data_cmd.vendor_null_close_line({"price_capture": {}}) is None
    assert data_cmd.vendor_null_close_line({}) is None


def test_data_capture_prints_the_line(monkeypatch, capsys):
    details = {"capture_id": "c", "price_capture": {"vendor_null_close": {
        "requested": 526, "latest_session": "2026-09-30", "latest_count": 175, "latest_share": 175 / 526,
        "threshold_share": 0.02, "above_threshold": True, "counts": {"2026-09-30": 175}}}}

    class Ctx:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(data_cmd, "RunContext", Ctx)
    monkeypatch.setattr(data_cmd, "YahooClient", lambda **k: object())
    monkeypatch.setattr(data_cmd, "capture_run", lambda ctx, client: Result(status="ok", counts={"bundles": 526}, details=details))
    monkeypatch.setattr(data_cmd, "load_config", lambda *a, **k: SimpleNamespace())
    assert data_cmd.data_capture_cmd(SimpleNamespace(actor_kind="system", by="cli", as_of="2026-09-30")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Data capture completed: 526 bundles"
    assert len(out) == 2 and "175" in out[1] and "G3 needs 98%" in out[1]
