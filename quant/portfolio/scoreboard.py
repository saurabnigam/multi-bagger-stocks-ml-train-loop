"""Portfolio scoreboard computation, benchmark comparison, and alpha metrics (C08)."""

from __future__ import annotations

import math
import sqlite3
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.evaluation.stats import hac_mean_test
from quant.portfolio.paper import net_selection_spread

if TYPE_CHECKING:
    from quant.config import Config


def compute(conn: sqlite3.Connection, through: str, cfg: Config) -> pd.DataFrame:
    """Compute alpha scoreboard across paper portfolios through the specified date.

    Returns DataFrame with columns:
      portfolio_id, window_start, window_end, n_months, ret_net, excess_net,
      ir, hac_t, ci_lo, ci_hi, turnover, cost_drag, max_drawdown, verdict, evidence_refs.
    """
    cols = [
        "portfolio_id",
        "window_start",
        "window_end",
        "n_months",
        "ret_net",
        "excess_net",
        "ir",
        "hac_t",
        "ci_lo",
        "ci_hi",
        "turnover",
        "cost_drag",
        "max_drawdown",
        "verdict",
        "evidence_refs",
    ]

    p_rows = conn.execute(
        "SELECT portfolio_id, model_id, subject_kind, subject_id, subject_version, cohort_id, rule, cadence, inception "
        "FROM portfolios"
    ).fetchall()
    known_portfolios = {r["portfolio_id"]: dict(r) for r in p_rows}

    ret_pids = conn.execute(
        "SELECT DISTINCT portfolio_id FROM portfolio_returns WHERE month_end <= ?",
        (through,),
    ).fetchall()
    for r in ret_pids:
        pid = r["portfolio_id"]
        if pid not in known_portfolios:
            known_portfolios[pid] = {
                "portfolio_id": pid,
                "model_id": None,
                "subject_kind": "unknown",
                "subject_id": pid,
                "subject_version": "1",
                "cohort_id": None,
                "rule": "unknown",
                "cadence": "monthly",
                "inception": None,
            }

    if not known_portfolios:
        return pd.DataFrame(columns=cols)

    rows = []
    for pid, p_meta in sorted(known_portfolios.items(), key=lambda x: x[0]):
        # Query monthly returns with latest revision per month_end
        ret_rows = conn.execute(
            "SELECT month_end, revision, evidence_hash, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew "
            "FROM portfolio_returns "
            "WHERE portfolio_id = ? AND month_end <= ? "
            "ORDER BY month_end ASC, revision DESC",
            (pid, through),
        ).fetchall()

        # Deduplicate to latest revision per month_end
        seen_months: set[str] = set()
        records: list[dict[str, Any]] = []
        for r in ret_rows:
            m_end = r["month_end"]
            if m_end not in seen_months:
                seen_months.add(m_end)
                records.append(dict(r))
        records.sort(key=lambda x: x["month_end"])

        n_months = len(records)
        if n_months == 0:
            rows.append({
                "portfolio_id": pid,
                "window_start": None,
                "window_end": None,
                "n_months": 0,
                "ret_net": None,
                "excess_net": None,
                "ir": None,
                "hac_t": None,
                "ci_lo": None,
                "ci_hi": None,
                "turnover": None,
                "cost_drag": None,
                "max_drawdown": None,
                "verdict": "insufficient",
                "evidence_refs": [],
            })
            continue

        window_start = records[0]["month_end"]
        window_end = records[-1]["month_end"]
        ev_refs = [r["evidence_hash"] for r in records if r.get("evidence_hash")]

        # Compounded net return
        ret_net_list = [float(r["ret_net"] or 0.0) for r in records]
        ret_net = float(np.prod([1.0 + r for r in ret_net_list]) - 1.0)

        # Cost drag and turnover summed across the interval
        cost_drag = float(sum(float(r["cost"] or 0.0) for r in records))
        turnover = float(sum(float(r["turnover_one_way"] or 0.0) for r in records))

        # Max drawdown
        nav = [1.0]
        for r in ret_net_list:
            nav.append(nav[-1] * (1.0 + r))
        nav_arr = np.array(nav)
        peaks = np.maximum.accumulate(nav_arr)
        drawdowns = (peaks - nav_arr) / peaks
        max_drawdown = float(np.max(drawdowns))

        # Matched benchmark net return
        rule = p_meta.get("rule", "")
        excess_net: float | None = None
        diffs: list[float] | None = None
        bm_found = False

        # Case 1: matched EW minus itself is 0
        if (
            rule in ("cohort_matched_ew", "ew_universe")
            or pid.endswith("_matched_ew")
            or pid.endswith("_ew_universe")
        ):
            excess_net = 0.0
            diffs = [0.0] * n_months
            bm_found = True

        # Case 2: top quintile cohort book -> compare with cohort_matched_ew
        elif rule == "cohort_top_quintile":
            bm_pid = pid.replace("cohort_top_quintile", "cohort_matched_ew")
            bm_rows = conn.execute(
                "SELECT month_end, ret_net FROM portfolio_returns "
                "WHERE portfolio_id = ? AND month_end <= ? ORDER BY month_end ASC, revision DESC",
                (bm_pid, through),
            ).fetchall()
            bm_map = {}
            for br in bm_rows:
                if br["month_end"] not in bm_map:
                    bm_map[br["month_end"]] = float(br["ret_net"] or 0.0)

            if all(r["month_end"] in bm_map for r in records):
                bm_ret_list = [bm_map[r["month_end"]] for r in records]
                bm_net = float(np.prod([1.0 + b for b in bm_ret_list]) - 1.0)
                excess_net = float(ret_net - bm_net)
                diffs = [p_r - b_r for p_r, b_r in zip(ret_net_list, bm_ret_list)]
                bm_found = True

        # Case 3: rolling model portfolio
        else:
            cand_pids = []
            if p_meta.get("model_id"):
                cand_pids.extend([
                    f"{p_meta['model_id']}_matched_ew",
                    f"{p_meta['model_id']}_ew_universe",
                ])
            cand_pids.extend([f"{pid}_matched_ew", "ew_universe", "matched_ew"])

            matched_pid = None
            for cp in cand_pids:
                has_cand = conn.execute(
                    "SELECT 1 FROM portfolio_returns WHERE portfolio_id = ? LIMIT 1",
                    (cp,),
                ).fetchone()
                if has_cand:
                    matched_pid = cp
                    break

            if matched_pid:
                bm_rows = conn.execute(
                    "SELECT month_end, ret_net FROM portfolio_returns "
                    "WHERE portfolio_id = ? AND month_end <= ? ORDER BY month_end ASC, revision DESC",
                    (matched_pid, through),
                ).fetchall()
                bm_map = {}
                for br in bm_rows:
                    if br["month_end"] not in bm_map:
                        bm_map[br["month_end"]] = float(br["ret_net"] or 0.0)

                if all(r["month_end"] in bm_map for r in records):
                    bm_ret_list = [bm_map[r["month_end"]] for r in records]
                    bm_net = float(np.prod([1.0 + b for b in bm_ret_list]) - 1.0)
                    excess_net = float(ret_net - bm_net)
                    diffs = [p_r - b_r for p_r, b_r in zip(ret_net_list, bm_ret_list)]
                    bm_found = True

            if not bm_found and all(r.get("bm_ew") is not None for r in records):
                bm_ret_list = [float(r["bm_ew"]) for r in records]
                bm_net = float(np.prod([1.0 + b for b in bm_ret_list]) - 1.0)
                excess_net = float(ret_net - bm_net)
                diffs = [p_r - b_r for p_r, b_r in zip(ret_net_list, bm_ret_list)]
                bm_found = True

        # IR and annualized inference: absent below 24 portfolio months
        ir: float | None = None
        hac_t: float | None = None
        ci_lo: float | None = None
        ci_hi: float | None = None
        verdict = "insufficient"

        if n_months >= 24 and diffs is not None:
            if all(abs(d) < 1e-12 for d in diffs):
                ir = 0.0
                verdict = "insufficient"
            else:
                mean_d = float(np.mean(diffs))
                std_d = float(np.std(diffs, ddof=1))
                ir = float((mean_d / std_d) * math.sqrt(12)) if std_d > 0 else None

                hac_res = hac_mean_test(diffs, lag=3)
                hac_t = hac_res.t
                ci_lo = hac_res.ci_lo
                ci_hi = hac_res.ci_hi

                if (excess_net is not None and excess_net < 0) or (hac_t is not None and hac_t < 0):
                    verdict = "negative"
                elif ir is not None and hac_t is not None and ir > 0.5 and hac_t >= 2.0:
                    verdict = "positive"
                elif excess_net is not None and excess_net > 0:
                    verdict = "weak positive"
                else:
                    verdict = "insufficient"

        rows.append({
            "portfolio_id": pid,
            "window_start": window_start,
            "window_end": window_end,
            "n_months": n_months,
            "ret_net": ret_net,
            "excess_net": excess_net,
            "ir": ir,
            "hac_t": hac_t,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
            "turnover": turnover,
            "cost_drag": cost_drag,
            "max_drawdown": max_drawdown,
            "verdict": verdict,
            "evidence_refs": ev_refs,
        })

    return pd.DataFrame(rows, columns=cols)
