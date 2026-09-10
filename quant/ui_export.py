"""UI and evidence payload exporter (C11).

Generates browser-native standalone data payloads in ui_dir:
  1. data.js - ranking, stock details, turnaround views
  2. data_learning.js - evidence curves, OOS learning records
  3. data_scoreboard.js - portfolios, NAVs, pending orders, benchmarks
  4. data_factors.js - factor registry, definitions, diagnostics
  5. data_kb.js - hypotheses, decisions, proposals, lessons
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config


def export(conn: sqlite3.Connection, cfg: Config) -> list[Path]:
    """Export persisted database state into offline UI javascript files."""
    ui_dir = Path(cfg.paths.ui_dir)
    ui_dir.mkdir(parents=True, exist_ok=True)
    out_files: list[Path] = []

    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    # 1. Export data.js (ranking and universe)
    latest_cohort_row = cur.execute(
        "SELECT * FROM cohorts ORDER BY as_of DESC, published_at DESC LIMIT 1"
    ).fetchone()
    
    as_of = latest_cohort_row["as_of"] if latest_cohort_row else "none"
    track = latest_cohort_row["track"] if latest_cohort_row else "live"
    gen_at = latest_cohort_row["generated_at"] if latest_cohort_row else ""
    cutoff = latest_cohort_row["knowledge_cutoff"] if latest_cohort_row else ""
    cohort_id = latest_cohort_row["cohort_id"] if latest_cohort_row else ""

    scores_rows = []
    if cohort_id:
        scores_rows = cur.execute(
            """
            SELECT s.*, sec.isin, sec.name as company_name, sh.nse_symbol, sh.yahoo_ticker
            FROM scores s
            JOIN securities sec ON s.security_id = sec.security_id
            LEFT JOIN symbol_history sh ON s.security_id = sh.security_id AND sh.valid_to IS NULL
            WHERE s.cohort_id = ?
            ORDER BY s.final DESC
            """,
            (cohort_id,),
        ).fetchall()

    stocks = []
    turnaround = []
    for r in scores_rows:
        d = dict(r)
        ticker = d.get("yahoo_ticker") or f"{d.get('nse_symbol', 'STOCK')}.NS"
        stock_item = {
            "security_id": d["security_id"],
            "ticker": ticker,
            "nse_symbol": d.get("nse_symbol") or "",
            "company_name": d.get("company_name") or "",
            "isin": d.get("isin") or "",
            "model_id": d["model_id"],
            "final_score": d["final"],
            "composite": d["composite"],
            "rank": d["rank"],
            "decile": d["decile"],
            "quintile": d["quintile"],
            "sector_group": d["sector_group"],
            "dc_flag": d["dc_flag"],
            "eligible": d["eligible"],
        }
        stocks.append(stock_item)
        if d.get("composite", 0) >= 80 and d.get("final", 0) < 50:
            turnaround.append(stock_item)

    data_payload = {
        "as_of": as_of,
        "track": track,
        "generated_at": gen_at,
        "source_cutoff": cutoff,
        "cohort_id": cohort_id,
        "freshness": gen_at[:10] if gen_at else "",
        "stocks": stocks,
        "turnaround": turnaround,
    }
    data_path = ui_dir / "data.js"
    data_path.write_text(f"window.QUANT_DATA = {json.dumps(data_payload, indent=2)};\n", encoding="utf-8")
    out_files.append(data_path)

    # 2. Export data_learning.js (curves, evaluations, inferential validation)
    eval_rows = cur.execute("SELECT * FROM evaluations ORDER BY as_of ASC, horizon_m ASC").fetchall()
    evaluations = []
    for r in eval_rows:
        d = dict(r)
        u_stat = d.get("status", "unknown")
        ci_lo = d.get("ci90_lo")
        ci_hi = d.get("ci90_hi")

        # A row that claims an estimable interval must carry both endpoints (MASTER_SPEC 7.3:
        # never print a missing band as a number). Per-date cross-sections normally have no
        # band at all and are labelled not_applicable.
        claims_band = u_stat == "estimable" or (ci_lo is not None) != (ci_hi is not None)
        if claims_band and (ci_lo is None or ci_hi is None):
            raise ValueError(
                f"Claimed estimable interval whose endpoints are absent: eval_id={d['eval_id']}, metric={d['metric']}"
            )
        band_status = "ok" if (ci_lo is not None and ci_hi is not None) else "not_applicable"

        eval_item = {
            "eval_id": d["eval_id"],
            "subject_kind": d["subject_kind"],
            "subject_id": d["subject_id"],
            "subject_version": d["subject_version"],
            "as_of": d["as_of"],
            "horizon_m": d["horizon_m"],
            "track": d["track"],
            "metric": d["metric"],
            "value": d["value"],
            "n": d["n"],
            "n_eff": d["n_eff"],
            "method": d["method"],
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
            "uncertainty_status": u_stat,
            "band_status": band_status,
            "evidence_hash": d["evidence_hash"],
        }
        evaluations.append(eval_item)

    curve_rows = cur.execute("SELECT * FROM evidence_curve ORDER BY horizon_m ASC").fetchall()
    curves = []
    for r in curve_rows:
        c = dict(r)
        # Invariant: a curve claiming an estimable band must carry both endpoints; an
        # insufficient/constant curve must not carry a band.
        has_band = c.get("ci90_lo") is not None and c.get("ci90_hi") is not None
        if c.get("status") == "ok" and not has_band:
            raise ValueError(f"evidence_curve {c.get('subject_id')} h={c.get('horizon_m')} status ok without band endpoints")
        if c.get("status") != "ok" and has_band:
            raise ValueError(f"evidence_curve {c.get('subject_id')} h={c.get('horizon_m')} status {c.get('status')} with a band")
        c["band_status"] = "ok" if c.get("status") == "ok" else f"unavailable ({c.get('status')})"
        curves.append(c)

    learning_payload = {
        "as_of": as_of,
        "evaluations": evaluations,
        "curves": curves,
    }
    learning_path = ui_dir / "data_learning.js"
    learning_path.write_text(f"window.QUANT_LEARNING = {json.dumps(learning_payload, indent=2)};\n", encoding="utf-8")
    out_files.append(learning_path)

    # 3. Export data_scoreboard.js (portfolios, returns, pending orders)
    port_rows = cur.execute("SELECT * FROM portfolios").fetchall()
    portfolios = [dict(r) for r in port_rows]

    ret_rows = cur.execute("SELECT * FROM portfolio_returns ORDER BY month_end ASC, revision DESC").fetchall()
    returns = [dict(r) for r in ret_rows]

    order_rows = cur.execute(
        """
        SELECT po.*, sec.isin, sh.nse_symbol
        FROM portfolio_orders po
        JOIN securities sec ON po.security_id = sec.security_id
        LEFT JOIN symbol_history sh ON po.security_id = sh.security_id AND sh.valid_to IS NULL
        ORDER BY po.created_at DESC
        """
    ).fetchall()
    orders = [dict(r) for r in order_rows]

    bench_rows = cur.execute("SELECT * FROM benchmarks_monthly ORDER BY month_end ASC").fetchall()
    benchmarks = [dict(r) for r in bench_rows]

    scoreboard_payload = {
        "as_of": as_of,
        "generated_at": gen_at,
        "portfolios": portfolios,
        "returns": returns,
        "pending_orders": orders,
        "benchmarks": benchmarks,
    }
    scoreboard_path = ui_dir / "data_scoreboard.js"
    scoreboard_path.write_text(f"window.QUANT_SCOREBOARD = {json.dumps(scoreboard_payload, indent=2)};\n", encoding="utf-8")
    out_files.append(scoreboard_path)

    # 4. Export data_factors.js (registry, exposures, definitions)
    fac_rows = cur.execute("SELECT * FROM factor_registry ORDER BY factor_id ASC").fetchall()
    factors = [dict(r) for r in fac_rows]

    contracts_rows = cur.execute("SELECT * FROM field_contracts").fetchall()
    contracts = [dict(r) for r in contracts_rows]

    factors_payload = {
        "factors": factors,
        "contracts": contracts,
    }
    factors_path = ui_dir / "data_factors.js"
    factors_path.write_text(f"window.QUANT_FACTORS = {json.dumps(factors_payload, indent=2)};\n", encoding="utf-8")
    out_files.append(factors_path)

    # 5. Export data_kb.js (decisions, proposals, lessons, hypotheses)
    dec_rows = cur.execute("SELECT * FROM decisions ORDER BY decided_on DESC").fetchall()
    decisions = [dict(r) for r in dec_rows]

    prop_rows = cur.execute("SELECT * FROM proposals ORDER BY as_of DESC").fetchall()
    proposals = [dict(r) for r in prop_rows]

    les_rows = cur.execute("SELECT * FROM lessons ORDER BY recorded_on DESC").fetchall()
    lessons = [dict(r) for r in les_rows]

    hyp_rows = cur.execute("SELECT * FROM hypotheses ORDER BY registered_on DESC").fetchall()
    hypotheses = [dict(r) for r in hyp_rows]

    kb_payload = {
        "decisions": decisions,
        "proposals": proposals,
        "lessons": lessons,
        "hypotheses": hypotheses,
    }
    kb_path = ui_dir / "data_kb.js"
    kb_path.write_text(f"window.QUANT_KB = {json.dumps(kb_payload, indent=2)};\n", encoding="utf-8")
    out_files.append(kb_path)

    return out_files
