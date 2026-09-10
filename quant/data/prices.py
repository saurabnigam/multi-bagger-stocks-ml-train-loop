"""Price store, basis normalization, TRI and liquidity calculations (C03).

Basis (MASTER_SPEC 4.3): the store keeps the unadjusted quoted close
(`close_raw`), raw volume and cash dividend per share, plus the split ratio on
the ex-date. Yahoo's `Close` is split-adjusted for the whole downloaded window;
`normalize_source` undoes the *future* splits inside the window so that
`close_raw` is the historically quoted price. Total return is then
``TRI[d] = TRI[d-1] * split_ratio[d] * (close_raw[d] + dividend_raw[d]) / close_raw[d-1]``.

Every observation carries ``observed_at`` (its vintage). Readers pass
``vintage_at`` and see, for each date, the latest version observed on or before
that timestamp; nothing observed later can leak into a cohort.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import numpy as np
import pandas as pd

from quant.data.yahoo import YahooClient
from quant.errors import Refused
from quant.run import RunContext
from quant.types import Check, Draft, Result


PRICE_SCHEMA_PATH = "quant/db/price_schema.sql"
YAHOO_FIELDS = {"Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"}
UNEXPLAINED_REVISION_TOL = 0.02


def normalize_source(source: pd.DataFrame, metadata: Dict[str, Any]) -> pd.DataFrame:
    """Normalize vendor prices to canonical unadjusted quoted basis."""
    close_basis = metadata.get("close_basis")
    if close_basis not in ("split_adjusted", "raw"):
        raise Refused("unknown_basis", f"Unknown close_basis: {close_basis}")

    volume_basis = metadata.get("volume_basis", "delivered")
    dividend_basis = metadata.get("dividend_basis", "delivered")

    df = source.copy()
    if "date" in df.columns:
        df["date"] = df["date"].astype(str).str[:10]
    else:
        df["date"] = pd.Index(df.index).astype(str).str[:10]
    df = df.sort_values("date").reset_index(drop=True)

    n = len(df)

    def _col(*names: str, default: Any) -> np.ndarray:
        for nm in names:
            if nm in df.columns:
                return pd.to_numeric(df[nm], errors="coerce").values.astype(float)
        return np.full(n, default, dtype=float)

    splits = _col("split_ratio", "splits", "Stock Splits", default=1.0)
    dividends = _col("dividend", "dividends", "Dividends", default=0.0)
    closes = _col("close", "Close", default=np.nan)
    volumes = _col("volume", "Volume", default=0.0)
    adj_closes = _col("adj_close", "Adj Close", default=np.nan)

    splits = np.where(pd.isna(splits) | (splits <= 0), 1.0, splits)
    dividends = np.where(pd.isna(dividends), 0.0, dividends)
    volumes = np.where(pd.isna(volumes), 0.0, volumes)

    if close_basis == "split_adjusted":
        # Multiplier to undo every split strictly after each date: prod(splits[e] for e > d)
        rev_cum = np.cumprod(splits[::-1])[::-1]          # prod(splits[d:])
        future_mult = rev_cum / splits                      # prod(splits[d+1:])
        close_raw = closes * future_mult
        volume_raw = volumes / future_mult if volume_basis == "split_adjusted" else volumes
        dividend_raw = dividends * future_mult if dividend_basis == "split_adjusted" else dividends
    else:
        close_raw = closes
        volume_raw = volumes
        dividend_raw = dividends
        future_mult = np.ones(n)

    def _ohlc(*names: str) -> np.ndarray:
        for nm in names:
            if nm in df.columns:
                vals = pd.to_numeric(df[nm], errors="coerce").values.astype(float)
                return vals * future_mult if close_basis == "split_adjusted" else vals
        return close_raw

    sid = int(metadata["security_id"])
    obs_at = str(metadata["observed_at"])
    cap_id = str(metadata.get("capture_id", ""))
    sha = str(metadata.get("source_sha256", ""))

    norm_df = pd.DataFrame({
        "security_id": sid,
        "date": df["date"],
        "open_raw": _ohlc("open", "Open"),
        "high_raw": _ohlc("high", "High"),
        "low_raw": _ohlc("low", "Low"),
        "close_raw": close_raw,
        "volume_raw": volume_raw,
        "dividend_raw": dividend_raw,
        "split_ratio": splits,
        "yahoo_close": closes,
        "yahoo_adj_close": adj_closes,
        "observed_at": obs_at,
        "capture_id": cap_id,
        "source_sha256": sha,
    })
    norm_df = norm_df[pd.notna(norm_df["close_raw"])].reset_index(drop=True)
    return norm_df


def frames_from_download(df: pd.DataFrame, tickers: Iterable[str]) -> Dict[str, pd.DataFrame]:
    """Split a yfinance ``download`` frame (single or multi-ticker) into per-ticker frames."""
    out: Dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out
    tickers = list(tickers)
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(map(str, df.columns.get_level_values(0)))
        field_level = 0 if lvl0 & YAHOO_FIELDS else 1
        ticker_level = 1 - field_level
        present = set(map(str, df.columns.get_level_values(ticker_level)))
        for t in tickers:
            if t not in present:
                continue
            sub = df.xs(t, axis=1, level=ticker_level)
            sub = sub.dropna(how="all")
            if not sub.empty:
                out[t] = sub.copy()
    else:
        if len(tickers) == 1:
            out[tickers[0]] = df.dropna(how="all").copy()
        else:
            raise Refused("ambiguous_download", "Flat download frame with several tickers")
    return out


class PriceStore:
    """Isolated price cache manager using price_schema.sql."""

    def __init__(self, path: Path | str, state_conn: sqlite3.Connection):
        self.path = Path(path)
        self.state_conn = state_conn
        self._init_db()

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = PRICE_SCHEMA_PATH
        if not os.path.exists(schema_path):
            schema_path = os.path.join(os.path.dirname(__file__), "../db/price_schema.sql")
        with open(schema_path, "r") as f:
            ddl = f.read()
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(ddl)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _insert_row(cur: sqlite3.Cursor, row: pd.Series) -> int:
        cur.execute(
            """
            INSERT OR IGNORE INTO prices_daily (
                security_id, date, open_raw, high_raw, low_raw, close_raw,
                volume_raw, dividend_raw, split_ratio, yahoo_close, yahoo_adj_close,
                observed_at, capture_id, source_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["security_id"]),
                str(row["date"]),
                float(row["open_raw"]),
                float(row["high_raw"]),
                float(row["low_raw"]),
                float(row["close_raw"]),
                float(row["volume_raw"]),
                float(row["dividend_raw"]),
                float(row["split_ratio"]),
                None if pd.isna(row["yahoo_close"]) else float(row["yahoo_close"]),
                None if pd.isna(row["yahoo_adj_close"]) else float(row["yahoo_adj_close"]),
                str(row["observed_at"]),
                str(row["capture_id"]),
                str(row["source_sha256"]),
            ),
        )
        return cur.rowcount

    def ingest(self, ctx: RunContext, source: pd.DataFrame, metadata: Dict[str, Any]) -> Result:
        """Normalize and ingest a raw price series as a new vintage (no reconciliation)."""
        norm_df = normalize_source(source, metadata)
        with self.conn() as p_conn:
            cur = p_conn.cursor()
            for _, row in norm_df.iterrows():
                self._insert_row(cur, row)
        return Result(status="ok", counts={"rows": len(norm_df)},
                      details={"message": f"Ingested {len(norm_df)} price rows"})

    def reconcile(
        self,
        ctx: RunContext,
        normalized: pd.DataFrame,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Result:
        """Reconcile a normalized vintage against stored history.

        New dates are appended. For an existing date, a close within 2% of the
        latest stored version is accepted as a new observation of the same fact
        (byte-identical rows are ignored by the PK). A larger difference without
        an accepted revision is quarantined and never enters prices_daily.
        `metadata` is accepted for callers that still pass a raw source frame.
        """
        norm_df = normalize_source(normalized, metadata) if metadata is not None else normalized
        quarantined = 0
        accepted = 0
        with self.conn() as p_conn:
            cur = p_conn.cursor()
            for _, row in norm_df.iterrows():
                sid = int(row["security_id"])
                d = str(row["date"])
                new_close = float(row["close_raw"])
                cur.execute(
                    "SELECT close_raw FROM prices_daily WHERE security_id = ? AND date = ? "
                    "ORDER BY observed_at DESC LIMIT 1",
                    (sid, d),
                )
                prev = cur.fetchone()
                if prev:
                    old_close = float(prev[0])
                    if old_close > 0 and abs(new_close - old_close) / old_close > UNEXPLAINED_REVISION_TOL:
                        cur.execute(
                            "SELECT 1 FROM accepted_price_revisions WHERE security_id = ? AND date = ? AND observed_at = ?",
                            (sid, d, str(row["observed_at"])),
                        )
                        if cur.fetchone() is None:
                            cur.execute(
                                """
                                INSERT OR IGNORE INTO prices_daily_quarantine (
                                    security_id, date, observed_at, payload_json, reason, source_sha256
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    sid, d, str(row["observed_at"]),
                                    row.to_json(),
                                    f"unexplained_revision: {old_close} -> {new_close}",
                                    str(row["source_sha256"]),
                                ),
                            )
                            quarantined += 1
                            continue
                    if abs(new_close - old_close) <= 1e-12:
                        # unchanged fact: do not duplicate the observation
                        continue
                self._insert_row(cur, row)
                accepted += 1

        status = "quarantined" if quarantined > 0 else "ok"
        return Result(
            status=status,
            counts={"accepted": accepted, "quarantined": quarantined, "rows": len(norm_df)},
            details={"message": f"Reconciled {len(norm_df)} rows; accepted {accepted}, quarantined {quarantined}"},
        )

    # -------------------------------------------------------------- acquisition
    def _archive_download(self, ctx: RunContext, raw: pd.DataFrame, start: str, end: str) -> tuple[str, str]:
        """Archive the exact downloaded frame (gzip CSV) and register the capture."""
        archive_dir = Path(ctx.cfg.paths.archive_dir) / "captures" / "prices"
        archive_dir.mkdir(parents=True, exist_ok=True)
        csv_bytes = raw.to_csv().encode("utf-8")
        sha = hashlib.sha256(csv_bytes).hexdigest()
        capture_id = f"cap_prices_{sha[:16]}"
        path = archive_dir / f"{capture_id}.csv.gz"
        if not path.exists():
            with gzip.open(path, "wb") as f:
                f.write(csv_bytes)
        meta = {
            "capture_id": capture_id,
            "fetched_at": raw.attrs.get("fetched_at", ctx.clock.iso()),
            "start": start,
            "end": end,
            "tickers": raw.attrs.get("tickers", []),
            "sha256": sha,
            "source": raw.attrs.get("source", "yfinance"),
        }
        (archive_dir / f"{capture_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if ctx.conn is not None:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                "VALUES (?, ?, 'yahoo_prices', ?, ?, ?, ?)",
                (capture_id, meta["fetched_at"], str(path), sha, str(meta["source"]), ctx.run_id),
            )
        return capture_id, sha

    def _acquire(
        self,
        ctx: RunContext,
        client: YahooClient,
        ticker_map: Dict[int, str],
        start: str,
        end: str,
    ) -> Result:
        tickers = sorted(set(ticker_map.values()))
        if not tickers:
            return Result(status="ok", counts={"securities": 0, "rows": 0})
        raw = client.download_batch(tickers, start=start, end=end)
        if raw is None or raw.empty:
            return Result(status="empty", counts={"securities": 0, "rows": 0},
                          details={"message": "Download returned no rows"})
        fetched_at = str(raw.attrs.get("fetched_at", ctx.clock.iso()))
        capture_id, sha = self._archive_download(ctx, raw, start, end)
        frames = frames_from_download(raw, tickers)

        rows = accepted = quarantined = covered = 0
        for sid, ticker in ticker_map.items():
            frame = frames.get(ticker)
            if frame is None or frame.empty:
                continue
            norm = normalize_source(
                frame,
                {
                    "security_id": sid,
                    "observed_at": fetched_at,
                    "capture_id": capture_id,
                    "source_sha256": sha,
                    "close_basis": "split_adjusted",
                    "volume_basis": "split_adjusted",
                    "dividend_basis": "split_adjusted",
                },
            )
            if norm.empty:
                continue
            res = self.reconcile(ctx, norm)
            covered += 1
            rows += len(norm)
            accepted += res.counts.get("accepted", 0)
            quarantined += res.counts.get("quarantined", 0)

        return Result(
            status="ok",
            counts={"securities": covered, "requested": len(ticker_map), "rows": rows,
                    "accepted": accepted, "quarantined": quarantined},
            details={"capture_id": capture_id, "sha256": sha, "start": start, "end": end},
        )

    def backfill(self, ctx: RunContext, client: YahooClient, tickers: Dict[int, str], start: str, end: str) -> Result:
        """Download and reconcile the full history window for the given securities."""
        return self._acquire(ctx, client, tickers, start, end)

    def update(self, ctx: RunContext, client: YahooClient, security_ids: List[int], through: str) -> Result:
        """Bring the store up to ``through`` for the given securities.

        The download window starts at the configured lookback before ``through``
        (or ``history_start`` for securities with no history) so that every
        split inside the window is visible to the basis normalisation.
        """
        from quant.data.identity import yahoo_ticker

        ticker_map: Dict[int, str] = {}
        for sid in security_ids:
            try:
                t = yahoo_ticker(ctx.conn, int(sid), through)
            except ValueError:
                t = None
            if t:
                ticker_map[int(sid)] = t
        if not ticker_map:
            return Result(status="ok", counts={"securities": 0, "rows": 0})

        history_start = str(getattr(ctx.cfg.yahoo, "history_start", "2015-01-01"))
        lookback_months = int(getattr(ctx.cfg.yahoo, "lookback_months", 13))
        latest = self.latest_date(list(ticker_map.keys()))
        window_start = (pd.Timestamp(through) - pd.DateOffset(months=lookback_months)).strftime("%Y-%m-%d")
        start = history_start if latest is None else min(window_start, latest)
        # yfinance ``end`` is exclusive; include the through session itself
        end_excl = (pd.Timestamp(through) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return self._acquire(ctx, client, ticker_map, start, end_excl)

    # ------------------------------------------------------------------ reads
    def latest_date(self, security_ids: List[int]) -> Optional[str]:
        if not security_ids:
            return None
        placeholders = ",".join("?" for _ in security_ids)
        with self.conn() as p_conn:
            row = p_conn.execute(
                f"SELECT min(md) FROM (SELECT max(date) AS md FROM prices_daily "
                f"WHERE security_id IN ({placeholders}) GROUP BY security_id)",
                [int(s) for s in security_ids],
            ).fetchone()
        return row[0] if row and row[0] else None

    def _versioned(self, security_ids: List[int], start: str, end: str, vintage_at: str, cols: str) -> pd.DataFrame:
        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, {cols},
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date >= ? AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, {cols}
        FROM ranked
        WHERE rn = 1
        ORDER BY date ASC
        """
        params = list(security_ids) + [start, end, vintage_at]
        with self.conn() as p_conn:
            return pd.read_sql_query(query, p_conn, params=params)

    def close_raw(self, security_ids: List[int], start: str, end: str, vintage_at: str) -> pd.DataFrame:
        """Return raw closing prices indexed by date with security_id columns."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]
        df = self._versioned(security_ids, start, end, vintage_at, "close_raw")
        if df.empty:
            return pd.DataFrame(columns=security_ids)
        return df.pivot(index="date", columns="security_id", values="close_raw").reindex(columns=security_ids)

    def volume(self, security_ids: List[int], start: str, end: str, vintage_at: str) -> pd.DataFrame:
        """Return raw trading volume indexed by date with security_id columns."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]
        df = self._versioned(security_ids, start, end, vintage_at, "volume_raw")
        if df.empty:
            return pd.DataFrame(columns=security_ids)
        return df.pivot(index="date", columns="security_id", values="volume_raw").reindex(columns=security_ids)

    def close_split(self, security_ids: List[int], start: str, end: str, vintage_at: str) -> pd.DataFrame:
        """Return split-adjusted close rebased to end date."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]
        df = self._versioned(security_ids, start, end, vintage_at, "close_raw, split_ratio")
        if df.empty:
            return pd.DataFrame(columns=security_ids)
        out_series = {}
        for sid, group in df.groupby("security_id"):
            g = group.sort_values("date").reset_index(drop=True)
            splits = g["split_ratio"].values.astype(float)
            closes = g["close_raw"].values.astype(float)
            rev_cum = np.cumprod(splits[::-1])[::-1]
            future_mult = rev_cum / splits
            out_series[sid] = pd.Series(closes / future_mult, index=g["date"])
        return pd.DataFrame(out_series).reindex(columns=security_ids)

    def tri(self, security_ids: List[int], start: str, end: str, vintage_at: str) -> pd.DataFrame:
        """Total Return Index starting at 100.0 on each security's first bar in the window.

        TRI[d] = TRI[d-1] * split_ratio[d] * (close_raw[d] + dividend_raw[d]) / close_raw[d-1]
        Ratios of TRI across dates within one call are total returns; the base
        date differs per security, so never compare TRI levels across calls
        with different ``start`` values.
        """
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]
        df = self._versioned(security_ids, start, end, vintage_at, "close_raw, dividend_raw, split_ratio")
        if df.empty:
            return pd.DataFrame(columns=security_ids)
        tri_series = {}
        for sid, group in df.groupby("security_id"):
            g = group.sort_values("date").reset_index(drop=True)
            closes = g["close_raw"].values.astype(float)
            divs = g["dividend_raw"].values.astype(float)
            splits = g["split_ratio"].values.astype(float)
            factors = np.ones(len(g))
            prev = closes[:-1]
            valid = prev > 0
            factors[1:] = np.where(valid, splits[1:] * (closes[1:] + divs[1:]) / np.where(valid, prev, 1.0), 1.0)
            tri_series[sid] = pd.Series(100.0 * np.cumprod(factors), index=g["date"])
        return pd.DataFrame(tri_series).reindex(columns=security_ids)

    def tri_at(self, security_ids: List[int], date: str, vintage_at: str, base_start: str) -> pd.Series:
        """TRI level on the last session on or before ``date`` from a common base start."""
        df = self.tri(security_ids, start=base_start, end=date, vintage_at=vintage_at)
        if df.empty:
            return pd.Series(np.nan, index=[int(s) for s in security_ids])
        return df.ffill().iloc[-1].reindex([int(s) for s in security_ids])

    def adv_inr(self, security_ids: List[int], as_of: str, vintage_at: str, window: int = 63) -> pd.DataFrame:
        """Average daily turnover (INR) and positive-volume session count over the trailing window."""
        cols = [f"adv_{window}_inr", f"n_days_{window}", f"pos_sessions_{window}"]
        if not security_ids:
            return pd.DataFrame(columns=cols)
        security_ids = [int(x) for x in security_ids]
        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, close_raw, volume_raw,
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, close_raw, volume_raw
        FROM ranked
        WHERE rn = 1
        ORDER BY date DESC
        """
        params = list(security_ids) + [as_of, vintage_at]
        with self.conn() as p_conn:
            df = pd.read_sql_query(query, p_conn, params=params)

        out = {}
        for sid in security_ids:
            s_rows = df[df["security_id"] == sid].head(window)
            if s_rows.empty:
                out[sid] = {cols[0]: np.nan, cols[1]: 0, cols[2]: 0}
            else:
                turnover = s_rows["close_raw"] * s_rows["volume_raw"].fillna(0.0)
                out[sid] = {
                    cols[0]: float(turnover.mean()),
                    cols[1]: int(len(s_rows)),
                    cols[2]: int((s_rows["volume_raw"].fillna(0.0) > 0).sum()),
                }
        res = pd.DataFrame.from_dict(out, orient="index")
        res.index.name = "security_id"
        return res

    def quarantined_securities(self, since: str, through: str) -> List[int]:
        """Distinct securities with quarantined observations dated in (since, through]."""
        with self.conn() as p_conn:
            rows = p_conn.execute(
                "SELECT DISTINCT security_id FROM prices_daily_quarantine WHERE date > ? AND date <= ?",
                (since, through),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def session_dates(self, start: str, end: str, min_securities: int = 1) -> List[str]:
        """Dates on which at least ``min_securities`` securities have a close (empirical sessions)."""
        with self.conn() as p_conn:
            rows = p_conn.execute(
                "SELECT date FROM prices_daily WHERE date >= ? AND date <= ? "
                "GROUP BY date HAVING count(DISTINCT security_id) >= ? ORDER BY date",
                (start, end, int(min_securities)),
            ).fetchall()
        return [r[0] for r in rows]

    # -------------------------------------------------------------- manifests
    def _manifest_rows(self, vintage_at: str) -> list:
        with self.conn() as p_conn:
            cur = p_conn.execute(
                """
                WITH ranked AS (
                    SELECT security_id, date, close_raw, volume_raw, split_ratio, dividend_raw,
                           ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) AS rn
                    FROM prices_daily WHERE observed_at <= ?
                )
                SELECT security_id, date, close_raw, volume_raw, split_ratio, dividend_raw
                FROM ranked WHERE rn = 1 ORDER BY security_id ASC, date ASC
                """,
                (vintage_at,),
            )
            return [[r[0], r[1], float(r[2]), float(r[3] or 0.0), float(r[4]), float(r[5])] for r in cur.fetchall()]

    def manifest_hash(self, vintage_at: str) -> tuple[str, int]:
        rows = self._manifest_rows(vintage_at)
        rows_json = json.dumps(rows, sort_keys=True)
        return hashlib.sha256(rows_json.encode("utf-8")).hexdigest(), len(rows)

    def manifest_write(self, output: Path | str, vintage_at: str) -> str:
        """Write price manifest JSON for the latest versions observed up to vintage_at."""
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        content_hash, n = self.manifest_hash(vintage_at)
        with open(out_path, "w") as f:
            json.dump({"vintage_at": vintage_at, "sha256": content_hash, "row_count": n}, f, indent=2)
        return content_hash

    def manifest_verify(self, manifest: Path | str) -> Check:
        """Verify the store reproduces a committed manifest; mismatch is SOURCE_CHANGED."""
        with open(Path(manifest), "r") as f:
            data = json.load(f)
        expected_hash = data["sha256"]
        current_hash, _ = self.manifest_hash(data["vintage_at"])
        ok = current_hash == expected_hash
        return Check(
            id="price_manifest",
            status="PASS" if ok else "FAIL",
            observed=current_hash,
            expected=expected_hash,
            reason="Price manifest matches" if ok else "SOURCE_CHANGED: price manifest hash mismatch",
            blocking=True,
        )


def monthly_panel(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    """Build the prices_monthly panel for a draft cohort.

    ``tri`` is the total-return index from the configured history start so
    that ratios between two months' panels are total returns.
    """
    store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    sids = sorted([int(x) for x in draft.members["security_id"].unique()])
    base_start = str(getattr(ctx.cfg.yahoo, "history_start", "2015-01-01"))

    close_df = store.close_raw(sids, start=draft.as_of, end=draft.as_of, vintage_at=draft.knowledge_cutoff)
    tri_s = store.tri_at(sids, date=draft.as_of, vintage_at=draft.knowledge_cutoff, base_start=base_start)
    adv_df = store.adv_inr(sids, as_of=draft.as_of, vintage_at=draft.knowledge_cutoff, window=63)

    attrs = {}
    if ctx.conn is not None:
        placeholders = ",".join("?" for _ in sids)
        rows = ctx.conn.execute(
            f"""
            WITH ranked AS (
                SELECT security_id, mcap_inr, shares_out,
                       ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY captured_at DESC) AS rn
                FROM security_attributes WHERE captured_at <= ? AND security_id IN ({placeholders})
            ) SELECT security_id, mcap_inr, shares_out FROM ranked WHERE rn = 1
            """,
            [draft.knowledge_cutoff, *sids],
        ).fetchall()
        attrs = {int(r[0]): (r[1], r[2]) for r in rows}

    manifest_sha = draft.source_refs.get("price_manifest_sha", "")
    records = []
    for sid in sids:
        c_val = np.nan
        if not close_df.empty and sid in close_df.columns and not close_df[sid].dropna().empty:
            c_val = float(close_df[sid].dropna().iloc[-1])
        t_val = float(tri_s.get(sid, np.nan)) if sid in tri_s.index else np.nan
        adv_row = adv_df.loc[sid] if sid in adv_df.index else {}
        mcap, shares = attrs.get(sid, (None, None))
        records.append({
            "cohort_id": draft.cohort_id,
            "as_of": draft.as_of,
            "security_id": sid,
            "close_raw": c_val,
            "tri": t_val,
            "adv_63_inr": adv_row.get("adv_63_inr", np.nan) if len(adv_row) else np.nan,
            "n_days_63": int(adv_row.get("n_days_63", 0)) if len(adv_row) else 0,
            "mcap_inr": mcap,
            "shares_out": shares,
            "quote_legacy": None,
            "source": "yahoo",
            "price_manifest_sha": manifest_sha,
            "run_id": ctx.run_id,
        })
    return pd.DataFrame(records)
