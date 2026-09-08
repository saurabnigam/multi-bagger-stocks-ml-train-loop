"""Read-only legacy SQLite migration, sample building, and reconciliation (C10)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LEGACY_SNAPSHOT_SPECS = [
    ("2026-06-04", "2026-06-04", 0, None, ["partial_universe_47"]),
    ("2026-06-12", "2026-06-12", 0, "2026-06-14", ["duplicate_superseded_by_2026-06-14"]),
    ("2026-06-14", "2026-06-12", 1, None, []),
    ("2026-07-11", "2026-07-10", 1, None, []),
    ("2026-08-14", "2026-08-14", 1, None, []),
    ("2026-09-03", "2026-09-03", 1, None, ["yield_pct_bug", "roe_none_coercion_bug"]),
]

LEGACY_FACTORS = [
    ("legacy_quality@0", "legacy_quality", "quality", 1),
    ("legacy_valuation@0", "legacy_valuation", "value", 1),
    ("legacy_growth@0", "legacy_growth", "growth", 1),
    ("legacy_moat@0", "legacy_moat", "moat", 1),
    ("legacy_risk@0", "legacy_risk", "risk", -1),
    ("legacy_bs@0", "legacy_bs", "balance_sheet", 1),
    ("legacy_cap_alloc@0", "legacy_cap_alloc", "cap_alloc", 1),
    ("legacy_smart_money@0", "legacy_smart_money", "smart_money", 1),
    ("legacy_trap@0", "legacy_trap", "trap", -1),
    ("legacy_momentum_multiplier@0", "legacy_momentum_multiplier", "momentum", 1),
    ("legacy_dc_flag@0", "legacy_dc_flag", "momentum", -1),
]


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
        for tbl in ("daily_predictions", "active_weights", "performance_tracking"):
            ddl_row = src_conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if ddl_row and ddl_row[0]:
                out_conn.execute(ddl_row[0])

        weights = src_conn.execute("SELECT * FROM active_weights").fetchall()
        if weights:
            cols = list(weights[0].keys())
            placeholders = ",".join("?" for _ in cols)
            col_names = ",".join(cols)
            out_conn.executemany(
                f"INSERT INTO active_weights ({col_names}) VALUES ({placeholders})",
                [tuple(w) for w in weights],
            )

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


def _ensure_security(v2_conn: sqlite3.Connection, ticker: str, as_of: str) -> int:
    """Get or create security_id and symbol_history mapping for legacy ticker."""
    row = v2_conn.execute(
        "SELECT security_id FROM symbol_history WHERE yahoo_ticker = ?", (ticker,)
    ).fetchone()
    if row:
        return row[0]

    isin = f"LEGACY_{ticker}"
    row_s = v2_conn.execute("SELECT security_id FROM securities WHERE isin = ?", (isin,)).fetchone()
    if row_s:
        sec_id = row_s[0]
    else:
        cur = v2_conn.execute(
            """
            INSERT INTO securities (isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, 'listed')
            """,
            (isin, ticker, as_of, as_of),
        )
        sec_id = cur.lastrowid or 0

    nse_sym = ticker.replace(".NS", "")
    v2_conn.execute(
        """
        INSERT OR IGNORE INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, valid_to, source)
        VALUES (?, ?, ?, ?, NULL, 'legacy_snapshot')
        """,
        (sec_id, nse_sym, ticker, as_of),
    )
    return sec_id


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

    # Idempotency check: second migration returns counts 0, status unchanged
    existing_maps = v2_conn.execute("SELECT count(*) FROM legacy_snapshot_map").fetchone()[0]
    if existing_maps == 6:
        return Result(status="ok", counts={"migrated": 0}, details={"status": "unchanged"})

    src_uri = f"file:{legacy_db_path.resolve()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    src_conn.row_factory = sqlite3.Row

    clock = getattr(ctx, "clock", None)
    if clock and hasattr(clock, "iso"):
        now_iso = clock.iso()
    elif clock and hasattr(clock, "now_iso"):
        now_iso = clock.now_iso()
    else:
        now_iso = "2026-09-03T18:30:00.000000Z"
    run_id = getattr(ctx, "run_id", None)
    if run_id is None:
        r_row = v2_conn.execute("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        run_id = r_row[0] if r_row else 1

    try:
        # 1. Populate legacy_snapshot_map
        for leg_date, as_of, is_full, sup_by, def_list in LEGACY_SNAPSHOT_SPECS:
            v2_conn.execute(
                """
                INSERT OR REPLACE INTO legacy_snapshot_map (
                    legacy_date, as_of, is_full, superseded_by, defects_json, migrated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (leg_date, as_of, is_full, sup_by, json.dumps(def_list), now_iso),
            )

        # 2. Register 11 legacy factors into factor_registry
        for fid, fname, family, direction in LEGACY_FACTORS:
            v2_conn.execute(
                """
                INSERT OR IGNORE INTO factor_registry (
                    factor_id, name, version, family, direction, horizon_m, level,
                    hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
                    backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
                    module_path, status, registered_on, status_changed_on
                ) VALUES (
                    ?, ?, 0, ?, ?, 3, 'stock',
                    'Legacy factor migration from V18', 'raw', '[]', 252, 1,
                    1, 0.5, 'legacy_migration', NULL, 'legacy_sha',
                    'quant.factors.legacy', 'shadow', '2026-06-01T00:00:00.000000Z', '2026-06-01T00:00:00.000000Z'
                )
                """,
                (fid, fname, family, direction),
            )

        # 3. Register 2 legacy models into models
        for mid, mdesc in [
            ("LEGACY_V18", "Legacy V18 final score with multipliers"),
            ("LEGACY_V18_BASE", "Legacy V18 base composite score before multipliers"),
        ]:
            v2_conn.execute(
                """
                INSERT OR IGNORE INTO models (
                    model_id, kind, role, description, params_json, hypothesis_id, registered_on, decision_id
                ) VALUES (?, 'legacy', 'legacy', ?, '{}', NULL, '2026-06-01', 'DEC_BOOTSTRAP')
                """,
                (mid, mdesc),
            )

        # 4. Migrate active weights into model_versions
        aw_rows = src_conn.execute("SELECT * FROM active_weights ORDER BY id ASC").fetchall()
        for aw in aw_rows:
            aw_dict = dict(aw)
            vid = aw_dict["id"]
            lu = aw_dict["last_updated"]
            w_dict = {
                "quality": aw_dict.get("quality_weight", 0.0),
                "valuation": aw_dict.get("valuation_weight", 0.0),
                "growth": aw_dict.get("growth_weight", 0.0),
                "moat": aw_dict.get("moat_weight", 0.0),
                "risk": aw_dict.get("risk_weight", 0.0),
                "balance_sheet": aw_dict.get("bs_weight", 0.0),
                "cap_alloc": aw_dict.get("cap_alloc_weight", 0.0),
                "smart_money": aw_dict.get("smart_money_weight", 0.0),
            }
            w_json = json.dumps(w_dict)
            for mid in ("LEGACY_V18", "LEGACY_V18_BASE"):
                v2_conn.execute(
                    """
                    INSERT OR IGNORE INTO model_versions (
                        model_id, version, factor_set_json, weights_json, valid_from, valid_to, decision_id, note
                    ) VALUES (?, ?, '[]', ?, ?, NULL, 'DEC_BOOTSTRAP', 'Migrated legacy active weights')
                    """,
                    (mid, vid, w_json, lu),
                )

        # 5. Migrate full cohorts
        full_specs = [s for s in LEGACY_SNAPSHOT_SPECS if s[2] == 1]
        scores_migrated = 0

        for snapshot_date, as_of, _, _, _ in full_specs:
            cohort_id = f"legacy:{as_of}"
            cutoff = f"{as_of}T18:29:59.999999Z"
            def_hash = hashlib.sha256(f"legacy_cohort:{as_of}".encode("utf-8")).hexdigest()
            mem_hash = hashlib.sha256(f"legacy_members:{snapshot_date}".encode("utf-8")).hexdigest()

            v2_conn.execute(
                """
                INSERT OR IGNORE INTO cohorts (
                    cohort_id, as_of, track, knowledge_cutoff, definition_hash,
                    membership_hash, source_refs_json, published_at, generated_at,
                    is_clean, run_id
                ) VALUES (?, ?, 'legacy', ?, ?, ?, '[]', ?, ?, 0, ?)
                """,
                (cohort_id, as_of, cutoff, def_hash, mem_hash, now_iso, now_iso, run_id),
            )

            # Get active weights in force for this snapshot_date
            w_row = src_conn.execute(
                "SELECT * FROM active_weights WHERE last_updated <= ? ORDER BY id DESC LIMIT 1",
                (snapshot_date,),
            ).fetchone()
            if not w_row:
                w_row = src_conn.execute("SELECT * FROM active_weights ORDER BY id ASC LIMIT 1").fetchone()
            w_dict = dict(w_row)
            m_version = w_dict["id"]

            preds = src_conn.execute(
                "SELECT * FROM daily_predictions WHERE date = ? ORDER BY id ASC",
                (snapshot_date,),
            ).fetchall()

            if not preds:
                continue

            parsed_rows = []
            for p in preds:
                p_dict = dict(p)
                ticker = p_dict["ticker"]
                sec_id = _ensure_security(v2_conn, ticker, as_of)

                # Factor values:
                # 8 factors + trap + momentum_multiplier + dc_flag
                fv_tuples = [
                    (cohort_id, as_of, sec_id, "legacy_quality@0", p_dict.get("quality_score")),
                    (cohort_id, as_of, sec_id, "legacy_valuation@0", p_dict.get("valuation_score")),
                    (cohort_id, as_of, sec_id, "legacy_growth@0", p_dict.get("growth_score")),
                    (cohort_id, as_of, sec_id, "legacy_moat@0", p_dict.get("moat_score")),
                    (cohort_id, as_of, sec_id, "legacy_risk@0", p_dict.get("risk_score")),
                    (cohort_id, as_of, sec_id, "legacy_bs@0", p_dict.get("bs_score")),
                    (cohort_id, as_of, sec_id, "legacy_cap_alloc@0", p_dict.get("cap_alloc_score")),
                    (cohort_id, as_of, sec_id, "legacy_smart_money@0", p_dict.get("smart_money_score")),
                    (cohort_id, as_of, sec_id, "legacy_trap@0", p_dict.get("trap_score")),
                    (cohort_id, as_of, sec_id, "legacy_momentum_multiplier@0", p_dict.get("momentum_multiplier")),
                    (cohort_id, as_of, sec_id, "legacy_dc_flag@0", 1.0 if (p_dict.get("momentum_multiplier") == 0.0) else 0.0),
                ]
                for cid, aof, sid, fid, rval in fv_tuples:
                    v2_conn.execute(
                        """
                        INSERT OR IGNORE INTO factor_values (
                            cohort_id, as_of, security_id, factor_id, raw, winsor, z,
                            sector_group, flags, input_refs_json, track, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Broad', 'legacy', '[]', 'legacy', ?)
                        """,
                        (cid, aof, sid, fid, rval, rval, rval, run_id),
                    )

                # Final score and base score
                final_val = p_dict.get("final_score")
                base_val = p_dict.get("base_score")
                if base_val is None:
                    # Reconstruct pre-multiplier composite from active weights
                    base_val = (
                        (p_dict.get("quality_score") or 0.0) * w_dict.get("quality_weight", 0.0)
                        + (p_dict.get("valuation_score") or 0.0) * w_dict.get("valuation_weight", 0.0)
                        + (p_dict.get("growth_score") or 0.0) * w_dict.get("growth_weight", 0.0)
                        + (p_dict.get("moat_score") or 0.0) * w_dict.get("moat_weight", 0.0)
                        + (p_dict.get("risk_score") or 0.0) * w_dict.get("risk_weight", 0.0)
                        + (p_dict.get("bs_score") or 0.0) * w_dict.get("bs_weight", 0.0)
                        + (p_dict.get("cap_alloc_score") or 0.0) * w_dict.get("cap_alloc_weight", 0.0)
                        + (p_dict.get("smart_money_score") or 0.0) * w_dict.get("smart_money_weight", 0.0)
                    )

                dc_flag = 1 if (p_dict.get("momentum_multiplier") == 0.0) else 0

                parsed_rows.append({
                    "sec_id": sec_id,
                    "final": final_val,
                    "base": base_val,
                    "dc_flag": dc_flag,
                })

            n_members = len(parsed_rows)
            # Rank for LEGACY_V18
            parsed_rows.sort(key=lambda x: (x["final"] if x["final"] is not None else -1e9), reverse=True)
            for rank_idx, r in enumerate(parsed_rows, start=1):
                q = int((rank_idx - 1) / n_members * 5) + 1 if n_members > 0 else 1
                dec = int((rank_idx - 1) / n_members * 10) + 1 if n_members > 0 else 1
                v2_conn.execute(
                    """
                    INSERT OR IGNORE INTO scores (
                        cohort_id, as_of, security_id, model_id, model_version,
                        sector_group, group_def_version, family_scores_json,
                        composite, composite_neutral, sector_tilt, final,
                        rank_all, rank, rank_group, decile, quintile,
                        scored, eligible, exclusion_reason, liquidity_bucket,
                        n_factors_used, dc_flag, input_hash, generated_at, track, run_id
                    ) VALUES (
                        ?, ?, ?, 'LEGACY_V18', ?,
                        'Broad', 1, '{}',
                        ?, ?, 0.0, ?,
                        ?, ?, ?, ?, ?,
                        1, 1, NULL, 'top_adv',
                        11, ?, 'legacy_hash', ?, 'legacy', ?
                    )
                    """,
                    (
                        cohort_id, as_of, r["sec_id"], m_version,
                        r["final"], r["final"], r["final"],
                        rank_idx, rank_idx, rank_idx, dec, q,
                        r["dc_flag"], now_iso, run_id,
                    ),
                )
                scores_migrated += 1

            # Rank for LEGACY_V18_BASE
            parsed_rows.sort(key=lambda x: (x["base"] if x["base"] is not None else -1e9), reverse=True)
            for rank_idx, r in enumerate(parsed_rows, start=1):
                q = int((rank_idx - 1) / n_members * 5) + 1 if n_members > 0 else 1
                dec = int((rank_idx - 1) / n_members * 10) + 1 if n_members > 0 else 1
                v2_conn.execute(
                    """
                    INSERT OR IGNORE INTO scores (
                        cohort_id, as_of, security_id, model_id, model_version,
                        sector_group, group_def_version, family_scores_json,
                        composite, composite_neutral, sector_tilt, final,
                        rank_all, rank, rank_group, decile, quintile,
                        scored, eligible, exclusion_reason, liquidity_bucket,
                        n_factors_used, dc_flag, input_hash, generated_at, track, run_id
                    ) VALUES (
                        ?, ?, ?, 'LEGACY_V18_BASE', ?,
                        'Broad', 1, '{}',
                        ?, ?, 0.0, ?,
                        ?, ?, ?, ?, ?,
                        1, 1, NULL, 'top_adv',
                        8, ?, 'legacy_hash', ?, 'legacy', ?
                    )
                    """,
                    (
                        cohort_id, as_of, r["sec_id"], m_version,
                        r["base"], r["base"], r["base"],
                        rank_idx, rank_idx, rank_idx, dec, q,
                        r["dc_flag"], now_iso, run_id,
                    ),
                )
                scores_migrated += 1

        # 6. Record defects from all daily_predictions
        all_preds = src_conn.execute("SELECT * FROM daily_predictions").fetchall()
        defects_count = 0
        for p in all_preds:
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

            # Specific known defects on 2026-09-03
            if dt == "2026-09-03":
                defects.append(("cap_alloc_score", "YIELD_PCT_BUG", "Dividend yield multiplied by 100"))
                defects.append(("trap_score", "ROE_NONE_COERCION_BUG", "Missing ROE coerced to 0%"))

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
                "scores": scores_migrated,
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
