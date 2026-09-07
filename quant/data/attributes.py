"""Point-in-time security attributes and capture."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from quant.data.yahoo import RawBundle, normalize_info
from quant.run import RunContext
from quant.types import Result


VALID_ATTRIBUTE_FIELDS = {
    "mcap_inr",
    "shares_out",
    "float_shares",
    "ev_inr",
    "trailing_pe",
    "price_to_book",
    "dividend_rate_inr",
    "beta",
    "yahoo_sector",
    "yahoo_industry",
}


def capture(ctx: RunContext, bundles: Dict[int, RawBundle]) -> Result:
    """Capture normalized security attributes from raw bundles."""
    cur = ctx.conn.cursor()
    inserted = 0

    for sid, bundle in bundles.items():
        norm, _ = normalize_info(bundle.info, close=None)
        captured_at = bundle.fetched_at
        info_bytes = json.dumps(bundle.info, sort_keys=True).encode("utf-8")
        src_sha = hashlib.sha256(info_bytes).hexdigest()

        cur.execute(
            """
            INSERT OR IGNORE INTO security_attributes (
                captured_at, security_id, mcap_inr, shares_out, float_shares, ev_inr,
                trailing_pe, price_to_book, dividend_rate_inr, beta, yahoo_sector, yahoo_industry, source_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                sid,
                norm.get("mcap_inr"),
                norm.get("shares_out"),
                norm.get("float_shares"),
                norm.get("ev_inr"),
                norm.get("trailing_pe"),
                norm.get("price_to_book"),
                norm.get("dividend_rate_inr"),
                norm.get("beta"),
                norm.get("yahoo_sector"),
                norm.get("yahoo_industry"),
                src_sha,
            ),
        )
        inserted += 1

    return Result(
        status="ok",
        counts={"rows": inserted},
        details={"message": f"Captured attributes for {inserted} securities"},
    )


def at(
    conn: sqlite3.Connection,
    cutoff: str,
    field: str,
    security_ids: List[int],
) -> pd.Series:
    """Return point-in-time attribute value for securities at cutoff date."""
    if not security_ids:
        return pd.Series(dtype=object)

    if field not in VALID_ATTRIBUTE_FIELDS:
        raise ValueError(f"Unknown attribute field: {field}")

    cur = conn.cursor()
    out = {}

    for sid in security_ids:
        cur.execute(
            f"""
            SELECT {field}
            FROM security_attributes
            WHERE security_id = ? AND captured_at <= ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (sid, cutoff),
        )
        row = cur.fetchone()
        if row is not None:
            out[sid] = row[0]
        else:
            out[sid] = None if field in ("yahoo_sector", "yahoo_industry") else np.nan

    return pd.Series(out, index=security_ids, name=field)
