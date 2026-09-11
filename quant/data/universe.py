from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from typing import Any
import pandas as pd
import requests

from quant.config import Config
from quant.errors import Blocked, Refused
from quant.types import Clock, Result

REQUIRED_HEADERS = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}


def fetch_list(name: str, cfg: Config, clock: Clock) -> tuple[bytes, dict[str, Any]]:
    name_lower = name.lower()
    if name_lower == "nifty500":
        filename = cfg.indexes.nifty500
    elif name_lower == "momentum30":
        filename = cfg.indexes.momentum30
    elif name_lower == "quality30":
        filename = cfg.indexes.quality30
    else:
        filename = name

    base_url = cfg.indexes.base_url.rstrip("/")
    url = f"{base_url}/{filename.lstrip('/')}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    content = resp.content

    meta = {
        "url": url,
        "captured_at": clock.iso(),
        "source_version": "nifty_v1",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    return content, meta


def parse_list(content: bytes) -> pd.DataFrame:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]

    missing = REQUIRED_HEADERS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required constituent headers: {missing}")

    # Strip string columns
    for col in REQUIRED_HEADERS:
        df[col] = df[col].astype(str).str.strip()

    # Check for duplicate ISIN
    dup_isin = df[df.duplicated("ISIN Code", keep=False)]
    if not dup_isin.empty:
        raise Refused("duplicate_isin", f"Duplicate ISINs found: {list(dup_isin['ISIN Code'].unique())}")

    # Check for duplicate Symbol
    dup_sym = df[df.duplicated("Symbol", keep=False)]
    if not dup_sym.empty:
        raise Refused("duplicate_symbols", f"Duplicate Symbols found: {list(dup_sym['Symbol'].unique())}")

    renamed = df.rename(
        columns={
            "Company Name": "company_name",
            "Industry": "nse_sector",
            "Symbol": "symbol",
            "Series": "series",
            "ISIN Code": "isin",
        }
    )
    return renamed[["company_name", "nse_sector", "symbol", "series", "isin"]]


def capture(ctx: Any) -> Result:
    raw_bytes, meta = fetch_list("nifty500", ctx.cfg, ctx.clock)
    df = parse_list(raw_bytes)

    min_rows = getattr(ctx.cfg.universe, "min_rows", 480)
    if len(df) < min_rows:
        raise Blocked(
            "short_universe",
            f"Universe capture returned {len(df)} rows, minimum required is {min_rows}",
        )

    capture_id = f"cap_universe_{meta['sha256'][:16]}"
    archive_dir = ctx.cfg.paths.archive_dir / "captures" / "universe"
    archive_dir.mkdir(parents=True, exist_ok=True)
    raw_path = archive_dir / f"{capture_id}.csv"
    raw_path.write_bytes(raw_bytes)
    meta_path = archive_dir / f"{capture_id}.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    captured_date = meta["captured_at"][:10]

    ctx.conn.execute("SAVEPOINT quant_universe_capture")
    try:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
            "VALUES (?, ?, 'nifty500', ?, ?, 'nifty_v1', ?)",
            (capture_id, meta["captured_at"], str(raw_path), meta["sha256"], ctx.run_id),
        )

        for _, r in df.iterrows():
            # Upsert security
            cur = ctx.conn.execute("SELECT security_id FROM securities WHERE isin = ?", (r["isin"],))
            row = cur.fetchone()
            if row:
                sec_id = row["security_id"]
                ctx.conn.execute(
                    "UPDATE securities SET last_seen = ? WHERE security_id = ?",
                    (captured_date, sec_id),
                )
            else:
                cur_ins = ctx.conn.execute(
                    "INSERT INTO securities (isin, name, first_seen, last_seen, status) VALUES (?, ?, ?, ?, 'listed')",
                    (r["isin"], r["company_name"], captured_date, captured_date),
                )
                sec_id = cur_ins.lastrowid

            # Upsert symbol_history
            yahoo_t = f"{r['symbol']}.NS"
            ctx.conn.execute(
                "INSERT OR IGNORE INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                "VALUES (?, ?, ?, ?, 'nifty500_csv')",
                (sec_id, r["symbol"], yahoo_t, captured_date),
            )

            # Insert into universe_membership
            ctx.conn.execute(
                "INSERT OR IGNORE INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, series, source, source_sha256) "
                "VALUES (?, ?, ?, 'NIFTY500', ?, ?, ?, 'nse_csv', ?)",
                (captured_date, meta["captured_at"], sec_id, r["symbol"], r["nse_sector"], r["series"], meta["sha256"]),
            )
        ctx.conn.execute("RELEASE quant_universe_capture")
    except Exception:
        ctx.conn.execute("ROLLBACK TO quant_universe_capture")
        ctx.conn.execute("RELEASE quant_universe_capture")
        raise

    return Result(
        status="ok",
        counts={"members": len(df)},
        details={"capture_id": capture_id, "sha256": meta["sha256"]},
    )


def members_at(conn: sqlite3.Connection, cutoff: str, index_name: str = "NIFTY500") -> pd.DataFrame:
    cutoff_date = cutoff[:10]
    cutoff_iso = cutoff if "T" in cutoff else f"{cutoff}T23:59:59.999999Z"

    # Find latest capture on or before cutoff
    cur = conn.execute(
        "SELECT capture_id, captured_at FROM captures WHERE kind = 'nifty500' AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (cutoff_iso,),
    )
    cap_row = cur.fetchone()
    if not cap_row:
        raise Blocked("universe_missing", f"No universe capture observed on or before {cutoff}")

    captured_at = cap_row["captured_at"]
    cap_date = captured_at[:10]

    # Check staleness: max 62 days
    dt_cutoff = datetime.fromisoformat(cutoff_date)
    dt_cap = datetime.fromisoformat(cap_date)
    days_old = (dt_cutoff - dt_cap).days
    if days_old > 62:
        raise Blocked(
            "stale_universe",
            f"Latest universe capture {captured_at} is {days_old} days old at cutoff {cutoff} (exceeds 62 days)",
        )

    # Query members at this latest capture
    query = """
        SELECT m.security_id, s.isin, m.nse_symbol as symbol, s.name as company_name,
               COALESCE(m.nse_sector, 'UNCLASSIFIED') as nse_sector,
               COALESCE(m.series, 'EQ') as series
        FROM universe_membership m
        JOIN securities s ON s.security_id = m.security_id
        WHERE m.index_name = ? AND m.observed_at = ?
        ORDER BY m.security_id ASC
    """
    cur_m = conn.execute(query, (index_name, captured_at))
    rows = [dict(r) for r in cur_m.fetchall()]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.set_index("security_id", drop=False)
    return df
