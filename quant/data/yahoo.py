"""Throttled Yahoo Finance client, serialization and normalization."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
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
