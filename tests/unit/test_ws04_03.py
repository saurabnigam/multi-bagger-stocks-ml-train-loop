"""Acceptance tests for WS04.03: Post-compute coverage (G8), replay callbacks (G9) and leakage (G10)."""
import json
import sqlite3
import numpy as np
import pandas as pd
import pytest

from quant.config import Config, load
from quant.data.gates import run as run_gates
from quant.errors import Blocked, Refused
from quant.run import RunContext
from quant.types import Actor, Check, CheckReport, Draft, FrozenClock


@pytest.fixture
def ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    
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


def test_g8_financial_applicability_excluded_from_denominator(ctx):
    """G8 excludes structurally non-applicable financial securities from coverage denominator."""
    # 80 non-financials, 20 financials
    members = pd.DataFrame([{"security_id": i} for i in range(100)])
    groups = pd.Series({i: "Financial Services" if i >= 80 else "Technology" for i in range(100)})
    
    # roce factor: applies_to_financials is False.
    # 20 financials have NaN, 80 tech have valid z
    records = []
    for i in range(100):
        val = np.nan if i >= 80 else 1.2
        records.append({
            "cohort_id": "C-2026-09-30-live",
            "as_of": "2026-09-30",
            "security_id": i,
            "factor_id": "roce@1",
            "z": val,
            "sector_group": groups[i],
            "applies_to_financials": 0,
        })
    fv = pd.DataFrame(records)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=groups,
        source_refs={"has_prior_cohort": False},
        factor_values=fv,
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    report = run_gates(ctx, draft, phase="post", strict=False)
    g8 = next(c for c in report.checks if c.id == "G8")
    # Coverage for non-financials is 80/80 = 100%, so roce@1 is NOT excluded
    assert g8.status == "PASS"
    assert g8.observed == 0  # 0 excluded factors


def test_g8_blocks_on_three_or_more_excluded_actives(ctx):
    """G8 blocks when >= 3 active factors fall below required coverage."""
    members = pd.DataFrame([{"security_id": i} for i in range(100)])
    groups = pd.Series({i: "Technology" for i in range(100)})

    # 3 factors with only 10% coverage (< 70% threshold)
    records = []
    for fid in ["f1@1", "f2@1", "f3@1"]:
        for i in range(100):
            val = 1.0 if i < 10 else np.nan
            records.append({
                "cohort_id": "C-2026-09-30-live",
                "as_of": "2026-09-30",
                "security_id": i,
                "factor_id": fid,
                "z": val,
                "sector_group": "Technology",
                "applies_to_financials": 1,
            })
    fv = pd.DataFrame(records)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=groups,
        source_refs={"has_prior_cohort": False},
        factor_values=fv,
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    report = run_gates(ctx, draft, phase="post", strict=False)
    g8 = next(c for c in report.checks if c.id == "G8")
    assert g8.status == "FAIL"
    assert g8.blocking is True
    assert g8.observed == 3


def test_g8_passes_on_less_than_three_excluded_actives(ctx):
    """G8 passes when 1 or 2 active factors are excluded on coverage."""
    members = pd.DataFrame([{"security_id": i} for i in range(100)])
    groups = pd.Series({i: "Technology" for i in range(100)})

    # 2 factors fail coverage, 1 passes
    records = []
    for fid, n_valid in [("f1@1", 10), ("f2@1", 10), ("f3@1", 90)]:
        for i in range(100):
            val = 1.0 if i < n_valid else np.nan
            records.append({
                "cohort_id": "C-2026-09-30-live",
                "as_of": "2026-09-30",
                "security_id": i,
                "factor_id": fid,
                "z": val,
                "sector_group": "Technology",
                "applies_to_financials": 1,
            })
    fv = pd.DataFrame(records)

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=members,
        groups=groups,
        source_refs={"has_prior_cohort": False},
        factor_values=fv,
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    report = run_gates(ctx, draft, phase="post", strict=False)
    g8 = next(c for c in report.checks if c.id == "G8")
    assert g8.status == "PASS"
    assert g8.observed == 2


def test_g9_first_month_deferred(ctx):
    """G9 historical replay is legitimately DEFERRED on first month."""
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={"has_prior_cohort": False},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    report = run_gates(ctx, draft, phase="post", strict=False)
    g9 = next(c for c in report.checks if c.id == "G9")
    assert g9.status == "DEFERRED"
    assert g9.blocking is False


def test_g9_callback_absent_raises_refused(ctx):
    """When prior cohort exists, an absent G9 callback is an implementation error."""
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={"has_prior_cohort": True},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    with pytest.raises(Refused) as exc_info:
        run_gates(ctx, draft, phase="post", strict=False, g9_replay_cb=None)
    assert "missing_callback" in str(exc_info.value)


def test_g9_callback_mismatch_fails_and_blocks(ctx):
    """G9 replay mismatch fails and blocks."""
    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": 1}]),
        groups=pd.Series({1: "Technology"}),
        source_refs={"has_prior_cohort": True},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )

    def failing_replay_cb(c, d):
        return Check(
            id="G9",
            status="FAIL",
            observed="diff_hash",
            expected="orig_hash",
            reason="Prior cohort scores mismatch",
            blocking=True,
        )

    with pytest.raises(Blocked) as exc_info:
        run_gates(ctx, draft, phase="post", strict=True, g9_replay_cb=failing_replay_cb)
    assert "GATE_FAILURE" in str(exc_info.value)
