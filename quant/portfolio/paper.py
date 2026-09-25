"""Paper portfolio planning, execution settlement, NAV roll-forward and attribution (C08)."""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.data.calendar import Calendar
from quant.data.prices import PriceStore
from quant.portfolio.construct import rebalance
from quant.portfolio.costs import bucket, cost_bps_one_way
from quant.types import Result

if TYPE_CHECKING:
    from quant.run import RunContext

# MASTER_SPEC 5.3: families whose members are "diagnostic, never weighted" --
# they cannot be promoted into the composite regardless of lifecycle status,
# so an attribution book (TOP_Q20/MATCHED_EW) for one of them can never
# supply promotion evidence (MASTER_SPEC 8) and is pure storage overhead
# (MASTER_SPEC 10.5, decision D8). This is distinct from a factor whose
# *launch role* happens to say "diagnostic" (e.g. rev_1m, max_ret_21): those
# sit in a weighted family (momentum, low_risk) and can still be promoted, so
# they keep their books. Read from the registry's `family` column, not a
# hard-coded factor_id list, so a future family added under either name is
# excluded automatically.
from quant.factors.registry import NEVER_WEIGHTED_FAMILIES  # noqa: E402  (single source, MASTER_SPEC 5.3)


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


def _price_store(ctx: RunContext | None = None, *, conn: sqlite3.Connection | None = None, cfg: Any = None) -> PriceStore | None:
    """The PriceStore for reads: ``ctx.store`` when present, else one opened at
    ``cfg.paths.prices_db``.

    Never guesses at a sibling database filename. When neither a live store
    nor a usable config path exists, callers must treat prices as unavailable
    (orders stay pending, returns are not computed) instead of fabricating data.
    """
    store = getattr(ctx, "store", None) if ctx is not None else None
    if store is not None:
        return store
    if cfg is None and ctx is not None:
        cfg = getattr(ctx, "cfg", None)
    if cfg is None or not hasattr(cfg, "paths") or not hasattr(cfg.paths, "prices_db"):
        return None
    state_conn = conn if conn is not None else (getattr(ctx, "conn", None) if ctx is not None else None)
    try:
        return PriceStore(cfg.paths.prices_db, state_conn=state_conn)
    except Exception:
        return None


def _effective_vintage(ctx: RunContext | None, boundary_iso: str) -> str:
    """Vintage for a price read: the later of the clock's current time and
    ``boundary_iso``.

    In production the clock is always at or after any ``through``/month-end it
    is asked to process, so this is simply ``ctx.clock.iso()``. The max() only
    guards a frozen/absent clock (tests; CLI handlers, which pass ``clock=None``)
    from excluding prices that are already known as of the requested boundary.
    """
    clock = getattr(ctx, "clock", None) if ctx is not None else None
    clock_iso = None
    if clock is not None:
        try:
            clock_iso = clock.iso()
        except Exception:
            clock_iso = None
    if not clock_iso:
        return boundary_iso
    return max(clock_iso, boundary_iso)


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
    cash_by_portfolio: dict[str, float] = {}

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
        cash_w = float(positions["cash_weight"].iloc[0]) if ("cash_weight" in positions.columns and len(positions)) else 0.0
        if cash_w > 1e-9 and getattr(ctx, "clock", None) is not None and getattr(ctx, "run_id", None) is not None:
            # MASTER_SPEC 8: hold residual cash and report capacity; never scale weights up.
            from quant.data.gates import record_event
            record_event(ctx, code="CAPACITY_CASH", severity="INFO",
                         detail={"portfolio_id": portfolio_id, "cohort_id": cohort_id, "cash_weight": cash_w,
                                 "n_positions": int(len(positions))})
        cash_by_portfolio[portfolio_id] = cash_w
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
            cfg=ctx.cfg,
        )

    # 2b. Factor attribution books -- never for a factor whose family can
    # never be weighted (MASTER_SPEC 5.3); active/shadow/probation members of
    # a real weighted family still get books.
    never_weighted_placeholders = ",".join("?" for _ in NEVER_WEIGHTED_FAMILIES)
    factor_rows = conn.execute(
        "SELECT DISTINCT fv.factor_id, fr.version "
        "FROM factor_values fv "
        "JOIN factor_registry fr ON fv.factor_id = fr.factor_id "
        "WHERE fv.cohort_id = ? AND fr.status IN ('active', 'shadow', 'probation') "
        f"  AND fr.family NOT IN ({never_weighted_placeholders})",
        (cohort_id, *NEVER_WEIGHTED_FAMILIES),
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
            cfg=ctx.cfg,
        )

    return Result(status="ok", counts={"orders_planned": orders_planned}, details={"cash_weight": cash_by_portfolio})


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
    cfg: Any = None,
) -> int:
    """Helper to plan TOP_Q20 and MATCHED_EW attribution books."""
    orders_planned = 0
    k = len(sorted_candidates)
    if k == 0:
        return 0

    bucket_c_max_weight = 0.02
    if cfg is not None and hasattr(cfg, "portfolio") and hasattr(cfg.portfolio, "bucket_c_max_weight"):
        bucket_c_max_weight = float(cfg.portfolio.bucket_c_max_weight)

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
        c_weight = min(nom_w, bucket_c_max_weight)
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

    store = _price_store(ctx, conn=conn, cfg=getattr(ctx, "cfg", None))
    if store is None:
        return Result(
            status="ok",
            counts={"fills": 0},
            details={"message": "Price store unavailable; orders stay pending, returns not computed"},
        )

    vintage_at = _effective_vintage(ctx, through_iso)
    try:
        manifest_sha = store.manifest_hash(vintage_at)[0]
    except Exception:
        manifest_sha = ""

    # Batch the exact-session close lookup: one store call per distinct
    # execution date across all pending orders, never per order.
    sids_by_date: dict[str, set[int]] = {}
    for o in pending_orders:
        sids_by_date.setdefault(o["earliest_exec_at"][:10], set()).add(int(o["security_id"]))
    price_cache: dict[str, pd.DataFrame] = {}
    for exec_date, sids in sids_by_date.items():
        try:
            price_cache[exec_date] = store.close_raw(sorted(sids), start=exec_date, end=exec_date, vintage_at=vintage_at)
        except Exception:
            price_cache[exec_date] = pd.DataFrame()

    def _close_on_exact_session(exec_date: str, security_id: int) -> float | None:
        """A close for the exact session only -- never falls back to a nearby
        date, so an order whose execution close is unavailable stays pending
        rather than filling against a moved date."""
        df = price_cache.get(exec_date)
        if df is None or df.empty or security_id not in df.columns:
            return None
        col = df[security_id].dropna()
        return float(col.iloc[-1]) if not col.empty else None

    fills = 0
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

        # 2. Fills only when a close exists for the exact earliest_exec_at
        # session; an unavailable close leaves the order pending (never move
        # the date backward, never synthesize a fill).
        fill_price = _close_on_exact_session(exec_date, security_id)
        if fill_price is None or fill_price <= 0:
            continue

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

    return Result(status="ok", counts={"fills": fills})


def roll_forward(ctx: RunContext, through: str, portfolio_id: str | None = None) -> Result:
    """Derive NAV and monthly returns across actual sessions and dated fills.

    Month boundaries are sessions (the last price-store session on or before
    each calendar month end), never bare calendar dates -- a calendar
    month-start/end is usually not a session, which silently zeroed every
    return under the old implementation. Each period's per-position return is
    the TRI ratio between its start and end session; positions drift with
    those returns between fills ((1+r_i)/(1+r_portfolio)), and a rebalance's
    recorded ``weight_delta`` is applied on top of the drifted weight.
    Produces no pre-inception returns.

    TRI reads are batched: every portfolio/period in one call shares the same
    ``vintage_at`` and ``start`` (the configured history start), so the only
    axis that varies is the end date, and a wider window is a superset of a
    narrower one for the same start/vintage (TRI is 100-based at the first
    bar on/after ``start``, so widening ``end`` never changes values on
    shared dates -- see ``PriceStore.tri``). This call therefore fetches one
    TRI matrix per distinct ``(vintage_at, start, end)`` actually needed --
    the union of every security touched by any in-scope portfolio, over the
    widest end date any of them needs -- and every portfolio/period slices
    its own return out of that shared matrix, instead of issuing its own
    ``tri()`` call per portfolio-period. A different ``roll_forward`` call
    (a different ``through``/vintage) never reuses another call's matrix.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"returns_updated": 0})

    p_query = "SELECT portfolio_id, inception, rule FROM portfolios"
    params: list[Any] = []
    if portfolio_id:
        p_query += " WHERE portfolio_id = ?"
        params.append(portfolio_id)
    portfolios = conn.execute(p_query, params).fetchall()

    store = _price_store(ctx, conn=conn, cfg=getattr(ctx, "cfg", None))
    if store is None:
        return Result(
            status="ok",
            counts={"returns_updated": 0},
            details={"message": "Price store unavailable; returns not computed"},
        )

    through_date = through[:10]
    boundary_iso = through if "T" in through else f"{through}T23:59:59.999999Z"
    vintage_at = _effective_vintage(ctx, boundary_iso)
    history_start = "2015-01-01"
    if ctx.cfg is not None and hasattr(ctx.cfg, "yahoo") and hasattr(ctx.cfg.yahoo, "history_start"):
        history_start = str(ctx.cfg.yahoo.history_start)

    returns_updated = 0

    # Pass 1: work out, per in-scope portfolio, the periods it needs and the
    # trades that drive them -- purely from portfolio_trades, no pricing.
    # Which securities are held in a period is determined by trades alone
    # (drift only rescales an existing holding; a holding leaves the book
    # only via a trade), so this never needs a TRI read.
    plans: list[dict[str, Any]] = []
    all_sids: set[int] = set()
    widest_end: str | None = None

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
        inception_session = first_trade["min_exec"][:10]
        if inception_session > through_date:
            continue

        try:
            sessions_in_range = store.session_dates(inception_session, through_date, min_securities=1)
        except Exception:
            sessions_in_range = []
        month_end_sessions: list[str] = []
        if sessions_in_range:
            s_df = pd.DataFrame({"date": sessions_in_range})
            s_df["month"] = s_df["date"].str[:7]
            month_end_sessions = sorted(s_df.groupby("month")["date"].max().tolist())

        # points[0] is the inception session itself; every later point is a
        # month-end session strictly after it. Fewer than 2 points means the
        # first period's endpoint session has not completed yet.
        points = [inception_session] + [m for m in month_end_sessions if m > inception_session]
        if len(points) < 2:
            continue

        all_trades = conn.execute(
            "SELECT security_id, exec_at, weight_delta, cost_bps, liquidity_bucket "
            "FROM portfolio_trades WHERE portfolio_id = ? ORDER BY exec_at ASC",
            (pid,),
        ).fetchall()

        all_sids.update(int(t["security_id"]) for t in all_trades)
        widest_end = points[-1] if widest_end is None else max(widest_end, points[-1])
        plans.append({"pid": pid, "points": points, "all_trades": all_trades})

    # One TRI fetch for the whole call: same vintage_at/start throughout, so
    # the widest end plus the union of securities covers every period any
    # in-scope portfolio needs to slice below. Exception: PriceStore.tri
    # truncates a security's history before an unresolved suspected corporate
    # action inside the fetched window, so a suspect dated after a period's end
    # would wrongly truncate that earlier period in the widest window. Those
    # few securities keep the per-period fetch (end = that period's end).
    def _fetch(sids: list[int], end: str) -> pd.DataFrame | None:
        try:
            return store.tri(sids, start=history_start, end=end, vintage_at=vintage_at)
        except Exception:
            return None

    suspect_sids: set[int] = set()
    master_tri_df = None
    if all_sids and widest_end:
        try:
            suspect_sids = set(store.unresolved_actions(sorted(all_sids), history_start, widest_end, vintage_at))
        except Exception:
            suspect_sids = set()
        master_tri_df = _fetch(sorted(all_sids - suspect_sids), widest_end)
    suspect_tri: dict[str, pd.DataFrame | None] = {}

    def _tri_for(sid: int, end: str) -> pd.DataFrame | None:
        if sid not in suspect_sids:
            return master_tri_df
        if end not in suspect_tri:
            suspect_tri[end] = _fetch(sorted(suspect_sids), end)
        return suspect_tri[end]

    # Pass 2: same per-period computation as before, sliced from the shared matrix.
    for plan in plans:
        pid = plan["pid"]
        points = plan["points"]
        all_trades = plan["all_trades"]

        current_weights: dict[int, float] = {}
        entry_dates: dict[int, str] = {}
        bucket_by_sid: dict[int, str] = {}
        lower_bound_exclusive: str | None = None

        for idx in range(1, len(points)):
            period_start, period_end = points[idx - 1], points[idx]

            if idx == 1:
                period_trades = [t for t in all_trades if t["exec_at"][:10] <= period_end]
            else:
                period_trades = [
                    t for t in all_trades
                    if lower_bound_exclusive < t["exec_at"][:10] <= period_end
                ]
            lower_bound_exclusive = period_end

            turnover = 0.5 * sum(abs(float(t["weight_delta"])) for t in period_trades)
            cost = sum(abs(float(t["weight_delta"])) * (float(t["cost_bps"]) / 10000.0) for t in period_trades)
            cost_stress = sum(
                abs(float(t["weight_delta"])) * (float(t["cost_bps"]) * 1.5 / 10000.0) for t in period_trades
            )

            # This period's trades apply on top of the weights carried from
            # the previous period's drifted end-state -- the result is what
            # was actually held while this period's return accrued.
            held_weights = dict(current_weights)
            for t in period_trades:
                sid = int(t["security_id"])
                held_weights[sid] = held_weights.get(sid, 0.0) + float(t["weight_delta"])
                bucket_by_sid[sid] = t["liquidity_bucket"]
                if held_weights[sid] > 1e-9:
                    entry_dates.setdefault(sid, t["exec_at"][:10])
            held_weights = {sid: w for sid, w in held_weights.items() if abs(w) > 1e-9}

            sids = sorted(held_weights.keys())
            r_by_sid: dict[int, float] = {}
            for sid in sids:
                tri_df = _tri_for(sid, period_end)
                if (
                    tri_df is None
                    or tri_df.empty
                    or sid not in tri_df.columns
                    or period_start not in tri_df.index
                    or period_end not in tri_df.index
                ):
                    continue
                v0, v1 = tri_df.at[period_start, sid], tri_df.at[period_end, sid]
                if pd.notna(v0) and pd.notna(v1) and float(v0) > 0:
                    r_by_sid[sid] = float(v1) / float(v0) - 1.0

            ret_gross = sum(held_weights[sid] * r_by_sid.get(sid, 0.0) for sid in sids)

            # Positions drift with returns between fills.
            denom = 1.0 + ret_gross
            current_weights = {
                sid: (held_weights[sid] * (1.0 + r_by_sid.get(sid, 0.0)) / denom) if denom != 0 else held_weights[sid]
                for sid in sids
            }

            ret_net = ret_gross - cost
            ret_net_stress = ret_gross - cost_stress
            n_positions = sum(1 for w in current_weights.values() if abs(w) > 1e-5)

            evidence_str = f"{pid}:{period_end}:{ret_gross:.10f}:{ret_net:.10f}:{cost:.10f}"
            ev_hash = hashlib.sha256(evidence_str.encode()).hexdigest()[:16]

            existing = conn.execute(
                "SELECT evidence_hash FROM portfolio_returns WHERE portfolio_id = ? AND month_end = ?",
                (pid, period_end),
            ).fetchone()

            if not existing:
                conn.execute(
                    "INSERT OR IGNORE INTO portfolio_returns "
                    "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, bm_ew, bm_ew_sector, bm_cw, bm_index, n_positions, cost_model_version) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, '1')",
                    (
                        pid,
                        period_end,
                        ev_hash,
                        f"{period_end}T23:59:59.000000Z",
                        ret_gross,
                        turnover,
                        cost,
                        ret_net,
                        ret_net_stress,
                        n_positions,
                    ),
                )
                returns_updated += 1

            for sid, w in current_weights.items():
                conn.execute(
                    "INSERT OR IGNORE INTO portfolio_positions "
                    "(portfolio_id, as_of, security_id, weight, entry_as_of, rank_at_entry, liquidity_bucket) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (pid, period_end, sid, w, entry_dates.get(sid, period_start), bucket_by_sid.get(sid)),
                )

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

    Signature matches INTERFACES.md C08 exactly (six positional parameters,
    ``conn`` only -- no ``ctx``/``cfg``). The per-stock return is the raw
    entry/exit fill-price ratio: this function has no PriceStore access path
    under the contracted signature, so it cannot reflect an interim
    corporate action between entry and exit via a TRI ratio. Threading a
    PriceStore through here would need either a new parameter (an
    INTERFACES.md amendment via governance) or reusing conn/some other
    already-contracted value to reach one, neither of which exists today.
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

    exec_start = min(t["exec_at"] for t in trades)
    exec_end = max(t["exec_at"] for t in trades)

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

            c_entry = w0 * (float(e_trade["cost_bps"]) / 10000.0)
            c_exit = abs(float(x_trade["weight_delta"])) * (float(x_trade["cost_bps"]) / 10000.0)
            total_costs += (c_entry + c_exit)

            # Raw entry/exit fill-price ratio (no PriceStore access path
            # under the contracted six-argument signature -- see docstring).
            p0, p1 = float(e_trade["fill_price"]), float(x_trade["fill_price"])
            stock_ret = (p1 / p0) - 1.0 if p0 > 0 else 0.0

            book_ret_gross += w0 * stock_ret

        net_returns[pid] = book_ret_gross - total_costs

    spread_val = net_returns[p_top] - net_returns[p_ew]
    ev_refs = sorted(list({t["order_id"] for t in trades}))

    return {
        "value": float(spread_val),
        "n_cost_events": len(trades),
        "status": "ok",
        "execution_start": exec_start,
        "execution_end": exec_end,
        "evidence_refs": ev_refs,
    }
