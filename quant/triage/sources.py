"""Evidence sources for triage: Yahoo actions and news, an optional RSS feed, and the entity match.

Every source answers with a :class:`SourceResult` whose ``status`` is one of ``ok``, ``empty``,
``error`` or ``skipped`` (``types.SOURCE_STATUSES``). Only ``ok`` carries envelopes. ``ok`` is
necessary for ``supports_rules`` and not sufficient: news must also name the entity, and actions must
carry a split or a nearby dividend. The other three statuses say nothing about the world: ``empty`` is
not "no such action", ``error`` is not "no news" and ``skipped`` is not "checked". A data-shape problem
(a missing or renamed field, an unreadable feed) becomes a status, never an exception, so one bad source
cannot stop a collect run.

The envelopes built here are already validated by ``evidence.normalise_envelope``. Archiving them is
the caller's job (``evidence.register``, which stamps ``captured_at`` from the run clock), so nothing
in this module carries or chooses a capture time. The ``clock`` argument of :func:`news_result` is
used only to refuse a headline dated after the moment of the fetch.

Policy (design section 3): Yahoo news keeps a headline (at most 200 characters), a link and the
provider's timestamp, never a body. The feed list ships empty. Automated access to the IRDAI
(robots.txt disallows everything), NSE and BSE sites is refused in code (``BLOCKED_HOSTS``).
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from quant.errors import Refused
from quant.triage import evidence, pit
from quant.triage.types import (
    ISIN_RE, MAX_HEADLINE_CHARS, MAX_TEXT_CHARS, SOURCE_CLASSES, SOURCE_STATUSES, canonical_bytes, is_number,
    valid_date,
)

# ------------------------------------------------------------------ constants

#: Yahoo splits are listed when within this many calendar days of the ex-date. It is wide on purpose: in
#: the recorded cases a vendor split sits 64 to 277 days from the jump it really explains.
ACTIONS_WINDOW_DAYS = 400
#: Dividends are listed only this close to the ex-date (a cash payout can look like a drop).
DIVIDEND_WINDOW_DAYS = 5
MAX_SPLITS_LISTED = 3            # nearest first; ``splits_truncated`` says when more were in the window
MAX_DIVIDENDS_LISTED = 2
MAX_NEWS_ENVELOPES = 10
MAX_TITLE_CHARS = 300            # the envelope's title cap
MAX_LINK_CHARS = 500             # the envelope's url cap
MAX_FEED_BYTES = 2_000_000
MAX_FEED_ITEMS = 500
FETCH_TIMEOUT_S = 20
USER_AGENT = "quant-triage/1"

#: Hosts the engine never fetches from: IRDAI (robots.txt is ``Disallow: /`` for every agent), NSE and BSE
#: (terms not verified; design section 3). A human attaches documents from these sites by hand.
BLOCKED_HOSTS = ("irdai.gov.in", "nseindia.com", "bseindia.com")

PROVENANCE_ACTIONS = "yahoo_actions"
PROVENANCE_NEWS = "yahoo_news"
PROVENANCE_FEED = "sebi_rss"      # the closed provenance set has one feed value; load_feeds refuses other names

_FEED_NAME_RE = re.compile(r"^[a-z0-9_]{1,30}$")


@dataclass(frozen=True)
class FeedSpec:
    """One RSS 2.0 feed the engine may poll. ``source_class`` is how its items are graded."""
    name: str
    url: str
    source_class: str


#: Ships empty. Enabling a feed is a code edit, made only after ``docs/operations/triage_sources.md``
#: records its URL, the date checked and a verdict on its terms. The SEBI feed is described there.
FEEDS: Tuple[FeedSpec, ...] = ()


# ------------------------------------------------------------------ result type

@dataclass(frozen=True)
class SourceResult:
    """What one source said about one anomaly.

    ``ok`` means there is something to cite (``envelopes`` is not empty); every other status carries
    no envelope. ``dropped`` lists ``(what, reason)`` for items that were read but not archived.
    ``supports_rules`` is stricter: see :func:`_envelope_supports_rules`.
    """
    source: str
    status: str
    detail: str
    envelopes: Tuple[Dict[str, Any], ...]
    dropped: Tuple[Tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.status not in SOURCE_STATUSES:
            raise ValueError(f"status {self.status!r} not in {list(SOURCE_STATUSES)}")
        object.__setattr__(self, "envelopes", tuple(self.envelopes))
        object.__setattr__(self, "dropped", tuple(self.dropped))
        object.__setattr__(self, "detail", _clip(self.detail, MAX_TEXT_CHARS))
        if self.status == "ok" and not self.envelopes:
            raise ValueError("status 'ok' needs at least one envelope")
        if self.status != "ok" and self.envelopes:
            raise ValueError(f"status {self.status!r} may not carry envelopes")

    @property
    def supports_rules(self) -> bool:
        """True only when the source answered and at least one envelope is relevant to the rules.

        Relevant means: a news or feed item whose entity match passed, or a Yahoo actions envelope that
        carries a split or a nearby dividend. An ``ok`` result of wrong-company headlines, or of an
        actions table with nothing near the ex-date, keeps its status and supports nothing.
        """
        return self.status == "ok" and any(_envelope_supports_rules(e) for e in self.envelopes)


def _envelope_supports_rules(env: Mapping[str, Any]) -> bool:
    if env.get("provenance") == PROVENANCE_ACTIONS:
        tf = env.get("typed_fields") or {}
        return any(is_number(tf.get(k)) and tf[k] > 0 for k in ("n_splits", "n_dividends_pm5d"))
    return env.get("entity_match") is True


def source_status_map(results: Iterable[SourceResult]) -> Dict[str, Dict[str, Any]]:
    """The pack's ``source_status``: every source that was asked, with its state. Names must be unique."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in results:
        if r.source in out:
            raise ValueError(f"duplicate source name {r.source!r}")
        out[r.source] = {"status": r.status, "detail": r.detail, "n_envelopes": len(r.envelopes)}
    return out


def _clip(text: Any, cap: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= cap else s[: cap - 1].rstrip() + "."


def _result(source: str, status: str, detail: str, envelopes: Sequence[Mapping[str, Any]] = (),
            dropped: Sequence[Tuple[str, str]] = ()) -> SourceResult:
    return SourceResult(source, status, detail, tuple(dict(e) for e in envelopes), tuple(dropped))


def _subject_ok(isin: Any, ex_date: Any) -> bool:
    return isinstance(isin, str) and bool(ISIN_RE.match(isin)) and valid_date(ex_date)


def _build(fields: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Validate through the same function ``evidence.register`` uses. Returns (envelope, '') or (None, reason)."""
    try:
        return evidence.normalise_envelope(fields), ""
    except Refused as exc:
        return None, f"envelope_invalid: {_clip(exc, 80)}"


def _dedupe(envelopes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for env in envelopes:
        key = canonical_bytes(env)
        if key not in seen:
            seen.add(key)
            out.append(env)
    return out


# ------------------------------------------------------------------ entity match

class EntityMatch(NamedTuple):
    matched: bool
    # name_tokens | symbol_token | name_single_token+related_ticker | name_single_token_unconfirmed
    # | name_prefix_ambiguous | no_token_match
    basis: str


_TOKEN_RE = re.compile(r"[^\W_]+")
_LEGAL_SUFFIXES = frozenset({"limited", "ltd", "pvt", "private", "inc", "incorporated", "corp", "corporation",
                             "plc", "company", "co", "llp"})
MIN_SINGLE_TOKEN_CHARS = 3


def _contains(seq: Sequence[str], sub: Sequence[str]) -> bool:
    n = len(sub)
    return n > 0 and any(list(seq[i:i + n]) == list(sub) for i in range(len(seq) - n + 1))


#: Quarter, half-year and fiscal-year labels that follow a company name in a headline ("Tata Motors Q2 results").
_PERIOD_LABEL_RE = re.compile(r"^(?:q[1-4]|h[12]|fy\d{2,4})(?:fy\d{2,4})?$", re.IGNORECASE)


def _name_occurrences(text: str, tokens: Sequence[str]) -> List[bool]:
    """One flag per occurrence of the name's token run in ``text``: True when it may be the start of a longer name.

    An occurrence is ambiguous when the next word is capitalised, is not a legal suffix or a period label,
    and follows with nothing but whitespace in between ("Tata Motors Finance"). Punctuation, a lower-case word,
    a number or the end of the text end the name.
    """
    spans = [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]
    folded = [w.casefold() for w, _, _ in spans]
    n = len(tokens)
    flags: List[bool] = []
    for i in range(len(spans) - n + 1):
        if folded[i:i + n] != list(tokens):
            continue
        j = i + n
        if j >= len(spans):
            flags.append(False)
            continue
        word, start, _ = spans[j]
        gap = text[spans[j - 1][2]:start]
        flags.append(not gap.strip() and word[:1].isupper() and folded[j] not in _LEGAL_SUFFIXES
                     and not _PERIOD_LABEL_RE.match(word))
    return flags


def _name_tokens(name: Any) -> List[str]:
    if not isinstance(name, str):
        return []
    tokens = [t.casefold() for t in _TOKEN_RE.findall(name)]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return tokens


def _listed(ticker: Any, related_tickers: Any) -> bool:
    """Does the provider's own ticker list for this item hold the company's ticker? Garbage is not a yes."""
    if not isinstance(ticker, str) or not ticker.strip() or not isinstance(related_tickers, (list, tuple, set, frozenset)):
        return False
    wanted = ticker.strip().upper()
    return any(isinstance(t, str) and t.strip().upper() == wanted for t in related_tickers)


def match_entity(text: Any, *, symbol: Any = None, names: Any = None, ticker: Any = None,
                 related_tickers: Any = None) -> EntityMatch:
    """Does ``text`` name the company? Whole tokens only, never a substring.

    Text is split into alphanumeric tokens. In order: a full company name (legal suffixes such as
    Limited removed) as a run of consecutive tokens, case-insensitive, with every distinctive token
    present; the exchange symbol written as the symbol (upper case, so the word "idea" is not IDEA);
    a company name that is a single distinctive word of at least 3 characters, only when a second
    signal agrees: ``related_tickers`` (Yahoo ``relatedTickers`` of that item) holds ``ticker``. A single
    word alone is not enough, because it collides with other entities: a "Titan submersible" is not
    Titan Company and "Delta Air Lines" is not Delta Corp. Such a headline gets the basis
    ``name_single_token_unconfirmed`` and is not a match. ``Trentham`` is not ``Trent``, ``Exchanger``
    is not ``Exchange`` and ``Lloyds`` is not ``Lloyd``.

    A multi-word name can also be the start of a longer, different company: ``Tata Motors`` in ``Tata Motors
    Finance``. When every occurrence of the name is followed by a capitalised word (not a legal suffix, not a
    quarter or fiscal-year label) the name is not taken as a match; the basis is ``name_prefix_ambiguous`` and
    the symbol and single-word checks still run. This fails safe: a title-case headline ("Tata Motors Reports
    Record Sales") is graded as context, never as the entity.
    """
    if not isinstance(text, str) or not text.strip():
        return EntityMatch(False, "no_token_match")
    original = _TOKEN_RE.findall(text)
    folded = [t.casefold() for t in original]
    name_list: List[Any] = [names] if isinstance(names, str) else list(names or [])
    single_names: List[List[str]] = []
    ambiguous = False
    for name in name_list:
        tokens = _name_tokens(name)
        if len(tokens) == 1:
            if len(tokens[0]) >= MIN_SINGLE_TOKEN_CHARS:
                single_names.append(tokens)
        elif tokens:
            flags = _name_occurrences(text, tokens)
            if flags and not all(flags):
                return EntityMatch(True, "name_tokens")
            ambiguous = ambiguous or bool(flags)
    if isinstance(symbol, str):
        sym = [t.upper() for t in _TOKEN_RE.findall(symbol)]
        if sym and not (len(sym) == 1 and len(sym[0]) < MIN_SINGLE_TOKEN_CHARS) and _contains(original, sym):
            return EntityMatch(True, "symbol_token")
    unconfirmed = False
    for tokens in single_names:
        if _contains(folded, tokens):
            if _listed(ticker, related_tickers):
                return EntityMatch(True, "name_single_token+related_ticker")
            unconfirmed = True
    if ambiguous:
        return EntityMatch(False, "name_prefix_ambiguous")
    return EntityMatch(False, "name_single_token_unconfirmed" if unconfirmed else "no_token_match")


# ------------------------------------------------------------------ Yahoo news

def _http_link(value: Any) -> Optional[str]:
    if isinstance(value, str):
        v = value.strip()
        if v.startswith(("https://", "http://")) and len(v) <= MAX_LINK_CHARS and not any(c.isspace() for c in v):
            return v
    return None


def news_result(raw: Any, *, isin: str, ex_date: str, symbol: Any, names: Any, clock: Any) -> SourceResult:
    """Turn a ``RawNews`` into news envelopes. Headlines that do not name the company are kept as context.

    A wrong-company headline is archived with ``entity_match=False`` and ``relevance='context'`` instead of
    being discarded: it cannot support a claim about this security, but its keywords (a demerger) still
    reach the engine's negative check, which then fails safe. A headline dated after the fetch moment is
    dropped. A missing or unreadable timestamp stays null and is never defaulted.

    An item may carry ``related_tickers`` (the provider's own ticker tags for the story, a list of strings).
    It is read for the entity match, as the second signal a single-word company name needs, and is never
    archived. ``RawNews.items`` from ``YahooClient.news`` carry it only if the client keeps it; without it a
    single-word name matches through the exchange symbol alone.
    """
    name = PROVENANCE_NEWS
    if not _subject_ok(isin, ex_date):
        return _result(name, "error", "subject must be a 12-character ISIN and a YYYY-MM-DD ex-date")
    status = getattr(raw, "status", None)
    if status in ("empty", "error"):
        first = (getattr(raw, "errors", None) or [""])[0]
        return _result(name, status, first or ("no news items returned" if status == "empty" else "no answer"))
    items = getattr(raw, "items", None)
    if status != "ok" or not isinstance(items, list):
        return _result(name, "error", f"unrecognised news result (status {status!r})")
    try:
        now = clock.now()
    except Exception:
        return _result(name, "error", "no usable clock to date the fetch")
    ticker = getattr(raw, "ticker", None)
    typed = {"ticker": ticker} if isinstance(ticker, str) and 0 < len(ticker) <= 40 else {}

    envelopes: List[Dict[str, Any]] = []
    dropped: List[Tuple[str, str]] = []
    for item in items[:MAX_NEWS_ENVELOPES * 3]:
        item = item if isinstance(item, Mapping) else {}
        headline = item.get("headline")
        if not isinstance(headline, str) or not headline.strip():
            dropped.append(("?", "no_headline"))
            continue
        headline = " ".join(headline.split())[:MAX_HEADLINE_CHARS].rstrip()
        published = item.get("published_at")
        if published is not None:
            try:
                published, precision = pit.normalise_published_at(published) if isinstance(published, str) \
                    else (None, None)
            except ValueError:
                published, precision = None, None
            if published is not None:
                later = date.fromisoformat(published) > pit.ist_date(now) if precision == "date" \
                    else pit.parse_instant(published) > now
                if later:
                    dropped.append((headline[:40], "published_after_now"))
                    continue
        match = match_entity(headline, symbol=symbol, names=names, ticker=ticker,
                             related_tickers=item.get("related_tickers"))
        env, reason = _build({
            "provenance": PROVENANCE_NEWS, "subject": {"isin": isin, "ex_date": ex_date},
            "url": _http_link(item.get("link")), "title": headline, "headline": headline,
            "published_at": published, "published_at_basis": "source" if published else None,
            "source_class": "news", "relevance": "entity" if match.matched else "context",
            "relevance_basis": match.basis, "entity_match": match.matched, "typed_fields": typed,
        })
        if env is None:
            dropped.append((headline[:40], reason))
        else:
            envelopes.append(env)
    envelopes = _dedupe(envelopes)[:MAX_NEWS_ENVELOPES]
    if not envelopes:
        bad = [r for _, r in dropped if r.startswith("envelope_invalid")]
        return _result(name, "error" if bad else "empty", f"no usable news item ({len(dropped)} dropped)", (), dropped)
    n_match = sum(1 for e in envelopes if e["entity_match"])
    return _result(name, "ok", f"{len(envelopes)} headlines, {n_match} name the entity", envelopes, dropped)


# ------------------------------------------------------------------ Yahoo actions

def actions_result(raw: Any, *, isin: str, ex_date: str) -> SourceResult:
    """Turn a ``RawActions`` into one envelope of vendor splits and dividends near the ex-date.

    The envelope is undated: Yahoo's action table carries an effective date, not a publication time,
    and ``published_at`` is never defaulted. So it is context for the engine's vendor-conflict features and
    can never be the decisive citation of a proposal. It is also deterministic (no fetch time inside), so a
    second collect with the same answer registers the same capture.

    ``typed_fields``: ``ticker``, ``window_days``, ``n_splits`` (all within +-window_days of the ex-date),
    ``split_<k>_date`` and ``split_<k>_ratio`` (k = 1..3, nearest first, ratio as the vendor gives it),
    ``splits_truncated``, ``n_dividends_pm5d`` and ``dividend_<k>_date`` / ``dividend_<k>_amount`` (k = 1..2).
    """
    name = PROVENANCE_ACTIONS
    if not _subject_ok(isin, ex_date):
        return _result(name, "error", "subject must be a 12-character ISIN and a YYYY-MM-DD ex-date")
    status = getattr(raw, "status", None)
    if status in ("empty", "error"):
        first = (getattr(raw, "errors", None) or [""])[0]
        return _result(name, status, first or ("no corporate actions returned" if status == "empty" else "no answer"))
    splits, dividends = getattr(raw, "splits", None), getattr(raw, "dividends", None)
    if status != "ok" or not isinstance(splits, Mapping) or not isinstance(dividends, Mapping):
        return _result(name, "error", f"unrecognised actions result (status {status!r})")

    ex = date.fromisoformat(ex_date)
    dropped: List[Tuple[str, str]] = []

    def near(table: Mapping[Any, Any], days: int) -> List[Tuple[int, str, float]]:
        rows = []
        for key, value in table.items():
            if not valid_date(key) or not is_number(value) or value <= 0:
                dropped.append((str(key)[:20], "unreadable_action"))
                continue
            offset = abs((date.fromisoformat(key) - ex).days)
            if offset <= days:
                rows.append((offset, key, float(value)))
        return sorted(rows)

    split_rows = near(splits, ACTIONS_WINDOW_DAYS)
    dividend_rows = near(dividends, DIVIDEND_WINDOW_DAYS)
    ticker = getattr(raw, "ticker", None)
    ticker = ticker if isinstance(ticker, str) and 0 < len(ticker) <= 40 else None
    typed: Dict[str, Any] = {}
    if ticker:
        typed["ticker"] = ticker
    typed["window_days"] = ACTIONS_WINDOW_DAYS
    typed["n_splits"] = len(split_rows)
    for k, (_, day, ratio) in enumerate(split_rows[:MAX_SPLITS_LISTED], start=1):
        typed[f"split_{k}_date"], typed[f"split_{k}_ratio"] = day, ratio
    typed["splits_truncated"] = len(split_rows) > MAX_SPLITS_LISTED
    typed["n_dividends_pm5d"] = len(dividend_rows)
    for k, (_, day, amount) in enumerate(dividend_rows[:MAX_DIVIDENDS_LISTED], start=1):
        typed[f"dividend_{k}_date"], typed[f"dividend_{k}_amount"] = day, amount
    env, reason = _build({
        "provenance": PROVENANCE_ACTIONS, "subject": {"isin": isin, "ex_date": ex_date}, "url": None,
        "title": f"Yahoo corporate actions for {ticker or 'ticker'}", "headline": None, "published_at": None,
        "source_class": "other", "relevance": "context", "relevance_basis": "ticker_query_unverified",
        "entity_match": None, "typed_fields": typed,
    })
    if env is None:
        return _result(name, "error", reason, (), dropped)
    return _result(name, "ok", f"{len(split_rows)} splits within {ACTIONS_WINDOW_DAYS} days, "
                               f"{len(dividend_rows)} dividends within {DIVIDEND_WINDOW_DAYS}", [env], dropped)


# ------------------------------------------------------------------ Yahoo collect

def _skipped(source: str, detail: str) -> SourceResult:
    return _result(source, "skipped", detail)


def collect_yahoo(client: Any, *, ticker: Any, isin: str, ex_date: str, symbol: Any = None,
                  names: Any = None) -> List[SourceResult]:
    """Fetch Yahoo actions and news for one security through ``client`` (a ``YahooClient``) and return two results.

    Both calls take the client's throttle. Nothing a client or a response does is allowed to raise
    out of here. A security without a Yahoo ticker is ``skipped`` and the client is not touched.
    """
    t = ticker.strip() if isinstance(ticker, str) else ""
    if not t:
        return [_skipped(PROVENANCE_ACTIONS, "no yahoo ticker for this security"),
                _skipped(PROVENANCE_NEWS, "no yahoo ticker for this security")]
    try:
        actions = actions_result(client.actions(t), isin=isin, ex_date=ex_date)
    except Exception as exc:
        actions = _result(PROVENANCE_ACTIONS, "error", f"{type(exc).__name__}: {exc}")
    try:
        news = news_result(client.news(t), isin=isin, ex_date=ex_date, symbol=symbol, names=names,
                           clock=getattr(client, "clock", None))
    except Exception as exc:
        news = _result(PROVENANCE_NEWS, "error", f"{type(exc).__name__}: {exc}")
    return [actions, news]


# ------------------------------------------------------------------ RSS feeds

@dataclass(frozen=True)
class FeedItem:
    title: str
    link: Optional[str]
    published_at: Optional[str]      # a plain date or a UTC timestamp, null when the feed gave none we can trust
    precision: Optional[str]         # 'date' | 'timestamp' | None


@dataclass(frozen=True)
class ParsedFeed:
    status: str                      # ok | empty | error
    detail: str
    items: Tuple[FeedItem, ...] = ()
    n_unparsed: int = 0


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), start=1)}
_PUBDATE_RE = re.compile(
    r"^(?:[A-Za-z]{3,9},?\s+)?(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*(Z|GMT|UTC|UT|[+-]\d{4})?$")


def parse_pubdate(text: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(value, precision)`` of an RSS ``pubDate``, or ``(None, None)`` when it cannot be trusted.

    SEBI's item dates are date-only (``30 Sep, 2026 +0530``): the result is the date with precision
    ``date``, and the channel's own time is never borrowed. A time without a timezone is ambiguous
    and stays unknown.
    """
    if not isinstance(text, str):
        return None, None
    m = _PUBDATE_RE.match(text.strip())
    if not m:
        return None, None
    day, mon, year, hh, mm, ss, tz = m.groups()
    month = _MONTHS.get(mon[:3].casefold())
    try:
        if month is None:
            return None, None
        calendar_day = date(int(year), month, int(day))
        if hh is None:
            return pit.normalise_published_at(calendar_day.isoformat())
        if tz is None:
            return None, None
        offset = timedelta(0) if tz in ("Z", "GMT", "UTC", "UT") else \
            (1 if tz[0] == "+" else -1) * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        moment = datetime(calendar_day.year, calendar_day.month, calendar_day.day, int(hh), int(mm), int(ss or 0),
                          tzinfo=timezone(offset))
        return pit.normalise_published_at(moment.isoformat())
    except ValueError:
        return None, None


def parse_rss(xml: Any) -> ParsedFeed:
    """Parse an RSS 2.0 document into items. Never raises; non-UTF-8 documents, DTDs and entities are refused."""
    if isinstance(xml, str):
        raw = xml.encode("utf-8")
    elif isinstance(xml, (bytes, bytearray)):
        raw = bytes(xml)
    else:
        return ParsedFeed("error", f"unrecognised feed response type {type(xml).__name__}")
    if not raw.strip():
        return ParsedFeed("error", "empty feed response")
    if len(raw) > MAX_FEED_BYTES:
        return ParsedFeed("error", f"feed larger than {MAX_FEED_BYTES} bytes")
    not_utf8 = "feed is not UTF-8 (other encodings are refused, so the DTD check sees every byte)"
    if b"\x00" in raw:             # UTF-16 without a byte order mark is ASCII-range bytes and NULs, valid UTF-8
        return ParsedFeed("error", not_utf8)
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ParsedFeed("error", not_utf8)
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return ParsedFeed("error", "feed carries a DTD or entity declaration: refused")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return ParsedFeed("error", f"feed is not well-formed XML: {exc}")
    channel = root.find("channel") if root.tag.casefold() == "rss" else None
    if channel is None:
        return ParsedFeed("error", "not an RSS 2.0 document (no rss/channel)")
    elements = channel.findall("item")[:MAX_FEED_ITEMS]
    if not elements:
        return ParsedFeed("empty", "feed has no items")
    items: List[FeedItem] = []
    unparsed = 0
    for el in elements:
        title = " ".join((el.findtext("title") or "").split())
        if not title:
            unparsed += 1
            continue
        value, precision = parse_pubdate(el.findtext("pubDate"))
        items.append(FeedItem(title, _http_link(el.findtext("link")), value, precision))
    if not items:
        return ParsedFeed("error", f"none of {len(elements)} feed items had a title", (), unparsed)
    return ParsedFeed("ok", f"{len(items)} items", tuple(items), unparsed)


def _check_url(url: Any) -> None:
    """Raise ValueError unless ``url`` is an https URL on a host the engine may fetch from."""
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    host = (parts.hostname or "") if parts else ""
    if parts is None or parts.scheme != "https" or not host or parts.username or parts.password:
        raise ValueError(f"feed url must be a plain https URL: {url!r}")
    if any(host == d or host.endswith("." + d) for d in BLOCKED_HOSTS):
        raise ValueError(f"automated access to {host} is not allowed (policy: no IRDAI, NSE or BSE fetch)")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect may not lead to a host or scheme the first check would have refused."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_fetch(url: str) -> bytes:
    """Default feed fetcher: https only, blocked hosts refused (also across redirects), 20 s timeout, size capped."""
    _check_url(url)
    opener = urllib.request.build_opener(_CheckedRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/rss+xml, application/xml;q=0.9"})
    with opener.open(request, timeout=FETCH_TIMEOUT_S) as response:
        return response.read(MAX_FEED_BYTES + 1)


def _validate_feed(spec: Any) -> Optional[str]:
    if not isinstance(spec, FeedSpec):
        return "not a FeedSpec"
    if not _FEED_NAME_RE.match(spec.name or ""):
        return f"feed name {spec.name!r} must be 1-30 lowercase letters, digits or underscores"
    if spec.name != PROVENANCE_FEED:
        return (f"feed name {spec.name!r} is refused: envelopes are labelled {PROVENANCE_FEED!r}, the only feed value "
                "in the closed provenance set (quant/triage/types.py)")
    if spec.source_class not in SOURCE_CLASSES:
        return f"source_class {spec.source_class!r} not in {list(SOURCE_CLASSES)}"
    try:
        _check_url(spec.url)
    except ValueError as exc:
        return str(exc)
    return None


FeedSnapshot = List[Tuple[Optional[FeedSpec], ParsedFeed]]


def load_feeds(feeds: Optional[Sequence[FeedSpec]] = None,
               fetch: Optional[Callable[[str], Any]] = None) -> FeedSnapshot:
    """Fetch and parse every enabled feed once. An empty list fetches nothing (``FEEDS`` ships empty).

    Call this once per collect run and reuse the snapshot for every anomaly: a feed is a firehose that
    does not depend on the security. An invalid spec is reported as an error entry and is never fetched.
    """
    specs = FEEDS if feeds is None else tuple(feeds)
    getter = fetch or http_fetch
    snapshot: FeedSnapshot = []
    for spec in specs:
        problem = _validate_feed(spec)
        if problem:
            snapshot.append((spec if isinstance(spec, FeedSpec) else None, ParsedFeed("error", problem)))
            continue
        try:
            body = getter(spec.url)
        except Exception as exc:
            snapshot.append((spec, ParsedFeed("error", f"fetch failed: {type(exc).__name__}: {exc}")))
            continue
        snapshot.append((spec, parse_rss(body)))
    return snapshot


def feed_results(snapshot: FeedSnapshot, *, isin: str, ex_date: str, symbol: Any = None,
                 names: Any = None) -> List[SourceResult]:
    """One result per feed: the items that name this company, as regulator or other feed envelopes.

    Unlike Yahoo news, unrelated feed items are counted and not archived (the feed is not queried by
    ticker, so most items are about other companies).
    """
    if not snapshot:
        return [_skipped("feeds", "FEEDS is empty: no feed is enabled")]
    results: List[SourceResult] = []
    for index, (spec, parsed) in enumerate(snapshot):
        name = spec.name if spec is not None and _FEED_NAME_RE.match(spec.name or "") else f"feed_{index + 1}"
        if not _subject_ok(isin, ex_date):
            results.append(_result(name, "error", "subject must be a 12-character ISIN and a YYYY-MM-DD ex-date"))
            continue
        if parsed.status != "ok" or spec is None:
            results.append(_result(name, parsed.status if parsed.status in ("empty", "error") else "error",
                                   parsed.detail))
            continue
        envelopes: List[Dict[str, Any]] = []
        dropped: List[Tuple[str, str]] = [("?", "no_title")] * parsed.n_unparsed
        for item in parsed.items:
            match = match_entity(item.title, symbol=symbol, names=names)
            if not match.matched:
                continue
            env, reason = _build({
                "provenance": PROVENANCE_FEED, "subject": {"isin": isin, "ex_date": ex_date}, "url": item.link,
                "title": item.title[:MAX_TITLE_CHARS], "headline": item.title[:MAX_HEADLINE_CHARS],
                "published_at": item.published_at,
                "published_at_basis": "source" if item.published_at else None,
                "published_at_precision": item.precision,
                "source_class": spec.source_class, "relevance": "entity", "relevance_basis": match.basis,
                "entity_match": True, "typed_fields": {"feed": spec.name},
            })
            if env is None:
                dropped.append((item.title[:40], reason))
            else:
                envelopes.append(env)
        envelopes = _dedupe(envelopes)
        detail = f"{len(envelopes)} of {len(parsed.items)} items name the entity"
        results.append(_result(name, "ok" if envelopes else "empty", detail, envelopes, dropped))
    return results


def collect_feeds(*, isin: str, ex_date: str, symbol: Any = None, names: Any = None,
                  feeds: Optional[Sequence[FeedSpec]] = None,
                  fetch: Optional[Callable[[str], Any]] = None) -> List[SourceResult]:
    """Convenience for one anomaly: :func:`load_feeds` then :func:`feed_results`."""
    return feed_results(load_feeds(feeds, fetch), isin=isin, ex_date=ex_date, symbol=symbol, names=names)
