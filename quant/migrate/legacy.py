"""Read-only legacy SQLite migration, sample building, and reconciliation (C10)."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
import pandas as pd

from quant.db.core import append_rows
from quant.factors import standardise
from quant.sectors import taxonomy
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LEGACY_SNAPSHOT_SPECS = [
    ("2026-06-04", "2026-06-04", 0, None, ["partial_universe_47"]),
    ("2026-06-12", "2026-06-12", 0, "2026-06-14", ["duplicate_superseded_by_2026-06-14"]),
    ("2026-06-14", "2026-06-12", 1, None, ["yield_pct_bug"]),
    ("2026-07-11", "2026-07-10", 1, None, ["yield_pct_bug"]),
    ("2026-08-14", "2026-08-14", 1, None, ["yield_pct_bug", "roe_none_coercion_bug"]),
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

# factor_id -> source column on daily_predictions; None means "derived" (see _derived_raw).
FACTOR_ID_TO_FIELD: dict[str, Optional[str]] = {
    "legacy_quality@0": "quality_score",
    "legacy_valuation@0": "valuation_score",
    "legacy_growth@0": "growth_score",
    "legacy_moat@0": "moat_score",
    "legacy_risk@0": "risk_score",
    "legacy_bs@0": "bs_score",
    "legacy_cap_alloc@0": "cap_alloc_score",
    "legacy_smart_money@0": "smart_money_score",
    "legacy_trap@0": "trap_score",
    "legacy_momentum_multiplier@0": "momentum_multiplier",
    "legacy_dc_flag@0": None,
}
FACTOR_ID_TO_DIRECTION: dict[str, int] = {fid: direction for fid, _, _, direction in LEGACY_FACTORS}

# Red-team review (docs/analysis/red_team_review.md section 5) plus direct inspection of
# quant_engine.db's raw_json: `Div_Yield_%` (the dividend-yield x100 Cap-Alloc bug) is present
# on every full cohort from the date the field first appears (2026-06-14) through 2026-09-03 -
# a measured 64.5%-65.4% of the universe shows an implausible >25% yield on each of those four
# dates (322/499, 324/499, 324/499, 327/500), and the field is simply absent on the two
# pre-06-14 partial/duplicate snapshots. (The review's separate "385/500 (77%)" figure is for
# a different, downstream metric - stocks with capital-allocation score >= 90 - not for this
# yield>25% condition; do not conflate the two.) The ROE None->0 trap-score coercion shows a
# sharp, distinct structural break in the trap_score histogram starting 2026-08-14 (modal
# bucket count jumps from ~65 stocks to 257, then 288 on 2026-09-03 - exactly the "288 of 500
# stocks" figure the review reports for the September snapshot), so it is flagged only on the
# two dates where that break is actually observed.
YIELD_PCT_BUG_DATES = {"2026-06-14", "2026-07-11", "2026-08-14", "2026-09-03"}
ROE_NONE_COERCION_BUG_DATES = {"2026-08-14", "2026-09-03"}

# Chronological order of legacy snapshot dates, for adjacent-snapshot quote comparisons.
LEGACY_DATE_ORDER = [spec[0] for spec in LEGACY_SNAPSHOT_SPECS]

IDENTITY_MAP_COLUMNS = ["yahoo_ticker", "nse_symbol", "isin", "company_name", "nse_sector", "source", "resolved"]
CONSTITUENTS_CSV_NAME = "nifty500_constituents_2026-09-09.csv"
IDENTITY_MAP_CSV_NAME = "legacy_identity_map_v1.csv"
SECTOR_GROUPS_CSV_NAME = "sector_groups_v1.csv"


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


# ---------------------------------------------------------------------------- identity map


def build_identity_map(constituents_csv: Path, output_csv: Path) -> Result:
    """Deterministically build the legacy ticker -> ISIN identity map.

    Reads the canonical NSE constituent headers (Company Name, Industry, Symbol, Series,
    ISIN Code) and writes one resolved row per constituent keyed by
    yahoo_ticker = Symbol + '.NS'. A legacy ticker absent from this map is UNRESOLVED and
    falls back to a LEGACY_<ticker> placeholder ISIN at migration time (MASTER_SPEC 10.6).
    """
    constituents_csv = Path(constituents_csv)
    output_csv = Path(output_csv)
    if not constituents_csv.exists():
        raise FileNotFoundError(f"Constituents CSV '{constituents_csv}' not found")

    rows: list[dict[str, Any]] = []
    with open(constituents_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            symbol = (r.get("Symbol") or "").strip()
            if not symbol:
                continue
            rows.append({
                "yahoo_ticker": f"{symbol}.NS",
                "nse_symbol": symbol,
                "isin": (r.get("ISIN Code") or "").strip(),
                "company_name": (r.get("Company Name") or "").strip(),
                "nse_sector": (r.get("Industry") or "").strip(),
                "source": constituents_csv.stem,
                "resolved": True,
            })

    rows.sort(key=lambda r: r["yahoo_ticker"])

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=IDENTITY_MAP_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return Result(
        status="ok",
        counts={"rows": len(rows)},
        details={"source": str(constituents_csv), "output": str(output_csv)},
    )


def _resolve_config_path(ctx: Optional["RunContext"], filename: str) -> Path:
    """Resolve a config/<filename> path, preferring the run's configured data_dir sibling
    but falling back to the plain repo-relative path (tests point data_dir at tmp_path)."""
    if ctx is not None:
        cfg = getattr(ctx, "cfg", None)
        data_dir = getattr(getattr(cfg, "paths", None), "data_dir", None)
        if data_dir is not None:
            candidate = Path(data_dir) / ".." / "config" / filename
            if candidate.exists():
                return candidate
    return Path("config") / filename


def _load_identity_map(ctx: Optional["RunContext"] = None) -> dict[str, dict[str, Any]]:
    """Load (generating on first use if necessary) the legacy ticker identity map."""
    map_path = _resolve_config_path(ctx, IDENTITY_MAP_CSV_NAME)
    if not map_path.exists():
        constituents_path = _resolve_config_path(ctx, CONSTITUENTS_CSV_NAME)
        build_identity_map(constituents_path, map_path)

    out: dict[str, dict[str, Any]] = {}
    with open(map_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            out[r["yahoo_ticker"]] = r
    return out


def _ensure_security(
    v2_conn: sqlite3.Connection,
    ticker: str,
    as_of: str,
    identity_map: dict[str, dict[str, Any]],
) -> tuple[int, Optional[str], bool]:
    """Get or create security_id and symbol_history mapping for a legacy ticker.

    Returns (security_id, nse_sector, resolved). Only tickers absent from the identity map
    fall back to the LEGACY_<ticker> placeholder ISIN; every resolved ticker is upserted by
    its real ISIN from the NSE constituent map.
    """
    entry = identity_map.get(ticker)
    resolved = entry is not None
    if resolved:
        isin = entry["isin"]
        nse_sym = entry["nse_symbol"]
        company_name = entry["company_name"] or ticker
        nse_sector = entry["nse_sector"] or None
    else:
        isin = f"LEGACY_{ticker}"
        nse_sym = ticker.replace(".NS", "")
        company_name = ticker
        nse_sector = None

    row = v2_conn.execute(
        "SELECT security_id FROM symbol_history WHERE yahoo_ticker = ?", (ticker,)
    ).fetchone()
    if row:
        return row[0], nse_sector, resolved

    row_s = v2_conn.execute("SELECT security_id FROM securities WHERE isin = ?", (isin,)).fetchone()
    if row_s:
        sec_id = row_s[0]
    else:
        cur = v2_conn.execute(
            """
            INSERT INTO securities (isin, name, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, 'listed')
            """,
            (isin, company_name, as_of, as_of),
        )
        sec_id = cur.lastrowid or 0

    v2_conn.execute(
        """
        INSERT OR IGNORE INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, valid_to, source)
        VALUES (?, ?, ?, ?, NULL, 'legacy_snapshot')
        """,
        (sec_id, nse_sym, ticker, as_of),
    )
    return sec_id, nse_sector, resolved


def _derived_raw(field: Optional[str], p_dict: dict[str, Any]) -> Any:
    """Value for factors with no direct daily_predictions column (currently dc_flag)."""
    if field is not None:
        return p_dict.get(field)
    return 1.0 if (p_dict.get("momentum_multiplier") == 0.0) else 0.0


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
        return Result(
            status="ok",
            counts={"scores": 0, "defects": 0, "migrated": 0},
            details={"status": "unchanged", "msg": "Legacy database already migrated"},
        )

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

    identity_map = _load_identity_map(ctx)
    sector_rules_path = _resolve_config_path(ctx, SECTOR_GROUPS_CSV_NAME)
    sector_rules = taxonomy.load_rules(str(sector_rules_path))
    group_def_version = int(getattr(getattr(ctx.cfg, "sectors", None), "group_def_version", 1)) if hasattr(ctx, "cfg") and ctx.cfg else 1

    resolved_tickers: set[str] = set()
    unresolved_tickers: set[str] = set()
    unresolved_defect_emitted: set[str] = set()
    secmap_written: set[int] = set()

    try:
        # 1. Populate legacy_snapshot_map
        for leg_date, as_of, is_full, sup_by, def_list in LEGACY_SNAPSHOT_SPECS:
            v2_conn.execute(
                """
                INSERT OR IGNORE INTO legacy_snapshot_map (
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

        # 5. Migrate full cohorts: identity, membership, sector groups, factors, scores
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

            # Active weights in force for this snapshot_date
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

            # Pass 1: resolve identity, collect membership / sector inputs / raw factor values
            membership_rows: list[dict[str, Any]] = []
            sector_input_rows: list[dict[str, Any]] = []
            raw_by_factor: dict[str, dict[int, Any]] = {fid: {} for fid in FACTOR_ID_TO_FIELD}
            parsed_rows = []

            for p in preds:
                p_dict = dict(p)
                ticker = p_dict["ticker"]
                sec_id, nse_sector, resolved = _ensure_security(v2_conn, ticker, as_of, identity_map)

                if resolved:
                    resolved_tickers.add(ticker)
                else:
                    unresolved_tickers.add(ticker)
                    if ticker not in unresolved_defect_emitted:
                        defect_id = hashlib.sha256(
                            f"identity:{ticker}:UNRESOLVED_ISIN".encode("utf-8")
                        ).hexdigest()[:16]
                        v2_conn.execute(
                            """
                            INSERT OR IGNORE INTO legacy_defects (
                                defect_id, snapshot_date, scope, field, ticker, defect_code, detail
                            ) VALUES (?, ?, 'identity', 'isin', ?, 'UNRESOLVED_ISIN', ?)
                            """,
                            (
                                defect_id, as_of, ticker,
                                f"No NSE constituent match for legacy ticker {ticker}; assigned LEGACY_ placeholder ISIN",
                            ),
                        )
                        unresolved_defect_emitted.add(ticker)

                nse_symbol_out = (identity_map.get(ticker) or {}).get("nse_symbol") or ticker.replace(".NS", "")
                membership_rows.append({
                    "as_of": as_of,
                    "observed_at": now_iso,
                    "security_id": sec_id,
                    "index_name": "NIFTY500",
                    "nse_symbol": nse_symbol_out,
                    "nse_sector": nse_sector,
                    "series": None,
                    "source": "legacy_snapshot",
                    "source_sha256": sha256,
                })
                sector_input_rows.append({"security_id": sec_id, "nse_sector": nse_sector or ""})

                for fid, field in FACTOR_ID_TO_FIELD.items():
                    raw_by_factor[fid][sec_id] = _derived_raw(field, p_dict)

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

            # Membership: one row per member of this legacy cohort
            if membership_rows:
                append_rows(
                    ctx, "universe_membership", pd.DataFrame(membership_rows),
                    keys=["as_of", "index_name", "security_id", "observed_at"],
                )

            # Sector groups: frozen legacy membership/current-backfill group map
            members_df = pd.DataFrame(sector_input_rows).drop_duplicates(subset=["security_id"]).reset_index(drop=True)
            assigned = taxonomy.assign_groups(members_df, sector_rules, yahoo_industry=None, cfg=getattr(ctx, "cfg", None))
            group_by_sec: dict[int, str] = dict(zip(assigned["security_id"], assigned["sector_group"]))
            nse_sector_by_sec: dict[int, Optional[str]] = {
                int(row["security_id"]): (row["nse_sector"] or None) for row in sector_input_rows
            }

            new_secmap_rows = []
            for sid, grp in group_by_sec.items():
                sid = int(sid)
                if sid in secmap_written:
                    continue
                new_secmap_rows.append({
                    "security_id": sid,
                    "observed_at": now_iso,
                    "valid_from": as_of,
                    "valid_to": None,
                    "nse_sector": nse_sector_by_sec.get(sid),
                    "yahoo_sector": None,
                    "yahoo_industry": None,
                    "sector_group": grp,
                    "group_def_version": group_def_version,
                    "source": "legacy_backfill",
                    "confidence": 0.5,
                })
                secmap_written.add(sid)
            if new_secmap_rows:
                append_rows(ctx, "sector_map", pd.DataFrame(new_secmap_rows), keys=["security_id", "valid_from"])

            # Factor values: raw/winsor keep the original 0-100 legacy score; z is the
            # sector-neutral gaussian-rank transform of that raw score within the cohort.
            groups_series = pd.Series({int(k): v for k, v in group_by_sec.items()})
            fv_rows: list[dict[str, Any]] = []
            for fid, field in FACTOR_ID_TO_FIELD.items():
                raw_map = raw_by_factor[fid]
                raw_series = pd.Series(raw_map, dtype=float)
                direction = FACTOR_ID_TO_DIRECTION[fid]
                std_df = standardise.transform(raw_series, groups_series, direction, cfg=getattr(ctx, "cfg", None))
                for sid in raw_series.index:
                    rv = raw_series.loc[sid]
                    zv = std_df.loc[sid, "z"]
                    fv_rows.append({
                        "cohort_id": cohort_id,
                        "as_of": as_of,
                        "security_id": int(sid),
                        "factor_id": fid,
                        "raw": None if pd.isna(rv) else float(rv),
                        "winsor": None if pd.isna(rv) else float(rv),
                        "z": None if pd.isna(zv) else float(zv),
                        "sector_group": group_by_sec.get(sid, "UNCLASSIFIED"),
                        "flags": "legacy",
                        "input_refs_json": "[]",
                        "track": "legacy",
                        "run_id": run_id,
                    })
            if fv_rows:
                append_rows(ctx, "factor_values", pd.DataFrame(fv_rows), keys=["cohort_id", "security_id", "factor_id"])

            n_members = len(parsed_rows)

            def _score_rows(sorted_rows, model_id: str, model_version, n_factors_used: int) -> list[dict[str, Any]]:
                out = []
                for rank_idx, r in enumerate(sorted_rows, start=1):
                    q = int((rank_idx - 1) / n_members * 5) + 1 if n_members > 0 else 1
                    dec = int((rank_idx - 1) / n_members * 10) + 1 if n_members > 0 else 1
                    key_field = "final" if model_id == "LEGACY_V18" else "base"
                    value = r[key_field]
                    out.append({
                        "cohort_id": cohort_id,
                        "as_of": as_of,
                        "security_id": r["sec_id"],
                        "model_id": model_id,
                        "model_version": model_version,
                        "sector_group": group_by_sec.get(r["sec_id"], "UNCLASSIFIED"),
                        "group_def_version": group_def_version,
                        "family_scores_json": "{}",
                        "composite": value,
                        "composite_neutral": value,
                        "sector_tilt": 0.0,
                        "final": value,
                        "rank_all": rank_idx,
                        "rank": rank_idx,
                        "rank_group": rank_idx,
                        "decile": dec,
                        "quintile": q,
                        "scored": 1,
                        "eligible": 1,
                        "exclusion_reason": None,
                        "liquidity_bucket": "top_adv",
                        "n_factors_used": n_factors_used,
                        "dc_flag": r["dc_flag"],
                        "input_hash": "legacy_hash",
                        "generated_at": now_iso,
                        "track": "legacy",
                        "run_id": run_id,
                    })
                return out

            # Rank for LEGACY_V18 (final) and LEGACY_V18_BASE (base) independently
            parsed_rows.sort(key=lambda x: (x["final"] if x["final"] is not None else -1e9), reverse=True)
            v18_rows = _score_rows(parsed_rows, "LEGACY_V18", m_version, 11)
            if v18_rows:
                append_rows(ctx, "scores", pd.DataFrame(v18_rows), keys=["cohort_id", "security_id", "model_id"])
            scores_migrated += len(v18_rows)

            parsed_rows.sort(key=lambda x: (x["base"] if x["base"] is not None else -1e9), reverse=True)
            base_rows = _score_rows(parsed_rows, "LEGACY_V18_BASE", m_version, 8)
            if base_rows:
                append_rows(ctx, "scores", pd.DataFrame(base_rows), keys=["cohort_id", "security_id", "model_id"])
            scores_migrated += len(base_rows)

        # 6. Record defects from all daily_predictions
        all_preds = src_conn.execute("SELECT * FROM daily_predictions").fetchall()
        defects_count = 0
        price_by_ticker_date: dict[str, dict[str, float]] = {}

        for p in all_preds:
            p_dict = dict(p)
            dt = p_dict.get("date", "")
            ticker = p_dict.get("ticker", "")
            price = p_dict.get("price")
            score = p_dict.get("final_score")
            price_by_ticker_date.setdefault(ticker, {})[dt] = price

            defects = []
            if not dt or not DATE_REGEX.match(str(dt)):
                defects.append(("date", "INVALID_DATE_FORMAT", f"Invalid date: {dt}"))
            if price is not None and price <= 0:
                defects.append(("price", "NON_POSITIVE_PRICE", f"Non-positive price: {price}"))
            if score is not None and (score < 0 or score > 100):
                defects.append(("final_score", "SCORE_OUT_OF_BOUNDS", f"Score out of bounds: {score}"))

            # Known harness unit bugs (red_team_review.md section 5), verified against
            # quant_engine.db's raw_json Div_Yield_%/trap_score distributions - see
            # YIELD_PCT_BUG_DATES / ROE_NONE_COERCION_BUG_DATES above.
            if dt in YIELD_PCT_BUG_DATES:
                defects.append(("cap_alloc_score", "YIELD_PCT_BUG", "Dividend yield multiplied by 100"))
            if dt in ROE_NONE_COERCION_BUG_DATES:
                defects.append(("trap_score", "ROE_NONE_COERCION_BUG", "Missing ROE coerced to 0%"))

            # Momentum hard-kill: the 0.0x death-cross multiplier zeroes the final score
            if p_dict.get("momentum_multiplier") == 0.0:
                defects.append((
                    "momentum_multiplier", "LEGACY_HARD_KILL",
                    "0.0x death-cross multiplier hard-killed the final score",
                ))

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

        # Unadjusted split quotes: a >40% quote jump between adjacent legacy snapshots with
        # no recorded corporate action (red_team_review.md section 6, e.g. ZFCVINDIA.NS).
        for ticker, by_date in price_by_ticker_date.items():
            available = [d for d in LEGACY_DATE_ORDER if d in by_date and by_date[d] not in (None, 0)]
            for prev_d, next_d in zip(available, available[1:]):
                p0, p1 = by_date[prev_d], by_date[next_d]
                pct = (p1 - p0) / p0
                if abs(pct) > 0.40:
                    defect_id = hashlib.sha256(
                        f"{next_d}:price:{ticker}:SUSPECT_SPLIT_QUOTE".encode("utf-8")
                    ).hexdigest()[:16]
                    v2_conn.execute(
                        """
                        INSERT OR IGNORE INTO legacy_defects (
                            defect_id, snapshot_date, scope, field, ticker, defect_code, detail
                        ) VALUES (?, ?, 'daily_predictions', 'price', ?, 'SUSPECT_SPLIT_QUOTE', ?)
                        """,
                        (
                            defect_id, next_d, ticker,
                            f"Quote moved {pct:+.1%} vs {prev_d} ({p0}->{p1}) with no recorded corporate action",
                        ),
                    )
                    defects_count += 1

        # 7. Record factual system decision and ADR
        adr_rel = "knowledge/decisions/ADR-D-0000-migration.md"
        v2_conn.execute(
            """
            INSERT OR IGNORE INTO decisions (
                decision_id, proposal_id, kind, tier, subject_id, title, context,
                options_json, decision, evidence_refs_json, criteria_check_json,
                decided_on, decided_by, approver_kind, ratified_by, ratified_on,
                status, effective_from, applied_on, adr_path, supersedes, reverted_by, git_sha
            ) VALUES (
                'D-0000-migration', NULL, 'data_fix', 0, 'legacy_migration',
                'Legacy database migration and defect logging',
                'Migration of historical 2026 legacy snapshots and model active weights into V2 schema with defect tracking.',
                '["migrate_read_only", "discard_history"]',
                'Migrate legacy snapshots with defect flags into V2 tables without modifying source.',
                '["quant_engine.db"]', '{}',
                '2026-09-03', 'system:migration', 'system', NULL, NULL,
                'applied', '2026-06-14', ?, ?, NULL, NULL, 'git_sha'
            )
            """,
            (now_iso, adr_rel),
        )
        k_dir = Path("knowledge")
        if hasattr(ctx, "cfg") and ctx.cfg and hasattr(ctx.cfg, "paths") and hasattr(ctx.cfg.paths, "knowledge_dir"):
            k_dir = Path(ctx.cfg.paths.knowledge_dir)
        dec_dir = k_dir / "decisions"
        dec_dir.mkdir(parents=True, exist_ok=True)
        from quant.knowledge import adr
        adr.write(v2_conn, "D-0000-migration", dec_dir)

        return Result(
            status="ok",
            counts={
                "scores": scores_migrated,
                "defects": defects_count,
                "resolved": len(resolved_tickers),
                "unresolved": len(unresolved_tickers),
            },
            details={
                "resolved_tickers": sorted(resolved_tickers),
                "unresolved_tickers": sorted(unresolved_tickers),
            },
        )
    finally:
        src_conn.close()


def reconcile(conn: sqlite3.Connection, legacy_db_path: Path) -> pd.DataFrame:
    """Reconcile migrated legacy metrics with original red-team table."""
    legacy_db_path = Path(legacy_db_path)
    if not legacy_db_path.exists():
        raise FileNotFoundError(f"Legacy database '{legacy_db_path}' not found")

    columns = [
        "transition",
        "metric",
        "legacy_expected",
        "recomputed_original",
        "adjusted_descriptive",
        "difference",
        "status",
    ]

    src_uri = f"file:{legacy_db_path.resolve()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    src_conn.row_factory = sqlite3.Row

    FULL_UNIVERSE_MIN = 480

    try:
        preds = pd.read_sql_query(
            "SELECT id, date, ticker, price, quality_score, valuation_score, growth_score, "
            "moat_score, risk_score, bs_score, cap_alloc_score, smart_money_score, "
            "trap_score, momentum_multiplier, final_score, base_score, concall_sentiment_score "
            "FROM daily_predictions",
            src_conn,
        )
        counts = preds.groupby("date").size()
        min_tickers = 15 if len(preds) < 500 else FULL_UNIVERSE_MIN
        full_spec_dates = [s[0] for s in LEGACY_SNAPSHOT_SPECS if s[2] == 1]
        present_full = [d for d in full_spec_dates if d in counts.index and counts[d] >= min_tickers]
        if len(present_full) >= 2:
            snapshot_dates = present_full
        else:
            all_dates = sorted(counts[counts >= min_tickers].index.tolist())
            snapshot_dates = []
            for d in all_dates:
                if not snapshot_dates:
                    snapshot_dates.append(d)
                else:
                    days = (pd.to_datetime(d) - pd.to_datetime(snapshot_dates[-1])).days
                    if days >= 7:
                        snapshot_dates.append(d)

        price_matrix = preds.pivot_table(index="ticker", columns="date", values="price")
        weights_df = pd.read_sql_query("SELECT * FROM active_weights ORDER BY id", src_conn)

        transitions = []
        for i in range(len(snapshot_dates) - 1):
            transitions.append((snapshot_dates[i], snapshot_dates[i + 1]))

        print(f"Legacy snapshots: {len(snapshot_dates)}, transitions: {len(transitions)}")
        print(f"Uncertainty limitation: only {len(transitions)} irregular periods; statistical power is low, estimates are descriptive.")

        if not transitions:
            return pd.DataFrame(columns=columns)

        def _calc_rank_ic(x: pd.Series, y: pd.Series) -> float:
            if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
                return 0.0
            v = x.corr(y, method="spearman")
            return 0.0 if np.isnan(v) else float(v)

        from quant_math import FACTOR_WEIGHT_KEYS
        from weight_optimizer import FACTOR_MAP, weights_in_force

        red_team_expected = {
            ("2026-06-14 ➔ 2026-07-11", "final_score"): -0.063,
            ("2026-06-14 ➔ 2026-07-11", "momentum_multiplier"): -0.033,
            ("2026-06-14 ➔ 2026-07-11", "fundamental_composite"): 0.045,
            ("2026-06-14 ➔ 2026-07-11", "equal_weight_composite"): -0.006,

            ("2026-07-11 ➔ 2026-08-14", "final_score"): 0.092,
            ("2026-07-11 ➔ 2026-08-14", "momentum_multiplier"): 0.030,
            ("2026-07-11 ➔ 2026-08-14", "fundamental_composite"): 0.058,
            ("2026-07-11 ➔ 2026-08-14", "equal_weight_composite"): 0.115,

            ("2026-08-14 ➔ 2026-09-03", "final_score"): 0.117,
            ("2026-08-14 ➔ 2026-09-03", "momentum_multiplier"): 0.125,
            ("2026-08-14 ➔ 2026-09-03", "fundamental_composite"): 0.050,
            ("2026-08-14 ➔ 2026-09-03", "equal_weight_composite"): 0.029,

            ("MEAN", "final_score"): 0.049,
            ("MEAN", "momentum_multiplier"): 0.041,
            ("MEAN", "fundamental_composite"): 0.051,
            ("MEAN", "equal_weight_composite"): 0.046,
        }

        rows = []

        period_metrics_vals = {
            "final_score": [],
            "momentum_multiplier": [],
            "fundamental_composite": [],
            "equal_weight_composite": [],
        }
        period_adj_vals = {
            "final_score": [],
            "momentum_multiplier": [],
            "fundamental_composite": [],
            "equal_weight_composite": [],
        }
        period_full_ok: list[bool] = []

        for start_d, end_d in transitions:
            sub = preds[preds["date"] == start_d].copy()
            sub["p_start"] = sub["price"]
            sub["p_end"] = sub["ticker"].map(price_matrix[end_d])
            sub["fwd_return"] = (sub["p_end"] - sub["p_start"]) / sub["p_start"]
            sub = sub.dropna(subset=["fwd_return"])

            # Corporate action exclusion guard: |ret| > 0.60
            clean = sub[sub["fwd_return"].abs() <= 0.60].copy()

            if len(clean) < 10:
                continue

            # Full universe requires >=480 rows present on BOTH endpoints of the transition;
            # a sample subset is reported as INSUFFICIENT, never PASS (INTERFACES.md C10).
            full_ok = int(counts.get(start_d, 0)) >= FULL_UNIVERSE_MIN and int(counts.get(end_d, 0)) >= FULL_UNIVERSE_MIN
            period_full_ok.append(full_ok)

            inforce_w = weights_in_force(weights_df, start_d)
            eq_w = {k: 1.0 / len(FACTOR_WEIGHT_KEYS) for k in FACTOR_WEIGHT_KEYS}

            fund_comp = sum(clean[col] * inforce_w[wkey] for col, wkey in FACTOR_MAP.values())
            eq_comp = sum(clean[col] * eq_w[wkey] for col, wkey in FACTOR_MAP.values())

            r = clean["fwd_return"]
            final_ic = _calc_rank_ic(clean["final_score"], r)
            mom_ic = _calc_rank_ic(clean["momentum_multiplier"], r)
            fund_ic = _calc_rank_ic(fund_comp, r)
            eq_ic = _calc_rank_ic(eq_comp, r)

            # Adjusted / descriptive IC (unclipped return)
            r_adj = sub["fwd_return"]
            final_adj = _calc_rank_ic(sub["final_score"], r_adj)
            mom_adj = _calc_rank_ic(sub["momentum_multiplier"], r_adj)
            fund_sub = sum(sub[col] * inforce_w[wkey] for col, wkey in FACTOR_MAP.values())
            eq_sub = sum(sub[col] * eq_w[wkey] for col, wkey in FACTOR_MAP.values())
            fund_adj = _calc_rank_ic(fund_sub, r_adj)
            eq_adj = _calc_rank_ic(eq_sub, r_adj)

            trans_label = f"{start_d} ➔ {end_d}"

            metrics_map = [
                ("final_score", final_ic, final_adj),
                ("momentum_multiplier", mom_ic, mom_adj),
                ("fundamental_composite", fund_ic, fund_adj),
                ("equal_weight_composite", eq_ic, eq_adj),
            ]

            for m_name, orig_val, adj_val in metrics_map:
                period_metrics_vals[m_name].append(orig_val)
                period_adj_vals[m_name].append(adj_val)

                exp = red_team_expected.get((trans_label, m_name))
                if exp is not None:
                    diff = orig_val - exp
                    if full_ok:
                        status = "PASS" if abs(diff) <= 0.01 else "FAIL"
                    else:
                        status = "INSUFFICIENT"
                else:
                    exp = np.nan
                    diff = np.nan
                    status = "INFO"

                rows.append({
                    "transition": trans_label,
                    "metric": m_name,
                    "legacy_expected": exp,
                    "recomputed_original": round(float(orig_val), 4),
                    "adjusted_descriptive": round(float(adj_val), 4),
                    "difference": round(float(diff), 4) if not np.isnan(diff) else np.nan,
                    "status": status,
                })

        if len(transitions) >= 3:
            mean_full_ok = bool(period_full_ok) and all(period_full_ok)
            for m_name in ["final_score", "momentum_multiplier", "fundamental_composite", "equal_weight_composite"]:
                vals = period_metrics_vals[m_name]
                adj_vals = period_adj_vals[m_name]
                mean_orig = np.mean(vals)
                mean_adj = np.mean(adj_vals)
                exp = red_team_expected.get(("MEAN", m_name))
                if exp is not None:
                    diff = mean_orig - exp
                    if mean_full_ok:
                        status = "PASS" if abs(diff) <= 0.01 else "FAIL"
                    else:
                        status = "INSUFFICIENT"
                else:
                    exp = np.nan
                    diff = np.nan
                    status = "INFO"
                rows.append({
                    "transition": "MEAN",
                    "metric": m_name,
                    "legacy_expected": exp,
                    "recomputed_original": round(float(mean_orig), 4),
                    "adjusted_descriptive": round(float(mean_adj), 4),
                    "difference": round(float(diff), 4) if not np.isnan(diff) else np.nan,
                    "status": status,
                })

        return pd.DataFrame(rows, columns=columns)
    finally:
        src_conn.close()
