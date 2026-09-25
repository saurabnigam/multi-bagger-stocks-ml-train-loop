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

PLACEHOLDER_SYMBOL_PREFIX = "DUMMY"


def validate_isin(isin: Any) -> bool:
    """Structural + check-digit validation of an ISIN (ISO 6166).

    A valid ISIN is exactly 12 characters: a 2-letter country code, 9 further
    alphanumeric characters, and a 1-digit check digit. The check digit is the
    standard Luhn-style checksum computed over the numeral string obtained by
    mapping each letter to two digits (A=10 .. Z=35) and leaving digits as-is.

    Task T9 / decision D10: NSE's own placeholder rows for entities mid-demerger
    (e.g. "Dummy HEG Ltd.", symbol DUMMYHEG, ISIN DUM545A01024) are syntactically
    12-character codes but fail this check digit -- the same signal used by
    quant.model.screens to exclude them with reason index_placeholder.
    """
    if not isinstance(isin, str):
        return False
    code = isin.strip().upper()
    if len(code) != 12:
        return False
    if not code[:2].isalpha():
        return False
    if not code[2:11].isalnum():
        return False
    if not code[11].isdigit():
        return False

    numeral_chars = []
    for ch in code:
        if ch.isdigit():
            numeral_chars.append(ch)
        else:
            numeral_chars.append(str(ord(ch) - ord("A") + 10))
    numeral = "".join(numeral_chars)

    total = 0
    for i, ch in enumerate(numeral[::-1]):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_placeholder_symbol(symbol: Any) -> bool:
    """NSE lists index-placeholder rows for spun-off entities before the new listing
    has prices (task T9 / decision D10), e.g. "Dummy HEG Ltd." under symbol DUMMYHEG."""
    if pd.isna(symbol):
        return False
    return str(symbol).strip().upper().startswith(PLACEHOLDER_SYMBOL_PREFIX)


def is_index_placeholder(symbol: Any, isin: Any) -> bool:
    """True when a member is an NSE index-placeholder row: symbol prefixed DUMMY, or
    -- when an ISIN is actually present -- an ISIN that fails check-digit validation.
    A missing/blank ISIN is not itself placeholder evidence; only a present but
    malformed one is (MASTER_SPEC 6.1 exclusion reasons; task T9 / decision D10)."""
    if is_placeholder_symbol(symbol):
        return True
    if pd.isna(isin):
        return False
    isin_str = str(isin).strip()
    if not isin_str:
        return False
    return not validate_isin(isin_str)


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


def _upsert_symbol(ctx: Any, security_id: int, symbol: str, observed_date: str) -> None:
    """Maintain one open symbol_history row per security (valid_to NULL).

    Earlier releases inserted a row per capture without closing the previous one, leaving
    several open rows with the same symbol. Redundant duplicates are closed with a
    zero-length interval (valid_to = valid_from) so the earliest open row stays the
    single current symbol; every change is journaled through update_control.
    """
    from quant.db.core import update_control

    open_rows = ctx.conn.execute(
        "SELECT nse_symbol, valid_from FROM symbol_history WHERE security_id = ? AND valid_to IS NULL "
        "ORDER BY valid_from",
        (security_id,),
    ).fetchall()
    keep = None
    for row in open_rows:
        if row["nse_symbol"] == symbol and keep is None:
            keep = row
            continue
        if row["nse_symbol"] == symbol:
            update_control(ctx, "symbol_history", {"security_id": security_id, "valid_from": row["valid_from"]},
                           {"valid_to": row["valid_from"]})
        else:
            update_control(ctx, "symbol_history", {"security_id": security_id, "valid_from": row["valid_from"]},
                           {"valid_to": max(observed_date, row["valid_from"])})
    if keep is None:
        exists = ctx.conn.execute(
            "SELECT 1 FROM symbol_history WHERE security_id = ? AND valid_from = ?", (security_id, observed_date)
        ).fetchone()
        if exists is None:
            ctx.conn.execute(
                "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                "VALUES (?, ?, ?, ?, 'nifty500_csv')",
                (security_id, symbol, f"{symbol}.NS", observed_date),
            )


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

        isin_to_security_id: dict[str, int] = {}
        symbol_to_isin: dict[str, str] = {}

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

            isin_to_security_id[str(r["isin"])] = int(sec_id)
            symbol_to_isin[str(r["symbol"]).strip().upper()] = str(r["isin"])

            # symbol_history keeps exactly one open row per security: an unchanged symbol is
            # a no-op, a changed symbol closes the open row and opens a new one.
            _upsert_symbol(ctx, int(sec_id), str(r["symbol"]), captured_date)

            # Insert into universe_membership
            ctx.conn.execute(
                "INSERT OR IGNORE INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, series, source, source_sha256) "
                "VALUES (?, ?, ?, 'NIFTY500', ?, ?, ?, 'nse_csv', ?)",
                (captured_date, meta["captured_at"], sec_id, r["symbol"], r["nse_sector"], r["series"], meta["sha256"]),
            )

        # Task T9 / decision D10: NSE placeholder rows (e.g. DUMMYHEG for a
        # mid-demerger spin-off) get one WARN data_quality_events row each, recorded
        # as corporate-action evidence for the parent member the placeholder most
        # likely refers to -- its own symbol with the DUMMY prefix removed, when that
        # symbol is itself a member of this capture. Import kept local: quant.data.gates
        # does not otherwise depend on quant.data.universe, and vice versa.
        from quant.data.gates import record_event

        for _, r in df.iterrows():
            symbol = str(r["symbol"])
            isin = str(r["isin"])
            if not is_index_placeholder(symbol, isin):
                continue

            own_security_id = isin_to_security_id.get(isin)
            parent_symbol = None
            parent_security_id = None
            if is_placeholder_symbol(symbol):
                candidate = symbol.strip().upper()[len(PLACEHOLDER_SYMBOL_PREFIX):]
                candidate_isin = symbol_to_isin.get(candidate)
                if candidate_isin is not None:
                    parent_symbol = candidate
                    parent_security_id = isin_to_security_id.get(candidate_isin)

            record_event(
                ctx,
                code="INDEX_PLACEHOLDER",
                severity="WARN",
                detail={
                    "placeholder_symbol": symbol,
                    "placeholder_isin": isin,
                    "parent_symbol": parent_symbol,
                },
                security_id=parent_security_id if parent_security_id is not None else own_security_id,
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
