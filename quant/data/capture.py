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
