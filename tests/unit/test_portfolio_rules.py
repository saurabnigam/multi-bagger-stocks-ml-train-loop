"""Tests for portfolio construction rules (C08): sector caps on rebalance,
and the settle() guard that stops an exit from filling ahead of its entry.

Real-world motivation: the sector cap exists so a scoring model that happens
to love one sector this month (e.g. every IT compounder screens well at once)
cannot turn the paper portfolio into a sector bet -- MASTER_SPEC section 8
caps any single sector at 6 of the 30 names. Separately, every attribution
book trades in strict entry-then-exit pairs; if settlement ever let the exit
leg fill while the entry leg was still pending (a price gap, a cancelled
name, a delayed capture), the book would record a sale of stock it never
actually bought, corrupting NAV and turnover for that book from that point
on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from quant.data.calendar import Calendar
from quant.db.core import apply_schema, connect
from quant.portfolio.construct import rebalance
from quant.portfolio.paper import plan, settle
from quant.run import RunContext
from quant.types import Actor, Clock


# ---------------------------------------------------------------------------
# (a) quant.portfolio.construct.rebalance -- sector cap enforcement (M36)
# ---------------------------------------------------------------------------


def test_rebalance_enforces_sector_cap_against_concentrated_retained_holdings(cfg):
    """Twelve of the top-ranked names already held are all in one sector (IT).

    MASTER_SPEC section 8 caps any single sector at 6 names out of a 30-name
    book. If the retention step only checked "is there still room in the
    book" (dropping the sector-cap side of the AND), all 12 IT names would
    be retained just because the book has not yet reached 30 -- disabling
    the cap exactly when it matters, since a concentrated top of the
    ranking is the case the cap is meant to catch. The other 18 slots must
    still be filled from the rest of the ranked universe (other sectors),
    not left empty.
    """
    sector_cap = 6
    target_n = 30

    # Previously held: 12 names, all sector IT, ranked 1..12 (well inside the
    # buffer_rank=60 retention window), bucket A so nothing else disqualifies
    # them.
    previous = pd.DataFrame([
        {"security_id": sid, "weight": 1.0 / 30, "entry_as_of": "2026-06-30", "group": "IT"}
        for sid in range(1, 13)
    ])

    ranks = {sid: sid for sid in range(1, 13)}  # IT names occupy ranks 1..12
    groups = {sid: "IT" for sid in range(1, 13)}
    buckets = {sid: "A" for sid in range(1, 13)}

    # Fill the rest of the ranked universe (ranks 13..70) across five other
    # sectors, six names per sector -- enough breadth that a correctly
    # capped book can still reach target_n=30 from names outside IT.
    other_sectors = ["PHARMA", "AUTO", "FMCG", "BANK", "ENERGY", "METALS"]
    rank_cursor = 13
    for sector in other_sectors:
        for _ in range(10):  # 6 sectors * 10 = 60 candidates, ranks 13..72
            sid = 1000 + rank_cursor
            ranks[sid] = rank_cursor
            groups[sid] = sector
            buckets[sid] = "A"
            rank_cursor += 1

    ranks = pd.Series(ranks)
    groups = pd.Series(groups)
    buckets = pd.Series(buckets)
    eligible = pd.Series({sid: True for sid in ranks.index})

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    selected_sids = positions["security_id"].tolist()
    sector_of = {**groups.to_dict()}
    counts: dict[str, int] = {}
    for sid in selected_sids:
        counts[sector_of[sid]] = counts.get(sector_of[sid], 0) + 1

    # No sector may exceed the cap -- this is what M36 (AND -> OR) breaks:
    # under the bug all 12 IT names pass retention and IT ends up with 12.
    assert counts.get("IT", 0) <= sector_cap, (
        f"IT sector holds {counts.get('IT', 0)} names, exceeding the "
        f"sector cap of {sector_cap}: sector cap enforcement is broken"
    )
    for sector, n in counts.items():
        assert n <= sector_cap, f"sector {sector} holds {n} names, exceeding cap {sector_cap}"

    # The book must still be filled from other sectors, not left short
    # because IT was (correctly) throttled.
    assert len(selected_sids) == target_n

    # And the buffer/retention rule did keep *some* IT names (rank 1..6, the
    # best-ranked IT names) rather than dropping the sector outright.
    retained_it = [sid for sid in selected_sids if sector_of[sid] == "IT"]
    assert sorted(retained_it) == list(range(1, 7))


def test_rebalance_infeasible_book_holds_cash_not_scaled_weights(cfg):
    """Sanity check on the same construct.rebalance path used above: when
    fewer than target_n feasible names exist, the shortfall is reported as
    cash rather than scaling up the names that did make it in (MASTER_SPEC
    section 8). This pins the nominal-weight behaviour the sector-cap test
    above relies on, using independently derived numbers (1/30 per name).
    """
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])
    ranks = pd.Series({1: 1, 2: 2, 3: 3})
    groups = pd.Series({1: "IT", 2: "PHARMA", 3: "AUTO"})
    buckets = pd.Series({1: "A", 2: "A", 3: "A"})
    eligible = pd.Series({1: True, 2: True, 3: True})

    positions, _ = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg,
        rule="top30_buffer",
    )

    assert len(positions) == 3
    for w in positions["target_weight"]:
        assert w == pytest.approx(1.0 / 30)
    assert positions["cash_weight"].iloc[0] == pytest.approx(1.0 - 3.0 / 30)


# ---------------------------------------------------------------------------
# (b) quant.portfolio.paper.settle -- exit cannot fill ahead of its entry (M37)
# ---------------------------------------------------------------------------


class FrozenTestClock(Clock):
    def __init__(self, now_str: str):
        self._now_str = now_str

    def now(self) -> datetime:
        return datetime.fromisoformat(self._now_str.replace("Z", "+00:00"))

    def now_iso(self) -> str:
        return self._now_str

    def advance(self, dt: str) -> None:
        self._now_str = dt


@pytest.fixture
def settle_setup(tmp_path, cfg):
    """Minimal state+prices DB with one factor whose attribution book plan()
    will turn into entry/exit order pairs, following the fixture pattern in
    tests/unit/test_ws08_02.py.
    """
    db_path = tmp_path / "state.db"
    prices_db_path = tmp_path / "prices.db"

    conn = connect(db_path)
    apply_schema(conn, kind="state")

    p_conn = connect(prices_db_path)
    apply_schema(p_conn, kind="prices")

    test_cfg = cfg.with_paths(db=db_path, prices_db=prices_db_path)

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-07', 'plan', 'live', 1, '2026-09-07T09:00:00.000000Z', 'running', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-07', 'live', '2026-09-07T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-07T09:00:00.000000Z', '2026-09-07T09:00:00.000000Z', 1, 1)"
        )
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('M_CHAMPION', 'equal', 'champion', 'Champion Model', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('M_CHAMPION', 1, '[{\"factor_id\":\"mom_12_1@1\",\"family\":\"momentum\"}]', '{\"family\":{\"momentum\":1.0}}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES ('mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, 0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01')"
        )

        for sid in range(1, 6):
            conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                f"VALUES ({sid}, 'ISIN{sid:03d}', 'Stock {sid}', '2026-01-01', '2026-09-07', 'listed')"
            )
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, quintile, scored, eligible, liquidity_bucket, n_factors_used, dc_flag, input_hash, generated_at, track, run_id) "
                f"VALUES ('live:1', '2026-09-07', {sid}, 'M_CHAMPION', 1, 'SEC_A', 1, '{{}}', {100 - sid}, {100 - sid}, 0, {100 - sid}, {sid}, {sid}, {sid}, 1, 1, 1, 1, 'A', 5, 0, 'h', '2026-09-07T09:00:00.000000Z', 'live', 1)"
            )
            conn.execute(
                "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group, flags, input_refs_json, track, run_id) "
                f"VALUES ('live:1', '2026-09-07', {sid}, 'mom_12_1@1', {20 - sid}, {20 - sid}, {(20 - sid) / 5.0}, 'SEC_A', '', '[]', 'live', 1)"
            )

    sessions_df = pd.DataFrame([
        {"date": "2026-09-07", "close_at": "2026-09-07T10:00:00.000000Z"},
        {"date": "2026-09-08", "close_at": "2026-09-08T10:00:00.000000Z"},
        {"date": "2026-09-09", "close_at": "2026-09-09T10:00:00.000000Z"},
        {"date": "2026-09-30", "close_at": "2026-09-30T10:00:00.000000Z"},
        {"date": "2026-12-31", "close_at": "2026-12-31T10:00:00.000000Z"},
    ])
    cal = Calendar(sessions_df)

    clock = FrozenTestClock("2026-09-07T09:00:00.000000Z")
    actor = Actor(kind="system", name="test")

    ctx = RunContext(
        as_of="2026-09-07",
        kind="plan",
        track="live",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn
    ctx.calendar = cal

    return ctx, conn, p_conn, cal, test_cfg


def _insert_price(p_conn, sid: int, date: str, close: float):
    p_conn.execute(
        "INSERT INTO prices_daily (security_id, date, close_raw, dividend_raw, split_ratio, observed_at, capture_id, source_sha256) "
        f"VALUES ({sid}, '{date}', {close}, 0.0, 1.0, '{date}T10:00:00.000000Z', 'cap', 'sha')"
    )


def test_settle_never_fills_exit_whose_entry_never_filled(settle_setup):
    """An exit order must not fill, and must not create a trade, while its
    paired entry order is still pending (never filled, never cancelled).

    plan() creates entry orders at 2026-09-08 and exit orders (3M later, on
    the golden calendar's next available session) at 2026-12-31. We give
    the price store a close on 2026-12-31 (the exit date) but withhold the
    close on 2026-09-08 (the entry date), so the entry can never fill. A
    single settle() call covering both dates must still leave every exit
    order pending and must record zero trades for them -- M37 removes the
    guard's `continue`, which instead lets the exit fill against the
    2026-12-31 close it does have, producing a naked SELL trade for stock
    the book never bought.
    """
    ctx, conn, p_conn, cal, cfg = settle_setup

    res = plan(ctx, cohort_id="live:1")
    assert res.status == "ok"

    exit_orders_before = conn.execute(
        "SELECT order_id, security_id FROM portfolio_orders WHERE purpose = 'exit'"
    ).fetchall()
    assert len(exit_orders_before) > 0, "fixture must produce at least one entry/exit pair"

    # Withhold the entry-date close entirely; only the exit-date close exists.
    with p_conn:
        for sid in range(1, 6):
            _insert_price(p_conn, sid, "2026-12-31", 105.0)

    # One settle call spans both the entry's and the exit's earliest_exec_at,
    # so both are attempted in the same pass (entry first, by execution time).
    res = settle(ctx, through="2026-12-31T10:00:00.000000Z")

    assert res.counts["fills"] == 0, (
        "settle() reported fills, but the paired entry order never filled -- "
        "an exit filled ahead of (or without) its entry"
    )

    entry_statuses = {
        r["security_id"]: r["status"]
        for r in conn.execute(
            "SELECT security_id, status FROM portfolio_orders WHERE purpose = 'entry'"
        ).fetchall()
    }
    exit_rows = conn.execute(
        "SELECT order_id, security_id, status FROM portfolio_orders WHERE purpose = 'exit'"
    ).fetchall()

    for row in exit_rows:
        assert entry_statuses.get(row["security_id"]) == "pending", (
            "entry should still be pending: no close was ever provided for it"
        )
        assert row["status"] == "pending", (
            f"exit order {row['order_id']} filled while its entry never filled"
        )
        trade = conn.execute(
            "SELECT * FROM portfolio_trades WHERE order_id = ?", (row["order_id"],)
        ).fetchone()
        assert trade is None, (
            f"a portfolio_trades row was created for exit order {row['order_id']} "
            "whose entry never filled"
        )

    # No trade of any kind (entry or exit) should exist: the entry itself
    # never got a price either.
    total_trades = conn.execute("SELECT count(*) FROM portfolio_trades").fetchone()[0]
    assert total_trades == 0


def test_settle_fills_both_legs_of_a_normal_entry_then_exit(settle_setup):
    """Control case for the guard above: when the entry does fill first,
    the exit must fill too, three months later, as a real SELL trade.
    """
    ctx, conn, p_conn, cal, cfg = settle_setup

    plan(ctx, cohort_id="live:1")

    with p_conn:
        for sid in range(1, 6):
            _insert_price(p_conn, sid, "2026-09-08", 100.0)
            _insert_price(p_conn, sid, "2026-12-31", 110.0)

    res_entry = settle(ctx, through="2026-09-08T10:00:00.000000Z")
    assert res_entry.counts["fills"] > 0

    entry_rows = conn.execute(
        "SELECT order_id, security_id, status FROM portfolio_orders WHERE purpose = 'entry'"
    ).fetchall()
    assert all(r["status"] == "filled" for r in entry_rows)

    res_exit = settle(ctx, through="2026-12-31T10:00:00.000000Z")
    assert res_exit.counts["fills"] > 0

    exit_rows = conn.execute(
        "SELECT order_id, security_id, status FROM portfolio_orders WHERE purpose = 'exit'"
    ).fetchall()
    assert all(r["status"] == "filled" for r in exit_rows)

    for row in exit_rows:
        trade = conn.execute(
            "SELECT side, weight_delta FROM portfolio_trades WHERE order_id = ?",
            (row["order_id"],),
        ).fetchone()
        assert trade is not None
        assert trade["side"] == "sell"
        assert trade["weight_delta"] < 0
