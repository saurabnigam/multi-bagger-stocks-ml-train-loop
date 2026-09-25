"""Tests for work package `portfolio` (decision D8, MASTER_SPEC 8 / 5.3 / 10.5).

T4/T5: ``roll_forward`` fetches one TRI matrix per distinct (vintage_at, start,
end) instead of one ``PriceStore.tri()`` call per portfolio-period; ``plan()``
never opens TOP_Q20/MATCHED_EW attribution books for a factor whose family can
never be weighted (control/legacy, MASTER_SPEC 5.3); a monthly run over the
state-budget records a non-blocking WARN ``data_quality_events`` row.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.calendar import Calendar
from quant.db.core import apply_schema, connect
from quant.data.prices import PriceStore
from quant.portfolio import paper as paper_mod
from quant.portfolio.paper import NEVER_WEIGHTED_FAMILIES, _effective_vintage, _price_store, plan, roll_forward
from quant.run import RunContext, _commit_and_push
from quant.types import Actor, FrozenClock, Result


# =============================================================================
# T4: roll_forward TRI batching
# =============================================================================

def _make_cfg(base_dir: Path):
    """Same shape as tests/conftest.py's ``cfg`` fixture, parameterized by dir."""
    db_path = base_dir / "state.db"
    prices_db_path = base_dir / "prices.db"
    data_dir = base_dir / "data"
    archive_dir = base_dir / "archive"
    knowledge_dir = base_dir / "knowledge"
    ui_dir = base_dir / "ui"
    return load_config().with_paths(
        db=db_path,
        prices_db=prices_db_path,
        data_dir=data_dir,
        archive_dir=archive_dir,
        knowledge_dir=knowledge_dir,
        ui_dir=ui_dir,
    )


def _build_roll_forward_fixture(base_dir: Path):
    """Three portfolios with distinct entry dates, sharing a security universe.

    PA and PC enter on the same session; PB enters later. PC also gets a
    mid-history rebalance so both a pure-drift period and a traded period are
    exercised. Returns (ctx, conn, p_conn).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg(base_dir)

    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    p_conn = connect(cfg.paths.prices_db)
    apply_schema(p_conn, kind="prices")

    sessions = [
        "2026-09-08", "2026-09-30", "2026-10-30", "2026-11-30",
        "2026-12-31", "2027-01-29", "2027-02-26",
    ]
    sids = [1, 2, 3, 4, 5, 6]

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
            "config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-07', 'plan', 'live', 1, '2026-09-07T09:00:00.000000Z', 'ok', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
            "source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-07', 'live', '2026-09-07T18:29:59.999999Z', 'def', 'mem', '[]', "
            "'2026-09-07T09:00:00.000000Z', '2026-09-07T09:00:00.000000Z', 1, 1)"
        )
        for sid in sids:
            conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                f"VALUES ({sid}, 'ISIN{sid:03d}', 'Stock {sid}', '2026-01-01', '2027-02-26', 'listed')"
            )

        def _insert_portfolio(pid: str) -> None:
            conn.execute(
                "INSERT INTO portfolios (portfolio_id, model_id, subject_kind, subject_id, subject_version, "
                "cohort_id, rule, cadence, inception, rule_version) "
                f"VALUES ('{pid}', NULL, 'model', 'TEST', '1', 'live:1', 'top30_buffer', 'monthly', "
                "'2026-09-07', '1')"
            )

        def _insert_trade(pid: str, order_id: str, sid: int, exec_at: str, weight_delta: float,
                           purpose: str = "entry", cost_bps: float = 25.0, bucket: str = "B") -> None:
            conn.execute(
                "INSERT INTO portfolio_orders (order_id, portfolio_id, cohort_id, security_id, created_at, "
                "earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket, decision_id) "
                f"VALUES (?, ?, 'live:1', ?, ?, ?, ?, ?, ?, 'filled', ?, NULL)",
                (order_id, pid, sid, exec_at, exec_at,
                 purpose, "buy" if weight_delta >= 0 else "sell", abs(weight_delta), bucket),
            )
            conn.execute(
                "INSERT INTO portfolio_trades (order_id, portfolio_id, cohort_id, exec_at, security_id, side, "
                "weight_delta, fill_price, cost_bps, liquidity_bucket, price_manifest_sha) "
                "VALUES (?, ?, 'live:1', ?, ?, ?, ?, 100.0, ?, ?, 'sha')",
                (order_id, pid, exec_at, sid, "buy" if weight_delta >= 0 else "sell",
                 weight_delta, cost_bps, bucket),
            )

        _insert_portfolio("PA")
        _insert_portfolio("PB")
        _insert_portfolio("PC")

        entry_a = "2026-09-08T10:00:00.000000Z"
        for sid, w in ((1, 0.3), (2, 0.3), (3, 0.3)):
            _insert_trade("PA", f"PA:{sid}:entry", sid, entry_a, w)

        entry_b = "2026-10-30T10:00:00.000000Z"
        for sid, w in ((2, 0.3), (4, 0.3), (5, 0.3)):
            _insert_trade("PB", f"PB:{sid}:entry", sid, entry_b, w)

        entry_c = "2026-09-08T10:00:00.000000Z"
        for sid, w in ((3, 0.5), (6, 0.5)):
            _insert_trade("PC", f"PC:{sid}:entry", sid, entry_c, w)
        # Mid-history rebalance: trim security 3, add to security 6.
        rebal_c = "2026-11-30T10:00:00.000000Z"
        _insert_trade("PC", "PC:3:rebalance", 3, rebal_c, -0.2, purpose="rebalance")
        _insert_trade("PC", "PC:6:rebalance", 6, rebal_c, 0.1, purpose="rebalance")

    with p_conn:
        for i, date in enumerate(sessions):
            observed_at = f"{date}T10:00:00.000000Z"
            for sid in sids:
                close = 100.0 + sid * 3.0 + i * 2.5 + (sid * i * 0.1)
                p_conn.execute(
                    "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, "
                    "observed_at, capture_id, source_sha256) VALUES (?, ?, ?, 0.0, 1.0, ?, 'cap', 'sha')",
                    (sid, date, close, observed_at),
                )

    clock = FrozenClock("2026-01-01T00:00:00.000000Z")
    actor = Actor(kind="system", name="test")
    ctx = RunContext(as_of="2026-09-07", kind="portfolio", track="live", cfg=cfg, clock=clock, actor=actor)
    ctx.conn = conn
    ctx.store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    return ctx, conn, p_conn


def _reference_roll_forward(ctx: RunContext, through: str, portfolio_id: str | None = None) -> Result:
    """Verbatim pre-optimization algorithm: one ``store.tri()`` call per
    portfolio-period. Kept here only as the correctness oracle for the
    batched implementation in ``quant.portfolio.paper.roll_forward`` -- see
    git history (pre decision-D8 T4) for the original in-place version.
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
        return Result(status="ok", counts={"returns_updated": 0},
                       details={"message": "Price store unavailable; returns not computed"})

    through_date = through[:10]
    boundary_iso = through if "T" in through else f"{through}T23:59:59.999999Z"
    vintage_at = _effective_vintage(ctx, boundary_iso)
    history_start = "2015-01-01"
    if ctx.cfg is not None and hasattr(ctx.cfg, "yahoo") and hasattr(ctx.cfg.yahoo, "history_start"):
        history_start = str(ctx.cfg.yahoo.history_start)

    returns_updated = 0

    for port in portfolios:
        pid = port["portfolio_id"]
        inception = port["inception"]
        if not inception:
            continue

        first_trade = conn.execute(
            "SELECT min(exec_at) as min_exec FROM portfolio_trades WHERE portfolio_id = ?", (pid,),
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

        points = [inception_session] + [m for m in month_end_sessions if m > inception_session]
        if len(points) < 2:
            continue

        all_trades = conn.execute(
            "SELECT security_id, exec_at, weight_delta, cost_bps, liquidity_bucket "
            "FROM portfolio_trades WHERE portfolio_id = ? ORDER BY exec_at ASC",
            (pid,),
        ).fetchall()

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
                    t for t in all_trades if lower_bound_exclusive < t["exec_at"][:10] <= period_end
                ]
            lower_bound_exclusive = period_end

            turnover = 0.5 * sum(abs(float(t["weight_delta"])) for t in period_trades)
            cost = sum(abs(float(t["weight_delta"])) * (float(t["cost_bps"]) / 10000.0) for t in period_trades)
            cost_stress = sum(
                abs(float(t["weight_delta"])) * (float(t["cost_bps"]) * 1.5 / 10000.0) for t in period_trades
            )

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
            if sids:
                try:
                    tri_df = store.tri(sids, start=history_start, end=period_end, vintage_at=vintage_at)
                except Exception:
                    tri_df = None
                if (
                    tri_df is not None and not tri_df.empty
                    and period_start in tri_df.index and period_end in tri_df.index
                ):
                    row_s, row_e = tri_df.loc[period_start], tri_df.loc[period_end]
                    for sid in sids:
                        v0, v1 = row_s.get(sid), row_e.get(sid)
                        if pd.notna(v0) and pd.notna(v1) and float(v0) > 0:
                            r_by_sid[sid] = float(v1) / float(v0) - 1.0

            ret_gross = sum(held_weights[sid] * r_by_sid.get(sid, 0.0) for sid in sids)
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
                    "(portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, "
                    "cost, ret_net, ret_net_stress, bm_ew, bm_ew_sector, bm_cw, bm_index, n_positions, "
                    "cost_model_version) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, '1')",
                    (pid, period_end, ev_hash, f"{period_end}T23:59:59.000000Z", ret_gross, turnover, cost,
                     ret_net, ret_net_stress, n_positions),
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


def test_roll_forward_batches_tri_and_matches_reference(tmp_path):
    """New batched roll_forward matches the pre-optimization per-period algorithm
    bit-for-bit across 3 portfolios / different entry dates / two vintages, while
    calling PriceStore.tri() once per distinct (vintage_at, start, end) instead of
    once per portfolio-period.
    """
    ctx_ref, conn_ref, _ = _build_roll_forward_fixture(tmp_path / "ref")
    ctx_new, conn_new, _ = _build_roll_forward_fixture(tmp_path / "new")

    tri_calls: list[dict[str, Any]] = []
    orig_tri = ctx_new.store.tri

    def _counting_tri(security_ids, start, end, vintage_at):
        tri_calls.append({"start": start, "end": end, "vintage_at": vintage_at, "n_sids": len(security_ids)})
        return orig_tri(security_ids, start=start, end=end, vintage_at=vintage_at)

    ctx_new.store.tri = _counting_tri

    # Vintage 1
    res_new_1 = roll_forward(ctx_new, through="2026-11-30")
    res_ref_1 = _reference_roll_forward(ctx_ref, through="2026-11-30")
    assert res_new_1.counts == res_ref_1.counts
    assert res_new_1.counts["returns_updated"] > 0

    # Vintage 2 (later through -> later effective vintage_at; extends history)
    res_new_2 = roll_forward(ctx_new, through="2027-02-26")
    res_ref_2 = _reference_roll_forward(ctx_ref, through="2027-02-26")
    assert res_new_2.counts == res_ref_2.counts
    assert res_new_2.counts["returns_updated"] > 0

    # Exactly one PriceStore.tri() call per roll_forward invocation (one per
    # distinct vintage/window), never one per portfolio-period -- with 3
    # portfolios and multiple periods each this would otherwise be >= 6.
    assert len(tri_calls) == 2, f"expected 1 tri() call per roll_forward call, got {tri_calls}"
    assert tri_calls[0]["end"] == "2026-11-30"
    assert tri_calls[1]["end"] == "2027-02-26"
    assert tri_calls[0]["vintage_at"] != tri_calls[1]["vintage_at"]
    # The shared matrix covers the union of securities traded by any in-scope
    # portfolio (1..6), not just one portfolio's holdings.
    assert tri_calls[0]["n_sids"] == 6

    returns_cols = (
        "portfolio_id, month_end, evidence_hash, ret_gross, ret_net, ret_net_stress, "
        "turnover_one_way, cost, n_positions"
    )
    returns_new = [dict(r) for r in conn_new.execute(
        f"SELECT {returns_cols} FROM portfolio_returns ORDER BY portfolio_id, month_end"
    ).fetchall()]
    returns_ref = [dict(r) for r in conn_ref.execute(
        f"SELECT {returns_cols} FROM portfolio_returns ORDER BY portfolio_id, month_end"
    ).fetchall()]
    assert returns_new == returns_ref
    assert len(returns_new) > 0

    positions_cols = "portfolio_id, as_of, security_id, weight, entry_as_of, liquidity_bucket"
    positions_new = [dict(r) for r in conn_new.execute(
        f"SELECT {positions_cols} FROM portfolio_positions ORDER BY portfolio_id, as_of, security_id"
    ).fetchall()]
    positions_ref = [dict(r) for r in conn_ref.execute(
        f"SELECT {positions_cols} FROM portfolio_positions ORDER BY portfolio_id, as_of, security_id"
    ).fetchall()]
    assert positions_new == positions_ref
    assert len(positions_new) > 0


# =============================================================================
# T5: no attribution books for never-weighted (diagnostic) factor families
# =============================================================================

def _seed_plan_fixture(base_dir: Path):
    """Cohort with one champion model, one weighted (momentum) factor, one
    control-family diagnostic factor and one legacy-family diagnostic factor
    -- mirrors tests/unit/test_ws08_02.py::test_setup, extended per MASTER_SPEC
    5.3's launch set (size/liq/beta_252 = control; dc_flag = legacy).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg(base_dir)

    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    p_conn = connect(cfg.paths.prices_db)
    apply_schema(p_conn, kind="prices")

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
            "config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-07', 'plan', 'live', 1, '2026-09-07T09:00:00.000000Z', 'running', 'test', 'c', "
            "'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
            "source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-07', 'live', '2026-09-07T18:29:59.999999Z', 'def', 'mem', '[]', "
            "'2026-09-07T09:00:00.000000Z', '2026-09-07T09:00:00.000000Z', 1, 1)"
        )
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('M_CHAMPION', 'equal', 'champion', 'Champion Model', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('M_CHAMPION', 1, '[{\"factor_id\":\"mom_12_1@1\",\"family\":\"momentum\"}]', "
            "'{\"family\":{\"momentum\":1.0}}', '2026-01-01')"
        )

        factor_defs = [
            ("mom_12_1@1", "mom", "momentum", 1, "active"),
            ("size@1", "size", "control", 1, "active"),
            ("liq@1", "liq", "control", 1, "shadow"),
            ("dc_flag@1", "dc_flag", "legacy", 1, "active"),
        ]
        for factor_id, name, family, direction, status in factor_defs:
            conn.execute(
                "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
                "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, "
                "min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
                "VALUES (?, ?, 1, ?, ?, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', ?, "
                "'2026-01-01', '2026-01-01')",
                (factor_id, name, family, direction, status),
            )

        for sid in range(1, 21):
            conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                f"VALUES ({sid}, 'ISIN{sid:03d}', 'Stock {sid}', '2026-01-01', '2026-09-07', 'listed')"
            )
            bucket = "C" if sid == 1 else ("B" if sid <= 10 else "A")
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, "
                "group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, "
                "rank_all, rank, rank_group, decile, quintile, scored, eligible, liquidity_bucket, n_factors_used, "
                "dc_flag, input_hash, generated_at, track, run_id) "
                f"VALUES ('live:1', '2026-09-07', {sid}, 'M_CHAMPION', 1, 'SEC_A', 1, '{{}}', {100 - sid}, "
                f"{100 - sid}, 0, {100 - sid}, {sid}, {sid}, {sid}, {(sid - 1) // 2 + 1}, {(sid - 1) // 4 + 1}, 1, "
                f"1, '{bucket}', 5, 0, 'h', '2026-09-07T09:00:00.000000Z', 'live', 1)"
            )
            for factor_id, *_rest in factor_defs:
                conn.execute(
                    "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
                    "sector_group, flags, input_refs_json, track, run_id) "
                    f"VALUES ('live:1', '2026-09-07', {sid}, '{factor_id}', {20 - sid}, {20 - sid}, "
                    f"{(20 - sid) / 5.0}, 'SEC_A', '', '[]', 'live', 1)"
                )

    sessions_df = pd.DataFrame([
        {"date": "2026-09-07", "close_at": "2026-09-07T10:00:00.000000Z"},
        {"date": "2026-09-08", "close_at": "2026-09-08T10:00:00.000000Z"},
        {"date": "2026-12-31", "close_at": "2026-12-31T10:00:00.000000Z"},
    ])
    cal = Calendar(sessions_df)

    clock = FrozenClock("2026-09-07T09:00:00.000000Z")
    actor = Actor(kind="system", name="test")
    ctx = RunContext(as_of="2026-09-07", kind="plan", track="live", cfg=cfg, clock=clock, actor=actor)
    ctx.conn = conn
    ctx.calendar = cal
    return ctx, conn, p_conn


def test_diagnostic_factor_families_get_no_attribution_books(tmp_path):
    """control/legacy (never-weighted, MASTER_SPEC 5.3) factors get no TOP_Q20/
    MATCHED_EW books; the weighted momentum factor and the model still do, and
    the fix measurably reduces orders_planned versus the unfiltered behavior.
    """
    ctx, conn, _ = _seed_plan_fixture(tmp_path / "filtered")
    res = plan(ctx, cohort_id="live:1")
    assert res.status == "ok"

    factor_portfolio_subjects = {
        r["subject_id"] for r in conn.execute(
            "SELECT DISTINCT subject_id FROM portfolios WHERE subject_kind = 'factor'"
        ).fetchall()
    }
    assert "mom_12_1@1" in factor_portfolio_subjects
    assert "size@1" not in factor_portfolio_subjects
    assert "liq@1" not in factor_portfolio_subjects
    assert "dc_flag@1" not in factor_portfolio_subjects

    # Exactly TOP_Q20 + MATCHED_EW for the one promotable factor.
    n_mom_books = conn.execute(
        "SELECT count(*) FROM portfolios WHERE subject_kind = 'factor' AND subject_id = 'mom_12_1@1'"
    ).fetchone()[0]
    assert n_mom_books == 2

    # Model attribution books are unaffected by the factor-family filter.
    n_model_books = conn.execute(
        "SELECT count(*) FROM portfolios WHERE subject_kind = 'model'"
    ).fetchone()[0]
    assert n_model_books >= 2

    filtered_orders = conn.execute("SELECT count(*) FROM portfolio_orders").fetchone()[0]

    # Baseline: same fixture, family filter disabled (pre-fix behavior) --
    # every active/shadow/probation factor gets books, including control/legacy.
    ctx_base, conn_base, _ = _seed_plan_fixture(tmp_path / "unfiltered")
    empty_never_weighted: tuple[str, ...] = ()
    orig = paper_mod.NEVER_WEIGHTED_FAMILIES
    paper_mod.NEVER_WEIGHTED_FAMILIES = empty_never_weighted
    try:
        plan(ctx_base, cohort_id="live:1")
    finally:
        paper_mod.NEVER_WEIGHTED_FAMILIES = orig

    unfiltered_subjects = {
        r["subject_id"] for r in conn_base.execute(
            "SELECT DISTINCT subject_id FROM portfolios WHERE subject_kind = 'factor'"
        ).fetchall()
    }
    assert {"mom_12_1@1", "size@1", "liq@1", "dc_flag@1"} <= unfiltered_subjects

    unfiltered_orders = conn_base.execute("SELECT count(*) FROM portfolio_orders").fetchone()[0]

    assert unfiltered_orders > filtered_orders
    reduction = unfiltered_orders - filtered_orders
    reduction_pct = 100.0 * reduction / unfiltered_orders
    # Regression floor: three diagnostic factors' worth of TOP_Q20+MATCHED_EW
    # order pairs must actually disappear, not just round to zero.
    assert reduction_pct > 10.0, (
        f"expected a double-digit percentage reduction in orders_planned from dropping "
        f"diagnostic-family books; got {filtered_orders}/{unfiltered_orders} "
        f"({reduction_pct:.1f}% reduction)"
    )


def test_never_weighted_families_are_control_and_legacy():
    """Locks the constant to MASTER_SPEC 5.3's launch-set families so a future
    edit here is a visible spec decision, not a silent drift."""
    assert set(NEVER_WEIGHTED_FAMILIES) == {"control", "legacy"}


# =============================================================================
# T3/T5: STATE_BUDGET_EXCEEDED non-blocking WARN event
# =============================================================================

def test_state_budget_exceeded_recorded_as_warn_event(tmp_path):
    """When the state DB exceeds cfg.budgets.state_warn_bytes at the end of a
    monthly run, a non-blocking WARN data_quality_events row is recorded (the
    print is kept; no publication is blocked)."""
    base_dir = tmp_path / "budget"
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg(base_dir)

    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    conn.close()

    actual_bytes = cfg.paths.db.stat().st_size
    assert actual_bytes > 0

    raw = cfg.to_dict()
    raw["budgets"]["state_warn_bytes"] = 1  # force the oversized-state path
    from quant.config import Config
    tiny_cfg = Config(raw, cfg._root, policy_hash=cfg.policy_sha256)

    clock = FrozenClock("2026-09-07T09:00:00.000000Z")
    actor = Actor(kind="system", name="test")

    _commit_and_push(tiny_cfg, as_of="2026-09-07", commit=True, push=False, clock=clock, actor=actor)

    check_conn = connect(tiny_cfg.paths.db)
    try:
        rows = check_conn.execute(
            "SELECT severity, code, detail_json FROM data_quality_events WHERE code = 'STATE_BUDGET_EXCEEDED'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["severity"] == "WARN"
        import json
        detail = json.loads(row["detail_json"])
        assert detail["budget"] == 1
        assert detail["bytes"] == actual_bytes

        # Non-blocking: the run row for this maintenance write is 'ok', not blocked/failed.
        run_row = check_conn.execute(
            "SELECT status FROM runs WHERE kind = 'maintenance' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert run_row is not None
        assert run_row["status"] == "ok"
    finally:
        check_conn.close()


def test_state_budget_not_exceeded_records_no_event(tmp_path):
    """Below budget: no STATE_BUDGET_EXCEEDED row (the check is a threshold,
    not an unconditional log on every commit)."""
    base_dir = tmp_path / "budget_ok"
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg = _make_cfg(base_dir)

    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    conn.close()

    clock = FrozenClock("2026-09-07T09:00:00.000000Z")
    actor = Actor(kind="system", name="test")

    # Default budget (50 MB) comfortably exceeds a freshly-created empty schema.
    _commit_and_push(cfg, as_of="2026-09-07", commit=True, push=False, clock=clock, actor=actor)

    check_conn = connect(cfg.paths.db)
    try:
        n = check_conn.execute(
            "SELECT count(*) FROM data_quality_events WHERE code = 'STATE_BUDGET_EXCEEDED'"
        ).fetchone()[0]
        assert n == 0
    finally:
        check_conn.close()
