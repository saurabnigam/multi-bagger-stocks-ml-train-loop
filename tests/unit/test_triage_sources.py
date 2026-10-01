"""Evidence triage sources (WP4): Yahoo actions and news, the optional RSS feed, entity matching.

Every test is offline: ``yfinance.Ticker`` is replaced by a fake, the sleeper is a list, the clock
is a FrozenClock and the state database lives under tmp_path. The one network test carries the
``network`` marker and is excluded from the default run (pytest.ini); it also needs QUANT_NETWORK=1.
The cohort is as_of 2026-10-30 (Friday); its cutoff is 2026-10-30T18:29:59.999999Z.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import shutil
import textwrap
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data import yahoo
from quant.data.yahoo import YahooClient
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.triage import evidence, pit, sources
from quant.triage.types import MAX_HEADLINE_CHARS, SOURCE_STATUSES, canonical_bytes, sha256_hex
from quant.types import Actor, FrozenClock

AS_OF = "2026-10-30"
CUTOFF = "2026-10-30T18:29:59.999999Z"
NOON = "2026-10-30T06:30:00.000000Z"
ISIN = "INE849A01020"
EX = "2026-10-29"
TICKER = "TRENT.NS"
NAMES = ["Trent Limited"]


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("triage_sources") / "template.db"
    conn = connect(path)
    apply_schema(conn, kind="state")
    conn.close()
    return path


@pytest.fixture
def run_ctx(tmp_path, template_db):
    shutil.copy(template_db, tmp_path / "state.db")
    cfg = load_config().with_paths(
        db=tmp_path / "state.db", prices_db=tmp_path / "p.db", data_dir=tmp_path / "d",
        archive_dir=tmp_path / "a", knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    with RunContext(as_of=AS_OF, kind="triage", track="live", cfg=cfg, clock=FrozenClock(NOON),
                    actor=Actor(kind="system", name="pytest")) as ctx:
        yield ctx


def make_ticker(actions=None, news=None):
    """A stand-in for ``yfinance.Ticker``. ``actions`` and ``news`` are values or zero-argument callables."""
    calls: list[str] = []

    def value(v):
        return v() if callable(v) else v

    class FakeTicker:
        def __init__(self, ticker):
            calls.append(f"init:{ticker}")
            self.ticker = ticker

        @property
        def actions(self):
            calls.append("actions")
            return value(actions)

        @property
        def news(self):
            calls.append("news")
            return value(news)

    FakeTicker.calls = calls
    return FakeTicker


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(cfg, sleeps):
    return YahooClient(cfg, FrozenClock(NOON), sleeps.append)


def patch_ticker(monkeypatch, **kw):
    fake = make_ticker(**kw)
    monkeypatch.setattr(yahoo.yf, "Ticker", fake)
    return fake


def actions_frame(rows):
    """rows: [(date, dividend, split)]; index in the exchange's own timezone, like Yahoo's."""
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="Asia/Kolkata")
    return pd.DataFrame({"Dividends": [r[1] for r in rows], "Stock Splits": [r[2] for r in rows]}, index=idx)


def news_item_new(title, *, pub="2026-10-29T04:30:00Z", url="https://finance.yahoo.com/news/a.html", **extra):
    content = {"title": title, "summary": "SUMMARY-BODY " * 40, "pubDate": pub,
               "canonicalUrl": {"url": url}, "provider": {"displayName": "Wire"}, "body": "FULL-BODY-TEXT " * 50}
    content.update(extra)
    return {"id": "x1", "content": content}


def news_item_old(title, *, epoch, link="https://example.test/n/1"):
    return {"uuid": "u1", "title": title, "publisher": "Wire", "link": link, "providerPublishTime": epoch,
            "type": "STORY", "relatedTickers": ["TRENT.NS"]}


# ------------------------------------------------------------------ existing YahooClient behaviour is untouched

def _ast_hash(obj) -> str:
    return hashlib.sha256(ast.dump(ast.parse(textwrap.dedent(inspect.getsource(obj)))).encode()).hexdigest()[:16]


# Computed from the unmodified module before WP4 edited it (design amendment F3: only ADD methods).
# ast.dump output can differ between Python versions: these hashes were verified on CPython 3.14.7. If an
# interpreter upgrade alone fails this test, regenerate them from the unmodified git HEAD version of
# quant/data/yahoo.py, never from the working copy.
EXISTING_YAHOO_CODE = {
    "normalize_info": (yahoo.normalize_info, "56577fb6ed862398"),
    "RawBundle": (yahoo.RawBundle, "ee8d0ad679aa9de8"),
    "YahooClient.__init__": (yahoo.YahooClient.__init__, "78c631e33dfd1ee8"),
    "YahooClient._call_with_retry": (yahoo.YahooClient._call_with_retry, "87b34826819bd82d"),
    "YahooClient.bundle": (yahoo.YahooClient.bundle, "d3744c974c13ccdc"),
    "YahooClient.download_batch": (yahoo.YahooClient.download_batch, "22947a64f835370e"),
    "YahooClient.archive": (yahoo.YahooClient.archive, "8862633f536e8b45"),
}


@pytest.mark.parametrize("name", sorted(EXISTING_YAHOO_CODE))
def test_yahoo_existing_functions_are_unchanged(name):
    obj, expected = EXISTING_YAHOO_CODE[name]
    assert _ast_hash(obj) == expected, f"{name} was edited; WP4 may only add methods"


# ------------------------------------------------------------------ YahooClient.actions

def test_yahoo_actions_go_through_the_throttle_and_return_splits_and_dividends(monkeypatch, client, sleeps):
    frame = actions_frame([("2025-06-04", 0.0, 2.0), ("2026-06-04", 0.0, 0.0), ("2026-10-27", 2.5, 0.0)])
    fake = patch_ticker(monkeypatch, actions=frame)
    raw = client.actions(TICKER)
    assert sleeps and min(sleeps) >= 0.5
    assert sleeps == [0.5]
    assert fake.calls == [f"init:{TICKER}", "actions"]
    assert raw.status == "ok" and raw.errors == []
    assert raw.ticker == TICKER and raw.fetched_at == client.clock.iso()
    assert raw.splits == {"2025-06-04": 2.0}
    assert raw.dividends == {"2026-10-27": 2.5}


def test_yahoo_actions_retry_on_429_sleeps_at_least_half_a_second_each_attempt(monkeypatch, client, sleeps):
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return actions_frame([("2025-06-04", 0.0, 2.0)])

    patch_ticker(monkeypatch, actions=flaky)
    raw = client.actions(TICKER)
    assert raw.status == "ok" and raw.splits == {"2025-06-04": 2.0}
    assert sleeps == [0.5, client.on_429_sleep_s, 0.5]
    assert min(sleeps) >= 0.5


def test_yahoo_actions_empty_frame_is_empty_and_not_ok(monkeypatch, client):
    patch_ticker(monkeypatch, actions=pd.DataFrame())
    raw = client.actions(TICKER)
    assert raw.status == "empty" and raw.splits == {} and raw.dividends == {}


def test_yahoo_actions_error_is_reported_never_raised(monkeypatch, client):
    def boom():
        raise ValueError("No data found, symbol may be delisted")

    patch_ticker(monkeypatch, actions=boom)
    raw = client.actions(TICKER)
    assert raw.status == "error"
    assert raw.splits == {} and raw.dividends == {}
    assert any("delisted" in e for e in raw.errors)


def test_yahoo_actions_429_exhaustion_is_an_error(monkeypatch, client, sleeps):
    def limited():
        raise RuntimeError("Too Many Requests")

    patch_ticker(monkeypatch, actions=limited)
    raw = client.actions(TICKER)
    assert raw.status == "error" and any("429" in e for e in raw.errors)
    assert min(sleeps) >= 0.5


def test_yahoo_actions_constructor_failure_is_an_error(monkeypatch, client):
    class Broken:
        def __init__(self, ticker):
            raise RuntimeError("cannot build a ticker")

    monkeypatch.setattr(yahoo.yf, "Ticker", Broken)
    assert client.actions(TICKER).status == "error"


@pytest.mark.parametrize("payload", [
    None, {"Dividends": {}}, ["not", "a", "frame"], 42,
    pd.DataFrame({"Close": [1.0]}),                        # renamed columns
])
def test_yahoo_actions_unrecognised_shape_is_an_error_not_zero_splits(monkeypatch, client, payload):
    patch_ticker(monkeypatch, actions=payload)
    raw = client.actions(TICKER)
    assert raw.status == "error"
    assert raw.splits == {} and raw.dividends == {}
    assert raw.errors


@pytest.mark.parametrize("columns", [
    {"Dividends": [0.0, 1.5]},                             # split column missing or renamed
    {"Stock Splits": [0.0, 2.0]},                          # dividend column missing or renamed
])
def test_yahoo_actions_one_unreadable_table_is_an_error_not_a_zero(monkeypatch, client, columns):
    idx = pd.DatetimeIndex(["2025-06-04", "2025-07-04"], tz="UTC")
    patch_ticker(monkeypatch, actions=pd.DataFrame(columns, index=idx))
    raw = client.actions(TICKER)
    assert raw.status == "error"
    assert raw.splits == {} and raw.dividends == {}
    assert any("column" in e for e in raw.errors)
    result = sources.actions_result(raw, isin=ISIN, ex_date=EX)
    assert result.status == "error" and result.envelopes == () and not result.supports_rules


def test_yahoo_actions_tolerate_alternative_column_names_and_bad_cells(monkeypatch, client):
    idx = pd.DatetimeIndex(["2025-06-04", "2025-07-04", "2025-08-04"], tz="UTC")
    frame = pd.DataFrame({"Splits": [2.0, float("nan"), -1.0], "Dividend": [0.0, 1.0, float("inf")]}, index=idx)
    patch_ticker(monkeypatch, actions=frame)
    raw = client.actions(TICKER)
    assert raw.status == "ok"
    assert raw.splits == {"2025-06-04": 2.0}          # NaN and negative ratios are not splits
    assert raw.dividends == {"2025-07-04": 1.0}       # infinity is not a dividend


# ------------------------------------------------------------------ YahooClient.news

def test_yahoo_news_goes_through_the_throttle_and_keeps_headline_link_and_time_only(monkeypatch, client, sleeps):
    items = [news_item_new("Trent shares jump on strong Q2 demand"),
             news_item_old("Trent: brokerage raises target", epoch=int(utc(2026, 10, 28, 9, 15).timestamp()))]
    fake = patch_ticker(monkeypatch, news=items)
    raw = client.news(TICKER)
    assert sleeps == [0.5] and min(sleeps) >= 0.5
    assert fake.calls == [f"init:{TICKER}", "news"]
    assert raw.status == "ok" and raw.errors == []
    assert raw.items == [
        {"headline": "Trent shares jump on strong Q2 demand", "link": "https://finance.yahoo.com/news/a.html",
         "published_at": "2026-10-29T04:30:00Z"},
        {"headline": "Trent: brokerage raises target", "link": "https://example.test/n/1",
         "published_at": "2026-10-28T09:15:00Z"},
    ]
    blob = json.dumps(raw.items)
    assert "SUMMARY-BODY" not in blob and "FULL-BODY-TEXT" not in blob and "Wire" not in blob
    for item in raw.items:
        assert set(item) == {"headline", "link", "published_at"}


def test_yahoo_news_headline_is_capped_at_200_characters_and_whitespace_is_collapsed(monkeypatch, client):
    long_title = "Trent " + "x" * 400
    patch_ticker(monkeypatch, news=[news_item_new(long_title), news_item_new("  Spaced \n out\theadline  ")])
    raw = client.news(TICKER)
    assert len(raw.items[0]["headline"]) == MAX_HEADLINE_CHARS == 200
    assert raw.items[1]["headline"] == "Spaced out headline"


def test_yahoo_news_empty_list_is_empty(monkeypatch, client):
    patch_ticker(monkeypatch, news=[])
    raw = client.news(TICKER)
    assert raw.status == "empty" and raw.items == []


def test_yahoo_news_tolerates_missing_and_renamed_fields(monkeypatch, client):
    items = [
        {"content": {"summary": "no title here", "pubDate": "2026-10-29T04:30:00Z"}},     # headline missing: dropped
        {"content": {"title": "No link, no time"}},                                          # kept, both null
        {"title": "Flat item, odd link", "link": "ftp://example.test/x", "providerPublishTime": "not-a-number"},
        {"content": {"title": "Naive timestamp", "pubDate": "2026-10-29T04:30:00"}},        # no timezone: unknown
        {"content": {"title": "Click-through only", "clickThroughUrl": {"url": "https://example.test/c"},
                     "displayTime": "2026-10-27T03:00:00+05:30"}},
        "not even a mapping",
        {"content": None, "title": "Content is null, flat title wins"},
    ]
    patch_ticker(monkeypatch, news=items)
    raw = client.news(TICKER)
    assert raw.status == "ok"
    got = {i["headline"]: (i["link"], i["published_at"]) for i in raw.items}
    assert got == {
        "No link, no time": (None, None),
        "Flat item, odd link": (None, None),
        "Naive timestamp": (None, None),
        "Click-through only": ("https://example.test/c", "2026-10-26T21:30:00Z"),
        "Content is null, flat title wins": (None, None),
    }
    assert raw.n_unparsed == 2


def test_yahoo_news_with_no_recognisable_headline_is_an_error_not_empty(monkeypatch, client):
    patch_ticker(monkeypatch, news=[{"headlineText": "renamed field"}, {"x": 1}])
    raw = client.news(TICKER)
    assert raw.status == "error" and raw.items == [] and raw.n_unparsed == 2


@pytest.mark.parametrize("payload", [None, {"news": "x"}, "text", 7])
def test_yahoo_news_non_list_is_an_error(monkeypatch, client, payload):
    patch_ticker(monkeypatch, news=payload)
    raw = client.news(TICKER)
    assert raw.status == "error" and raw.items == []


def test_yahoo_news_error_is_reported_never_raised_and_caps_the_item_count(monkeypatch, client):
    def boom():
        raise OSError("connection reset")

    patch_ticker(monkeypatch, news=boom)
    raw = client.news(TICKER)
    assert raw.status == "error" and any("connection reset" in e for e in raw.errors)
    many = [news_item_new(f"Trent headline {i}") for i in range(40)]
    patch_ticker(monkeypatch, news=many)
    raw = client.news(TICKER)
    assert len(raw.items) == yahoo.MAX_NEWS_ITEMS == 10
    assert raw.items[0]["headline"] == "Trent headline 0"


# ------------------------------------------------------------------ entity matching (token boundary)

@pytest.mark.parametrize("text, symbol, names, matched, basis", [
    # R-10: a one-word company name alone is not a match; the symbol written as the symbol is
    ("Trent shares jump 8% after quarterly update", "TRENT", ["Trent Limited"], False, "name_single_token_unconfirmed"),
    ("TRENT shares jump 8% after quarterly update", "TRENT", ["Trent Limited"], True, "symbol_token"),
    ("Tata Motors Passenger Vehicles posts record sales", "TMPV", ["Tata Motors Passenger Vehicles Limited"],
     True, "name_tokens"),
    ("TMPV demerger record date announced", "TMPV", ["Tata Motors Passenger Vehicles Limited"], True, "symbol_token"),
    ("Larsen & Toubro wins order", "LT", ["Larsen & Toubro Limited"], True, "name_tokens"),
    # the wrong-company fixtures
    ("Tata Steel shares rally 6% on demerger report", "TATAMOTORS", ["Tata Motors Limited"], False, "no_token_match"),
    ("Tata Motors Finance loan growth slows", "TMPV", ["Tata Motors Passenger Vehicles Limited"], False,
     "no_token_match"),
    # token boundaries: a longer word is not the entity
    ("Indian Energy Exchanger recalls units", "IEX", ["Indian Energy Exchange Limited"], False, "no_token_match"),
    ("MIEX index gains", "IEX", ["Indian Energy Exchange Limited"], False, "no_token_match"),
    ("Lloyds Enterprises settles", "LLOYDSENT", ["Lloyd Enterprises Limited"], False, "no_token_match"),
    ("Trentham estates sold", "TRENT", ["Trent Limited"], False, "no_token_match"),
    # a symbol must be written as the symbol, so an ordinary word is not a match
    ("A great idea for investors", "IDEA", ["Vodafone Idea Limited"], False, "no_token_match"),
    ("IDEA shares rally on AGR relief", "IDEA", ["Vodafone Idea Limited"], True, "symbol_token"),
    ("Vodafone Idea shares rally", "IDEA", ["Vodafone Idea Limited"], True, "name_tokens"),
    # nothing to match against
    ("Anything at all", "", [], False, "no_token_match"),
    ("Anything at all", None, None, False, "no_token_match"),
    ("", "TRENT", ["Trent Limited"], False, "no_token_match"),
])
def test_entity_match_is_on_token_boundaries(text, symbol, names, matched, basis):
    m = sources.match_entity(text, symbol=symbol, names=names)
    assert (m.matched, m.basis) == (matched, basis)


def test_entity_match_ignores_legal_suffixes_and_refuses_short_single_tokens():
    assert sources.match_entity("Acme Industries Ltd. files results", symbol="ACME",
                                names=["Acme Industries Private Limited"]).matched
    # a two-letter name after stripping ("Co") must not match the ordinary word
    assert not sources.match_entity("Co-operative banks under review", symbol=None, names=["Co Limited"]).matched
    # a name that is only legal suffixes matches nothing
    assert not sources.match_entity("Limited edition", symbol=None, names=["Limited"]).matched


def test_entity_match_name_variants_any_one_suffices():
    names = ["Tata Motors Passenger Vehicles Limited", "Tata Motors Limited"]
    assert sources.match_entity("Tata Motors shares fall", symbol="TMPV", names=names).matched


BOTH_TATA_NAMES = ["Tata Motors Passenger Vehicles Limited", "Tata Motors Limited"]


@pytest.mark.parametrize("text, matched, basis", [
    # the short (former) name is the start of a different company: not a match, and it says why
    ("Tata Motors Finance loan growth slows", False, "name_prefix_ambiguous"),
    ("Tata Motors Finance: demerger of the lending arm", False, "name_prefix_ambiguous"),
    # a legal suffix, punctuation, a lower-case word, a number or a quarter label after the name is the name
    ("Tata Motors Ltd shares fall", True, "name_tokens"),
    ("Tata Motors, Finance minister says", True, "name_tokens"),
    ("Tata Motors: Finance chief quits", True, "name_tokens"),
    ("Tata Motors shares fall", True, "name_tokens"),
    ("Tata Motors 2026 outlook", True, "name_tokens"),
    ("Tata Motors Q2 results beat estimates", True, "name_tokens"),
    ("Tata Motors FY27 guidance", True, "name_tokens"),
    ("Tata Motors", True, "name_tokens"),
    # one clean mention is enough even when another mention is ambiguous
    ("Tata Motors Finance slows while Tata Motors shares rise", True, "name_tokens"),
    # the long name follows the same rule
    ("Tata Motors Passenger Vehicles unit gains", True, "name_tokens"),
    ("Tata Motors Passenger Vehicles Finance unit", False, "name_prefix_ambiguous"),
    # known limit, pinned: a title-case headline with a capitalised word after the name is graded as ambiguous.
    # That loses recall (the headline is kept as context) and never gains a false match.
    ("Tata Motors Reports Record Sales", False, "name_prefix_ambiguous"),
])
def test_entity_match_name_followed_by_a_capitalised_word_may_be_a_longer_company_name(text, matched, basis):
    m = sources.match_entity(text, symbol="TMPV", names=BOTH_TATA_NAMES)
    assert (m.matched, m.basis) == (matched, basis)


def test_entity_match_ambiguous_prefix_still_yields_to_the_symbol():
    m = sources.match_entity("TMPV: Tata Motors Finance stake sale", symbol="TMPV", names=BOTH_TATA_NAMES)
    assert (m.matched, m.basis) == (True, "symbol_token")


# ------------------------------------------------------------------ news -> envelopes

def raw_news(items, status="ok"):
    return yahoo.RawNews(ticker=TICKER, fetched_at=NOON, status=status, items=items, n_unparsed=0, errors=[])


def news_entry(headline, *, link="https://example.test/a", published_at="2026-10-29T04:30:00Z"):
    return {"headline": headline, "link": link, "published_at": published_at}


def test_news_result_builds_valid_entity_checked_news_envelopes():
    items = [news_entry("TRENT shares jump on strong Q2 demand"),
             news_entry("Tata Steel rallies 6% on demerger report", published_at="2026-10-28T10:00:00Z")]
    res = sources.news_result(raw_news(items), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                              clock=FrozenClock(NOON))
    assert res.source == "yahoo_news" and res.status == "ok" and res.supports_rules
    assert len(res.envelopes) == 2
    for env in res.envelopes:
        assert evidence.normalise_envelope(env) == env           # exactly what register() would archive
        assert env["provenance"] == "yahoo_news"
        assert env["source_class"] == "news"                      # never a primary class
        assert env["excerpt"] is None and env["body_sha256"] is None
        assert env["published_at_basis"] == "source" and env["published_at_precision"] == "timestamp"
        assert len(env["headline"]) <= 200 and env["title"] == env["headline"]
        assert env["subject"] == {"isin": ISIN, "ex_date": EX}
        assert env["typed_fields"] == {"ticker": TICKER}
    match, other = res.envelopes
    assert match["entity_match"] is True and match["relevance"] == "entity"
    assert match["relevance_basis"] == "symbol_token"
    assert match["published_at"] == "2026-10-29T04:30:00.000000Z"
    assert match["url"] == "https://example.test/a"
    # a wrong-company headline is kept as context, flagged, and cannot support an entity claim
    assert other["entity_match"] is False and other["relevance"] == "context"
    assert other["relevance_basis"] == "no_token_match"
    # it still carries its demerger keyword, so the engine's negative check sees it (fails safe)
    assert evidence.mentions_demerger(other)


def test_news_result_keeps_a_longer_company_headline_as_context_with_the_ambiguity_named():
    items = [news_entry("Tata Motors Finance loan growth slows, demerger talk"),
             news_entry("Tata Motors shares fall 3% on demand worries")]
    res = sources.news_result(raw_news(items), isin="INE155A01022", ex_date="2026-10-14", symbol="TMPV",
                              names=BOTH_TATA_NAMES, clock=FrozenClock(NOON))
    assert res.status == "ok"
    by_title = {e["title"]: e for e in res.envelopes}
    finance = by_title["Tata Motors Finance loan growth slows, demerger talk"]
    assert finance["entity_match"] is False and finance["relevance"] == "context"
    assert finance["relevance_basis"] == "name_prefix_ambiguous"
    assert finance["source_class"] == "news"
    genuine = by_title["Tata Motors shares fall 3% on demand worries"]
    assert genuine["entity_match"] is True and genuine["relevance"] == "entity"


def test_news_envelope_never_stores_a_body(monkeypatch, client):
    patch_ticker(monkeypatch, news=[news_item_new("Trent shares jump on strong Q2 demand")])
    raw = client.news(TICKER)
    res = sources.news_result(raw, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES, clock=FrozenClock(NOON))
    blob = json.dumps(res.envelopes)
    assert "SUMMARY-BODY" not in blob and "FULL-BODY-TEXT" not in blob
    assert all(e["excerpt"] is None for e in res.envelopes)


def test_news_result_undated_items_stay_undated_and_future_items_are_dropped():
    items = [news_entry("Trent undated note", published_at=None),
             news_entry("Trent from the future", published_at="2026-10-30T07:00:00Z"),     # after NOON
             news_entry("Trent on the dot", published_at="2026-10-30T06:30:00Z")]
    res = sources.news_result(raw_news(items), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                              clock=FrozenClock(NOON))
    by = {e["headline"]: e for e in res.envelopes}
    assert set(by) == {"Trent undated note", "Trent on the dot"}
    undated = by["Trent undated note"]
    assert undated["published_at"] is None and undated["published_at_basis"] is None
    assert undated["published_at_precision"] is None
    assert ("Trent from the future", "published_after_now") in res.dropped


def test_news_result_non_ok_sources_yield_no_envelopes_and_support_nothing():
    for status in ("empty", "error"):
        res = sources.news_result(raw_news([], status=status), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                                  clock=FrozenClock(NOON))
        assert res.status == status and res.envelopes == () and not res.supports_rules


def test_news_result_tolerates_garbage_items_without_raising():
    items = [{"headline": None, "link": None, "published_at": None}, {}, {"headline": 5},
             {"headline": "Trent ok", "link": "x" * 600, "published_at": "garbage"}]
    res = sources.news_result(raw_news(items), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                              clock=FrozenClock(NOON))
    assert res.status == "ok" and [e["headline"] for e in res.envelopes] == ["Trent ok"]
    env = res.envelopes[0]
    assert env["url"] is None and env["published_at"] is None          # unusable values become null, never defaulted
    assert len(res.dropped) == 3


def test_news_result_bad_subject_is_an_error_result_not_an_exception():
    res = sources.news_result(raw_news([news_entry("Trent ok")]), isin="bad", ex_date=EX, symbol="TRENT",
                              names=NAMES, clock=FrozenClock(NOON))
    assert res.status == "error" and res.envelopes == () and not res.supports_rules


def test_news_envelopes_register_with_the_clock_time_and_reuse_on_a_second_collect(run_ctx):
    raw = raw_news([news_entry("Trent shares jump on strong Q2 demand")])
    res = sources.news_result(raw, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES, clock=run_ctx.clock)
    first = [evidence.register(run_ctx, env) for env in res.envelopes]
    assert [r.reused for r in first] == [False]
    assert first[0].captured_at == run_ctx.clock.iso()
    again = sources.news_result(raw, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES, clock=run_ctx.clock)
    second = [evidence.register(run_ctx, env) for env in again.envelopes]
    assert [(r.capture_id, r.reused) for r in second] == [(first[0].capture_id, True)]
    n = run_ctx.conn.execute("SELECT count(*) FROM captures WHERE kind = 'triage_doc'").fetchone()[0]
    assert n == 1
    row = evidence.get_capture(run_ctx.conn, first[0].capture_id)
    ok, reason = pit.admissible(evidence.load(row), row, CUTOFF, decisive=True)
    assert (ok, reason) == (True, "ok")


# ------------------------------------------------------------------ actions -> envelope

def raw_actions(splits=None, dividends=None, status="ok"):
    return yahoo.RawActions(ticker=TICKER, fetched_at=NOON, status=status, splits=splits or {},
                            dividends=dividends or {}, errors=[])


def test_actions_result_window_nearest_first_and_undated():
    splits = {"2024-01-01": 5.0, "2026-06-04": 1.5, "2026-10-01": 2.0, "2026-10-28": 4.0, "2020-01-01": 10.0}
    dividends = {"2026-10-27": 2.5, "2026-10-01": 9.0, "2026-10-31": 1.0, "2026-10-30": 3.0}
    res = sources.actions_result(raw_actions(splits, dividends), isin=ISIN, ex_date=EX)
    assert res.source == "yahoo_actions" and res.status == "ok" and res.supports_rules
    (env,) = res.envelopes
    assert evidence.normalise_envelope(env) == env
    assert env["provenance"] == "yahoo_actions" and env["source_class"] == "other"
    assert env["published_at"] is None and env["published_at_basis"] is None      # no publication time is claimed
    assert env["entity_match"] is None and env["excerpt"] is None and env["headline"] is None
    tf = env["typed_fields"]
    assert tf["ticker"] == TICKER and tf["window_days"] == sources.ACTIONS_WINDOW_DAYS == 400
    assert tf["n_splits"] == 3                                  # 2020 is outside +-400 days of 2026-10-29
    assert (tf["split_1_date"], tf["split_1_ratio"]) == ("2026-10-28", 4.0)
    assert (tf["split_2_date"], tf["split_2_ratio"]) == ("2026-10-01", 2.0)
    assert (tf["split_3_date"], tf["split_3_ratio"]) == ("2026-06-04", 1.5)
    assert tf["splits_truncated"] is False
    # dividends: only within 5 days of the ex-date, nearest first
    assert tf["n_dividends_pm5d"] == 3
    assert (tf["dividend_1_date"], tf["dividend_1_amount"]) == ("2026-10-30", 3.0)
    assert (tf["dividend_2_date"], tf["dividend_2_amount"]) == ("2026-10-27", 2.5)     # tie on distance: earlier date
    assert "dividend_3_date" not in tf


def test_actions_result_truncates_to_three_splits_and_says_so():
    splits = {f"2026-0{m}-15": float(m + 1) for m in range(1, 7)}           # 6 splits, all in window
    (env,) = sources.actions_result(raw_actions(splits), isin=ISIN, ex_date=EX).envelopes
    tf = env["typed_fields"]
    assert tf["n_splits"] == 6 and tf["splits_truncated"] is True
    assert "split_4_date" not in tf and len(env["typed_fields"]) <= 16


def test_actions_result_ok_with_no_split_in_window_is_a_recorded_zero_not_a_missing_source():
    res = sources.actions_result(raw_actions({"2010-01-01": 2.0}, {}), isin=ISIN, ex_date=EX)
    assert res.status == "ok" and len(res.envelopes) == 1
    tf = res.envelopes[0]["typed_fields"]
    assert tf["n_splits"] == 0 and tf["n_dividends_pm5d"] == 0 and "split_1_date" not in tf


def test_actions_result_non_ok_sources_yield_no_envelopes_and_support_nothing():
    for status in ("empty", "error"):
        res = sources.actions_result(raw_actions(status=status), isin=ISIN, ex_date=EX)
        assert res.status == status and res.envelopes == () and not res.supports_rules


def test_actions_envelope_is_identical_on_a_second_collect():
    a = sources.actions_result(raw_actions({"2026-10-28": 4.0}), isin=ISIN, ex_date=EX).envelopes
    b = sources.actions_result(raw_actions({"2026-10-28": 4.0}), isin=ISIN, ex_date=EX).envelopes
    assert canonical_bytes(a[0]) == canonical_bytes(b[0])


def test_actions_result_bad_dates_do_not_raise():
    res = sources.actions_result(raw_actions({"not-a-date": 2.0, "2026-10-28": 4.0}), isin=ISIN, ex_date=EX)
    assert res.status == "ok" and res.envelopes[0]["typed_fields"]["n_splits"] == 1
    res = sources.actions_result(raw_actions({"2026-10-28": 4.0}), isin=ISIN, ex_date="bad")
    assert res.status == "error" and res.envelopes == ()


# ------------------------------------------------------------------ collect_yahoo

def test_collect_yahoo_runs_both_fetches_through_the_client_and_labels_each_source(monkeypatch, client, sleeps):
    patch_ticker(monkeypatch,
                 actions=actions_frame([("2026-10-28", 0.0, 4.0)]),
                 news=[news_item_new("Trent shares jump on strong Q2 demand")])
    results = sources.collect_yahoo(client, ticker=TICKER, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES)
    assert [r.source for r in results] == ["yahoo_actions", "yahoo_news"]
    assert [r.status for r in results] == ["ok", "ok"]
    assert sleeps == [0.5, 0.5] and min(sleeps) >= 0.5
    assert len(results[0].envelopes) == 1 and len(results[1].envelopes) == 1


def test_collect_yahoo_without_a_ticker_is_skipped_and_touches_nothing(monkeypatch, client, sleeps):
    fake = patch_ticker(monkeypatch)
    for ticker in (None, "", "   "):
        results = sources.collect_yahoo(client, ticker=ticker, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES)
        assert [(r.source, r.status) for r in results] == [("yahoo_actions", "skipped"), ("yahoo_news", "skipped")]
        assert all(r.envelopes == () and not r.supports_rules for r in results)
    assert fake.calls == [] and sleeps == []


def test_collect_yahoo_never_raises_when_the_client_itself_misbehaves():
    class BadClient:
        def actions(self, ticker):
            raise RuntimeError("client exploded")

        def news(self, ticker):
            return object()            # not a RawNews

    results = sources.collect_yahoo(BadClient(), ticker=TICKER, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES)
    assert [(r.source, r.status) for r in results] == [("yahoo_actions", "error"), ("yahoo_news", "error")]
    assert all(r.envelopes == () and not r.supports_rules for r in results)
    assert "client exploded" in results[0].detail


def test_a_source_that_did_not_answer_never_supports_a_rule():
    for status in ("empty", "error", "skipped"):
        res = sources.SourceResult(source="yahoo_news", status=status, detail="x", envelopes=(), dropped=())
        assert res.supports_rules is False
    with pytest.raises(ValueError):
        sources.SourceResult(source="yahoo_news", status="healthy", detail="", envelopes=(), dropped=())
    # an "ok" claim with nothing behind it is refused too: ok means there is something to cite
    with pytest.raises(ValueError):
        sources.SourceResult(source="yahoo_news", status="ok", detail="", envelopes=(), dropped=())
    # a non-ok result may not carry envelopes
    with pytest.raises(ValueError):
        sources.SourceResult(source="yahoo_news", status="error", detail="", envelopes=({"a": 1},), dropped=())


def test_source_status_map_for_the_pack_keeps_every_source_and_its_state():
    ok = sources.actions_result(raw_actions({"2026-10-28": 4.0}), isin=ISIN, ex_date=EX)
    empty = sources.news_result(raw_news([], status="empty"), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                                clock=FrozenClock(NOON))
    skipped = one_feed(feeds=(), fetch=None, isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES)
    status_map = sources.source_status_map([ok, empty, skipped])
    assert {k: v["status"] for k, v in status_map.items()} == {
        "yahoo_actions": "ok", "yahoo_news": "empty", "feeds": "skipped"}
    assert status_map["yahoo_actions"]["n_envelopes"] == 1
    assert status_map["yahoo_news"]["n_envelopes"] == 0
    assert all(v["status"] in SOURCE_STATUSES for v in status_map.values())


# ------------------------------------------------------------------ RSS feeds

# Five items copied verbatim from the real https://www.sebi.gov.in/sebirss.xml (fetched 2026-09-30/10-01).
# The item pubDate is date-only ("30 Sep, 2026 +0530"); only the channel pubDate carries a time.
SEBI_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?><rss version="2.0"><channel><ttl>60</ttl>'
    "<title>SEBI RSS Feed</title><description>SEBI RSS Feed</description>"
    "<link>https://www.sebi.gov.in/sebirss.xml</link><lastBuildDate>30 Sep 2026 22:00:02</lastBuildDate>"
    "<pubDate>30 Sep 2026 22:00:02 +0530</pubDate>"
    "<item><title>Final Order in the matter of Eqwires Research Analyst</title>"
    "<description>Final Order in the matter of Eqwires Research Analyst</description>"
    "<link>https://www.sebi.gov.in/enforcement/orders/sep-2026/final-order-in-the-matter-of-eqwires-research-analyst_104841.html</link>"
    "<pubDate>30 Sep, 2026 +0530</pubDate></item>"
    "<item><title>Settlement Order in the matter of Lloyd Enterprises Limited</title>"
    "<description>Settlement Order in the matter of Lloyd Enterprises Limited</description>"
    "<link>https://www.sebi.gov.in/enforcement/orders/sep-2026/settlement-order-in-the-matter-of-lloyd-enterprises-limited_104811.html</link>"
    "<pubDate>29 Sep, 2026 +0530</pubDate></item>"
    "<item><title>\nAdjudication Order in the matter of Shares Bazaar Pvt. Ltd. - Research Analyst"
    "                                              </title>"
    "<description>\nAdjudication Order in the matter of Shares Bazaar Pvt. Ltd. - Research Analyst"
    "                                              </description>"
    "<link>https://www.sebi.gov.in/enforcement/orders/sep-2026/adjudication-order-in-the-matter-of-shares-bazaar-pvt-ltd-research-analyst_104810.html</link>"
    "<pubDate>29 Sep, 2026 +0530</pubDate></item>"
    "<item><title>Remittance Order dated September 29, 2026 issued under RC No. 9222 of 2026 drawn against Ankit "
    "Ajitbhai Panchal in the matter of Trading activities of certain entities in the scrip of DU Digital "
    "Technologies Limited (now DU Digital Global Limited)</title>"
    "<description>Remittance Order dated September 29, 2026 issued under RC No. 9222 of 2026 drawn against Ankit "
    "Ajitbhai Panchal in the matter of Trading activities of certain entities in the scrip of DU Digital "
    "Technologies Limited (now DU Digital Global Limited)</description>"
    "<link>https://www.sebi.gov.in/enforcement/recovery-proceedings/sep-2026/remittance-order-dated-september-29-2026-issued-under-rc-no-9222-of-2026-drawn-against-ankit-ajitbhai-panchal-in-the-matter-of-trading-activities-of-certain-entities-in-the-scrip-of-du-digital-techn-_104807.html</link>"
    "<pubDate>29 Sep, 2026 +0530</pubDate></item>"
    "<item><title>Summary Settlement Order in the matter of Harbourfront India trust AIF</title>"
    "<description>Summary Settlement Order in the matter of Harbourfront India trust AIF</description>"
    "<link>https://www.sebi.gov.in/enforcement/orders/sep-2026/summary-settlement-order-in-the-matter-of-harbourfront-india-trust-aif_104764.html</link>"
    "<pubDate>28 Sep, 2026 +0530</pubDate></item>"
    "</channel></rss>"
)
SEBI = sources.FeedSpec(name="sebi_rss", url="https://www.sebi.gov.in/sebirss.xml", source_class="regulator")
LLOYD = dict(isin="INE093R01011", ex_date="2026-09-29", symbol="LLOYDSENT", names=["Lloyd Enterprises Limited"])


def one_feed(**kw):
    """collect_feeds returns one result per feed; every test below configures exactly one."""
    results = sources.collect_feeds(**kw)
    assert len(results) == 1
    return results[0]


def test_feeds_ship_empty_and_an_empty_feed_list_is_skipped_without_a_fetch():
    assert sources.FEEDS == ()
    calls = []

    def fetch(url):
        calls.append(url)
        return SEBI_XML

    res = one_feed(feeds=sources.FEEDS, fetch=fetch, **LLOYD)
    assert (res.source, res.status) == ("feeds", "skipped") and not res.supports_rules
    assert res.envelopes == () and calls == []
    # the module default is the shipped constant
    assert one_feed(fetch=fetch, **LLOYD).status == "skipped" and calls == []


def test_sebi_fixture_parses_with_date_only_precision_and_normalised_titles():
    parsed = sources.parse_rss(SEBI_XML)
    assert parsed.status == "ok" and len(parsed.items) == 5
    first = parsed.items[0]
    assert first.title == "Final Order in the matter of Eqwires Research Analyst"
    assert first.published_at == "2026-09-30" and first.precision == "date"    # the channel's time is not borrowed
    assert first.link.endswith("_104841.html")
    padded = parsed.items[2]
    assert padded.title == "Adjudication Order in the matter of Shares Bazaar Pvt. Ltd. - Research Analyst"
    assert [i.published_at for i in parsed.items] == ["2026-09-30", "2026-09-29", "2026-09-29", "2026-09-29",
                                                      "2026-09-28"]
    assert {i.precision for i in parsed.items} == {"date"}


@pytest.mark.parametrize("pub, expected", [
    ("30 Sep, 2026 +0530", ("2026-09-30", "date")),
    ("Wed, 30 Sep 2026 +0530", ("2026-09-30", "date")),
    ("30 Sep 2026", ("2026-09-30", "date")),
    ("30 Sep 2026 22:00:02 +0530", ("2026-09-30T16:30:02.000000Z", "timestamp")),
    ("Wed, 30 Sep 2026 22:00:02 GMT", ("2026-09-30T22:00:02.000000Z", "timestamp")),
    ("30 Sep 2026 22:00:02", (None, None)),             # a time without a zone is ambiguous: unknown
    ("31 Feb 2026 +0530", (None, None)),
    ("yesterday", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_feed_pubdate_parsing_never_guesses(pub, expected):
    assert sources.parse_pubdate(pub) == expected


def test_feed_parse_failures_are_statuses_not_exceptions():
    assert sources.parse_rss(b"<rss><channel>").status == "error"               # truncated xml
    assert sources.parse_rss("not xml at all").status == "error"
    assert sources.parse_rss(b"").status == "error"
    assert sources.parse_rss(b"<html><body>404</body></html>").status == "error"     # not RSS
    assert sources.parse_rss(b"<rss><channel><title>t</title></channel></rss>").status == "empty"
    bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    res = sources.parse_rss(bomb)
    assert res.status == "error" and "dtd" in res.detail.lower()
    assert sources.parse_rss(b"<rss><channel>" + b"<item/>" * 10 + b"</channel></rss>" + b" " * 3_000_000).status == "error"


def test_feed_items_missing_fields_are_tolerated():
    xml = ("<rss><channel>"
           "<item><title>No date here</title><link>https://example.test/1</link></item>"
           "<item><link>https://example.test/2</link><pubDate>30 Sep, 2026 +0530</pubDate></item>"      # no title
           "<item><title>Odd date</title><pubDate>someday</pubDate></item>"
           "<item><title>Bad link</title><link>javascript:alert(1)</link><pubDate>30 Sep, 2026 +0530</pubDate></item>"
           "</channel></rss>")
    parsed = sources.parse_rss(xml)
    assert parsed.status == "ok"
    got = {i.title: (i.link, i.published_at, i.precision) for i in parsed.items}
    assert got == {"No date here": ("https://example.test/1", None, None), "Odd date": (None, None, None),
                   "Bad link": (None, "2026-09-30", "date")}
    assert parsed.n_unparsed == 1


def test_feed_evidence_keeps_only_entity_matching_items_and_passes_the_point_in_time_check():
    res = one_feed(feeds=(SEBI,), fetch=lambda url: SEBI_XML, **LLOYD)
    assert (res.source, res.status) == ("sebi_rss", "ok") and res.supports_rules
    (env,) = res.envelopes
    assert evidence.normalise_envelope(env) == env
    assert env["provenance"] == "sebi_rss" and env["source_class"] == "regulator"
    assert env["title"] == "Settlement Order in the matter of Lloyd Enterprises Limited"
    assert env["published_at"] == "2026-09-29" and env["published_at_precision"] == "date"
    assert env["published_at_basis"] == "source"
    assert env["entity_match"] is True and env["relevance"] == "entity"
    assert env["url"].endswith("_104811.html") and env["excerpt"] is None and env["body_sha256"] is None
    assert env["typed_fields"] == {"feed": "sebi_rss"}
    assert res.dropped == () and "1 of 5" in res.detail           # unrelated items are counted, not archived
    # date-only publication compares at date level against the capture and the cutoff
    row = {"kind": "triage_doc", "captured_at": "2026-09-30T12:00:00.000000Z",
           "sha256": sha256_hex(canonical_bytes(env))}
    assert pit.admissible(env, row, "2026-09-30T18:29:59.999999Z", decisive=True) == (True, "ok")
    early = dict(row, captured_at="2026-09-28T12:00:00.000000Z")
    assert pit.admissible(env, early, "2026-09-30T18:29:59.999999Z")[0] is False      # published after capture


def test_feed_evidence_matches_the_renamed_company_in_a_parenthesis_and_rejects_near_names():
    hit = one_feed(feeds=(SEBI,), fetch=lambda url: SEBI_XML, isin="INE0NOT00001",
                                ex_date="2026-09-29", symbol="DUDIGITAL", names=["DU Digital Global Limited"])
    assert hit.status == "ok" and len(hit.envelopes) == 1
    assert hit.envelopes[0]["title"].startswith("Remittance Order dated September 29, 2026")
    near = one_feed(feeds=(SEBI,), fetch=lambda url: SEBI_XML, isin="INE0NOT00001",
                                 ex_date="2026-09-29", symbol="LLOYDSENT", names=["Lloyds Enterprises Limited"])
    assert near.status == "empty" and near.envelopes == () and not near.supports_rules     # "Lloyds" is not "Lloyd"


def test_feed_fetch_problems_are_error_results_and_never_raise():
    def down(url):
        raise OSError("connection refused")

    res = one_feed(feeds=(SEBI,), fetch=down, **LLOYD)
    assert res.status == "error" and "connection refused" in res.detail
    assert res.envelopes == () and not res.supports_rules
    res = one_feed(feeds=(SEBI,), fetch=lambda url: "<html/>", **LLOYD)
    assert res.status == "error"
    res = one_feed(feeds=(SEBI,), fetch=lambda url: 12345, **LLOYD)          # wrong type from the fetcher
    assert res.status == "error"


def test_feed_spec_is_validated_before_any_fetch():
    calls = []
    bad = sources.FeedSpec(name="x", url="file:///etc/passwd", source_class="regulator")
    res = one_feed(feeds=(bad,), fetch=lambda u: calls.append(u) or SEBI_XML, **LLOYD)
    assert res.status == "error" and calls == []
    bad_class = sources.FeedSpec(name="x", url="https://example.test/f.xml", source_class="primary")
    assert one_feed(feeds=(bad_class,), fetch=lambda u: SEBI_XML, **LLOYD).status == "error"
    # policy: no automated access to the IRDAI (robots.txt disallows all), NSE or BSE sites
    for url in ("https://irdai.gov.in/rss.xml", "https://www.nseindia.com/feed", "https://www.bseindia.com/x.xml",
                "https://IRDAI.gov.in/x"):
        spec = sources.FeedSpec(name="x", url=url, source_class="regulator")
        assert one_feed(feeds=(spec,), fetch=lambda u: calls.append(u) or SEBI_XML, **LLOYD).status == "error"
    assert calls == []
    # two feeds give two results, one per feed, and a failing feed does not hide a working one
    def fetch(url):
        if "other" in url:
            raise OSError("down")
        return SEBI_XML

    other = sources.FeedSpec(name="other", url="https://other.example.test/f.xml", source_class="regulator")
    results = sources.collect_feeds(feeds=(SEBI, other), fetch=fetch, **LLOYD)
    assert [(r.source, r.status) for r in results] == [("sebi_rss", "ok"), ("other", "error")]


def test_feed_spec_name_must_match_the_one_provenance_value_the_code_can_label():
    calls = []
    other = sources.FeedSpec(name="other", url="https://other.example.test/f.xml", source_class="regulator")
    res = one_feed(feeds=(other,), fetch=lambda u: calls.append(u) or SEBI_XML, **LLOYD)
    assert (res.source, res.status) == ("other", "error") and not res.supports_rules
    assert "sebi_rss" in res.detail and calls == []       # refused before any fetch, never mislabelled as SEBI


def test_feed_redirects_to_a_blocked_host_or_a_plain_http_url_are_refused():
    handler = sources._CheckedRedirect()
    req = urllib.request.Request("https://www.sebi.gov.in/sebirss.xml")
    for target in ("https://www.nseindia.com/feed", "https://irdai.gov.in/x", "http://www.sebi.gov.in/x",
                   "file:///etc/passwd", "https://user:pw@www.sebi.gov.in/x"):
        with pytest.raises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, target)
    followed = handler.redirect_request(req, None, 302, "Found", {}, "https://www.sebi.gov.in/elsewhere.xml")
    assert followed is not None and followed.full_url == "https://www.sebi.gov.in/elsewhere.xml"


def test_feed_that_is_not_utf8_is_refused_so_the_dtd_check_cannot_be_bypassed_by_encoding():
    bomb = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
            '<rss><channel><item><title>&a;</title></item></channel></rss>')
    for encoded in (bomb.encode("utf-16"), bomb.encode("utf-16-le"), bomb.encode("utf-16-be")):
        res = sources.parse_rss(encoded)
        assert res.status == "error" and res.items == () and "utf-8" in res.detail.lower()
    assert sources.parse_rss(b"\xff\xfe\x00\x00").status == "error"
    # a UTF-8 document, with or without a byte order mark, still parses
    plain = "<rss><channel><item><title>Caf\u00e9 order</title></item></channel></rss>"
    assert sources.parse_rss(plain.encode("utf-8")).items[0].title == "Caf\u00e9 order"
    assert sources.parse_rss(b"\xef\xbb\xbf" + plain.encode("utf-8")).status == "ok"


def test_feed_default_fetch_is_not_used_when_a_fetcher_is_given_and_is_https_only():
    assert callable(sources.http_fetch)
    with pytest.raises(ValueError):
        sources.http_fetch("file:///etc/passwd")
    with pytest.raises(ValueError):
        sources.http_fetch("ftp://example.test/x")
    with pytest.raises(ValueError):
        sources.http_fetch("https://irdai.gov.in/robots.txt")


# ------------------------------------------------------------------ module hygiene

def test_sources_module_stays_inside_the_triage_rules():
    path = Path(sources.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(m.startswith(("quant.factors", "quant.model", "quant.data.yahoo", "yfinance", "requests"))
                   for m in imported)


# ------------------------------------------------------------------ live (excluded by default)

@pytest.mark.network
@pytest.mark.skipif(os.environ.get("QUANT_NETWORK") != "1", reason="set QUANT_NETWORK=1 to call Yahoo")
def test_yahoo_live_actions_and_news_for_one_indian_ticker_return_a_status_and_respect_the_caps(cfg):
    import time

    clock = FrozenClock(datetime.now(timezone.utc))
    client = YahooClient(cfg, clock, time.sleep)
    results = sources.collect_yahoo(client, ticker="RELIANCE.NS", isin="INE002A01018", ex_date=EX,
                                    symbol="RELIANCE", names=["Reliance Industries Limited"])
    assert [r.source for r in results] == ["yahoo_actions", "yahoo_news"]
    for r in results:
        assert r.status in SOURCE_STATUSES          # behaviour for .NS tickers is unverified: any status is valid
        assert (not r.supports_rules) or r.status == "ok"      # ok is necessary, not sufficient (R-9)
        for env in r.envelopes:
            assert evidence.normalise_envelope(env) == env
            assert env["headline"] is None or len(env["headline"]) <= MAX_HEADLINE_CHARS
            assert env["excerpt"] is None


# ------------------------------------------------------------------ R-10: a single distinctive name token needs a second signal

TITAN_NAMES = ["Titan Company Limited"]
DELTA_NAMES = ["Delta Corp Limited"]


@pytest.mark.parametrize("text, symbol, names, related, matched, basis", [
    # the ruling's two named false positives
    ("Titan submersible implodes near the Titanic wreck", "TITAN", TITAN_NAMES, None, False,
     "name_single_token_unconfirmed"),
    ("Delta Air Lines posts record quarterly profit", "DELTACORP", DELTA_NAMES, None, False,
     "name_single_token_unconfirmed"),
    # a related-ticker list that does not hold this ticker is not a second signal
    ("Titan submersible implodes near the Titanic wreck", "TITAN", TITAN_NAMES, ["OCEANGATE", "BA"], False,
     "name_single_token_unconfirmed"),
    ("Delta Air Lines posts record quarterly profit", "DELTACORP", DELTA_NAMES, ["DAL"], False,
     "name_single_token_unconfirmed"),
    # second signal one: Yahoo relatedTickers holds the ticker (any case)
    ("Titan shares rally after jewellery demand beats estimates", "TITAN", TITAN_NAMES, ["TITAN.NS"], True,
     "name_single_token+related_ticker"),
    ("Delta Corp shares fall on tax notice", "DELTACORP", DELTA_NAMES, ["deltacorp.ns", "X"], True,
     "name_single_token+related_ticker"),
    # second signal two: the NSE symbol at a token boundary, written as the symbol
    ("TITAN shares rally after jewellery demand beats estimates", "TITAN", TITAN_NAMES, None, True, "symbol_token"),
    ("DELTACORP slumps on show-cause notice", "DELTACORP", DELTA_NAMES, None, True, "symbol_token"),
    # the symbol must be a whole token: TITANIUM is not TITAN
    ("TITANIUM prices climb", "TITAN", TITAN_NAMES, None, False, "no_token_match"),
    # related tickers alone, with no name in the text, name nothing
    ("Jewellery demand beats estimates", "TITAN", TITAN_NAMES, ["TITAN.NS"], False, "no_token_match"),
])
def test_a_single_distinctive_name_token_matches_only_with_a_second_signal(text, symbol, names, related, matched, basis):
    m = sources.match_entity(text, symbol=symbol, names=names, ticker=f"{symbol}.NS", related_tickers=related)
    assert (m.matched, m.basis) == (matched, basis)


@pytest.mark.parametrize("text", [
    "Passenger Vehicles demand rises",             # one of two distinctive tokens
    "Tata Motors shares fall on weak demand",      # the other one
    "Passenger Tata Vehicles Motors",              # all tokens present, not as the name
])
def test_a_multi_token_name_needs_every_distinctive_token(text):
    names = ["Tata Motors Passenger Vehicles Limited"]
    assert not sources.match_entity(text, symbol=None, names=names).matched
    assert not sources.match_entity(text, symbol=None, names=names, ticker="TMPV.NS",
                                    related_tickers=["TMPV.NS"]).matched          # a related ticker does not loosen it


def test_a_multi_token_name_needs_no_second_signal():
    m = sources.match_entity("Tata Motors Passenger Vehicles posts record sales", symbol=None,
                             names=["Tata Motors Passenger Vehicles Limited"])
    assert (m.matched, m.basis) == (True, "name_tokens")


def test_news_result_reads_related_tickers_per_item_and_grades_the_unconfirmed_headline_as_context():
    items = [{**news_entry("Titan submersible implodes near the Titanic wreck"), "related_tickers": ["OCEANGATE"]},
             {**news_entry("Titan shares rally on jewellery demand"), "related_tickers": ["TITAN.NS", "KALYANKJIL.NS"]},
             news_entry("Titan Q2 preview", published_at="2026-10-28T10:00:00Z")]
    raw = yahoo.RawNews(ticker="TITAN.NS", fetched_at=NOON, status="ok", items=items, n_unparsed=0, errors=[])
    res = sources.news_result(raw, isin="INE280A01028", ex_date=EX, symbol="TITAN", names=TITAN_NAMES,
                              clock=FrozenClock(NOON))
    by = {e["headline"]: e for e in res.envelopes}
    wreck = by["Titan submersible implodes near the Titanic wreck"]
    assert (wreck["entity_match"], wreck["relevance"], wreck["relevance_basis"]) == (
        False, "context", "name_single_token_unconfirmed")
    rally = by["Titan shares rally on jewellery demand"]
    assert (rally["entity_match"], rally["relevance"], rally["relevance_basis"]) == (
        True, "entity", "name_single_token+related_ticker")
    assert by["Titan Q2 preview"]["entity_match"] is False                       # no related tickers given: unconfirmed
    assert "related_tickers" not in json.dumps(res.envelopes)                    # the list is read, never archived


def test_a_garbage_related_tickers_value_is_ignored_not_trusted():
    for bad in ("TITAN.NS", 5, {"TITAN.NS": 1}, [None, 7]):
        items = [{**news_entry("Titan shares rally on jewellery demand"), "related_tickers": bad}]
        raw = yahoo.RawNews(ticker="TITAN.NS", fetched_at=NOON, status="ok", items=items, n_unparsed=0, errors=[])
        res = sources.news_result(raw, isin="INE280A01028", ex_date=EX, symbol="TITAN", names=TITAN_NAMES,
                                  clock=FrozenClock(NOON))
        assert res.envelopes[0]["entity_match"] is False


# ------------------------------------------------------------------ R-9: ok with nothing relevant does not support a rule

def test_ok_news_with_no_entity_match_has_status_ok_but_supports_no_rule():
    items = [news_entry("Tata Steel rallies 6% on demerger report"), news_entry("Titan submersible found")]
    res = sources.news_result(raw_news(items), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                              clock=FrozenClock(NOON))
    assert res.status == "ok" and len(res.envelopes) == 2
    assert all(e["entity_match"] is False for e in res.envelopes)
    assert res.supports_rules is False


def test_ok_news_with_one_entity_match_supports_rules():
    items = [news_entry("Tata Steel rallies 6% on demerger report"), news_entry("TRENT shares jump 8%")]
    res = sources.news_result(raw_news(items), isin=ISIN, ex_date=EX, symbol="TRENT", names=NAMES,
                              clock=FrozenClock(NOON))
    assert res.status == "ok" and res.supports_rules is True


def test_ok_actions_with_nothing_in_either_window_has_status_ok_but_supports_no_rule():
    res = sources.actions_result(raw_actions({"2010-01-01": 2.0}, {"2020-01-01": 1.0}), isin=ISIN, ex_date=EX)
    assert res.status == "ok" and len(res.envelopes) == 1
    assert res.envelopes[0]["typed_fields"]["n_splits"] == 0 and res.envelopes[0]["typed_fields"]["n_dividends_pm5d"] == 0
    assert res.supports_rules is False


@pytest.mark.parametrize("splits, dividends", [({"2026-10-28": 4.0}, {}), ({}, {"2026-10-27": 2.5}),
                                                ({"2026-06-04": 1.5}, {})])
def test_ok_actions_carrying_a_split_or_a_nearby_dividend_supports_rules(splits, dividends):
    res = sources.actions_result(raw_actions(splits, dividends), isin=ISIN, ex_date=EX)
    assert res.status == "ok" and res.supports_rules is True


def test_supports_rules_is_judged_on_the_envelopes_not_on_the_status_alone():
    news_env = sources.news_result(raw_news([news_entry("Tata Steel rallies on demerger report")]), isin=ISIN,
                                   ex_date=EX, symbol="TRENT", names=NAMES, clock=FrozenClock(NOON)).envelopes
    bare = sources.SourceResult("yahoo_news", "ok", "x", news_env, ())
    assert bare.status == "ok" and bare.supports_rules is False
    matched = dict(news_env[0], entity_match=True)
    assert sources.SourceResult("yahoo_news", "ok", "x", (matched,), ()).supports_rules is True
    # entity_match must be exactly True: null (unknown) is not a match
    assert sources.SourceResult("yahoo_news", "ok", "x", (dict(news_env[0], entity_match=None),), ()).supports_rules is False
