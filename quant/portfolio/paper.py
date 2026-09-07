"""Paper portfolio planning, execution settlement, NAV roll-forward and attribution (C08)."""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.data.calendar import Calendar
from quant.db.core import connect
from quant.portfolio.construct import rebalance
from quant.portfolio.costs import bucket, cost_bps_one_way
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext


def _add_months(as_of: str, months: int) -> str:
    """Calculate endpoint month and return the last calendar day in that month."""
    dt = datetime.date.fromisoformat(as_of)
    month = dt.month + months
    year = dt.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _get_calendar(ctx: RunContext) -> Calendar:
    if getattr(ctx, "calendar", None) is not None:
        return ctx.calendar
    dates = pd.bdate_range("2020-01-01", "2035-12-31")
    sessions_df = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "close_at": [f"{d.strftime('%Y-%m-%d')}T10:00:00.000000Z" for d in dates],
    })
    return Calendar(sessions_df)


def _discover_prices_conn(conn: sqlite3.Connection, cfg: Any = None) -> sqlite3.Connection | None:
    if cfg and hasattr(cfg, "paths") and hasattr(cfg.paths, "prices_db"):
        try:
            return connect(cfg.paths.prices_db, readonly=True)
        except Exception:
            pass

    try:
        db_rows = conn.execute("PRAGMA database_list").fetchall()
        for d_row in db_rows:
            if d_row["name"] == "main" and d_row["file"]:
                main_file = Path(d_row["file"])
                for cand_name in ("test_prices.db", "prices.db", "prices_daily.sqlite"):
                    cand_p = main_file.parent / cand_name
                    if cand_p.exists():
                        return connect(cand_p, readonly=True)
    except Exception:
        pass
    return None


def plan(ctx: RunContext, cohort_id: str) -> Result:
    """Create pending orders for rolling models and factor/model attribution books.

    Orders cannot fill before the next session close after generation.
    Attribution books create entry and exit orders pinning the cohort.
    A retry cannot add duplicate orders.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"orders_planned": 0})

    cal = _get_calendar(ctx)

    cohort_row = conn.execute(
        "SELECT as_of, track FROM cohorts WHERE cohort_id = ?", (cohort_id,)
    ).fetchone()
    if not cohort_row:
        return Result(status="ok", counts={"orders_planned": 0})

    as_of = cohort_row["as_of"]
    created_at = (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{as_of}T09:00:00.000000Z"
    )

    entry_exec_at = cal.first_exec_after(created_at)

    endpoint_date = _add_months(as_of, 3)
    try:
        endpoint_session = cal.last_session_on_or_before(endpoint_date)
        exit_exec_at = cal._close_ats[endpoint_session]
    except Exception:
        exit_exec_at = f"{endpoint_date}T10:00:00.000000Z"

    orders_planned = 0

    # 1. Rolling portfolios for scored models (e.g. top30_buffer)
    model_rows = conn.execute(
        "SELECT DISTINCT model_id, model_version FROM scores WHERE cohort_id = ?",
        (cohort_id,),
    ).fetchall()

    for m_row in model_rows:
        model_id = m_row["model_id"]
        model_version = m_row["model_version"]

        score_rows = conn.execute(
            "SELECT security_id, final, rank, scored, eligible, sector_group, liquidity_bucket "
            "FROM scores WHERE cohort_id = ? AND model_id = ? ORDER BY rank, security_id",
            (cohort_id, model_id),
        ).fetchall()

        if not score_rows:
            continue

        sids = [r["security_id"] for r in score_rows]
        ranks = pd.Series({r["security_id"]: r["rank"] for r in score_rows})
        eligible = pd.Series({r["security_id"]: bool(r["eligible"]) for r in score_rows})
        groups = pd.Series({r["security_id"]: r["sector_group"] for r in score_rows})
        buckets = pd.Series({r["security_id"]: r["liquidity_bucket"] for r in score_rows})

        portfolio_id = f"{model_id}_top30_buffer"
        conn.execute(
            "INSERT OR IGNORE INTO portfolios "
            "(portfolio_id, model_id, subject_kind, subject_id, subject_version, cohort_id, rule, cadence, inception, rule_version) "
            "VALUES (?, ?, 'model', ?, ?, ?, 'top30_buffer', 'monthly', ?, '1')",
            (portfolio_id, model_id, model_id, str(model_version), cohort_id, as_of),
        )

        prev_rows = conn.execute(
            "SELECT security_id, weight, entry_as_of FROM portfolio_positions "
            "WHERE portfolio_id = ? AND as_of = ("
            "  SELECT max(as_of) FROM portfolio_positions WHERE portfolio_id = ? AND as_of < ?"
            ")",
            (portfolio_id, portfolio_id, as_of),
        ).fetchall()
        prev_df = (
            pd.DataFrame([dict(r) for r in prev_rows])
            if prev_rows
            else pd.DataFrame(columns=["security_id", "weight", "entry_as_of"])
        )

        positions, deltas = rebalance(
            previous=prev_df,
            ranks=ranks,
            eligible=eligible,
            groups=groups,
            buckets=buckets,
            cfg=ctx.cfg,
            rule="top30_buffer",
        )

        target_map = dict(zip(positions["security_id"], positions["target_weight"]))
        for _, delta in deltas.iterrows():
            sid = int(delta["security_id"])
            side = str(delta["side"]).lower()
            target_w = float(target_map.get(sid, 0.0))
            b = str(buckets.get(sid, "B"))
            order_id = f"{portfolio_id}:{cohort_id}:{sid}:rebalance"

            cur = conn.execute(
                "INSERT OR IGNORE INTO portfolio_orders "
                "(order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'rebalance', ?, ?, 'pending', ?, NULL)",
                (order_id, portfolio_id, cohort_id, sid, created_at, entry_exec_at, side, target_w, b),
            )
            if cur.rowcount > 0:
                orders_planned += 1

    # 2. Factor and model attribution books: TOP_Q20 and MATCHED_EW (cadence: 3M)
    # 2a. Model attribution books
    for m_row in model_rows:
        model_id = m_row["model_id"]
        model_version = m_row["model_version"]

        score_rows = conn.execute(
            "SELECT security_id, final, rank, scored, eligible, liquidity_bucket "
            "FROM scores WHERE cohort_id = ? AND model_id = ? AND eligible = 1 AND liquidity_bucket != 'D' "
            "ORDER BY final DESC, security_id ASC",
            (cohort_id, model_id),
        ).fetchall()

        if not score_rows:
            continue

        orders_planned += _plan_attribution_pair(
            conn=conn,
            cohort_id=cohort_id,
            subject_kind="model",
            subject_id=model_id,
            subject_version=str(model_version),
            model_id=model_id,
            as_of=as_of,
            created_at=created_at,
            entry_exec_at=entry_exec_at,
            exit_exec_at=exit_exec_at,
            sorted_candidates=[(r["security_id"], r["liquidity_bucket"]) for r in score_rows],
        )

    # 2b. Factor attribution books
    factor_rows = conn.execute(
        "SELECT DISTINCT fv.factor_id, fr.version "
        "FROM factor_values fv "
        "JOIN factor_registry fr ON fv.factor_id = fr.factor_id "
        "WHERE fv.cohort_id = ? AND fr.status IN ('active', 'shadow', 'probation')",
        (cohort_id,),
    ).fetchall()

    for f_row in factor_rows:
        factor_id = f_row["factor_id"]
        version = str(f_row["version"])

        fv_rows = conn.execute(
            "SELECT fv.security_id, fv.z, s.liquidity_bucket "
            "FROM factor_values fv "
            "JOIN scores s ON fv.cohort_id = s.cohort_id AND fv.security_id = s.security_id "
            "WHERE fv.cohort_id = ? AND fv.factor_id = ? AND fv.z IS NOT NULL "
            "  AND s.eligible = 1 AND s.liquidity_bucket != 'D' "
            "ORDER BY fv.z DESC, fv.security_id ASC",
            (cohort_id, factor_id),
        ).fetchall()

        if not fv_rows:
            continue

        orders_planned += _plan_attribution_pair(
            conn=conn,
            cohort_id=cohort_id,
            subject_kind="factor",
            subject_id=factor_id,
            subject_version=version,
            model_id=None,
            as_of=as_of,
            created_at=created_at,
            entry_exec_at=entry_exec_at,
            exit_exec_at=exit_exec_at,
            sorted_candidates=[(r["security_id"], r["liquidity_bucket"]) for r in fv_rows],
        )

    conn.commit()
    return Result(status="ok", counts={"orders_planned": orders_planned})


def _plan_attribution_pair(
    conn: sqlite3.Connection,
    cohort_id: str,
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    model_id: str | None,
    as_of: str,
    created_at: str,
    entry_exec_at: str,
    exit_exec_at: str,
    sorted_candidates: list[tuple[int, str]],
) -> int:
    """Helper to plan TOP_Q20 and MATCHED_EW attribution books."""
    orders_planned = 0
    k = len(sorted_candidates)
    if k == 0:
        return 0

    # TOP_Q20: highest quintile (top 20%)
    n_q20 = max(1, k // 5)
    q20_candidates = sorted_candidates[:n_q20]
    ew_candidates = sorted_candidates

    books = [
        ("cohort_top_quintile", q20_candidates),
        ("cohort_matched_ew", ew_candidates),
    ]

    for rule, candidates in books:
        portfolio_id = f"{subject_kind}_{subject_id}@{subject_version}_{cohort_id}_{rule}"
        conn.execute(
            "INSERT OR IGNORE INTO portfolios "
            "(portfolio_id, model_id, subject_kind, subject_id, subject_version, cohort_id, rule, cadence, inception, rule_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '3M', ?, '1')",
            (portfolio_id, model_id, subject_kind, subject_id, subject_version, cohort_id, rule, as_of),
        )

        n_cand = len(candidates)
        nom_w = 1.0 / n_cand
        c_sids = [s for s, b in candidates if b == "C"]
        ab_sids = [s for s, b in candidates if b in ("A", "B")]
        c_weight = min(nom_w, 0.02)
        total_c = len(c_sids) * c_weight
        rem_w = max(0.0, 1.0 - total_c)
        ab_weight = (rem_w / len(ab_sids)) if ab_sids else 0.0

        for sid, b in candidates:
            target_w = c_weight if sid in c_sids else ab_weight

            # Entry order
            entry_order_id = f"{portfolio_id}:{cohort_id}:{sid}:entry"
            cur1 = conn.execute(
                "INSERT OR IGNORE INTO portfolio_orders "
                "(order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'entry', 'buy', ?, 'pending', ?, NULL)",
                (entry_order_id, portfolio_id, cohort_id, sid, created_at, entry_exec_at, target_w, b),
            )
            if cur1.rowcount > 0:
                orders_planned += 1

            # Exit order
            exit_order_id = f"{portfolio_id}:{cohort_id}:{sid}:exit"
            cur2 = conn.execute(
                "INSERT OR IGNORE INTO portfolio_orders "
                "(order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'exit', 'sell', 0.0, 'pending', ?, NULL)",
                (exit_order_id, portfolio_id, cohort_id, sid, created_at, exit_exec_at, b),
            )
            if cur2.rowcount > 0:
                orders_planned += 1

    return orders_planned


def settle(ctx: RunContext, through: str) -> Result:
    """Settle eligible pending orders into portfolio_trades using execution-session prices.

    Orders cannot fill before earliest_exec_at <= through.
    Missing prices leave orders pending.
    Repeated settlement adds no fills (idempotent).
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"fills": 0})

    through_iso = through if "T" in through else f"{through}T23:59:59.999999Z"

    pending_orders = conn.execute(
        "SELECT order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at, purpose, side, target_weight, liquidity_bucket "
        "FROM portfolio_orders "
        "WHERE status = 'pending' AND earliest_exec_at <= ? "
        "ORDER BY earliest_exec_at ASC, order_id ASC",
        (through_iso,),
    ).fetchall()

    if not pending_orders:
        return Result(status="ok", counts={"fills": 0})

    p_conn = _discover_prices_conn(conn, getattr(ctx, "cfg", None))
    fills = 0

    try:
        for order in pending_orders:
            order_id = order["order_id"]
            portfolio_id = order["portfolio_id"]
            cohort_id = order["cohort_id"]
            security_id = int(order["security_id"])
            purpose = order["purpose"]
            side = str(order["side"]).lower()
            target_weight = float(order["target_weight"])
            bucket_name = str(order["liquidity_bucket"])
            exec_at = order["earliest_exec_at"]
            exec_date = exec_at[:10]

            # 1. Exit guard: Never settle an exit for an unfilled/cancelled entry
            if purpose == "exit":
                entry_order = conn.execute(
                    "SELECT status FROM portfolio_orders "
                    "WHERE portfolio_id = ? AND cohort_id = ? AND security_id = ? AND purpose = 'entry'",
                    (portfolio_id, cohort_id, security_id),
                ).fetchone()

                if not entry_order or entry_order["status"] != "filled":
                    if entry_order and entry_order["status"] == "cancelled":
                        conn.execute(
                            "UPDATE portfolio_orders SET status = 'cancelled' WHERE order_id = ?",
                            (order_id,),
                        )
                    continue

            # 2. Look up price on exec_date
            price_row = None
            if p_conn is not None:
                try:
                    price_row = p_conn.execute(
                        "SELECT close_raw, dividend_raw, split_ratio, source_sha256 "
                        "FROM prices_daily "
                        "WHERE security_id = ? AND date = ? "
                        "ORDER BY observed_at DESC LIMIT 1",
                        (security_id, exec_date),
                    ).fetchone()
                except Exception:
                    price_row = None

            if price_row is None:
                try:
                    price_row = conn.execute(
                        "SELECT close_raw, dividend_raw, split_ratio, price_manifest_sha "
                        "FROM prices_monthly "
                        "WHERE security_id = ? AND month_end = ? LIMIT 1",
                        (security_id, exec_date),
                    ).fetchone()
                except Exception:
                    price_row = None

            if price_row is None:
                # Missing price leaves order pending
                continue

            fill_price = float(price_row["close_raw"])
            if fill_price <= 0:
                continue

            manifest_sha = str(
                price_row["source_sha256"]
                if "source_sha256" in price_row.keys()
                else (price_row["price_manifest_sha"] if "price_manifest_sha" in price_row.keys() else "sha")
            )

            cost_bps = cost_bps_one_way(bucket_name, ctx.cfg, stress=False)

            # Determine weight delta
            if purpose in ("entry", "rebalance") and side == "buy":
                weight_delta = target_weight
            elif purpose == "exit" or side == "sell":
                prev_trade = conn.execute(
                    "SELECT weight_delta FROM portfolio_trades "
                    "WHERE portfolio_id = ? AND security_id = ? AND side IN ('buy', 'BUY')",
                    (portfolio_id, security_id),
                ).fetchone()
                if prev_trade:
                    weight_delta = -abs(float(prev_trade["weight_delta"]))
                else:
                    weight_delta = -target_weight if target_weight != 0 else -0.05
            else:
                weight_delta = target_weight

            conn.execute(
                "INSERT INTO portfolio_trades "
                "(order_id, portfolio_id, cohort_id, exec_at, security_id, side, weight_delta, fill_price, cost_bps, liquidity_bucket, price_manifest_sha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    portfolio_id,
                    cohort_id,
                    exec_at,
                    security_id,
                    side,
                    weight_delta,
                    fill_price,
                    cost_bps,
                    bucket_name,
                    manifest_sha,
                ),
            )

            conn.execute(
                "UPDATE portfolio_orders SET status = 'filled' WHERE order_id = ?",
                (order_id,),
            )
            fills += 1

        conn.commit()
    finally:
        if p_conn is not None:
            p_conn.close()

    return Result(status="ok", counts={"fills": fills})


def roll_forward(ctx: RunContext, through: str, portfolio_id: str | None = None) -> Result:
    """Derive NAV and monthly returns across actual fill and corporate action dates.

    Produces no pre-inception returns.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"returns_updated": 0})

    cal = _get_calendar(ctx)

    p_query = "SELECT portfolio_id, inception, rule FROM portfolios"
    params: list[Any] = []
    if portfolio_id:
        p_query += " WHERE portfolio_id = ?"
        params.append(portfolio_id)

    portfolios = conn.execute(p_query, params).fetchall()
    returns_updated = 0

    p_conn = _discover_prices_conn(conn, getattr(ctx, "cfg", None))

    try:
        for port in portfolios:
            pid = port["portfolio_id"]
            inception = port["inception"]
            if not inception:
                continue

            first_trade = conn.execute(
                "SELECT min(exec_at) as min_exec FROM portfolio_trades WHERE portfolio_id = ?",
                (pid,),
            ).fetchone()
            if not first_trade or not first_trade["min_exec"]:
                continue

            inception_date = first_trade["min_exec"][:10]
            month_ends = cal.month_ends(inception_date, through[:10])

            for m_end in month_ends:
                month_start = f"{m_end[:7]}-01"

                m_trades = conn.execute(
                    "SELECT weight_delta, cost_bps, liquidity_bucket FROM portfolio_trades "
                    "WHERE portfolio_id = ? AND exec_at >= ? AND exec_at <= ?",
                    (pid, f"{month_start}T00:00:00.000000Z", f"{m_end}T23:59:59.999999Z"),
                ).fetchall()

                turnover = 0.5 * sum(abs(float(t["weight_delta"])) for t in m_trades)
                cost = sum(
                    abs(float(t["weight_delta"])) * (float(t["cost_bps"]) / 10000.0)
                    for t in m_trades
                )
                cost_stress = sum(
                    abs(float(t["weight_delta"])) * (float(t["cost_bps"]) * 1.5 / 10000.0)
                    for t in m_trades
                )

                active_trades = conn.execute(
                    "SELECT security_id, sum(weight_delta) as net_weight, min(exec_at) as entry_dt, liquidity_bucket "
                    "FROM portfolio_trades "
                    "WHERE portfolio_id = ? AND exec_at <= ? "
                    "GROUP BY security_id HAVING abs(sum(weight_delta)) > 1e-5",
                    (pid, f"{m_end}T23:59:59.999999Z"),
                ).fetchall()

                n_positions = len(active_trades)

                ret_gross = 0.0
                for at in active_trades:
                    sid = at["security_id"]
                    w = float(at["net_weight"])

                    p0, p1 = None, None
                    if p_conn:
                        try:
                            rows = p_conn.execute(
                                "SELECT date, close_raw, dividend_raw, split_ratio FROM prices_daily "
                                "WHERE security_id = ? AND date IN (?, ?) ORDER BY date ASC",
                                (sid, month_start, m_end),
                            ).fetchall()
                            if len(rows) == 2:
                                p0 = float(rows[0]["close_raw"])
                                p1 = float(rows[1]["close_raw"]) * float(rows[1]["split_ratio"]) + float(rows[1]["dividend_raw"])
                        except Exception:
                            pass

                    if p0 and p1 and p0 > 0:
                        stock_ret = (p1 / p0) - 1.0
                    else:
                        stock_ret = 0.0

                    ret_gross += w * stock_ret

                ret_net = ret_gross - cost
                ret_net_stress = ret_gross - cost_stress

                evidence_str = f"{pid}:{m_end}:{ret_gross:.6f}:{ret_net:.6f}:{cost:.6f}"
                ev_hash = hashlib.sha256(evidence_str.encode()).hexdigest()[:16]

                existing = conn.execute(
                    "SELECT evidence_hash FROM portfolio_returns WHERE portfolio_id = ? AND month_end = ?",
                    (pid, m_end),
                ).fetchone()

                if not existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO portfolio_returns "
                        "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, bm_ew_sector, bm_cw, bm_index, n_positions, cost_model_version) "
                        "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, '1')",
                        (
                            pid,
                            m_end,
                            ev_hash,
                            f"{m_end}T23:59:59.000000Z",
                            ret_gross,
                            turnover,
                            cost,
                            ret_net,
                            ret_net_stress,
                            n_positions,
                        ),
                    )
                    returns_updated += 1

                for at in active_trades:
                    sid = at["security_id"]
                    w = float(at["net_weight"])
                    conn.execute(
                        "INSERT OR IGNORE INTO portfolio_positions "
                        "(portfolio_id, as_of, security_id, weight, entry_as_of, rank_at_entry, liquidity_bucket) "
                        "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                        (pid, m_end, sid, w, at["entry_dt"][:10], at["liquidity_bucket"]),
                    )

        conn.commit()
    finally:
        if p_conn is not None:
            p_conn.close()

    return Result(status="ok", counts={"returns_updated": returns_updated})


def net_selection_spread(
    conn: sqlite3.Connection,
    cohort_id: str,
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    horizon_m: int = 3,
) -> dict:
    """Compute net selection spread of TOP_Q20 vs MATCHED_EW cohort books.

    Returns dict with value, n_cost_events, status, execution_start, execution_end, evidence_refs.
    If absent simulation or unsettled orders, returns status 'unavailable' or 'pending'.
    """
    empty_res = {
        "value": None,
        "n_cost_events": 0,
        "status": "unavailable",
        "execution_start": None,
        "execution_end": None,
        "evidence_refs": [],
    }

    books = conn.execute(
        "SELECT portfolio_id, rule FROM portfolios "
        "WHERE cohort_id = ? AND subject_kind = ? AND subject_id = ? AND subject_version = ? "
        "  AND rule IN ('cohort_top_quintile', 'cohort_matched_ew')",
        (cohort_id, subject_kind, subject_id, subject_version),
    ).fetchall()

    if len(books) < 2:
        return empty_res

    book_map = {b["rule"]: b["portfolio_id"] for b in books}
    p_top = book_map.get("cohort_top_quintile")
    p_ew = book_map.get("cohort_matched_ew")

    if not p_top or not p_ew:
        return empty_res

    order_statuses = conn.execute(
        "SELECT status, count(*) as cnt FROM portfolio_orders "
        "WHERE portfolio_id IN (?, ?) GROUP BY status",
        (p_top, p_ew),
    ).fetchall()

    status_counts = {r["status"]: r["cnt"] for r in order_statuses}
    if status_counts.get("pending", 0) > 0:
        return {
            "value": None,
            "n_cost_events": 0,
            "status": "pending",
            "execution_start": None,
            "execution_end": None,
            "evidence_refs": [],
        }

    if status_counts.get("cancelled", 0) > 0:
        return {
            "value": None,
            "n_cost_events": 0,
            "status": "expired",
            "execution_start": None,
            "execution_end": None,
            "evidence_refs": [],
        }

    trades = conn.execute(
        "SELECT trade_id, order_id, portfolio_id, exec_at, security_id, side, weight_delta, fill_price, cost_bps "
        "FROM portfolio_trades "
        "WHERE portfolio_id IN (?, ?) "
        "ORDER BY exec_at ASC, trade_id ASC",
        (p_top, p_ew),
    ).fetchall()

    if not trades:
        return empty_res

    p_conn = _discover_prices_conn(conn)

    try:
        net_returns = {}
        for pid in (p_top, p_ew):
            p_trades = [t for t in trades if t["portfolio_id"] == pid]
            entries = [t for t in p_trades if t["side"] in ("buy", "BUY")]
            exits = [t for t in p_trades if t["side"] in ("sell", "SELL")]

            if not entries or not exits:
                return empty_res

            entry_map = {int(t["security_id"]): t for t in entries}
            exit_map = {int(t["security_id"]): t for t in exits}

            book_ret_gross = 0.0
            total_costs = 0.0

            for sid, e_trade in entry_map.items():
                x_trade = exit_map.get(sid)
                if not x_trade:
                    continue

                w0 = abs(float(e_trade["weight_delta"]))
                p0 = float(e_trade["fill_price"])
                p1 = float(x_trade["fill_price"])

                c_entry = w0 * (float(e_trade["cost_bps"]) / 10000.0)
                c_exit = abs(float(x_trade["weight_delta"])) * (float(x_trade["cost_bps"]) / 10000.0)
                total_costs += (c_entry + c_exit)

                split_ratio = 1.0
                dividend_raw = 0.0

                if p_conn is not None:
                    try:
                        pr = p_conn.execute(
                            "SELECT dividend_raw, split_ratio FROM prices_daily "
                            "WHERE security_id = ? AND date = ? ORDER BY observed_at DESC LIMIT 1",
                            (sid, x_trade["exec_at"][:10]),
                        ).fetchone()
                        if pr:
                            split_ratio = float(pr["split_ratio"] or 1.0)
                            dividend_raw = float(pr["dividend_raw"] or 0.0)
                    except Exception:
                        pass

                if split_ratio == 1.0 and dividend_raw == 0.0:
                    try:
                        ca_row = conn.execute(
                            "SELECT dividend_raw, split_ratio FROM prices_monthly "
                            "WHERE security_id = ? AND month_end = ? LIMIT 1",
                            (sid, x_trade["exec_at"][:10]),
                        ).fetchone()
                        if ca_row:
                            split_ratio = float(ca_row["split_ratio"] or 1.0)
                            dividend_raw = float(ca_row["dividend_raw"] or 0.0)
                    except Exception:
                        pass

                economic_exit_price = (p1 + dividend_raw) * split_ratio
                stock_ret = (economic_exit_price / p0) - 1.0 if p0 > 0 else 0.0

                book_ret_gross += w0 * stock_ret

            net_returns[pid] = book_ret_gross - total_costs

        spread_val = net_returns[p_top] - net_returns[p_ew]
        exec_start = min(t["exec_at"] for t in trades)
        exec_end = max(t["exec_at"] for t in trades)
        ev_refs = sorted(list({t["order_id"] for t in trades}))

        return {
            "value": float(spread_val),
            "n_cost_events": len(trades),
            "status": "ok",
            "execution_start": exec_start,
            "execution_end": exec_end,
            "evidence_refs": ev_refs,
        }
    finally:
        if p_conn is not None:
            p_conn.close()
