"""Price store, basis normalization, TRI and liquidity calculations."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from quant.data.yahoo import YahooClient
from quant.errors import Refused
from quant.run import RunContext
from quant.types import Check, Draft, Result


PRICE_SCHEMA_PATH = "quant/db/price_schema.sql"


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
        df = df.sort_values("date").reset_index(drop=True)
    else:
        df["date"] = df.index.astype(str).str[:10]
        df = df.sort_values("date").reset_index(drop=True)

    n = len(df)
    splits = df["split_ratio"].values if "split_ratio" in df.columns else (df["splits"].values if "splits" in df.columns else np.ones(n))
    dividends = df["dividend"].values if "dividend" in df.columns else (df["dividends"].values if "dividends" in df.columns else np.zeros(n))
    closes = df["close"].values if "close" in df.columns else df["Close"].values
    volumes = df["volume"].values if "volume" in df.columns else (df["Volume"].values if "Volume" in df.columns else np.zeros(n))

    # Clean None/NaN in splits/dividends
    splits = np.where(pd.isna(splits) | (splits <= 0), 1.0, splits)
    dividends = np.where(pd.isna(dividends), 0.0, dividends)

    close_raw = np.zeros(n)
    volume_raw = np.zeros(n)
    dividend_raw = np.zeros(n)

    if close_basis == "split_adjusted":
        # Undo future splits: product(split_ratio[e] for e > d)
        for i in range(n):
            future_mult = float(np.prod(splits[i + 1:])) if i + 1 < n else 1.0
            close_raw[i] = float(closes[i]) * future_mult

            if volume_basis == "split_adjusted":
                volume_raw[i] = float(volumes[i]) / future_mult if future_mult > 0 else float(volumes[i])
            else:
                volume_raw[i] = float(volumes[i])

            if dividend_basis == "split_adjusted":
                dividend_raw[i] = float(dividends[i]) * future_mult
            else:
                dividend_raw[i] = float(dividends[i])
    else:  # raw
        close_raw = closes.astype(float)
        volume_raw = volumes.astype(float)
        dividend_raw = dividends.astype(float)

    open_raw = df["open"].values if "open" in df.columns else (df["Open"].values if "Open" in df.columns else close_raw)
    high_raw = df["high"].values if "high" in df.columns else (df["High"].values if "High" in df.columns else close_raw)
    low_raw = df["low"].values if "low" in df.columns else (df["Low"].values if "Low" in df.columns else close_raw)

    sid = int(metadata["security_id"])
    obs_at = str(metadata["observed_at"])
    cap_id = str(metadata.get("capture_id", ""))
    sha = str(metadata.get("source_sha256", ""))

    norm_df = pd.DataFrame({
        "security_id": sid,
        "date": df["date"],
        "open_raw": open_raw,
        "high_raw": high_raw,
        "low_raw": low_raw,
        "close_raw": close_raw,
        "volume_raw": volume_raw,
        "dividend_raw": dividend_raw,
        "split_ratio": splits,
        "yahoo_close": closes,
        "yahoo_adj_close": closes,
        "observed_at": obs_at,
        "capture_id": cap_id,
        "source_sha256": sha,
    })

    return norm_df


class PriceStore:
    """Isolated price cache manager using price_schema.sql."""

    def __init__(self, path: Path | str, state_conn: sqlite3.Connection):
        self.path = Path(path)
        self.state_conn = state_conn
        self._init_db()

    def _init_db(self) -> None:
        """Call canonical price DDL to initialize tables."""
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

    def ingest(self, ctx: RunContext, source: pd.DataFrame, metadata: Dict[str, Any]) -> Result:
        """Normalize and ingest raw price series into prices_daily."""
        norm_df = normalize_source(source, metadata)

        with self.conn() as p_conn:
            cur = p_conn.cursor()
            for _, row in norm_df.iterrows():
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
                        float(row["yahoo_close"]),
                        float(row["yahoo_adj_close"]),
                        str(row["observed_at"]),
                        str(row["capture_id"]),
                        str(row["source_sha256"]),
                    ),
                )

        return Result(
            status="ok",
            counts={"rows": len(norm_df)},
            details={"message": f"Ingested {len(norm_df)} price rows"},
        )

    def reconcile(
        self,
        ctx: RunContext,
        source: pd.DataFrame,
        metadata: Dict[str, Any],
    ) -> Result:
        """Reconcile new price observations against existing history; quarantine unexplained revisions."""
        norm_df = normalize_source(source, metadata)
        quarantined = 0

        with self.conn() as p_conn:
            cur = p_conn.cursor()
            for _, row in norm_df.iterrows():
                sid = int(row["security_id"])
                d = str(row["date"])
                new_close = float(row["close_raw"])

                # Check existing baseline
                cur.execute(
                    """
                    SELECT close_raw, observed_at
                    FROM prices_daily
                    WHERE security_id = ? AND date = ?
                    ORDER BY observed_at DESC
                    LIMIT 1
                    """,
                    (sid, d),
                )
                prev = cur.fetchone()
                if prev:
                    old_close = float(prev[0])
                    # If relative change > 2% without recorded action, quarantine
                    if abs(new_close - old_close) / old_close > 0.02:
                        payload = row.to_json()
                        reason = f"unexplained_revision: {old_close} -> {new_close}"
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO prices_daily_quarantine (
                                security_id, date, observed_at, payload_json, reason, source_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (sid, d, str(row["observed_at"]), payload, reason, str(row["source_sha256"])),
                        )
                        quarantined += 1
                        continue

                # If no revision discrepancy, insert into prices_daily
                cur.execute(
                    """
                    INSERT OR IGNORE INTO prices_daily (
                        security_id, date, open_raw, high_raw, low_raw, close_raw,
                        volume_raw, dividend_raw, split_ratio, yahoo_close, yahoo_adj_close,
                        observed_at, capture_id, source_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        d,
                        float(row["open_raw"]),
                        float(row["high_raw"]),
                        float(row["low_raw"]),
                        new_close,
                        float(row["volume_raw"]),
                        float(row["dividend_raw"]),
                        float(row["split_ratio"]),
                        float(row["yahoo_close"]),
                        float(row["yahoo_adj_close"]),
                        str(row["observed_at"]),
                        str(row["capture_id"]),
                        str(row["source_sha256"]),
                    ),
                )

        if quarantined > 0:
            return Result(
                status="quarantined",
                counts={"quarantined": quarantined, "accepted": len(norm_df) - quarantined},
                details={"message": f"Quarantined {quarantined} unexplained revisions"},
            )

        return Result(
            status="ok",
            counts={"accepted": len(norm_df)},
            details={"message": f"Reconciled {len(norm_df)} price rows"},
        )

    def close_raw(
        self,
        security_ids: List[int],
        start: str,
        end: str,
        vintage_at: str,
    ) -> pd.DataFrame:
        """Return raw closing prices indexed by date with security_id columns."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]

        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, close_raw,
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date >= ? AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, close_raw
        FROM ranked
        WHERE rn = 1
        ORDER BY date ASC
        """
        params = list(security_ids) + [start, end, vintage_at]

        with self.conn() as p_conn:
            df = pd.read_sql_query(query, p_conn, params=params)

        if df.empty:
            return pd.DataFrame(columns=security_ids)

        pivoted = df.pivot(index="date", columns="security_id", values="close_raw")
        return pivoted.reindex(columns=security_ids)

    def volume(
        self,
        security_ids: List[int],
        start: str,
        end: str,
        vintage_at: str,
    ) -> pd.DataFrame:
        """Return raw trading volume indexed by date with security_id columns."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]

        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, volume_raw,
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date >= ? AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, volume_raw
        FROM ranked
        WHERE rn = 1
        ORDER BY date ASC
        """
        params = list(security_ids) + [start, end, vintage_at]

        with self.conn() as p_conn:
            df = pd.read_sql_query(query, p_conn, params=params)

        if df.empty:
            return pd.DataFrame(columns=security_ids)

        pivoted = df.pivot(index="date", columns="security_id", values="volume_raw")
        return pivoted.reindex(columns=security_ids)

    def close_split(
        self,
        security_ids: List[int],
        start: str,
        end: str,
        vintage_at: str,
    ) -> pd.DataFrame:
        """Return split-adjusted close rebased to end date."""
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]

        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, close_raw, split_ratio,
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date >= ? AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, close_raw, split_ratio
        FROM ranked
        WHERE rn = 1
        ORDER BY date ASC
        """
        params = list(security_ids) + [start, end, vintage_at]

        with self.conn() as p_conn:
            df = pd.read_sql_query(query, p_conn, params=params)

        if df.empty:
            return pd.DataFrame(columns=security_ids)

        # For each security, adjust by splits up to end
        out_series = {}
        for sid, group in df.groupby("security_id"):
            g = group.sort_values("date").reset_index(drop=True)
            n = len(g)
            splits = g["split_ratio"].values
            closes = g["close_raw"].values
            adj_closes = np.zeros(n)
            for i in range(n):
                mult = float(np.prod(splits[i + 1:])) if i + 1 < n else 1.0
                adj_closes[i] = closes[i] / mult if mult > 0 else closes[i]
            out_series[sid] = pd.Series(adj_closes, index=g["date"])

        adj_df = pd.DataFrame(out_series)
        return adj_df.reindex(columns=security_ids)

    def tri(
        self,
        security_ids: List[int],
        start: str,
        end: str,
        vintage_at: str,
    ) -> pd.DataFrame:
        """Calculate Total Return Index (TRI) series starting at 100.0.
        
        Formula:
        TRI[0] = 100.0
        TRI[d] = TRI[d-1] * split_ratio[d] * (close_raw[d] + dividend_raw[d]) / close_raw[d-1]
        """
        if not security_ids:
            return pd.DataFrame()
        security_ids = [int(x) for x in security_ids]

        placeholders = ",".join("?" for _ in security_ids)
        query = f"""
        WITH ranked AS (
            SELECT security_id, date, close_raw, dividend_raw, split_ratio,
                   ROW_NUMBER() OVER (PARTITION BY security_id, date ORDER BY observed_at DESC) as rn
            FROM prices_daily
            WHERE security_id IN ({placeholders})
              AND date >= ? AND date <= ?
              AND observed_at <= ?
        )
        SELECT date, security_id, close_raw, dividend_raw, split_ratio
        FROM ranked
        WHERE rn = 1
        ORDER BY date ASC
        """
        params = list(security_ids) + [start, end, vintage_at]

        with self.conn() as p_conn:
            df = pd.read_sql_query(query, p_conn, params=params)

        if df.empty:
            return pd.DataFrame(columns=security_ids)

        tri_series = {}
        for sid, group in df.groupby("security_id"):
            g = group.sort_values("date").reset_index(drop=True)
            n = len(g)
            if n == 0:
                continue

            tri_vals = np.zeros(n)
            tri_vals[0] = 100.0

            closes = g["close_raw"].values
            divs = g["dividend_raw"].values
            splits = g["split_ratio"].values

            for d in range(1, n):
                prev_c = closes[d - 1]
                curr_c = closes[d]
                curr_div = divs[d]
                curr_split = splits[d]

                if prev_c > 0:
                    ret_factor = curr_split * (curr_c + curr_div) / prev_c
                    tri_vals[d] = tri_vals[d - 1] * ret_factor
                else:
                    tri_vals[d] = tri_vals[d - 1]

            tri_series[sid] = pd.Series(tri_vals, index=g["date"])

        res_df = pd.DataFrame(tri_series)
        return res_df.reindex(columns=security_ids)

    def adv_inr(
        self,
        security_ids: List[int],
        as_of: str,
        vintage_at: str,
        window: int = 63,
    ) -> pd.DataFrame:
        """Compute average daily turnover in INR over rolling window."""
        if not security_ids:
            return pd.DataFrame(columns=[f"adv_{window}_inr", f"n_days_{window}"])
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
                out[sid] = {f"adv_{window}_inr": np.nan, f"n_days_{window}": 0}
            else:
                turnover = s_rows["close_raw"] * s_rows["volume_raw"]
                out[sid] = {
                    f"adv_{window}_inr": float(turnover.mean()),
                    f"n_days_{window}": len(s_rows),
                }

        res = pd.DataFrame.from_dict(out, orient="index")
        res.index.name = "security_id"
        return res

    def manifest_write(self, output: Path | str, vintage_at: str) -> str:
        """Write price manifest JSON for observations up to vintage_at."""
        import hashlib
        import json

        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with self.conn() as p_conn:
            cur = p_conn.cursor()
            cur.execute(
                """
                SELECT security_id, date, close_raw, volume_raw, split_ratio, dividend_raw
                FROM prices_daily
                WHERE observed_at <= ?
                ORDER BY security_id ASC, date ASC
                """,
                (vintage_at,),
            )
            rows = cur.fetchall()

        rows_json = json.dumps([[r[0], r[1], float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows], sort_keys=True)
        content_hash = hashlib.sha256(rows_json.encode("utf-8")).hexdigest()

        manifest_data = {
            "vintage_at": vintage_at,
            "sha256": content_hash,
            "row_count": len(rows),
        }
        with open(out_path, "w") as f:
            json.dump(manifest_data, f, indent=2)

        return content_hash

    def manifest_verify(self, manifest: Path | str) -> Check:
        """Verify price database matches committed manifest hash."""
        import hashlib
        import json

        m_path = Path(manifest)
        with open(m_path, "r") as f:
            data = json.load(f)

        expected_hash = data["sha256"]
        vintage_at = data["vintage_at"]

        with self.conn() as p_conn:
            cur = p_conn.cursor()
            cur.execute(
                """
                SELECT security_id, date, close_raw, volume_raw, split_ratio, dividend_raw
                FROM prices_daily
                WHERE observed_at <= ?
                ORDER BY security_id ASC, date ASC
                """,
                (vintage_at,),
            )
            rows = cur.fetchall()

        rows_json = json.dumps([[r[0], r[1], float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows], sort_keys=True)
        current_hash = hashlib.sha256(rows_json.encode("utf-8")).hexdigest()

        if current_hash == expected_hash:
            return Check(
                id="price_manifest",
                status="PASS",
                observed=current_hash,
                expected=expected_hash,
                reason="Price manifest matches",
                blocking=True,
            )
        return Check(
            id="price_manifest",
            status="FAIL",
            observed=current_hash,
            expected=expected_hash,
            reason="Price manifest hash mismatch",
            blocking=True,
        )


def monthly_panel(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    """Build in-memory prices_monthly DataFrame matching state schema."""
    store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    sids = sorted([int(x) for x in draft.members["security_id"].unique()])

    close_df = store.close_raw(sids, start=draft.as_of, end=draft.as_of, vintage_at=draft.knowledge_cutoff)
    tri_df = store.tri(sids, start=draft.as_of, end=draft.as_of, vintage_at=draft.knowledge_cutoff)
    adv_df = store.adv_inr(sids, as_of=draft.as_of, vintage_at=draft.knowledge_cutoff, window=63)

    records = []
    manifest_sha = draft.source_refs.get("price_manifest_sha", "")

    for sid in sids:
        c_val = float(close_df[sid].iloc[0]) if not close_df.empty and sid in close_df.columns and not pd.isna(close_df[sid].iloc[0]) else np.nan
        t_val = float(tri_df[sid].iloc[0]) if not tri_df.empty and sid in tri_df.columns and not pd.isna(tri_df[sid].iloc[0]) else np.nan
        adv_row = adv_df.loc[sid] if sid in adv_df.index else {}
        adv_val = adv_row.get("adv_63_inr", np.nan)
        n_days = adv_row.get("n_days_63", 0)

        records.append({
            "cohort_id": draft.cohort_id,
            "as_of": draft.as_of,
            "security_id": sid,
            "close_raw": c_val,
            "tri": t_val,
            "adv_63_inr": adv_val,
            "n_days_63": int(n_days) if not pd.isna(n_days) else 0,
            "mcap_inr": np.nan,
            "shares_out": np.nan,
            "quote_legacy": None,
            "source": "yahoo",
            "price_manifest_sha": manifest_sha,
            "run_id": ctx.run_id,
        })

    return pd.DataFrame(records)

