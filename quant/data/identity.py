from __future__ import annotations

import sqlite3
from typing import Any


def upsert_security(
    ctx: Any,
    isin: str,
    name: str,
    symbol: str,
    observed_at: str,
) -> int:
    """Upsert security by ISIN and track symbol history with valid_from/valid_to."""
    conn: sqlite3.Connection = ctx.conn
    obs_date = observed_at[:10]
    yahoo_t = f"{symbol}.NS"

    conn.execute("SAVEPOINT quant_upsert_security")
    try:
        cur = conn.execute("SELECT security_id, name, last_seen FROM securities WHERE isin = ?", (isin,))
        row = cur.fetchone()
        if row:
            sec_id = row["security_id"]
            conn.execute(
                "UPDATE securities SET last_seen = max(last_seen, ?), name = ? WHERE security_id = ?",
                (obs_date, name, sec_id),
            )
            # Check symbol history
            cur_sym = conn.execute(
                "SELECT nse_symbol, valid_from FROM symbol_history WHERE security_id = ? ORDER BY valid_from DESC LIMIT 1",
                (sec_id,),
            )
            sym_row = cur_sym.fetchone()
            if sym_row and sym_row["nse_symbol"] != symbol:
                # Close previous symbol validity
                conn.execute(
                    "UPDATE symbol_history SET valid_to = ? WHERE security_id = ? AND valid_to IS NULL",
                    (obs_date, sec_id),
                )
                conn.execute(
                    "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                    "VALUES (?, ?, ?, ?, 'upsert')",
                    (sec_id, symbol, yahoo_t, obs_date),
                )
            elif not sym_row:
                conn.execute(
                    "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                    "VALUES (?, ?, ?, ?, 'upsert')",
                    (sec_id, symbol, yahoo_t, obs_date),
                )
            conn.execute("RELEASE quant_upsert_security")
            return sec_id
        else:
            cur_ins = conn.execute(
                "INSERT INTO securities (isin, name, first_seen, last_seen, status) VALUES (?, ?, ?, ?, 'listed')",
                (isin, name, obs_date, obs_date),
            )
            sec_id = cur_ins.lastrowid
            conn.execute(
                "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                "VALUES (?, ?, ?, ?, 'upsert')",
                (sec_id, symbol, yahoo_t, obs_date),
            )
            conn.execute("RELEASE quant_upsert_security")
            return sec_id
    except Exception:
        conn.execute("ROLLBACK TO quant_upsert_security")
        conn.execute("RELEASE quant_upsert_security")
        raise


def resolve_security_id(
    conn: sqlite3.Connection,
    *,
    isin: str | None = None,
    symbol: str | None = None,
    cutoff: str,
) -> int | None:
    """Resolve security_id by ISIN or by point-in-time NSE symbol."""
    cutoff_date = cutoff[:10]
    if isin:
        cur = conn.execute("SELECT security_id FROM securities WHERE isin = ?", (isin,))
        r = cur.fetchone()
        return r["security_id"] if r else None
    elif symbol:
        cur = conn.execute(
            "SELECT security_id FROM symbol_history "
            "WHERE nse_symbol = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
            "ORDER BY valid_from DESC LIMIT 1",
            (symbol, cutoff_date, cutoff_date),
        )
        r = cur.fetchone()
        return r["security_id"] if r else None
    return None


def yahoo_ticker(conn: sqlite3.Connection, security_id: int, cutoff: str) -> str:
    """Resolve point-in-time Yahoo ticker for a given security_id."""
    cutoff_date = cutoff[:10]
    cur = conn.execute(
        "SELECT yahoo_ticker FROM symbol_history "
        "WHERE security_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
        "ORDER BY valid_from DESC LIMIT 1",
        (security_id, cutoff_date, cutoff_date),
    )
    r = cur.fetchone()
    if r:
        return r["yahoo_ticker"]

    # Fallback to any recorded symbol
    cur_fb = conn.execute(
        "SELECT yahoo_ticker FROM symbol_history WHERE security_id = ? ORDER BY valid_from DESC LIMIT 1",
        (security_id,),
    )
    r_fb = cur_fb.fetchone()
    if r_fb:
        return r_fb["yahoo_ticker"]

    raise ValueError(f"No Yahoo ticker found for security_id {security_id}")


def tracked_securities(
    conn: sqlite3.Connection,
    cutoff: str,
    horizons: list[int],
) -> list[int]:
    """Derive all tracked securities: current members UNION unmatured cohort members UNION open orders."""
    tracked = set()
    cutoff_iso = cutoff if "T" in cutoff else f"{cutoff}T23:59:59.999999Z"
    cutoff_month = cutoff[:7]

    # 1. Members of latest universe capture on or before cutoff
    cur_m = conn.execute(
        "SELECT DISTINCT security_id FROM universe_membership WHERE observed_at <= ?",
        (cutoff_iso,),
    )
    tracked.update(r[0] for r in cur_m.fetchall())

    # 2. Members of published cohorts with at least one unmatured horizon
    cur_c = conn.execute(
        "SELECT cohort_id, as_of FROM cohorts WHERE as_of <= ?",
        (cutoff[:10],),
    )
    cohorts = cur_c.fetchall()
    for c in cohorts:
        c_as_of = c["as_of"]
        y, m = int(c_as_of[:4]), int(c_as_of[5:7])

        has_unmatured = False
        for h in horizons:
            total_m = m + h
            end_y = y + (total_m - 1) // 12
            end_m = (total_m - 1) % 12 + 1
            end_month = f"{end_y:04d}-{end_m:02d}"
            if end_month >= cutoff_month:
                has_unmatured = True
                break

        if has_unmatured:
            cur_cm = conn.execute(
                "SELECT DISTINCT security_id FROM universe_membership WHERE as_of = ?",
                (c_as_of,),
            )
            tracked.update(r[0] for r in cur_cm.fetchall())

    # 3. Securities with open / pending orders
    cur_o = conn.execute(
        "SELECT DISTINCT security_id FROM portfolio_orders WHERE status = 'pending'"
    )
    tracked.update(r[0] for r in cur_o.fetchall())

    return sorted(tracked)
