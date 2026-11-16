"""Closing prices from several vendors, with fallbacks (MASTER_SPEC R18).

Everything is offline: the CSV text is built here with the exact exchange header, and every fetch is a
fake. Worlds live on tmp_path databases. The tests cover the two parsers, the enabling decision, the
fill rules (only missing closes, never an overwrite), the source chain and its failures, the
disagreement and usage events, the capture registration, time honesty and the `prices update` command.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from quant.cli import main
from quant.config import Config, load as load_config
from quant.data import price_fallback as pf
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.knowledge.decisions import record_rule_change
from quant.run import RunContext
from quant.types import Actor, Clock, FrozenClock

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")
SESSION = "2026-11-13"
NOW = "2026-11-14T11:00:00.000000Z"
YAHOO_AT = "2026-11-13T12:00:00.000000Z"
HUMAN = Actor(kind="human", name="owner")


def isin(sid: int) -> str:
    return f"INE00000000{sid}"


def line(isin_: str, close, *, sym="SYM", series="EQ", tp="STK", date=SESSION, vol="1000", opn=None, hi=None, lo=None):
    """One CSV data row under HEADER (34 columns); values may carry spaces."""
    opn = close if opn is None else opn
    hi = close if hi is None else hi
    lo = close if lo is None else lo
    cols = [date, date, "CM", "01", tp, "1", isin_, sym, series, "", "", "", "", "NAME",
            str(opn), str(hi), str(lo), str(close), str(close), str(close), "", "", "", "", vol, "", "", "", "", "", "", "", "", ""]
    assert len(cols) == 34
    return ",".join(cols)


def csv_bytes(*lines: str, header: str = HEADER) -> bytes:
    return ("\n".join([header, *lines]) + "\n").encode("utf-8")


def zipped(raw: bytes, name: str = "BhavCopy.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, raw)
    return buf.getvalue()


# ----------------------------------------------------------------------------- 1. parsers

def test_registry_has_the_two_sources_with_their_kinds_and_headers():
    assert list(pf.SOURCES) == ["nse_udiff", "bse_udiff"]
    nse, bse = pf.SOURCES["nse_udiff"], pf.SOURCES["bse_udiff"]
    assert nse.kind == "nse_udiff_bhavcopy" and bse.kind == "bse_udiff_bhavcopy"
    assert nse.url.format(YYYYMMDD="20261113") == \
        "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip"
    assert bse.url.format(YYYYMMDD="20261113") == \
        "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV"
    assert nse.series == ("EQ", "BE", "BZ", "RR", "IV")                          # RR and IV: REITs and InvITs
    assert bse.series == ("A", "B", "T", "X", "XT", "Z", "M", "MT", "IF")        # IF: REITs
    assert nse.same_exchange is True and bse.same_exchange is False
    assert "User-Agent" in nse.headers and "Referer" not in nse.headers
    assert bse.headers["Referer"] == "https://www.bseindia.com/" and "User-Agent" in bse.headers
    for s in (nse, bse):                                # nothing identifies the user or the project
        text = json.dumps(s.headers).lower()
        for word in ("quant", "multi", "saurabh", "claude", "bagger", "python-requests"):
            assert word not in text


def test_parse_nse_zip_trims_spaces_and_keeps_stk_only():
    raw = zipped(csv_bytes(
        line(" INE000000001 ", " 101.50 ", sym=" ABC ", series=" EQ ", opn=" 100 ", hi=" 102 ", lo=" 99 ", vol=" 1,000".replace(",", "")),
        line("INE000000002", 55.0, sym="DEF"),
        line("INE000000003", 9.0, sym="OPT", tp="IDO"),
    ))
    df = pf.SOURCES["nse_udiff"].parse(raw, SESSION)
    assert list(df.columns) == ["isin", "symbol", "series", "date", "open", "high", "low", "close", "volume"]
    assert df["isin"].tolist() == ["INE000000001", "INE000000002"]       # the IDO row is not equity
    r = df.iloc[0]
    assert (r["symbol"], r["series"], r["date"]) == ("ABC", "EQ", SESSION)
    assert (r["open"], r["high"], r["low"], r["close"], r["volume"]) == (100.0, 102.0, 99.0, 101.5, 1000.0)


def test_parse_bse_plain_csv():
    raw = csv_bytes(line("INE000000001", 250.25, sym="XYZ", series="A"), line("INE000000009", 1.0, series="A", tp="FUT"))
    df = pf.SOURCES["bse_udiff"].parse(raw, SESSION)
    assert df["isin"].tolist() == ["INE000000001"] and df.iloc[0]["close"] == 250.25 and df.iloc[0]["series"] == "A"


def test_wrong_session_is_refused():
    raw = csv_bytes(line("INE000000001", 10, date="2026-11-12"))
    with pytest.raises(Refused) as exc:
        pf.SOURCES["bse_udiff"].parse(raw, SESSION)
    assert exc.value.code == "wrong_session"
    with pytest.raises(Refused) as exc:
        pf.SOURCES["nse_udiff"].parse(zipped(raw), SESSION)
    assert exc.value.code == "wrong_session"


def test_missing_column_is_refused():
    bad = HEADER.replace("ClsPric,", "")
    row = line("INE000000001", 10).split(",")
    del row[17]
    with pytest.raises(Refused) as exc:
        pf.SOURCES["bse_udiff"].parse(csv_bytes(",".join(row), header=bad), SESSION)
    assert exc.value.code == "missing_column" and "ClsPric" in exc.value.detail


def test_series_preference_nse_and_bse():
    nse = csv_bytes(line("INE000000001", 11, series="BE"), line("INE000000001", 10, series="EQ"),
                    line("INE000000002", 20, series="SM"),               # no preferred series: skipped
                    line("INE000000003", 30, series="BZ"), line("INE000000003", 31, series="BE"))
    df = pf.SOURCES["nse_udiff"].parse(zipped(nse), SESSION).set_index("isin")
    assert df["close"].to_dict() == {"INE000000001": 10.0, "INE000000003": 31.0}
    assert df["series"].to_dict() == {"INE000000001": "EQ", "INE000000003": "BE"}
    bse = csv_bytes(line("INE000000001", 11, series="B"), line("INE000000001", 10, series="A"),
                    line("INE000000002", 20, series="F"),                # F is not preferred: skipped
                    line("INE000000003", 30, series="MT"), line("INE000000003", 31, series="Z"))
    df = pf.SOURCES["bse_udiff"].parse(bse, SESSION).set_index("isin")
    assert df["series"].to_dict() == {"INE000000001": "A", "INE000000003": "Z"}


def test_non_numeric_and_non_positive_closes_are_dropped():
    raw = csv_bytes(line("INE000000001", 10), line("INE000000002", 0), line("INE000000003", -4),
                    line("INE000000004", "n/a"), line("INE000000005", ""))
    df = pf.SOURCES["bse_udiff"].parse(raw.replace(b",EQ,", b",A,"), SESSION)
    assert df["isin"].tolist() == ["INE000000001"]


def test_garbage_bytes_refuse_or_raise_the_listed_errors():
    with pytest.raises(zipfile.BadZipFile):
        pf.SOURCES["nse_udiff"].parse(b"<html>blocked</html>", SESSION)
    with pytest.raises(UnicodeDecodeError):
        pf.SOURCES["bse_udiff"].parse(b"\xff\xfe\x00\xd8garbage", SESSION)


# ----------------------------------------------------------------------------- 2. the client

def _cfg_with(cfg: Config, **price_sources) -> Config:
    raw = cfg.to_dict()
    raw["price_sources"] = {**raw["price_sources"], **price_sources}
    return Config(raw, cfg._root, policy_hash=cfg.policy_sha256)


def test_config_section_is_present_and_identical_in_the_contract():
    ps = load_config().price_sources
    assert (list(ps.fallback), ps.fallback_sessions, ps.disagreement_warn, ps.http_timeout_s, ps.http_retries) == \
        (["nse_udiff", "bse_udiff"], 3, 0.005, 30, 2)
    assert (ps.mode, ps.inbox_subdir) == ("manual", "inbox/prices")             # the shipped default never fetches
    assert ps.disagreement_warn_cross_exchange == 0.02
    root = Path(__file__).resolve().parents[2]
    assert (root / "config/quant.toml").read_bytes() == (root / "docs/spec/contracts/config.toml").read_bytes()


def test_client_sends_headers_timeout_and_stamps_fetched_at_on_arrival():
    calls = []

    class Ticking(Clock):
        def __init__(self):
            self.n = 0

        def now(self):
            from datetime import datetime, timedelta, timezone
            self.n += 1
            return datetime(2026, 11, 14, 11, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=self.n)

    def fetch(url, headers, timeout):
        calls.append((url, dict(headers), timeout))
        return 200, b"bytes"

    clock = Ticking()
    client = pf.ExchangeFileClient(load_config(), clock, fetch=fetch)
    raw, fetched_at = client.get("bse_udiff", SESSION)
    assert raw == b"bytes" and fetched_at == "2026-11-14T11:00:01.000000Z"      # one clock read, after the fetch
    (url, headers, timeout), = calls
    assert url.endswith("BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV") and timeout == 30
    assert headers["Referer"] == "https://www.bseindia.com/"


def test_client_404_is_source_unavailable_and_other_failures_retry():
    n = {"c": 0}

    def not_yet(url, headers, timeout):
        n["c"] += 1
        return 404, b""

    with pytest.raises(pf.SourceUnavailable):
        pf.ExchangeFileClient(load_config(), FrozenClock(NOW), fetch=not_yet).get("nse_udiff", SESSION)
    assert n["c"] == 1                                                         # 404 is not retried

    n["c"] = 0
    slept = []

    def flaky(url, headers, timeout):
        n["c"] += 1
        if n["c"] < 3:
            raise requests.ConnectionError("reset")
        return 200, b"ok"

    client = pf.ExchangeFileClient(load_config(), FrozenClock(NOW), fetch=flaky, sleep=slept.append)
    assert client.get("nse_udiff", SESSION)[0] == b"ok" and n["c"] == 3 and len(slept) == 2   # http_retries = 2

    def down(url, headers, timeout):
        n["c"] += 1
        raise requests.ConnectionError("down")

    n["c"] = 0
    with pytest.raises(requests.ConnectionError):
        pf.ExchangeFileClient(load_config(), FrozenClock(NOW), fetch=down, sleep=lambda s: None).get("nse_udiff", SESSION)
    assert n["c"] == 3

    n["c"] = 0

    def server_error(url, headers, timeout):
        n["c"] += 1
        return 503, b""

    with pytest.raises(requests.HTTPError):
        pf.ExchangeFileClient(load_config(), FrozenClock(NOW), fetch=server_error, sleep=lambda s: None).get("nse_udiff", SESSION)
    assert n["c"] == 3


# ----------------------------------------------------------------------------- 3. a small world

class Files:
    """A fake fetch serving (source, session) -> bytes; anything else is 404. Records every call."""

    def __init__(self):
        self.files = {}
        self.errors = {}
        self.calls = []

    def put(self, source, session, rows, *, raw=None):
        body = raw if raw is not None else csv_bytes(*rows, header=HEADER)
        if raw is None and source == "nse_udiff":
            body = zipped(body)
        self.files[(source, session)] = body
        return body

    def __call__(self, url, headers, timeout):
        source = "nse_udiff" if "nsearchives" in url else "bse_udiff"
        ymd = re.search(r"_(\d{8})_F_0000", url).group(1)
        session = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        self.calls.append((source, session))
        if (source, session) in self.errors:
            raise self.errors[(source, session)]
        if (source, session) in self.files:
            return 200, self.files[(source, session)]
        return 404, b""


def _put_prices(store, rows):
    """rows: (security_id, date, close, observed_at)."""
    recs = [(s, d, c, c, c, c, 1_000_000.0, 0.0, 1.0, c, c, obs, "cap_yahoo", "sha") for s, d, c, obs in rows]
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            "dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)


def _seed_state(conn, n=6):
    for sid in range(1, n + 1):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2026-01-01', '2026-11-14', 'listed')", (sid, isin(sid), f"S{sid}"))


def _build(tmp_path, mode):
    """Six securities. Yahoo holds 11-11 and 11-12 for all and 11-13 for 1..4 (5 and 6 are missing)."""
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    _seed_state(conn)
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "prices.db", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", data_dir=tmp_path / "data")
    cfg = _cfg_with(cfg, mode=mode)
    files = Files()
    with RunContext(as_of=SESSION, kind="prices_update", track="live", cfg=cfg, clock=FrozenClock(NOW), actor=HUMAN) as ctx:
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        rows = [(s, d, 100.0 + s, YAHOO_AT) for s in range(1, 7) for d in ("2026-11-11", "2026-11-12")]
        rows += [(s, SESSION, 100.0 + s, YAHOO_AT) for s in range(1, 5)]
        _put_prices(ctx.store, rows)
        yield SimpleNamespace(ctx=ctx, store=ctx.store, cfg=cfg, files=files, sids=list(range(1, 7)),
                              client=pf.ExchangeFileClient(cfg, ctx.clock, fetch=files, sleep=lambda s: None))


@pytest.fixture
def world(tmp_path):
    """Auto mode: the injected fetch serves the files."""
    yield from _build(tmp_path, "auto")


@pytest.fixture
def mworld(tmp_path):
    """Manual mode (the default): fetch and client must never be used."""
    yield from _build(tmp_path, "manual")


def enable(ctx):
    return record_rule_change(ctx, subject_id="price_fallback_sources", title="Fill missing closes from exchange files",
                              context="c", evidence="docs/operations/price_sources.md")


def events(ctx, code):
    rows = ctx.conn.execute("SELECT severity, detail_json FROM data_quality_events WHERE code = ? ORDER BY event_id", (code,)).fetchall()
    return [(r[0], json.loads(r[1])) for r in rows]


def price_rows(store, sid, date):
    with store.conn() as c:
        return c.execute("SELECT close_raw, observed_at, capture_id, yahoo_close, yahoo_adj_close, source_sha256, volume_raw, "
                         "dividend_raw, split_ratio FROM prices_daily WHERE security_id = ? AND date = ? ORDER BY observed_at",
                         (sid, date)).fetchall()


def fill(w, through=SESSION):
    return pf.fill_missing(w.ctx, w.store, w.sids, through, client=w.client)


# ----------------------------------------------------------------------------- 4. gating

def test_disabled_without_a_decision_does_nothing(world):
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    assert pf.enabled_decision(world.ctx.conn) is None
    res = fill(world)
    assert res.status == "disabled" and world.files.calls == []
    assert price_rows(world.store, 5, SESSION) == []
    assert events(world.ctx, "PRICE_FALLBACK_USED") == []


def test_enabled_after_the_human_decision_and_not_by_an_agent(world):
    did = enable(world.ctx)
    assert pf.enabled_decision(world.ctx.conn) == did
    world.ctx.conn.execute("UPDATE decisions SET approver_kind = 'llm' WHERE decision_id = ?", (did,))
    assert pf.enabled_decision(world.ctx.conn) is None                          # only a human decision counts
    world.ctx.conn.execute("UPDATE decisions SET approver_kind = 'human', status = 'reverted' WHERE decision_id = ?", (did,))
    assert pf.enabled_decision(world.ctx.conn) is None
    from quant.knowledge.decisions import ALLOWED_RULE_SUBJECTS
    assert "price_fallback_sources" in ALLOWED_RULE_SUBJECTS


# ----------------------------------------------------------------------------- 5. filling

def test_fills_only_missing_securities_and_never_changes_a_stored_close(world):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(1), 999.0), line(isin(5), 105.5, vol="777"), line(isin(6), 106.5)])
    res = fill(world)
    assert res.status == "ok" and res.counts["filled"] == 2 and res.counts["still_missing"] == 0
    assert [r[0] for r in price_rows(world.store, 1, SESSION)] == [101.0]       # stored close untouched, no second row
    (r5,) = price_rows(world.store, 5, SESSION)
    assert r5[0] == 105.5 and r5[6] == 777.0 and r5[7] == 0.0 and r5[8] == 1.0
    assert price_rows(world.store, 6, SESSION)[0][0] == 106.5
    closes = world.store.close_raw(world.sids, SESSION, SESSION, NOW)
    assert closes.loc[SESSION].notna().all()


def test_second_source_is_used_only_for_what_the_first_left_missing(world):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5)])
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    res = fill(world)
    assert price_rows(world.store, 5, SESSION)[0][0] == 105.5 and len(price_rows(world.store, 5, SESSION)) == 1
    assert price_rows(world.store, 6, SESSION)[0][0] == 106.8
    assert res.counts["filled"] == 2 and res.counts["still_missing"] == 0
    assert res.details["sources_used"] == ["nse_udiff", "bse_udiff"] and res.counts["sources_used"] == 2
    used = events(world.ctx, "PRICE_FALLBACK_USED")
    assert [(e[1]["source"], e[1]["filled"], e[1]["still_missing"]) for e in used] == [("nse_udiff", 1, 1), ("bse_udiff", 1, 0)]
    assert all(e[0] == "INFO" for e in used)


def test_the_second_source_is_not_fetched_when_the_first_filled_everything(world):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A")])
    fill(world)
    assert ("bse_udiff", SESSION) not in world.files.calls


def test_404_on_the_first_source_falls_through_without_a_failure_event(world):
    enable(world.ctx)
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    res = fill(world)                                                           # nse has no file: 404
    assert ("nse_udiff", SESSION) in world.files.calls and res.counts["filled"] == 2
    assert events(world.ctx, "PRICE_FALLBACK_SOURCE_FAILED") == []
    assert any(d["source"] == "nse_udiff" and d["session"] == SESSION and d["status"] == "unavailable" for d in res.details["sessions"])


def test_network_error_records_one_failure_event_and_falls_through(world):
    enable(world.ctx)
    world.files.errors[("nse_udiff", SESSION)] = requests.ConnectionError("reset by peer")
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    res = fill(world)
    failed = [e for e in events(world.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["date"] == SESSION]
    assert len(failed) == 1 and failed[0][0] == "WARN"
    assert failed[0][1]["source"] == "nse_udiff" and "reset by peer" in failed[0][1]["error"]
    assert res.counts["filled"] == 2 and res.counts["sources_failed"] >= 1


@pytest.mark.parametrize("body", [b"<html>denied</html>", csv_bytes(line(isin(5), 5, date="2026-11-12"))])
def test_unreadable_or_wrong_session_file_is_a_failure_event_not_a_raise(world, body):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, None, raw=body)
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    res = fill(world)
    assert len([e for e in events(world.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff"
                and e[1]["date"] == SESSION]) == 1
    assert res.counts["filled"] == 2


def test_unknown_source_name_in_config_is_refused(world):
    enable(world.ctx)
    bad = pf.ExchangeFileClient(world.cfg, world.ctx.clock, fetch=world.files)
    world.ctx.cfg = _cfg_with(world.cfg, fallback=["nse_udiff", "moneycontrol"])
    with pytest.raises(Refused) as exc:
        pf.fill_missing(world.ctx, world.store, world.sids, SESSION, client=bad)
    assert exc.value.code == "unknown_source" and "moneycontrol" in exc.value.detail
    assert world.files.calls == []


def test_a_security_with_a_close_is_never_written_to_even_by_a_later_source(world):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5)])
    world.files.put("bse_udiff", SESSION, [line(isin(5), 500.0, series="A"), line(isin(6), 106.8, series="A"), line(isin(2), 900.0, series="A")])
    fill(world)
    assert [r[0] for r in price_rows(world.store, 5, SESSION)] == [105.5]
    assert [r[0] for r in price_rows(world.store, 2, SESSION)] == [102.0]


# ----------------------------------------------------------------------------- 6. cross-check and usage events

def test_disagreement_event_has_count_compared_and_sample(world):
    enable(world.ctx)
    rows = [line(isin(1), 101.0), line(isin(2), 102.0 * 1.01), line(isin(3), 103.0 * 0.98), line(isin(4), 104.0 * 1.001),
            line(isin(5), 105.5), line(isin(6), 106.5)]
    world.files.put("nse_udiff", SESSION, rows)
    res = fill(world)
    (sev, d), = [e for e in events(world.ctx, "VENDOR_CLOSE_DISAGREEMENT") if e[1]["date"] == SESSION]
    assert sev == "WARN" and d["source"] == "nse_udiff" and d["count"] == 2 and d["compared"] == 4
    assert {s["security_id"] for s in d["sample"]} == {2, 3} and len(d["sample"]) <= 10
    top = d["sample"][0]
    assert set(top) == {"security_id", "isin", "stored_close", "vendor_close", "rel_diff"} and top["security_id"] == 3
    assert top["isin"] == isin(3) and top["stored_close"] == 103.0 and abs(top["rel_diff"] - 0.02) < 1e-9
    assert res.counts["disagreements"] == 2


def test_cross_check_runs_even_when_nothing_is_missing_and_a_clean_check_records_nothing(world):
    enable(world.ctx)
    _put_prices(world.store, [(5, SESSION, 105.0, YAHOO_AT), (6, SESSION, 106.0, YAHOO_AT)])
    world.files.put("nse_udiff", SESSION, [line(isin(s), 100.0 + s * (1.5 if s == 6 else 1.0)) for s in range(1, 7)])
    res = fill(world)
    assert world.files.calls[0] == ("nse_udiff", SESSION) and res.counts["filled"] == 0
    (_, d), = events(world.ctx, "VENDOR_CLOSE_DISAGREEMENT")
    assert d["count"] == 1 and d["compared"] == 6
    assert events(world.ctx, "PRICE_FALLBACK_USED") == []                       # nothing filled: no usage event


def test_sample_is_capped_at_ten(world):
    enable(world.ctx)
    conn = world.ctx.conn
    for sid in range(7, 19):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, 'x', '2026-01-01', '2026-11-14', 'listed')", (sid, f"INE0000000{sid:02d}"))
    _put_prices(world.store, [(s, SESSION, 50.0, YAHOO_AT) for s in range(7, 19)])
    world.files.put("nse_udiff", SESSION, [line(f"INE0000000{s:02d}", 60.0) for s in range(7, 19)])
    pf.fill_missing(world.ctx, world.store, list(range(1, 19)), SESSION, client=world.client)
    (_, d), = [e for e in events(world.ctx, "VENDOR_CLOSE_DISAGREEMENT") if e[1]["date"] == SESSION]
    assert d["count"] == 12 and len(d["sample"]) == 10


# ----------------------------------------------------------------------------- 7. capture and time honesty

def test_capture_registered_with_kind_sha_and_archive_file(world):
    enable(world.ctx)
    raw = world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(world)
    sha = hashlib.sha256(raw).hexdigest()
    cap = world.ctx.conn.execute("SELECT capture_id, captured_at, kind, archive_path, sha256, source_version, run_id "
                                 "FROM captures WHERE kind = 'nse_udiff_bhavcopy'").fetchall()
    # every fetched session is registered; the 13 Nov file is the one that filled
    mine = [c for c in cap if c["sha256"] == sha]
    assert len(mine) == 1
    c = mine[0]
    assert c["capture_id"] == f"cap_nse_udiff_bhavcopy_{sha[:16]}" and c["captured_at"] == NOW
    assert c["source_version"].endswith("BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip") and c["run_id"] == world.ctx.run_id
    path = Path(c["archive_path"])
    assert path.is_file() and path.name == f"{c['capture_id']}.zip.gz"
    assert path.parent == Path(world.cfg.paths.archive_dir) / "captures" / "prices_fallback"
    assert gzip.decompress(path.read_bytes()) == raw                            # the exact bytes
    meta = json.loads((path.parent / f"{c['capture_id']}.json").read_text())
    assert meta == {"capture_id": c["capture_id"], "fetched_at": NOW, "source": "nse_udiff", "url": c["source_version"],
                    "session": SESSION, "sha256": sha}
    # a plain CSV is archived as .csv.gz
    world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A")])
    world.ctx.conn.execute("DELETE FROM captures WHERE 0")                       # no-op: statement is allowed
    bse = [c for c in world.ctx.conn.execute("SELECT archive_path FROM captures WHERE kind = 'bse_udiff_bhavcopy'")]
    assert bse == []                                                            # BSE was not needed, so never fetched


def test_bse_archive_extension_is_csv_gz(world):
    enable(world.ctx)
    raw = world.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    fill(world)
    (row,) = world.ctx.conn.execute("SELECT archive_path, sha256 FROM captures WHERE kind = 'bse_udiff_bhavcopy' "
                                    "AND sha256 = ?", (hashlib.sha256(raw).hexdigest(),)).fetchall()
    assert row[0].endswith(".csv.gz") and gzip.decompress(Path(row[0]).read_bytes()) == raw


def test_observed_at_equals_captured_at_equals_the_fetch_time_and_yahoo_close_is_null(world):
    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5)])
    fill(world)
    (r,) = price_rows(world.store, 5, SESSION)
    cap = world.ctx.conn.execute("SELECT captured_at, sha256 FROM captures WHERE capture_id = ?", (r[2],)).fetchone()
    assert r[1] == cap["captured_at"] == NOW                                    # nothing is stamped before it was fetched
    assert r[3] is None and r[4] is None                                        # yahoo_close / yahoo_adj_close are not Yahoo values
    assert r[5] == cap["sha256"] and r[2].startswith("cap_nse_udiff_bhavcopy_")


def test_a_ticking_clock_stamps_the_row_with_the_capture_time(world):
    from datetime import datetime, timedelta, timezone

    class Ticking(Clock):
        def __init__(self):
            self.n = 0

        def now(self):
            self.n += 1
            return datetime(2026, 11, 14, 11, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=self.n)

    enable(world.ctx)
    world.ctx.clock = Ticking()
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5)])
    pf.fill_missing(world.ctx, world.store, world.sids, SESSION,
                    client=pf.ExchangeFileClient(world.cfg, world.ctx.clock, fetch=world.files))
    (r,) = price_rows(world.store, 5, SESSION)
    cap = world.ctx.conn.execute("SELECT captured_at FROM captures WHERE capture_id = ?", (r[2],)).fetchone()
    assert r[1] == cap["captured_at"]
    assert r[1] > "2026-11-14T11:00:00.000000Z"                                 # a clock read after the start, not NOW
    visible = world.store.close_raw([5], SESSION, SESSION, world.ctx.clock.iso())
    assert visible.loc[SESSION, 5] == 105.5                                     # visible once the clock has reached the fetch
    before = world.store.close_raw([5], SESSION, SESSION, "2026-11-14T10:59:59.000000Z")
    assert before.empty and list(before.columns) == [5]                         # no row at all before the fetch


def test_a_close_fetched_after_the_cutoff_is_still_late(world):
    from quant.data.price_admission import coverage
    from quant.run import knowledge_cutoff

    enable(world.ctx)
    world.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(world)
    cut = knowledge_cutoff(SESSION)
    assert coverage(world.store, world.sids, SESSION, cut) == pytest.approx(4 / 6)       # gate G3 input is unchanged
    assert coverage(world.store, world.sids, SESSION, NOW) == 1.0


def test_sessions_are_the_recent_dates_with_a_close_plus_through_newest_first(world):
    enable(world.ctx)
    _put_prices(world.store, [(1, "2026-11-05", 99.0, YAHOO_AT)])               # a fourth, older date: out of the window
    res = fill(world, through="2026-11-14")                                      # through itself has no close at all
    assert [d["session"] for d in res.details["sessions"] if d["source"] == "nse_udiff"] == \
        ["2026-11-14", "2026-11-13", "2026-11-12", "2026-11-11"]               # 3 dates with a close, plus through
    assert res.counts["sessions"] == 4


# ----------------------------------------------------------------------------- 8. the command

def _seed_command_state(db):
    conn = connect(db)
    apply_schema(conn, kind="state")
    for sid in range(1, 7):
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2026-01-01', '2026-11-14', 'listed')", (sid, isin(sid), f"S{sid}"))
        conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                     "VALUES (?, ?, ?, '2026-01-01', 'nifty500_csv')", (sid, f"S{sid}", f"T{sid}.NS"))
        conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, "
                     "series, source) VALUES ('2026-09-15', '2026-09-15T10:00:00.000000Z', ?, 'NIFTY500', ?, 'S', 'EQ', 'nse_csv')",
                     (sid, f"S{sid}"))
    conn.close()


class _FakeYahoo:
    """Delivers closes for T1..T4 only: T5 and T6 have no close on any date."""

    def __init__(self, cfg=None, clock=None, sleep=None, **kw):
        pass

    def download_batch(self, tickers, start, end):
        idx = pd.DatetimeIndex(["2026-11-11", "2026-11-12", "2026-11-13"])
        cols, data = [], {}
        for sid in (1, 2, 3, 4):
            for f in ("Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"):
                cols.append((f, f"T{sid}.NS"))
                data[(f, f"T{sid}.NS")] = [1000.0 if f == "Volume" else 0.0 if f in ("Dividends", "Stock Splits") else 100.0 + sid] * 3
        df = pd.DataFrame(data, index=idx, columns=pd.MultiIndex.from_tuples(cols))
        df.attrs["fetched_at"] = YAHOO_AT
        df.attrs["tickers"] = list(tickers)
        df.attrs["source"] = "yfinance"
        return df


def _decide(db, subject="price_fallback_sources"):
    return main(["kb", "decide", "--kind", "rule_change", "--subject", subject, "--title", "Fill closes from exchange files",
                 "--evidence", "docs/operations/price_sources.md", "--actor-kind", "human", "--by", "human:owner",
                 "--db-path", str(db), "--as-of", "2026-11-14"])


def _auto_config(tmp_path):
    root = Path(__file__).resolve().parents[2]
    p = tmp_path / "auto.toml"
    p.write_text((root / "config/quant.toml").read_text().replace('mode = "manual"', 'mode = "auto"'))
    return str(p)


def test_prices_update_command_fills_and_prints_the_summary(tmp_path, monkeypatch, capsys):
    from quant.data.price_admission import coverage
    from quant.commands import prices as prices_cmd

    db = tmp_path / "state.db"
    _seed_command_state(db)
    files = Files()
    files.put("nse_udiff", SESSION, [line(isin(s), 100.0 + s) for s in range(1, 7)])
    monkeypatch.setattr(prices_cmd, "YahooClient", _FakeYahoo)
    monkeypatch.setattr(pf, "_http_fetch", files)

    # not enabled: the line says so and the two securities stay without a close
    upd = ["prices", "update", "--through", SESSION, "--db-path", str(db), "--actor-kind", "human", "--by", "human:owner",
           "--config", _auto_config(tmp_path)]
    assert main(upd) == 0
    out = capsys.readouterr().out
    assert "Price fallback: disabled (no decision recorded)" in out and files.calls == []

    assert _decide(db) == 0
    capsys.readouterr()
    assert main(upd) == 0
    out = capsys.readouterr().out
    assert f"Price fallback [through {SESSION}]: mode=auto filled=2 still_missing=0 sources_used=['nse_udiff'] disagreements=0" in out
    assert out.index("Prices update [through") < out.index("Price fallback [") < out.index("Suspected corporate actions")

    cfg = load_config().with_paths(db=db)
    conn = connect(db, readonly=True)
    try:
        store = PriceStore.open_readonly(cfg.paths.prices_db, conn)
        assert coverage(store, list(range(1, 7)), SESSION, "9999-12-31T00:00:00.000000Z") == 1.0
        kinds = {r[0] for r in conn.execute("SELECT kind FROM captures")}
        assert "nse_udiff_bhavcopy" in kinds and "yahoo_prices" in kinds
    finally:
        conn.close()


def test_capture_run_reports_the_fallback_counts_under_price_fallback(tmp_path, monkeypatch):
    from quant.data import capture
    from quant.types import Result

    class Store:
        def update(self, ctx, client, sids, through):
            return Result(status="ok", counts={"rows": 0, "quarantined": 0}, details={})

    class Client:
        def bundle(self, t):
            return SimpleNamespace(ticker=t)

        def archive(self, bundles, ctx):
            return "cap_x"

    db = tmp_path / "state.db"
    _seed_command_state(db)
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "p.db", archive_dir=tmp_path / "a", knowledge_dir=tmp_path / "k")
    seen = {}

    def fake_fill(ctx, store, sids, through, client=None):
        seen["args"] = (sorted(sids), through)
        return Result(status="ok", counts={"sessions": 3, "filled": 7, "still_missing": 0, "disagreements": 0,
                                           "sources_used": 1, "sources_failed": 0}, details={})

    monkeypatch.setattr(capture, "ingest_bundles", lambda ctx, bundles: {})
    monkeypatch.setattr(pf, "fill_missing", fake_fill)
    with RunContext(as_of=SESSION, kind="capture", track="live", cfg=cfg, clock=FrozenClock(NOW), actor=HUMAN) as ctx:
        ctx.store = Store()
        res = capture.run(ctx, Client())
    assert res.details["price_fallback"]["filled"] == 7 and seen["args"] == ([1, 2, 3, 4, 5, 6], SESSION)


# ----------------------------------------------------------------------------- 9. status and kb

def test_kb_decide_enables_the_subject_and_the_context_names_the_rule(tmp_path, capsys):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    assert _decide(db) == 0
    assert _decide(db, subject="late_price_admission") == 0
    conn = connect(db, readonly=True)
    try:
        ctx_text = {r["subject_id"]: r["context"] for r in conn.execute("SELECT subject_id, context FROM decisions")}
        assert "R18" in ctx_text["price_fallback_sources"] and "R13" not in ctx_text["price_fallback_sources"]
        assert "R13" in ctx_text["late_price_admission"]
        assert pf.enabled_decision(conn) is not None
    finally:
        conn.close()


def test_status_reports_the_newest_price_capture_of_three_kinds_and_the_fallback_state(tmp_path):
    from quant import status

    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.execute("INSERT INTO runs (as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, "
                 "registry_sha256, python_version) VALUES ('2026-11-13', 'x', 'live', 1, 'n', 'ok', 'g', 'c', 'c', 'r', 'p')")
    for cid, kind, at in (("a", "yahoo_prices", "2026-11-13T12:00:00.000000Z"), ("b", "nse_udiff_bhavcopy", "2026-11-14T11:00:00.000000Z"),
                          ("c", "bse_udiff_bhavcopy", "2026-11-14T09:00:00.000000Z")):
        conn.execute("INSERT INTO captures VALUES (?, ?, ?, '/nowhere', 'sha', 'v', 1)", (cid, at, kind))
    cfg = load_config()
    out = status.read(conn, cfg)
    assert out["last_capture"]["yahoo_prices"]["capture_id"] == "b"
    assert out["price_fallback"] == {"enabled": False, "decision_id": None, "sources": ["nse_udiff", "bse_udiff"],
                                     "mode": "manual", "inbox": str((Path(cfg.paths.data_dir) / "inbox/prices").resolve())}
    conn.close()
    assert _decide(db) == 0
    conn = connect(db, readonly=True)
    out = status.read(conn, cfg)
    conn.close()
    assert out["price_fallback"]["enabled"] is True and out["price_fallback"]["decision_id"].startswith("D-2026-11-")
    json.dumps(out, default=str)                                                  # still one JSON document


# ----------------------------------------------------------------------------- 10. manual inbox mode (the default)

def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the network must not be touched in manual mode")

    monkeypatch.setattr(pf, "_http_fetch", boom)
    monkeypatch.setattr(pf, "ExchangeFileClient", boom)


def _inbox(w) -> Path:
    d = pf.inbox_dir(w.cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_manual_mode_ingests_an_inbox_file_and_never_calls_the_network(mworld, monkeypatch):
    import os

    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    body = zipped(csv_bytes(line(isin(5), 105.5), line(isin(6), 106.5)))
    f = _inbox(w) / "bhavcopy_nse_cm_0_0_0_20261113_f_0000.CSV.ZIP"             # matched case-insensitively
    f.write_bytes(body)
    os.utime(f, (946684800, 946684800))                                         # mtime: 1 Jan 2000
    res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION)                      # no client: it must not be built
    assert res.status == "ok" and res.counts["filled"] == 2 and res.counts["still_missing"] == 0
    assert res.details["mode"] == "manual" and res.details["needed_files"] == []
    sha = hashlib.sha256(body).hexdigest()
    cap = w.ctx.conn.execute("SELECT * FROM captures WHERE sha256 = ?", (sha,)).fetchone()
    assert cap["kind"] == "nse_udiff_bhavcopy" and cap["source_version"] == f"manual-file:{f.name}"
    assert gzip.decompress(Path(cap["archive_path"]).read_bytes()) == body
    (r,) = price_rows(w.store, 5, SESSION)
    assert r[1] == cap["captured_at"] == NOW and not r[1].startswith("2000")      # the clock at reading, not the mtime
    assert not f.exists() and (f.parent / "done" / f.name).read_bytes() == body


def test_manual_mode_reads_an_unzipped_nse_csv_and_a_bse_file(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    (_inbox(w) / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv").write_bytes(csv_bytes(line(isin(5), 105.5)))
    (_inbox(w) / "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV").write_bytes(csv_bytes(line(isin(6), 106.8, series="A")))
    res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    assert res.counts["filled"] == 2 and res.details["sources_used"] == ["nse_udiff", "bse_udiff"]
    versions = {r[0] for r in w.ctx.conn.execute("SELECT source_version FROM captures")}
    assert versions == {"manual-file:BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv", "manual-file:BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV"}


def test_done_folder_name_clash_adds_the_sha_prefix(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    done = _inbox(w) / "done"
    done.mkdir()
    name = "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV"
    (done / name).write_bytes(b"older")
    body = csv_bytes(line(isin(5), 105.7, series="A"))
    (_inbox(w) / name).write_bytes(body)
    pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    sha8 = hashlib.sha256(body).hexdigest()[:8]
    assert (done / name).read_bytes() == b"older" and (done / f"BhavCopy_BSE_CM_0_0_0_20261113_F_0000.{sha8}.CSV").read_bytes() == body


def test_absent_file_is_not_an_error_and_lists_needed_files(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    assert res.status == "ok" and res.counts["filled"] == 0 and res.counts["still_missing"] == 2
    assert res.counts["sources_failed"] == 0
    inbox = str(pf.inbox_dir(w.cfg))
    assert res.details["needed_files"] == [
        {"source": "nse_udiff", "session": SESSION, "inbox": inbox, "url": SOURCES_URL["nse_udiff"],
         "file_name": "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip"},
        {"source": "bse_udiff", "session": SESSION, "inbox": inbox, "url": SOURCES_URL["bse_udiff"],
         "file_name": "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV"}]
    n = w.ctx.conn.execute("SELECT count(*) FROM data_quality_events WHERE code LIKE 'PRICE_FALLBACK%' "
                           "OR code LIKE 'VENDOR_CLOSE%'").fetchone()[0]
    assert n == 0                                                               # no event for an absent file


SOURCES_URL = {
    "nse_udiff": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip",
    "bse_udiff": "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV",
}


def test_needed_files_empty_when_nothing_is_missing_for_through(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    res = pf.fill_missing(w.ctx, w.store, w.sids, "2026-11-12")                 # every member has a close that day
    assert res.counts["still_missing"] == 0 and res.details["needed_files"] == []


def test_a_corrupt_inbox_file_moves_to_failed_and_the_failure_is_recorded_once(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    f = _inbox(w) / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip"
    f.write_bytes(b"<html>not a zip</html>")
    res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    assert not f.exists() and not (f.parent / "done").exists()
    assert (f.parent / "failed" / f.name).read_bytes() == b"<html>not a zip</html>"
    (ev,) = [e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff" and e[1]["date"] == SESSION]
    assert ev[0] == "WARN" and res.counts["filled"] == 0
    assert w.ctx.conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0   # nothing registered for a bad file
    pf.fill_missing(w.ctx, w.store, w.sids, SESSION)                              # the next run does not see it again
    assert len([e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff"]) == 1


def test_failed_folder_name_clash_adds_the_sha_prefix(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    failed = _inbox(w) / "failed"
    failed.mkdir()
    name = "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV"
    (failed / name).write_bytes(b"older")
    body = csv_bytes(line(isin(5), 105.7, series="A", date="2026-11-12"))          # wrong session: refused
    (_inbox(w) / name).write_bytes(body)
    pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    sha8 = hashlib.sha256(body).hexdigest()[:8]
    assert (failed / name).read_bytes() == b"older"
    assert (failed / f"BhavCopy_BSE_CM_0_0_0_20261113_F_0000.{sha8}.CSV").read_bytes() == body


def test_auto_mode_prefers_an_inbox_file_and_otherwise_fetches(world):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 999.0)])                     # the fetch would give another value
    (_inbox(w) / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv").write_bytes(csv_bytes(line(isin(5), 105.5)))
    w.files.put("bse_udiff", SESSION, [line(isin(6), 106.8, series="A")])
    res = fill(w)
    assert ("nse_udiff", SESSION) not in w.files.calls                           # the inbox file won
    assert ("bse_udiff", SESSION) in w.files.calls                               # no BSE file in the inbox: fetched
    assert price_rows(w.store, 5, SESSION)[0][0] == 105.5 and price_rows(w.store, 6, SESSION)[0][0] == 106.8
    assert res.details["needed_files"] == []


def test_a_bad_mode_is_refused(world):
    enable(world.ctx)
    world.ctx.cfg = _cfg_with(world.cfg, mode="sometimes")
    with pytest.raises(Refused) as exc:
        pf.fill_missing(world.ctx, world.store, world.sids, SESSION, client=world.client)
    assert exc.value.code == "bad_mode"


def test_manual_command_prints_the_download_hint_then_fills_from_the_inbox(tmp_path, monkeypatch, capsys):
    from quant.commands import prices as prices_cmd

    db = tmp_path / "state.db"
    _seed_command_state(db)
    _no_network(monkeypatch)
    monkeypatch.setattr(prices_cmd, "YahooClient", _FakeYahoo)
    assert _decide(db) == 0
    capsys.readouterr()
    upd = ["prices", "update", "--through", SESSION, "--db-path", str(db), "--actor-kind", "human", "--by", "human:owner"]
    assert main(upd) == 0
    out = capsys.readouterr().out
    inbox = load_config().paths.data_dir / "inbox/prices"
    assert f"Price fallback [through {SESSION}]: mode=manual filled=0 still_missing=2 sources_used=[] disagreements=0" in out
    assert (f"Price fallback: download BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip from {SOURCES_URL['nse_udiff']} "
            f"into {inbox}, then run this command again.") in out
    assert "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV from " in out
    inbox.mkdir(parents=True)
    (inbox / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip").write_bytes(zipped(csv_bytes(*[line(isin(s), 100.0 + s) for s in range(1, 7)])))
    assert main(upd) == 0
    out = capsys.readouterr().out
    assert "mode=manual filled=2 still_missing=0 sources_used=['nse_udiff']" in out and "download" not in out
    assert (inbox / "done" / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip").is_file()


# ----------------------------------------------------------------------------- 11. fix round D

LATER = "2026-11-14T15:00:00.000000Z"
BETWEEN = "2026-11-14T13:00:00.000000Z"


def _captures(conn, capture_id=None):
    q = "SELECT capture_id, captured_at, kind, sha256 FROM captures"
    if capture_id:
        return conn.execute(q + " WHERE capture_id = ?", (capture_id,)).fetchall()
    return conn.execute(q).fetchall()


def _put_orphans(store, rows, capture_id):
    """Price rows of a capture that is not in `captures`: (security_id, date, close, observed_at)."""
    recs = [(s, d, c, c, c, c, 1000.0, 0.0, 1.0, None, None, obs, capture_id, "sha") for s, d, c, obs in rows]
    with store.conn() as c:
        c.executemany(
            "INSERT INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            "dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)


# D1: the capture row survives a staging rollback

def test_a_capture_survives_a_staging_rollback_and_the_filled_rows_stay_honest(world):
    from quant.data.price_admission import coverage, dishonest_rows

    w = world
    enable(w.ctx)
    w.ctx.checkpoint()                                                          # the decision is committed work
    raw = w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    w.ctx.rollback_staging()                                                    # the caller failed after the fill
    cid = f"cap_nse_udiff_bhavcopy_{hashlib.sha256(raw).hexdigest()[:16]}"
    (row,) = _captures(w.ctx.conn, cid)
    assert row["captured_at"] == NOW
    w.ctx.checkpoint()                                                          # ... and it is committed with the run
    conn = connect(w.cfg.paths.db, readonly=True)
    try:
        assert [r["captured_at"] for r in _captures(conn, cid)] == [NOW]
        assert dishonest_rows(w.store, conn, w.sids, "2026-11-01", SESSION, NOW)["rows"] == 0
        assert dishonest_rows(w.store, conn, w.sids, "2026-11-01", SESSION, BETWEEN)["rows"] == 0
        store = PriceStore.open_readonly(w.cfg.paths.prices_db, conn)
        assert coverage(store, w.sids, SESSION, NOW) == 1.0                     # the filled closes count
    finally:
        conn.close()


def test_a_second_fill_in_the_same_run_after_a_rollback_does_not_conflict(world):
    w = world
    enable(w.ctx)
    w.ctx.checkpoint()
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    w.ctx.rollback_staging()
    w.ctx.clock = FrozenClock(LATER)
    fill(w)                                                                     # same bytes again, later: no ImmutableConflict
    w.ctx.checkpoint()
    assert [r["captured_at"] for r in _captures(w.ctx.conn) if r["kind"] == "nse_udiff_bhavcopy"] == [NOW]


# D1b: orphan repair

def test_orphan_price_rows_get_their_first_seen_time_as_captured_at(world):
    from quant.data.price_admission import dishonest_rows

    w = world
    enable(w.ctx)
    first_seen = "2026-11-14T09:00:00.000000Z"
    raw = w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    cid = f"cap_nse_udiff_bhavcopy_{hashlib.sha256(raw).hexdigest()[:16]}"
    _put_orphans(w.store, [(5, SESSION, 105.5, first_seen), (6, SESSION, 106.5, "2026-11-14T09:30:00.000000Z")], cid)
    assert _captures(w.ctx.conn, cid) == []
    fill(w)                                                                     # the same bytes again, at NOW
    (row,) = _captures(w.ctx.conn, cid)
    assert row["captured_at"] == first_seen                                     # the earliest observed_at, not NOW
    for vintage in (first_seen, "2026-11-14T09:45:00.000000Z", BETWEEN, NOW):
        assert dishonest_rows(w.store, w.ctx.conn, w.sids, "2026-11-01", SESSION, vintage)["rows"] == 0


def test_an_existing_capture_row_is_never_changed(world):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    w.ctx.clock = FrozenClock(LATER)
    fill(w)
    nse = [r for r in _captures(w.ctx.conn) if r["kind"] == "nse_udiff_bhavcopy"]
    assert [r["captured_at"] for r in nse] == [NOW]
    assert all(r[1] == NOW for r in price_rows(w.store, 5, SESSION))             # one row, observed when first filled


# D1c: the inbox file is moved last

def test_the_inbox_file_stays_when_a_write_fails(mworld, monkeypatch):
    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    f = _inbox(w) / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip"
    f.write_bytes(zipped(csv_bytes(line(isin(5), 105.5), line(isin(6), 106.5))))

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(w.store, "reconcile", boom)
    with pytest.raises(RuntimeError):
        pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    assert f.exists() and not (f.parent / "done").exists()                      # moved only after every write succeeded


# D2: a re-delivered bar is unchanged only when close, split ratio and dividend are all equal

def _vendor_row(sid, date, close, obs, *, dividend=0.0, split=1.0):
    from quant.data.prices import normalize_source

    one = pd.DataFrame([{"date": date, "open": close, "high": close, "low": close, "close": close, "volume": 1000.0,
                         "dividend": dividend, "split_ratio": split}])
    return normalize_source(one, {"security_id": sid, "observed_at": obs, "capture_id": "cap_yahoo_later",
                                  "source_sha256": "s", "close_basis": "raw"})


def _newest(store, sid, date):
    df = store._versioned([sid], date, date, "9999-12-31T00:00:00.000000Z", "close_raw, dividend_raw, split_ratio")
    return float(df.iloc[0]["close_raw"]), float(df.iloc[0]["dividend_raw"]), float(df.iloc[0]["split_ratio"])


@pytest.mark.parametrize("dividend, split", [(2.5, 1.0), (0.0, 2.0)])
def test_a_vendor_row_with_a_dividend_or_split_replaces_a_fallback_bar_of_the_same_close(world, dividend, split):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    assert _newest(w.store, 5, SESSION) == (105.5, 0.0, 1.0)
    res = w.store.reconcile(w.ctx, _vendor_row(5, SESSION, 105.5, LATER, dividend=dividend, split=split))
    assert res.counts["accepted"] == 1 and res.counts["quarantined"] == 0
    assert len(price_rows(w.store, 5, SESSION)) == 2
    assert _newest(w.store, 5, SESSION) == (105.5, dividend, split)


def test_a_redelivery_identical_in_close_split_and_dividend_is_still_skipped(world):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    same = w.store.reconcile(w.ctx, _vendor_row(5, SESSION, 105.5, LATER))
    assert same.counts["accepted"] == 0 and len(price_rows(w.store, 5, SESSION)) == 1
    w.store.reconcile(w.ctx, _vendor_row(5, SESSION, 105.5, LATER, dividend=2.5))
    again = w.store.reconcile(w.ctx, _vendor_row(5, SESSION, 105.5, "2026-11-14T16:00:00.000000Z", dividend=2.5 * (1 + 1e-10)))
    assert again.counts["accepted"] == 0 and len(price_rows(w.store, 5, SESSION)) == 2   # within the dividend tolerance


def test_a_changed_close_beyond_two_percent_is_still_quarantined(world):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(5), 105.5), line(isin(6), 106.5)])
    fill(w)
    res = w.store.reconcile(w.ctx, _vendor_row(5, SESSION, 105.5 * 1.03, LATER, dividend=2.5))
    assert res.counts["quarantined"] == 1 and res.counts["accepted"] == 0
    assert _newest(w.store, 5, SESSION) == (105.5, 0.0, 1.0)


# D3: a file with no trade dates is refused

def test_a_file_whose_trade_date_cells_are_all_blank_is_refused():
    raw = csv_bytes(line("INE000000001", 10, date=""), line("INE000000002", 11, date=""))
    for src, body in (("bse_udiff", raw), ("nse_udiff", zipped(raw))):
        with pytest.raises(Refused) as exc:
            pf.SOURCES[src].parse(body.replace(b",EQ,", b",A,") if src == "bse_udiff" else body, SESSION)
        assert exc.value.code == "wrong_session"


# D4: a source problem never raises out of fill_missing

def test_an_unreadable_inbox_file_is_a_source_failure_and_the_chain_goes_on(mworld, monkeypatch):
    import os

    w = mworld
    _no_network(monkeypatch)
    enable(w.ctx)
    nse = _inbox(w) / "BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip"
    nse.write_bytes(zipped(csv_bytes(line(isin(5), 105.5))))
    (_inbox(w) / "BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV").write_bytes(
        csv_bytes(line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")))
    os.chmod(nse, 0)
    try:
        try:
            nse.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("file permissions cannot be simulated here (running as a privileged user)")
        res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION)
    finally:
        os.chmod(nse, 0o644)
    (ev,) = [e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff" and e[1]["date"] == SESSION]
    assert ev[0] == "WARN" and res.counts["filled"] == 2 and res.counts["sources_failed"] >= 1
    assert nse.exists()                                                         # not parsed, so not moved to failed/


def test_a_timeout_from_the_fetch_is_a_source_failure_and_the_chain_goes_on(world):
    w = world
    enable(w.ctx)
    w.files.errors[("nse_udiff", SESSION)] = TimeoutError("timed out")
    w.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    res = fill(w)
    (ev,) = [e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff"]
    assert ev[0] == "WARN" and "timed out" in ev[1]["error"] and res.counts["filled"] == 2


def test_the_client_retries_an_os_error_like_a_request_error():
    n = {"c": 0}

    def flaky(url, headers, timeout):
        n["c"] += 1
        if n["c"] < 3:
            raise ConnectionResetError("reset")
        return 200, b"ok"

    client = pf.ExchangeFileClient(load_config(), FrozenClock(NOW), fetch=flaky, sleep=lambda s: None)
    assert client.get("nse_udiff", SESSION)[0] == b"ok" and n["c"] == 3


def test_a_bad_zip_member_and_a_large_zip_are_source_failures(world, monkeypatch):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, None, raw=zipped(csv_bytes(line(isin(5), 105.5))))
    w.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])

    def large(self, raw, session, zipped=None):
        raise zipfile.LargeZipFile("needs zip64")

    real = pf.Source.parse
    monkeypatch.setattr(pf.Source, "parse", lambda self, raw, session, zipped=None:
                        large(self, raw, session, zipped) if self.name == "nse_udiff" else real(self, raw, session, zipped))
    res = fill(w)
    assert len([e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff"]) == 1
    assert res.counts["filled"] == 2
    monkeypatch.setattr(pf.Source, "parse", lambda self, raw, session, zipped=None:
                        (_ for _ in ()).throw(KeyError("There is no item named 'x' in the archive")) if self.name == "nse_udiff"
                        else real(self, raw, session, zipped))
    fill(w)                                                                     # a KeyError from the unzip is a failure too


def test_status_with_a_bad_mode_still_returns_one_json_document(tmp_path):
    from quant import status

    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    cfg = _cfg_with(load_config(), mode="sometimes")
    out = status.read(conn, cfg)
    conn.close()
    pfs = out["price_fallback"]
    assert pfs["enabled"] is False and "sometimes" in pfs["error"] and "mode" not in pfs
    json.loads(json.dumps(out, default=str))


# D6: a connection-level failure disables a source for the rest of the call

def test_a_connection_failure_skips_the_source_for_the_remaining_sessions(world):
    w = world
    enable(w.ctx)
    n = {"nse": 0}

    def fetch(url, headers, timeout):
        if "nsearchives" in url:
            n["nse"] += 1
            raise requests.ConnectionError("no route to host")
        return w.files(url, headers, timeout)

    w.files.put("bse_udiff", SESSION, [line(isin(5), 105.7, series="A"), line(isin(6), 106.8, series="A")])
    client = pf.ExchangeFileClient(w.cfg, w.ctx.clock, fetch=fetch, sleep=lambda s: None)
    res = pf.fill_missing(w.ctx, w.store, w.sids, SESSION, client=client)
    assert n["nse"] == 3                                                        # one session, 1 + 2 retries; none after
    assert len([e for e in events(w.ctx, "PRICE_FALLBACK_SOURCE_FAILED") if e[1]["source"] == "nse_udiff"]) == 1
    nse = {d["session"]: d["status"] for d in res.details["sessions"] if d["source"] == "nse_udiff"}
    assert nse == {SESSION: "failed", "2026-11-12": "skipped", "2026-11-11": "skipped"}
    assert res.counts["filled"] == 2 and res.counts["sources_failed"] == 1


def test_an_http_status_failure_is_not_connection_level_and_is_tried_again_next_session(world):
    w = world
    enable(w.ctx)
    n = {"nse": 0}

    def fetch(url, headers, timeout):
        if "nsearchives" in url:
            n["nse"] += 1
            return 503, b""
        return w.files(url, headers, timeout)

    client = pf.ExchangeFileClient(w.cfg, w.ctx.clock, fetch=fetch, sleep=lambda s: None)
    pf.fill_missing(w.ctx, w.store, w.sids, SESSION, client=client)
    assert n["nse"] == 9                                                        # 3 sessions x (1 + 2 retries)


# D7: REITs and InvITs

def test_reit_series_are_preferred_after_the_equity_series():
    nse = csv_bytes(line("INE000000001", 10, series="RR"), line("INE000000002", 20, series="IV"),
                    line("INE000000003", 30, series="RR"), line("INE000000003", 31, series="EQ"))
    df = pf.SOURCES["nse_udiff"].parse(zipped(nse), SESSION).set_index("isin")
    assert df["series"].to_dict() == {"INE000000001": "RR", "INE000000002": "IV", "INE000000003": "EQ"}
    bse = csv_bytes(line("INE000000001", 10, series="IF"), line("INE000000002", 20, series="A"),
                    line("INE000000002", 21, series="IF"))
    df = pf.SOURCES["bse_udiff"].parse(bse, SESSION).set_index("isin")
    assert df["series"].to_dict() == {"INE000000001": "IF", "INE000000002": "A"}


# D8: BSE is another exchange, so it has its own tolerance

def _disagreements(w, source):
    return [e for e in events(w.ctx, "VENDOR_CLOSE_DISAGREEMENT") if e[1]["date"] == SESSION and e[1]["source"] == source]


def test_cross_check_uses_the_same_exchange_tolerance_for_nse_and_the_wider_one_for_bse(world):
    w = world
    enable(w.ctx)
    w.files.put("nse_udiff", SESSION, [line(isin(1), 101.0), line(isin(2), 102.0 * 1.01), line(isin(3), 103.0), line(isin(4), 104.0)])
    w.files.put("bse_udiff", SESSION, [line(isin(1), 101.0 * 1.015, series="A"), line(isin(2), 102.0 * 1.03, series="A"),
                                       line(isin(5), 105.5, series="A"), line(isin(6), 106.5, series="A")])
    fill(w)
    (_, nse), = _disagreements(w, "nse_udiff")
    assert nse["count"] == 1 and nse["sample"][0]["security_id"] == 2 and nse["tolerance"] == 0.005
    (_, bse), = _disagreements(w, "bse_udiff")                                   # 1.5% is inside 2%; 3% is not
    assert bse["count"] == 1 and bse["sample"][0]["security_id"] == 2 and bse["tolerance"] == 0.02


def test_the_cross_exchange_tolerance_comes_from_the_config(world):
    w = world
    enable(w.ctx)
    w.ctx.cfg = _cfg_with(w.cfg, disagreement_warn_cross_exchange=0.01)
    w.files.put("nse_udiff", SESSION, [line(isin(1), 101.0)])
    w.files.put("bse_udiff", SESSION, [line(isin(1), 101.0 * 1.015, series="A"), line(isin(5), 105.5, series="A"),
                                       line(isin(6), 106.5, series="A")])
    fill(w)
    (_, bse), = _disagreements(w, "bse_udiff")
    assert bse["count"] == 1 and bse["tolerance"] == 0.01
