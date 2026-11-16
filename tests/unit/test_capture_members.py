"""Capture member selection: tracked at ctx.as_of UNION tracked at the capture date.

Inside a monthly run ctx.as_of is the target month-end, so index members that joined after
it must still be fetched. Offline: fake client, fake price store, no ingest.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quant.config import load
from quant.data import capture
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock, Result

OLD_ASOF = "2026-09-30"


class _Client:
    def __init__(self):
        self.tickers = []

    def bundle(self, ticker):
        self.tickers.append(ticker)
        return SimpleNamespace(ticker=ticker)

    def archive(self, bundles, ctx):
        return "cap_test"


class _Store:
    def __init__(self):
        self.sids = None

    def update(self, ctx, client, sids, through):
        self.sids = sorted(sids)
        return Result(status="ok", counts={"rows": 0, "quarantined": 0}, details={})


def _seed(conn, members):
    """members: (sid, observed_at); every sid gets a ticker T<sid>.NS."""
    for sid, obs in members:
        conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2026-01-01', '2026-11-01', 'listed')", (sid, f"INE{sid:09d}", f"S{sid}"))
        conn.execute("INSERT OR IGNORE INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                     "VALUES (?, ?, ?, '2026-01-01', 'nifty500_csv')", (sid, f"S{sid}", f"T{sid}.NS"))
        conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, "
                     "series, source) VALUES (?, ?, ?, 'NIFTY500', ?, 'S', 'EQ', 'nse_csv')",
                     (obs[:10], obs, sid, f"S{sid}"))


def _run(tmp_path, as_of, now, extra=(), monkeypatch=None):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    _seed(conn, [(1, "2026-09-15T10:00:00.000000Z"), (2, "2026-09-15T10:00:00.000000Z")] + list(extra))
    conn.close()
    cfg = load().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d", archive_dir=tmp_path / "a",
                            knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    client, store = _Client(), _Store()
    monkeypatch.setattr(capture, "ingest_bundles", lambda ctx, bundles: {})
    with RunContext(as_of=as_of, kind="capture", track="live", cfg=cfg, clock=FrozenClock(now),
                    actor=Actor(kind="system", name="t")) as ctx:
        ctx.store = store
        res = capture.run(ctx, client)
        ctx.status = "ok"
    return res, client, store


NEW = [(3, "2026-11-10T10:00:00.000000Z"), (4, "2026-11-10T10:00:00.000000Z")]


def test_i_members_added_after_target_as_of_are_fetched(tmp_path, monkeypatch):
    res, client, store = _run(tmp_path, OLD_ASOF, "2026-11-12T10:00:00.000000Z", NEW, monkeypatch)
    assert sorted(client.tickers) == ["T1.NS", "T2.NS", "T3.NS", "T4.NS"]
    assert store.sids == [1, 2, 3, 4]
    assert res.counts["securities"] == 4 and res.counts["bundles"] == 4


def test_j_no_universe_change_equals_todays_set(tmp_path, monkeypatch):
    res, client, store = _run(tmp_path, OLD_ASOF, "2026-11-12T10:00:00.000000Z", (), monkeypatch)
    assert sorted(client.tickers) == ["T1.NS", "T2.NS"] and store.sids == [1, 2]
    assert res.counts["securities"] == 2 and res.counts["bundles"] == 2


def test_k_standalone_capture_unchanged(tmp_path, monkeypatch):
    res, client, store = _run(tmp_path, "2026-11-12", "2026-11-12T10:00:00.000000Z", NEW, monkeypatch)
    assert store.sids == [1, 2, 3, 4] and res.counts["securities"] == 4


def test_capture_date_failure_falls_back_to_as_of_set(tmp_path, monkeypatch):
    real = capture.tracked_securities

    def flaky(conn, cutoff, horizons):
        if cutoff != OLD_ASOF:
            raise RuntimeError("boom")
        return real(conn, cutoff, horizons)

    monkeypatch.setattr(capture, "tracked_securities", flaky)
    res, client, store = _run(tmp_path, OLD_ASOF, "2026-11-12T10:00:00.000000Z", NEW, monkeypatch)
    assert store.sids == [1, 2] and res.counts["securities"] == 2
