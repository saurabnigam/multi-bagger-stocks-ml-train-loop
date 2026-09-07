"""Data capture orchestration across fundamentals, holdings, attributes, and prices."""
from __future__ import annotations

from typing import Dict, List
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

    # 1. Archive raw bundles
    capture_id = client.archive(list(bundles.values()), ctx)

    # 2. Ingest fundamentals
    fundamentals_ingest(ctx, bundles)

    # 3. Ingest holdings
    holdings_capture(ctx, bundles)

    # 4. Ingest attributes
    attributes_capture(ctx, bundles)

    # 5. Raw price window batch download & archive
    tickers = list(set(ticker_map.values()))
    start = getattr(ctx.cfg.yahoo, "history_start", "2015-01-01")
    end = ctx.as_of
    client.download_batch(tickers, start=start, end=end)

    return Result(
        status="ok",
        counts={"securities": len(sids), "bundles": len(bundles)},
        details={"capture_id": capture_id},
    )
