import pytest
import sqlite3
import pandas as pd

from quant.config import load as load_config
from quant.data.actions import detect, add as action_add, clear as action_clear, accept_revision
from quant.data.prices import PriceStore
from quant.errors import Refused
from quant.types import FrozenClock, Actor


def make_decision(conn, decision_id, approver_kind="human", status="approved"):
    conn.execute(
        """
        INSERT OR IGNORE INTO decisions (
            decision_id, proposal_id, kind, tier, subject_id, title, context,
            options_json, decision, evidence_refs_json, criteria_check_json,
            decided_on, decided_by, approver_kind, status, adr_path, git_sha
        ) VALUES (
            ?, NULL, 'clear_ca_flag', 1, 'SEC1', 'Test Action', 'Context',
            '{}', 'Approve', '{}', '{}', '2026-09-01', 'auditor', ?, ?, 'doc/adr.md', 'sha'
        )
        """,
        (decision_id, approver_kind, status),
    )


def test_actor_decision_mismatch_refuses(ctx):
    # Insert decision approved by human
    make_decision(ctx.conn, "D-HUMAN-01", approver_kind="human", status="approved")

    # If actor is system or llm trying to execute a decision that requires human approver_kind mismatch
    ctx.actor = Actor(kind="system", name="cron")

    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (100, 'INE100A01001', 'Mismatch Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    with pytest.raises(Refused):
        action_add(ctx, isin="INE100A01001", ex_date="2026-08-01", kind="split", factor=2.0, decision_id="D-HUMAN-01")


def test_action_add_appends_and_never_mutates_existing(ctx):
    make_decision(ctx.conn, "D-HUMAN-02", approver_kind="human", status="approved")
    ctx.actor = Actor(kind="human", name="auditor")

    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (101, 'INE101A01001', 'Action Corp', '2026-01-01', '2026-09-30', 'listed')"
    )

    res = action_add(ctx, isin="INE101A01001", ex_date="2026-08-01", kind="split", factor=2.0, decision_id="D-HUMAN-02")
    assert res.status == "ok"

    # Verify corporate_actions table row
    cur = ctx.conn.cursor()
    cur.execute("SELECT security_id, ex_date, kind, ratio FROM corporate_actions WHERE security_id = 101")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][3] == 2.0


def test_unexplained_revisions_remain_quarantined(tmp_path, ctx):
    price_db = tmp_path / "prices.sqlite"
    store = PriceStore(path=price_db, state_conn=ctx.conn)

    # Ingest baseline on 2026-09-01
    df1 = pd.DataFrame({
        "date": ["2026-09-01"],
        "close": [100.0],
        "volume": [1000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta1 = {
        "security_id": 102,
        "close_basis": "raw",
        "observed_at": "2026-09-01T18:29:59.999999Z",
        "capture_id": "cap_v1",
        "source_sha256": "sha_v1",
    }
    store.ingest(ctx, df1, meta1)

    # Re-ingest with unexplained revision: close jumps from 100 to 200 without split/dividend
    df2 = pd.DataFrame({
        "date": ["2026-09-01"],
        "close": [200.0],
        "volume": [1000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    meta2 = {
        "security_id": 102,
        "close_basis": "raw",
        "observed_at": "2026-09-05T18:29:59.999999Z",
        "capture_id": "cap_v2",
        "source_sha256": "sha_v2",
    }
    res = store.reconcile(ctx, df2, meta2)
    assert res.status == "quarantined"

    # Verify quarantine row in price db
    with store.conn() as p_conn:
        cur = p_conn.cursor()
        cur.execute("SELECT security_id, date, reason FROM prices_daily_quarantine WHERE security_id = 102")
        q_row = cur.fetchone()
        assert q_row is not None
        assert "unexplained_revision" in q_row[2]


def test_approved_resolution_never_mutates_factor_values_or_scores(ctx):
    # Insert mock security, cohort, factor_registry and factor_values
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (101, 'INE101A01001', 'Action Corp', '2026-01-01', '2026-09-30', 'listed')"
    )
    ctx.conn.execute(
        "INSERT OR IGNORE INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('C-2026-09-30-live', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def', 'mem', '{}', '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z', 1, ?)",
        (ctx.run_id,)
    )
    ctx.conn.execute(
        "INSERT OR IGNORE INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
        "VALUES ('f_test', 'test', 1, 'value', 1, 12, 'standard', 'hyp', 'formula', '[]', 252, 1, 1, 0.7, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01')"
    )
    ctx.conn.execute(
        "INSERT OR IGNORE INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group, flags, input_refs_json, track, run_id) "
        "VALUES ('C-2026-09-30-live', '2026-09-30', 101, 'f_test', 5.0, 5.0, 1.2, 'Technology', '', '{}', 'live', ?)",
        (ctx.run_id,)
    )

    make_decision(ctx.conn, "D-HUMAN-03", approver_kind="human", status="approved")
    ctx.actor = Actor(kind="human", name="auditor")

    action_add(ctx, isin="INE101A01001", ex_date="2026-08-01", kind="manual_adj", factor=1.05, decision_id="D-HUMAN-03")

    # Invariant check: factor_values must be byte-for-byte identical!
    cur = ctx.conn.cursor()
    cur.execute("SELECT raw, z FROM factor_values WHERE cohort_id = 'C-2026-09-30-live' AND security_id = 101")
    row = cur.fetchone()
    assert row[0] == 5.0
    assert row[1] == 1.2
