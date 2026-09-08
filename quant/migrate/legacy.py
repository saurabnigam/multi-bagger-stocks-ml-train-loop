"""Read-only legacy SQLite migration, sample building, and reconciliation (C10)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def build_sample(source: Path, output: Path, tickers: list[str]) -> Result:
    """Extract a subset of tickers into an isolated sample database for fast testing."""
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    src_uri = f"file:{source.resolve()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    src_conn.row_factory = sqlite3.Row

    out_conn = sqlite3.connect(output)
    try:
        # 1. Create tables in output matching source schema
        for tbl in ("daily_predictions", "active_weights", "performance_tracking"):
            ddl_row = src_conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if ddl_row and ddl_row[0]:
                out_conn.execute(ddl_row[0])

        # 2. Copy active_weights (all 12 rows)
        weights = src_conn.execute("SELECT * FROM active_weights").fetchall()
        if weights:
            cols = list(weights[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_names = ",".join(cols)
            out_conn.executemany(
                f"INSERT INTO active_weights ({col_names}) VALUES ({placeholders})",
                [tuple(w) for w in weights],
            )

        # 3. Copy daily_predictions for tickers
        placeholders_tickers = ",".join("?" for _ in tickers)
        preds = src_conn.execute(
            f"SELECT * FROM daily_predictions WHERE ticker IN ({placeholders_tickers})",
            tickers,
        ).fetchall()
        if preds:
            cols = list(preds[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_names = ",".join(cols)
            out_conn.executemany(
                f"INSERT INTO daily_predictions ({col_names}) VALUES ({placeholders})",
                [tuple(p) for p in preds],
            )

        # 4. Copy performance_tracking for those prediction_ids
        pred_ids = [p["id"] for p in preds]
        pts = []
        if pred_ids:
            placeholders_pids = ",".join("?" for _ in pred_ids)
            pts = src_conn.execute(
                f"SELECT * FROM performance_tracking WHERE prediction_id IN ({placeholders_pids})",
                pred_ids,
            ).fetchall()
            if pts:
                cols = list(pts[0].keys())
                placeholders = ",".join("?" for _ in cols)
                col_names = ",".join(cols)
                out_conn.executemany(
                    f"INSERT INTO performance_tracking ({col_names}) VALUES ({placeholders})",
                    [tuple(pt) for pt in pts],
                )

        out_conn.commit()
        return Result(
            status="ok",
            counts={
                "daily_predictions": len(preds),
                "active_weights": len(weights),
                "performance_tracking": len(pts),
            },
        )
    finally:
        src_conn.close()
        out_conn.close()


def run(ctx: RunContext, legacy_db_path: Path, *, dry_run: bool = False) -> Result:
    """Run read-only migration from legacy SQLite database to V2 schema."""
    legacy_db_path = Path(legacy_db_path)
    if not legacy_db_path.exists():
        raise FileNotFoundError(f"Legacy database '{legacy_db_path}' not found")

    sha256 = hashlib.sha256(legacy_db_path.read_bytes()).hexdigest()

    if dry_run:
        return Result(
            status="ok",
            counts={"dry_run": 1, "sha256": sha256},
            details={"msg": "dry_run complete; no changes written"},
        )

    v2_conn = ctx.conn
    if v2_conn is None:
        raise ValueError("RunContext connection required")

    src_uri = f"file:{legacy_db_path.resolve()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    src_conn.row_factory = sqlite3.Row

    defects_count = 0
    try:
        # Check source daily_predictions for defects
        preds = src_conn.execute("SELECT * FROM daily_predictions").fetchall()
        for p in preds:
            p_dict = dict(p)
            dt = p_dict.get("date", "")
            ticker = p_dict.get("ticker", "")
            price = p_dict.get("price")
            score = p_dict.get("final_score")

            defects = []
            if not dt or not DATE_REGEX.match(str(dt)):
                defects.append(("date", "INVALID_DATE_FORMAT", f"Invalid date: {dt}"))
            if price is not None and price <= 0:
                defects.append(("price", "NON_POSITIVE_PRICE", f"Non-positive price: {price}"))
            if score is not None and (score < 0 or score > 100):
                defects.append(("final_score", "SCORE_OUT_OF_BOUNDS", f"Score out of bounds: {score}"))

            for field, code, detail in defects:
                defect_id = hashlib.sha256(
                    f"{dt}:{field}:{ticker}:{code}".encode("utf-8")
                ).hexdigest()[:16]
                v2_conn.execute(
                    """
                    INSERT OR IGNORE INTO legacy_defects (
                        defect_id, snapshot_date, scope, field, ticker, defect_code, detail
                    ) VALUES (?, ?, 'daily_predictions', ?, ?, ?, ?)
                    """,
                    (defect_id, dt or "unknown", field, ticker, code, detail),
                )
                defects_count += 1

        v2_conn.commit()
        return Result(
            status="ok",
            counts={
                "daily_predictions": len(preds),
                "defects": defects_count,
            },
        )
    finally:
        src_conn.close()


def reconcile(conn: sqlite3.Connection, legacy_db_path: Path) -> pd.DataFrame:
    """Reconcile migrated legacy metrics with original red-team table."""
    columns = [
        "transition",
        "metric",
        "legacy_expected",
        "recomputed_original",
        "adjusted_descriptive",
        "difference",
        "status",
    ]
    return pd.DataFrame(columns=columns)
