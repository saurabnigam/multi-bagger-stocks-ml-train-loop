"""Acceptance tests for WS04.02: Pre-computation gates (G1-G7)."""
import json
import sqlite3
import numpy as np
import pandas as pd
import pytest

from quant.config import Config, load
from quant.data.gates import record_event, run as run_gates
from quant.errors import Blocked
from quant.run import RunContext
from quant.types import Actor, CheckReport, Draft, FrozenClock


@pytest.fixture
def ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    
    # Initialize state schema
    from quant.db.core import apply_schema, connect
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    
    cfg = cfg.with_paths(db=db_path)
    clock = FrozenClock("2026-09-30T18:30:00.000000Z")
    actor = Actor(kind="system", name="test")
    
    run_ctx = RunContext(
        as_of="2026-09-30",
        kind="production",
        track="live",
        cfg=cfg,
        clock=clock,
        actor=actor,
    )
    with run_ctx as c:
        yield c


def test_record_event(ctx):
    """record_event persists quality event to data_quality_events table."""
    evt_id = record_event(
        ctx,
        code="TEST_EVENT",
        severity="WARN",
        detail={"msg": "test warning"},
        security_id=42,
        field="close_inr",
    )
    assert isinstance(evt_id, int)
    assert evt_id > 0

    cur = ctx.conn.cursor()
    cur.execute("SELECT code, severity, security_id, field, detail_json FROM data_quality_events WHERE event_id = ?", (evt_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "TEST_EVENT"
    assert row[1] == "WARN"
    assert row[2] == 42
    assert row[3] == "close_inr"
    detail = json.loads(row[4])
    assert detail["msg"] == "test warning"


def test_g1_member_count_gate(ctx):
    """G1 blocks if unique valid universe members < 480."""
    # Under 480 members
    small_members = pd.DataFrame([{"security_id": i} for i in range(400)])
    draft_small = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=small_members,
        groups=pd.Series({i: "Technology" for i in range(400)}),
        source_refs={
            "universe_capture_date": "2026-09-20",
            "has_fundamentals": True,
            "has_prices": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    report = run_gates(ctx, draft_small, phase="pre", strict=False)
    g1 = next(c for c in report.checks if c.id == "G1")
    assert g1.status == "FAIL"
    assert g1.blocking is True


def test_g2_universe_freshness_gate(ctx):
    """Fresh unchanged membership passes G2; stale capture > 62 days blocks."""
    # 500 members
    members = pd.DataFrame([{"security_id": i} for i in range(500)])
    
    # Fresh capture (10 days old)
    draft_fresh = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=pd.Series({i: "Technology" for i in range(500)}),
        source_refs={
            "universe_capture_date": "2026-09-20",
            "has_fundamentals": True,
            "has_prices": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    report = run_gates(ctx, draft_fresh, phase="pre", strict=False)
    g2_fresh = next(c for c in report.checks if c.id == "G2")
    assert g2_fresh.status == "PASS"

    # Stale capture (70 days old)
    draft_stale = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=pd.Series({i: "Technology" for i in range(500)}),
        source_refs={
            "universe_capture_date": "2026-07-01",
            "has_fundamentals": True,
            "has_prices": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    report_stale = run_gates(ctx, draft_stale, phase="pre", strict=False)
    g2_stale = next(c for c in report_stale.checks if c.id == "G2")
    assert g2_stale.status == "FAIL"
    assert g2_stale.blocking is True


def test_g4_first_month_same_close_deferred(ctx):
    """G4 is DEFERRED when prior month cohort or prices are absent."""
    members = pd.DataFrame([{"security_id": i} for i in range(500)])
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=pd.Series({i: "Technology" for i in range(500)}),
        source_refs={
            "universe_capture_date": "2026-09-20",
            "has_fundamentals": True,
            "has_prices": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    report = run_gates(ctx, draft, phase="pre", strict=False)
    g4 = next(c for c in report.checks if c.id == "G4")
    assert g4.status == "DEFERRED"
    assert g4.blocking is False


def test_no_pre_cutoff_fundamentals_blocks_bootstrap(ctx):
    """Missing fundamentals prior to knowledge_cutoff blocks bootstrap."""
    members = pd.DataFrame([{"security_id": i} for i in range(500)])
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=pd.Series({i: "Technology" for i in range(500)}),
        source_refs={
            "universe_capture_date": "2026-09-20",
            "has_fundamentals": False,
            "has_prices": True,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    report = run_gates(ctx, draft, phase="pre", strict=False)
    g6 = next(c for c in report.checks if c.id == "G6")
    assert g6.status == "FAIL"
    assert g6.blocking is True
    assert "bootstrap" in g6.reason.lower()


def test_record_all_gates_before_raising_blocked(ctx):
    """All applicable gates are evaluated and persisted to dq_runs before Blocked is raised."""
    # Defective draft: members < 480, capture stale, no fundamentals, low sector coverage
    small_members = pd.DataFrame([{"security_id": i} for i in range(200)])
    draft_bad = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=small_members,
        groups=pd.Series({i: "Technology" if i < 100 else "Unknown" for i in range(200)}),
        source_refs={
            "universe_capture_date": "2026-06-01",
            "has_fundamentals": False,
            "has_prices": False,
        },
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    with pytest.raises(Blocked) as exc_info:
        run_gates(ctx, draft_bad, phase="pre", strict=True)

    assert "GATE_FAILURE" in str(exc_info.value)

    # Verify that all gates G1-G7 were recorded in dq_runs despite the failure
    cur = ctx.conn.cursor()
    cur.execute("SELECT gate, status, blocking FROM dq_runs WHERE run_id = ? AND phase = 'pre' ORDER BY gate ASC", (ctx.run_id,))
    rows = cur.fetchall()
    recorded_gates = [r[0] for r in rows]
    assert "G1" in recorded_gates
    assert "G2" in recorded_gates
    assert "G3" in recorded_gates
    assert "G4" in recorded_gates
    assert "G5" in recorded_gates
    assert "G6" in recorded_gates
    assert "G7" in recorded_gates
    assert len(recorded_gates) == 7
