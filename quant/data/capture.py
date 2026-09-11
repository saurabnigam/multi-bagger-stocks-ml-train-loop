"""Data capture orchestration across fundamentals, holdings, attributes, and prices.

``run`` downloads fresh bundles and archives them before ingesting; ``ingest_archive``
replays a retained archive (MASTER_SPEC 4.4: rebuild from retained archives), so a
failed ingest never forces a second vendor download of the same observations.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd

from quant.data.attributes import capture as attributes_capture
from quant.data.fundamentals import ingest as fundamentals_ingest
from quant.data.holdings import capture as holdings_capture
from quant.data.identity import tracked_securities, yahoo_ticker
from quant.data.universe import members_at
from quant.data.yahoo import RawBundle, YahooClient
from quant.run import RunContext
from quant.types import Result


def run(ctx: RunContext, client: YahooClient) -> Result:
    """Run full Yahoo data acquisition and archive orchestration."""
    horizons = getattr(ctx.cfg.horizons, "tracked_m", [1, 3, 6, 12, 24, 36])
    sids = tracked_securities(ctx.conn, ctx.as_of, horizons)

    if not sids:
        # Fallback to current universe members if no prior cohorts exist
        m_df = members_at(ctx.conn, ctx.as_of)
        if not m_df.empty:
            sids = sorted(list(m_df["security_id"].unique()))

    ticker_map: Dict[int, str] = {}
    for sid in sids:
        ticker = yahoo_ticker(ctx.conn, sid, ctx.as_of)
        if ticker:
            ticker_map[sid] = ticker

    bundles: Dict[int, RawBundle] = {}
    for sid, ticker in ticker_map.items():
        bundle = client.bundle(ticker)
        bundles[sid] = bundle

    # 1. Archive raw bundles (exact vendor bytes, before any interpretation)
    capture_id = client.archive(list(bundles.values()), ctx)

    # 2-4. Fundamentals, holdings, attributes
    ingest_bundles(ctx, bundles)

    # 5. Price windows: downloaded in batches, archived with their vintage, normalised
    #    to the raw quoted basis and reconciled into the versioned price store.
    from quant.data.prices import PriceStore
    store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    price_res = store.update(ctx, client, list(ticker_map.keys()), through=ctx.as_of)

    return Result(
        status="ok",
        counts={
            "securities": len(sids),
            "bundles": len(bundles),
            "price_rows": int(price_res.counts.get("rows", 0)),
            "price_quarantined": int(price_res.counts.get("quarantined", 0)),
        },
        details={"capture_id": capture_id, "price_capture": price_res.details},
    )


def ingest_bundles(ctx: RunContext, bundles: Dict[int, RawBundle]) -> Dict[str, int]:
    """Ingest already-archived bundles: fundamentals, holdings and attributes."""
    f = fundamentals_ingest(ctx, bundles)
    h = holdings_capture(ctx, bundles)
    a = attributes_capture(ctx, bundles)
    return {
        "fundamental_rows": int(f.counts.get("rows", 0)),
        "holdings_rows": int(h.counts.get("rows", 0)),
        "attribute_rows": int(a.counts.get("rows", 0)),
    }


def _label_to_date(label: Any) -> str:
    """Statement column labels come back from the archive as epoch-ms ints or timestamps."""
    if isinstance(label, pd.Timestamp):
        ts = label
    elif isinstance(label, (int, float)) and abs(float(label)) > 1e11:
        ts = pd.Timestamp(int(label), unit="ms")
    elif isinstance(label, str) and label.isdigit() and len(label) >= 12:
        ts = pd.Timestamp(int(label), unit="ms")
    else:
        try:
            ts = pd.Timestamp(label)
        except (ValueError, TypeError):
            return str(label)[:10]
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.strftime("%Y-%m-%d")


def _index_to_timestamps(index: Any) -> pd.DatetimeIndex:
    out = []
    for label in index:
        if isinstance(label, pd.Timestamp):
            ts = label
        elif isinstance(label, (int, float)) and abs(float(label)) > 1e11:
            ts = pd.Timestamp(int(label), unit="ms")
        else:
            ts = pd.to_datetime(label, errors="coerce")
        if ts is pd.NaT or pd.isna(ts):
            continue
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        out.append(ts)
    return pd.DatetimeIndex(out)


def load_archive(path: Path | str) -> List[RawBundle]:
    """Reconstruct RawBundle objects from a bundle archive written by YahooClient.archive."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    bundles: List[RawBundle] = []
    for b in data:
        statements: Dict[str, pd.DataFrame] = {}
        for name, payload in (b.get("statements") or {}).items():
            if not payload:
                statements[name] = pd.DataFrame()
                continue
            df = pd.read_json(io.StringIO(payload), orient="split", convert_axes=False, convert_dates=False)
            df.columns = [_label_to_date(c) for c in df.columns]
            statements[name] = df
        ed = pd.DataFrame()
        payload = b.get("earnings_dates") or ""
        if payload:
            ed = pd.read_json(io.StringIO(payload), orient="split", convert_axes=False, convert_dates=False)
            idx = _index_to_timestamps(ed.index)
            ed = ed.iloc[: len(idx)] if len(idx) != len(ed) else ed
            ed.index = idx
        bundles.append(RawBundle(
            ticker=str(b["ticker"]),
            fetched_at=str(b["fetched_at"]),
            info=b.get("info") or {},
            statements=statements,
            earnings_dates=ed,
            errors=list(b.get("errors") or []),
        ))
    return bundles


def ingest_archive(
    ctx: RunContext,
    archive_path: Path | str,
    client: Optional[YahooClient] = None,
    *,
    prices: bool = True,
) -> Result:
    """Replay a retained bundle archive into the state database (and update prices if a client is given)."""
    archive_path = Path(archive_path)
    bundles = load_archive(archive_path)
    rows = ctx.conn.execute(
        "SELECT security_id, yahoo_ticker FROM symbol_history WHERE valid_to IS NULL ORDER BY valid_from DESC"
    ).fetchall()
    ticker_to_sid: Dict[str, int] = {}
    for r in rows:
        ticker_to_sid.setdefault(str(r["yahoo_ticker"]), int(r["security_id"]))

    by_sid: Dict[int, RawBundle] = {}
    unmapped: List[str] = []
    for b in bundles:
        sid = ticker_to_sid.get(b.ticker)
        if sid is None:
            unmapped.append(b.ticker)
        else:
            by_sid[sid] = b

    capture_id = archive_path.stem
    captured_at = max((b.fetched_at for b in bundles), default=ctx.clock.iso())
    sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    ctx.conn.execute(
        "INSERT OR IGNORE INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
        "VALUES (?, ?, 'yahoo_bundle', ?, ?, 'yfinance_archive', ?)",
        (capture_id, captured_at, str(archive_path), sha256, ctx.run_id),
    )
    counts: Dict[str, int] = {"bundles": len(bundles), "mapped": len(by_sid), "unmapped": len(unmapped)}
    counts.update(ingest_bundles(ctx, by_sid))

    price_details: Dict[str, Any] = {"skipped": True}
    if prices and client is not None and by_sid:
        from quant.data.prices import PriceStore
        store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
        res = store.update(ctx, client, list(by_sid.keys()), through=ctx.as_of)
        counts["price_rows"] = int(res.counts.get("rows", 0))
        counts["price_quarantined"] = int(res.counts.get("quarantined", 0))
        price_details = res.details

    return Result(
        status="ok",
        counts=counts,
        details={"capture_id": capture_id, "sha256": sha256, "unmapped_tickers": unmapped[:20], "prices": price_details},
    )
