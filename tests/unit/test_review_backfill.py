"""Review fix tests for quant.evaluation.backfill.replay (C07).

These prove the rewrite computes real, PIT price/volume factor values
through the standard registry pipeline (never a fabricated z-scored TRI
level with a hard-coded sector), respects warmup accounting, and stays
idempotent on re-run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.data.prices import PriceStore
from quant.evaluation.backfill import replay as backfill_replay
from quant.factors.controls import Size
from quant.factors.momentum import Mom12_1
from quant.factors.registry import sync as factor_sync

SECTORS = ["SECTOR_A", "SECTOR_B", "SECTOR_C"]
N_PER_SECTOR = 6
SECURITY_IDS = list(range(1, len(SECTORS) * N_PER_SECTOR + 1))
N_SESSIONS = 400


def _session_dates() -> list[str]:
    idx = pd.bdate_range(start="2024-01-01", periods=N_SESSIONS)
    return [d.strftime("%Y-%m-%d") for d in idx]


def _seed_world(ctx, session_dates: list[str]) -> None:
    conn = ctx.conn

    for sid in SECURITY_IDS:
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2015-01-01', '2026-12-31', 'listed')",
            (sid, f"INE{sid:09d}", f"Synthetic {sid}"),
        )

    observed_at = "2026-09-01T00:00:00.000000Z"
    for i, sid in enumerate(SECURITY_IDS):
        sector = SECTORS[i % len(SECTORS)]
        conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, "
            "nse_symbol, nse_sector, series, source) VALUES (?, ?, ?, 'NIFTY500', ?, ?, 'EQ', 'nse_csv')",
            ("2026-09-01", observed_at, sid, f"SYM{sid}", sector),
        )
        conn.execute(
            "INSERT INTO sector_map (security_id, observed_at, valid_from, valid_to, nse_sector, "
            "yahoo_sector, yahoo_industry, sector_group, group_def_version, source, confidence) "
            "VALUES (?, ?, '2015-01-01', NULL, ?, NULL, NULL, ?, 1, 'nse_csv', 1.0)",
            (sid, observed_at, sector, sector),
        )

    # Register one real backfillable price factor (mom_12_1) and one real
    # non-backfillable control factor (size) through the actual registry.
    factor_sync(ctx, [Mom12_1().spec, Size().spec])

    # Seed daily prices directly into the price store: distinct per-security
    # drift so raw momentum values genuinely differ (never a hard-coded
    # 'Industrials' / price-level fabrication).
    store = PriceStore(ctx.cfg.paths.prices_db, state_conn=conn)
    rows = []
    for i, sid in enumerate(SECURITY_IDS):
        drift = 0.0003 * ((i % 5) - 2) + 0.0002
        price = 100.0
        for j, d in enumerate(session_dates):
            price *= 1.0 + drift + 0.0004 * np.sin(i + j * 0.13)
            rows.append(
                (
                    sid, d, price, price, price, price,
                    100000.0 + sid * 137.0, 0.0, 1.0, price, price,
                    f"{d}T00:00:00.000000Z", "cap_synth", "sha_synth",
                )
            )
    with store.conn() as p_conn:
        p_conn.executemany(
            "INSERT OR IGNORE INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, "
            "close_raw, volume_raw, dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, "
            "observed_at, capture_id, source_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def test_backfill_warmup_real_values_and_idempotency(ctx):
    session_dates = _session_dates()
    _seed_world(ctx, session_dates)
    start, end = session_dates[0], session_dates[-1]

    res = backfill_replay(ctx, start=start, end=end)

    assert res.status == "ok"
    assert res.details["requested_start"] == start
    assert res.details["requested_end"] == end
    assert res.details["cohorts_created"] > 0
    assert len(res.details["warmup_excluded_as_of"]) > 0
    assert res.details["membership_source"] == "universe_membership"
    assert "survivorship" in res.details["survivorship_caveat"].lower()

    cohort_rows = ctx.conn.execute(
        "SELECT cohort_id, as_of FROM cohorts WHERE track = 'backfill' ORDER BY as_of"
    ).fetchall()
    created_as_of = [r["as_of"] for r in cohort_rows]
    assert created_as_of, "expected at least one backfill cohort to be created"

    # Warmup accounting: every excluded candidate precedes every created cohort.
    assert max(res.details["warmup_excluded_as_of"]) < min(created_as_of)
    # Not a promised fixed count: fewer cohorts than raw candidate month-ends.
    assert res.details["cohorts_created"] < len(res.details["candidate_month_ends"])

    # Only backfillable factors reach factor_values -- size@1 (backfillable=False)
    # must never appear even though compute_all computed it upstream.
    size_rows = ctx.conn.execute(
        "SELECT COUNT(*) FROM factor_values WHERE factor_id = 'size@1'"
    ).fetchone()[0]
    assert size_rows == 0

    # The most mature cohort has full lookback: real, dispersed, correctly
    # standardised values -- never a fabricated constant-per-sector value.
    last_cohort_id = cohort_rows[-1]["cohort_id"]
    fv = pd.read_sql_query(
        "SELECT security_id, sector_group, raw, z FROM factor_values "
        "WHERE cohort_id = ? AND factor_id = 'mom_12_1@1'",
        ctx.conn,
        params=(last_cohort_id,),
    )
    assert len(fv) == len(SECURITY_IDS)
    assert fv["z"].notna().all()
    assert (fv["z"].abs() <= 3.0 + 1e-9).all()
    assert set(fv["sector_group"].unique()) == set(SECTORS)
    assert fv["z"].nunique() > 1
    assert fv["raw"].nunique() > 1

    for _, sub in fv.groupby("sector_group"):
        assert abs(sub["z"].mean()) < 1e-6

    # Published cohort rows are marked survivorship-biased and unclean.
    cohort_meta = ctx.conn.execute(
        "SELECT is_clean, source_refs_json FROM cohorts WHERE cohort_id = ?",
        (last_cohort_id,),
    ).fetchone()
    assert cohort_meta["is_clean"] == 0
    assert "backfill" in cohort_meta["source_refs_json"]
    assert "survivorship" in cohort_meta["source_refs_json"].lower()

    counts_before = {
        "cohorts": ctx.conn.execute("SELECT COUNT(*) FROM cohorts WHERE track='backfill'").fetchone()[0],
        "factor_values": ctx.conn.execute("SELECT COUNT(*) FROM factor_values WHERE track='backfill'").fetchone()[0],
        "prices_monthly": ctx.conn.execute(
            "SELECT COUNT(*) FROM prices_monthly WHERE cohort_id LIKE 'backfill:%'"
        ).fetchone()[0],
    }

    res2 = backfill_replay(ctx, start=start, end=end)
    assert res2.status == "ok"
    assert res2.details["cohorts_created"] == 0

    counts_after = {
        "cohorts": ctx.conn.execute("SELECT COUNT(*) FROM cohorts WHERE track='backfill'").fetchone()[0],
        "factor_values": ctx.conn.execute("SELECT COUNT(*) FROM factor_values WHERE track='backfill'").fetchone()[0],
        "prices_monthly": ctx.conn.execute(
            "SELECT COUNT(*) FROM prices_monthly WHERE cohort_id LIKE 'backfill:%'"
        ).fetchone()[0],
    }
    assert counts_before == counts_after


def test_backfill_never_leaks_current_mcap_into_historical_panel(ctx):
    """MASTER_SPEC 4.5: 'Attributes such as current market cap are not
    backfillable price factors.' A security_attributes snapshot captured
    near the run clock must never end up on a historical backfill cohort's
    persisted prices_monthly row -- for either the earliest or the most
    recent cohort created."""
    session_dates = _session_dates()
    _seed_world(ctx, session_dates)

    conn = ctx.conn
    captured_at = "2026-09-01T00:00:00.000000Z"  # near the frozen run clock, 2026-10-01
    sentinel = {sid: 999_000_000_000.0 + sid for sid in SECURITY_IDS}
    for sid in SECURITY_IDS:
        conn.execute(
            "INSERT INTO security_attributes (captured_at, security_id, mcap_inr, shares_out, "
            "source_sha256) VALUES (?, ?, ?, ?, 'sha_attrs')",
            (captured_at, sid, sentinel[sid], 1_000_000.0 + sid),
        )

    start, end = session_dates[0], session_dates[-1]
    res = backfill_replay(ctx, start=start, end=end)
    assert res.status == "ok"
    assert res.details["cohorts_created"] > 0

    cohort_ids = [
        r["cohort_id"]
        for r in conn.execute(
            "SELECT cohort_id FROM cohorts WHERE track = 'backfill' ORDER BY as_of"
        ).fetchall()
    ]
    assert cohort_ids

    for cohort_id in (cohort_ids[0], cohort_ids[-1]):
        panel = pd.read_sql_query(
            "SELECT security_id, mcap_inr, shares_out FROM prices_monthly WHERE cohort_id = ?",
            conn,
            params=(cohort_id,),
        )
        assert len(panel) == len(SECURITY_IDS)
        # Never today's (future, relative to the historical as_of) snapshot.
        for _, row in panel.iterrows():
            assert row["mcap_inr"] != sentinel[int(row["security_id"])]
        # Never any real value at all -- backfill has no PIT attribute source.
        assert panel["mcap_inr"].isna().all()
        assert panel["shares_out"].isna().all()


def test_backfill_no_price_data_never_fabricates(ctx):
    """No price store data at all: no candidate month-ends, no cohorts, no factor rows."""
    res = backfill_replay(ctx, start="2024-01-31", end="2024-12-31")

    assert res.status == "ok"
    assert res.details["cohorts_created"] == 0
    assert res.details["actual_cohorts"] == 0
    assert res.details["candidate_month_ends"] == []
    assert "dq_notes" in res.details

    fv_count = ctx.conn.execute("SELECT COUNT(*) FROM factor_values").fetchone()[0]
    cohort_count = ctx.conn.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0]
    assert fv_count == 0
    assert cohort_count == 0


def test_backfill_no_universe_capture_falls_back_to_unclassified(ctx):
    """Real price data but no universe_membership capture: fall back to price-store
    securities with UNCLASSIFIED sector groups and an explicit DQ note, never a
    hard-coded sector like the old 'Industrials' fabrication."""
    conn = ctx.conn
    fallback_sids = list(range(101, 107))  # 6 securities, one UNCLASSIFIED group
    for sid in fallback_sids:
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2015-01-01', '2026-12-31', 'listed')",
            (sid, f"INF{sid:09d}", f"Fallback {sid}"),
        )
    factor_sync(ctx, [Mom12_1().spec])

    session_dates = _session_dates()
    store = PriceStore(ctx.cfg.paths.prices_db, state_conn=conn)
    rows = []
    for i, sid in enumerate(fallback_sids):
        drift = 0.0004 * (i - 2)
        price = 100.0
        for j, d in enumerate(session_dates):
            price *= 1.0 + drift + 0.0003 * np.sin(i + j * 0.17)
            rows.append(
                (
                    sid, d, price, price, price, price,
                    50000.0, 0.0, 1.0, price, price,
                    f"{d}T00:00:00.000000Z", "cap_synth", "sha_synth",
                )
            )
    with store.conn() as p_conn:
        p_conn.executemany(
            "INSERT OR IGNORE INTO prices_daily (security_id, date, open_raw, high_raw, low_raw, "
            "close_raw, volume_raw, dividend_raw, split_ratio, yahoo_close, yahoo_adj_close, "
            "observed_at, capture_id, source_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    res = backfill_replay(ctx, start=session_dates[0], end=session_dates[-1])

    assert res.status == "ok"
    assert res.details["membership_source"] == "price_store_fallback"
    assert "dq_notes" in res.details
    assert res.details["cohorts_created"] > 0

    last_cohort_id = conn.execute(
        "SELECT cohort_id FROM cohorts WHERE track = 'backfill' ORDER BY as_of DESC LIMIT 1"
    ).fetchone()[0]
    fv = pd.read_sql_query(
        "SELECT security_id, sector_group, z FROM factor_values "
        "WHERE cohort_id = ? AND factor_id = 'mom_12_1@1'",
        conn,
        params=(last_cohort_id,),
    )
    assert len(fv) == len(fallback_sids)
    assert set(fv["sector_group"].unique()) == {"UNCLASSIFIED"}
    assert "Industrials" not in fv["sector_group"].unique()
    assert fv["z"].notna().all()
    assert abs(fv["z"].mean()) < 1e-6
    assert fv["z"].nunique() > 1
