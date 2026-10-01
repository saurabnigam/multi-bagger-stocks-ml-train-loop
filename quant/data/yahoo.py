"""Throttled Yahoo Finance client, serialization and normalization."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

import numpy as np
import pandas as pd
import yfinance as yf

from quant.config import Config
from quant.run import RunContext
from quant.types import Clock


@dataclass
class RawBundle:
    ticker: str
    fetched_at: str
    info: Dict[str, Any]
    statements: Dict[str, pd.DataFrame]
    earnings_dates: pd.DataFrame
    errors: List[str] = field(default_factory=list)


@dataclass
class RawActions:
    """Yahoo corporate actions for one ticker, as ``{YYYY-MM-DD: value}`` maps (evidence triage).

    ``status`` is ``ok`` (the response had at least one split or dividend), ``empty`` (it answered and
    held none) or ``error`` (no answer, or a shape this code does not recognise). ``empty`` is not proof
    that no action exists: yfinance also returns an empty frame for a ticker it could not read.
    """
    ticker: str
    fetched_at: str
    status: str
    splits: Dict[str, float] = field(default_factory=dict)        # vendor ratio: 2.0 means two new shares per old
    dividends: Dict[str, float] = field(default_factory=dict)     # cash per share
    errors: List[str] = field(default_factory=list)


@dataclass
class RawNews:
    """Yahoo news for one ticker, reduced to headline, link and provider timestamp (never a body)."""
    ticker: str
    fetched_at: str
    status: str                                                   # ok | empty | error, as for RawActions
    items: List[Dict[str, Optional[str]]] = field(default_factory=list)   # {headline, link, published_at}
    n_unparsed: int = 0                                           # response items with no readable headline
    errors: List[str] = field(default_factory=list)


MAX_NEWS_ITEMS = 10
MAX_NEWS_HEADLINE_CHARS = 200
MAX_NEWS_LINK_CHARS = 500
_SPLIT_COLUMNS = ("Stock Splits", "Splits", "Stock Split")
_DIVIDEND_COLUMNS = ("Dividends", "Dividend")
_EPOCH_RE = re.compile(r"^\d{9,13}(\.\d+)?$")


def normalize_info(info: Dict[str, Any], close: Optional[float] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize raw Yahoo info dictionary into canonical attributes and check flags."""
    flags: List[str] = []
    norm: Dict[str, Any] = {}

    if not info:
        return norm, flags

    # Pass-through basic metadata
    norm["symbol"] = info.get("symbol")
    norm["short_name"] = info.get("shortName")
    norm["yahoo_sector"] = info.get("sector")
    norm["yahoo_industry"] = info.get("industry")
    norm["beta"] = info.get("beta")
    norm["mcap_inr"] = info.get("marketCap")
    norm["shares_out"] = info.get("sharesOutstanding")
    norm["float_shares"] = info.get("floatShares")
    norm["ev_inr"] = info.get("enterpriseValue")
    norm["trailing_pe"] = info.get("trailingPE")
    norm["price_to_book"] = info.get("priceToBook")

    # None stays missing: ROE, margins, etc.
    norm["return_on_equity"] = info.get("returnOnEquity")
    norm["operating_margins"] = info.get("operatingMargins")

    # 1. debtToEquity: e.g. 357.0 (%) -> 3.57 (ratio)
    de = info.get("debtToEquity")
    if de is not None and not pd.isna(de):
        try:
            norm["debt_to_equity"] = float(de) / 100.0
        except (ValueError, TypeError):
            norm["debt_to_equity"] = None
    else:
        norm["debt_to_equity"] = None

    # 2. Dividend normalization: dividendRate / price (never use dividendYield directly)
    if "dividendYield" in info and info["dividendYield"] is not None:
        flags.append("ignored_raw_dividend_yield")

    div_rate = info.get("dividendRate")
    if div_rate is not None and not pd.isna(div_rate):
        try:
            norm["dividend_rate_inr"] = float(div_rate)
        except (ValueError, TypeError):
            norm["dividend_rate_inr"] = None
    else:
        norm["dividend_rate_inr"] = None

    if norm["dividend_rate_inr"] is not None and close is not None and close > 0:
        norm["dividend_yield"] = norm["dividend_rate_inr"] / float(close)
    else:
        norm["dividend_yield"] = None

    # 3. Ownership / Holdings: fractions in [0, 1]
    for key, target in [("heldPercentInstitutions", "inst_pct"), ("heldPercentInsiders", "insider_pct")]:
        val = info.get(key)
        if val is not None and not pd.isna(val):
            try:
                fval = float(val)
                if fval > 1.0:
                    fval /= 100.0
                norm[target] = fval
            except (ValueError, TypeError):
                norm[target] = None
        else:
            norm[target] = None

    return norm, flags


def _date_key(index_value: Any) -> Optional[str]:
    """``YYYY-MM-DD`` of an actions-frame index entry, in the index's own timezone (None when unreadable)."""
    try:
        ts = pd.Timestamp(index_value)
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _positive_by_date(frame: pd.DataFrame, names: Tuple[str, ...]) -> Optional[Dict[str, float]]:
    """``{date: value}`` of the strictly positive, finite cells of the first recognised column.

    None means no recognised column at all (a renamed or missing field), which is different from
    ``{}``: a column that is present and holds no positive value.
    """
    column = next((c for c in frame.columns if c in names), None)
    if column is None:
        return None
    series = frame[column]
    if isinstance(series, pd.DataFrame):      # duplicated column name: take the first
        series = series.iloc[:, 0]
    out: Dict[str, float] = {}
    for index_value, cell in series.items():
        try:
            value = float(cell)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        key = _date_key(index_value)
        if key is not None:
            out[key] = value
    return out


def normalize_actions(response: Any) -> Tuple[str, Dict[str, float], Dict[str, float], List[str]]:
    """Classify a ``Ticker.actions`` response: ``(status, splits, dividends, problems)``. Never raises."""
    try:
        if response is None:
            return "error", {}, {}, ["Yahoo returned no actions object"]
        if not isinstance(response, pd.DataFrame):
            return "error", {}, {}, [f"unrecognised actions response type {type(response).__name__}"]
        if response.empty:
            return "empty", {}, {}, []
        splits = _positive_by_date(response, _SPLIT_COLUMNS)
        dividends = _positive_by_date(response, _DIVIDEND_COLUMNS)
        if splits is None and dividends is None:
            return "error", {}, {}, [f"no recognised actions column in {sorted(map(str, response.columns))[:8]}"]
        if splits is None or dividends is None:
            # One table unreadable: reading it as {} would record a healthy zero (n_splits=0) for a missing field.
            missing = "split" if splits is None else "dividend"
            return "error", {}, {}, [f"{missing} column not recognised in {sorted(map(str, response.columns))[:8]}"]
        return ("ok" if splits or dividends else "empty"), splits, dividends, []
    except Exception as exc:  # a data-shape problem must never reach the caller
        return "error", {}, {}, [f"unreadable actions response: {type(exc).__name__}: {exc}"]


def _utc_iso(value: Any) -> Optional[str]:
    """An epoch (seconds or milliseconds) or a timezone-aware ISO string as ``YYYY-MM-DDTHH:MM:SSZ``.

    Anything ambiguous (no timezone, not a number, out of range) is None: a wrong guess could move a
    headline across a cohort cutoff, so an unknown time stays unknown.
    """
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            text = value.strip()
            if _EPOCH_RE.match(text):
                value = float(text)
            else:
                dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
                if dt.tzinfo is None:
                    return None
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(value, (int, float)):
            seconds = float(value)
            if not math.isfinite(seconds) or seconds <= 0:
                return None
            if seconds > 1e11:
                seconds /= 1000.0
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            if not 2000 <= dt.year <= 2100:
                return None
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _first_link(candidates: List[Any]) -> Optional[str]:
    for cand in candidates:
        url = cand.get("url") if isinstance(cand, dict) else cand
        if isinstance(url, str):
            url = url.strip()
            if url.startswith(("https://", "http://")) and len(url) <= MAX_NEWS_LINK_CHARS and not any(c.isspace() for c in url):
                return url
    return None


def normalize_news(response: Any) -> Tuple[str, List[Dict[str, Optional[str]]], int, List[str]]:
    """Classify a ``Ticker.news`` response: ``(status, items, n_unparsed, problems)``. Never raises.

    Both known response shapes are read (nested ``content`` with ``pubDate``, and the older flat
    ``title``/``link``/``providerPublishTime``). Only the headline (at most 200 characters), a link and a
    provider timestamp are kept: no summary, no body and no publisher are ever copied.
    """
    try:
        if response is None:
            return "error", [], 0, ["Yahoo returned no news object"]
        if not isinstance(response, (list, tuple)):
            return "error", [], 0, [f"unrecognised news response type {type(response).__name__}"]
        if len(response) == 0:
            return "empty", [], 0, []
        items: List[Dict[str, Optional[str]]] = []
        unparsed = 0
        for raw in response:
            if not isinstance(raw, dict):
                unparsed += 1
                continue
            content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
            headline = None
            for cand in (content.get("title"), raw.get("title")):
                if isinstance(cand, str) and cand.strip():
                    headline = " ".join(cand.split())[:MAX_NEWS_HEADLINE_CHARS].rstrip()
                    break
            if headline is None:
                unparsed += 1
                continue
            link = _first_link([content.get("canonicalUrl"), content.get("clickThroughUrl"), content.get("link"),
                                content.get("url"), raw.get("link"), raw.get("url")])
            published = None
            for cand in (content.get("pubDate"), content.get("displayTime"), raw.get("providerPublishTime"),
                         content.get("providerPublishTime"), raw.get("pubDate")):
                published = _utc_iso(cand)
                if published is not None:
                    break
            items.append({"headline": headline, "link": link, "published_at": published})
        if not items:
            return "error", [], unparsed, [f"none of {len(response)} news items had a readable headline"]
        return "ok", items[:MAX_NEWS_ITEMS], unparsed, []
    except Exception as exc:  # a data-shape problem must never reach the caller
        return "error", [], 0, [f"unreadable news response: {type(exc).__name__}: {exc}"]


class YahooClient:
    """Throttled yfinance client with rate-limiting and 429 retry logic."""

    def __init__(self, cfg: Config, clock: Clock, sleep: Callable[[float], None]):
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.accessor_sleep_s = getattr(cfg.yahoo, "accessor_sleep_s", 0.5)
        self.batch_size = getattr(cfg.yahoo, "batch_size", 25)
        self.batch_sleep_s = getattr(cfg.yahoo, "batch_sleep_s", 1.0)
        self.on_429_sleep_s = getattr(cfg.yahoo, "on_429_sleep_s", 120.0)
        self.max_retries = getattr(cfg.yahoo, "max_retries", 5)

    def _call_with_retry(self, fn: Callable[[], Any], description: str) -> Tuple[Any, List[str]]:
        """Invoke accessor fn with 429 retry and accessor throttling."""
        errors: List[str] = []
        retries = 0

        while True:
            self.sleep(self.accessor_sleep_s)
            try:
                result = fn()
                return result, errors
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "Too Many Requests" in err_str:
                    retries += 1
                    if retries <= self.max_retries:
                        self.sleep(self.on_429_sleep_s)
                        continue
                    else:
                        errors.append(f"429 rate limit exceeded for {description} after {retries} retries: {err_str}")
                        return None, errors
                else:
                    errors.append(f"Error fetching {description}: {err_str}")
                    return None, errors

    def bundle(self, ticker: str) -> RawBundle:
        """Fetch raw statements, info, and earnings dates for a ticker with throttling."""
        fetched_at = self.clock.iso()
        all_errors: List[str] = []

        t = yf.Ticker(ticker)

        # 1. Info
        info, errs = self._call_with_retry(lambda: t.info, f"{ticker}.info")
        all_errors.extend(errs)
        if info is None:
            info = {}

        # 2. Statements
        statements: Dict[str, pd.DataFrame] = {}
        stmt_attrs = [
            ("income_stmt", "income_stmt"),
            ("quarterly_income_stmt", "quarterly_income_stmt"),
            ("balance_sheet", "balance_sheet"),
            ("quarterly_balance_sheet", "quarterly_balance_sheet"),
            ("cashflow", "cashflow"),
            ("quarterly_cashflow", "quarterly_cashflow"),
        ]

        for name, attr in stmt_attrs:
            res, s_errs = self._call_with_retry(lambda: getattr(t, attr), f"{ticker}.{attr}")
            all_errors.extend(s_errs)
            if isinstance(res, pd.DataFrame):
                statements[name] = res
            else:
                statements[name] = pd.DataFrame()

        # 3. Earnings dates
        ed, ed_errs = self._call_with_retry(lambda: getattr(t, "earnings_dates"), f"{ticker}.earnings_dates")
        all_errors.extend(ed_errs)
        if not isinstance(ed, pd.DataFrame):
            ed = pd.DataFrame()

        return RawBundle(
            ticker=ticker,
            fetched_at=fetched_at,
            info=info,
            statements=statements,
            earnings_dates=ed,
            errors=all_errors,
        )

    def download_batch(self, tickers: List[str], start: str, end: str) -> pd.DataFrame:
        """Download prices in batches of <=25 with threads=False and throttling."""
        if not tickers:
            return pd.DataFrame()

        batches = [tickers[i:i + self.batch_size] for i in range(0, len(tickers), self.batch_size)]
        frames: List[pd.DataFrame] = []

        for idx, batch in enumerate(batches):
            if idx > 0:
                self.sleep(self.batch_sleep_s)

            df = yf.download(
                batch,
                start=start,
                end=end,
                threads=False,
                progress=False,
                auto_adjust=False,
                actions=True,
            )
            if df is not None and not df.empty:
                frames.append(df)

        if not frames:
            result = pd.DataFrame()
        elif len(frames) == 1:
            result = frames[0]
        else:
            result = pd.concat(frames, axis=1)

        result.attrs["tickers"] = tickers
        result.attrs["start"] = start
        result.attrs["end"] = end
        result.attrs["source"] = "yfinance"
        result.attrs["fetched_at"] = self.clock.iso()

        return result

    def archive(self, bundles: List[RawBundle], ctx: RunContext) -> str:
        """Archive exact returned bundles to archive_dir and register in captures table."""
        archive_dir = ctx.cfg.paths.archive_dir / "captures" / "yahoo"
        archive_dir.mkdir(parents=True, exist_ok=True)

        capture_id = f"cap_yah_{uuid.uuid4().hex[:12]}"
        archive_file = archive_dir / f"{capture_id}.json"

        # Serialize bundles
        serializable_bundles = []
        for b in bundles:
            stmts_dict = {}
            for sname, sdf in b.statements.items():
                stmts_dict[sname] = sdf.to_json(orient="split") if not sdf.empty else ""

            ed_json = b.earnings_dates.to_json(orient="split") if not b.earnings_dates.empty else ""

            serializable_bundles.append({
                "ticker": b.ticker,
                "fetched_at": b.fetched_at,
                "info": b.info,
                "statements": stmts_dict,
                "earnings_dates": ed_json,
                "errors": b.errors,
            })

        content_bytes = json.dumps(serializable_bundles, sort_keys=True).encode("utf-8")
        with open(archive_file, "wb") as f:
            f.write(content_bytes)

        sha256 = hashlib.sha256(content_bytes).hexdigest()
        source_version = f"yfinance_{yf.__version__}"
        now_iso = ctx.clock.iso()

        cur = ctx.conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO captures (
                capture_id, captured_at, kind, archive_path, sha256, source_version, run_id
            ) VALUES (?, ?, 'yahoo_bundle', ?, ?, ?, ?)
            """,
            (
                capture_id,
                now_iso,
                str(archive_file),
                sha256,
                source_version,
                ctx.run_id,
            )
        )

        return capture_id

    def actions(self, ticker: str) -> RawActions:
        """Corporate actions (splits, dividends) for one ticker, through the same throttle and 429 retry.

        Never raises for a missing, renamed or malformed field: the result carries ``status`` and ``errors``.
        """
        fetched_at = self.clock.iso()
        response, errors = self._call_with_retry(lambda: yf.Ticker(ticker).actions, f"{ticker}.actions")
        status, splits, dividends, problems = normalize_actions(response)
        if response is None and errors:          # the transport error is the better explanation
            problems = []
        return RawActions(ticker=ticker, fetched_at=fetched_at, status=status, splits=splits,
                          dividends=dividends, errors=list(errors) + problems)

    def news(self, ticker: str) -> RawNews:
        """Recent news for one ticker, reduced to headline, link and provider timestamp.

        Same throttle and retry as every other accessor. Never raises for a data-shape problem.
        """
        fetched_at = self.clock.iso()
        response, errors = self._call_with_retry(lambda: yf.Ticker(ticker).news, f"{ticker}.news")
        status, items, unparsed, problems = normalize_news(response)
        if response is None and errors:
            problems = []
        return RawNews(ticker=ticker, fetched_at=fetched_at, status=status, items=items,
                       n_unparsed=unparsed, errors=list(errors) + problems)
