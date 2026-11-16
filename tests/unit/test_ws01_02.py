import json
from pathlib import Path
import pytest

from quant.data.identity import (
    upsert_security,
    resolve_security_id,
    yahoo_ticker,
    tracked_securities,
)


def test_same_isin_rename_and_changed_isin(ctx):
    # Initial insert: Symbol FOO with ISIN1
    sec_id1 = upsert_security(ctx, isin="INE001A01010", name="Foo Ltd", symbol="FOO", observed_at="2026-01-01T10:00:00.000000Z")
    assert isinstance(sec_id1, int)

    # Same ISIN, renamed to FOONEW
    sec_id2 = upsert_security(ctx, isin="INE001A01010", name="Foo New Ltd", symbol="FOONEW", observed_at="2026-06-01T10:00:00.000000Z")
    assert sec_id2 == sec_id1  # Retains security_id

    # Changed ISIN -> must create a separate security_id
    sec_id3 = upsert_security(ctx, isin="INE999B01019", name="Bar Ltd", symbol="BAR", observed_at="2026-06-01T10:00:00.000000Z")
    assert sec_id3 != sec_id1


def test_resolve_security_id_and_yahoo_ticker(ctx):
    sec_id = upsert_security(ctx, isin="INE002A01010", name="Test Co", symbol="TESTCO", observed_at="2026-01-01T10:00:00.000000Z")
    
    # Resolve by ISIN
    assert resolve_security_id(ctx.conn, isin="INE002A01010", cutoff="2026-09-30") == sec_id

    # Resolve by Symbol
    assert resolve_security_id(ctx.conn, symbol="TESTCO", cutoff="2026-09-30") == sec_id
    assert resolve_security_id(ctx.conn, symbol="NONEXISTENT", cutoff="2026-09-30") is None

    # Yahoo ticker
    assert yahoo_ticker(ctx.conn, sec_id, cutoff="2026-09-30") == "TESTCO.NS"


def test_tracked_securities_retention(ctx):
    # Seed securities 1 and 2
    sec1 = upsert_security(ctx, isin="INE001A01001", name="Sec 1", symbol="SEC1", observed_at="2026-01-01T10:00:00.000000Z")
    sec2 = upsert_security(ctx, isin="INE002A01002", name="Sec 2", symbol="SEC2", observed_at="2026-01-01T10:00:00.000000Z")
    sec_dropped = upsert_security(ctx, isin="INE003A01003", name="Dropped", symbol="DROPPED", observed_at="2026-01-01T10:00:00.000000Z")
    sec_order = upsert_security(ctx, isin="INE004A01004", name="Order", symbol="ORDERED", observed_at="2026-01-01T10:00:00.000000Z")

    # Current membership at 2026-09-30 includes only sec1 and sec2
    for s in [sec1, sec2]:
        ctx.conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
            "VALUES ('2026-09-30', '2026-09-30T12:00:00.000000Z', ?, 'NIFTY500', 'S', 'nse_csv')",
            (s,)
        )

    # sec_dropped was in a published cohort from 2026-06-30
    ctx.conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T18:29:59.999999Z', 'd', 'm', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, ?)",
        (ctx.run_id,)
    )
    ctx.conn.execute(
        "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
        "VALUES ('2026-06-30', '2026-06-30T12:00:00.000000Z', ?, 'NIFTY500', 'DROPPED', 'nse_csv')",
        (sec_dropped,)
    )

    # sec_order has a pending order
    ctx.conn.execute(
        "INSERT INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, cohort_id, rule, cadence, rule_version) "
        "VALUES ('p1', 'model', 'M1', '1', 'live:2026-06-30', 'top30', '1M', '1')"
    )
    ctx.conn.execute(
        "INSERT INTO portfolio_orders (order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at, purpose, side, target_weight, status, liquidity_bucket) "
        "VALUES ('ord1', 'p1', 'live:2026-06-30', ?, '2026-09-30T10:00:00.000000Z', '2026-10-01T10:00:00.000000Z', 'entry', 'buy', 0.05, 'pending', 'A')",
        (sec_order,)
    )

    # Tracked securities at 2026-09-30 with horizons [1, 3, 6, 12, 24, 36]
    # sec_dropped has unmatured horizons (e.g. 6M is Dec 2026, 12M is June 2027) -> MUST BE INCLUDED
    # sec_order has open order -> MUST BE INCLUDED
    tracked = tracked_securities(ctx.conn, cutoff="2026-09-30", horizons=[1, 3, 6, 12, 24, 36])
    assert sec1 in tracked
    assert sec2 in tracked
    assert sec_dropped in tracked
    assert sec_order in tracked
