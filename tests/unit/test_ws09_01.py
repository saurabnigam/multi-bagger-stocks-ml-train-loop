"""Tests for WS09.01: Recorded bootstrap and hypothesis budget (C09)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pytest

from quant.config import Config
from quant.data.calendar import Calendar
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.knowledge.bootstrap import seed
from quant.knowledge.registry import budget_status, new_hypothesis
from quant.run import RunContext
from quant.types import Actor, Clock


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
def golden_cases():
    cases_path = Path("docs/spec/contracts/golden_cases.json")
    with open(cases_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", data)


@pytest.fixture
def test_ctx(tmp_path, cfg):
    db_path = tmp_path / "test_knowledge.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    clock = FrozenTestClock("2026-09-01T09:00:00.000000Z")
    actor = Actor(kind="system", name="cli")

    ctx = RunContext(
        as_of="2026-09-01",
        kind="bootstrap",
        track="live",
        cfg=cfg.with_paths(db=db_path),
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn
    return ctx


def test_bootstrap_idempotent_system_tier0(test_ctx):
    """Seed only exact launch definitions with system/spec-hash decision, idempotently; no fake human or Tier2."""
    spec_sha = "3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f"
    res1 = seed(test_ctx, spec_sha256=spec_sha)
    assert res1.status == "ok"

    # Verify Tier-0 system decision
    d_row = test_ctx.conn.execute(
        "SELECT * FROM decisions WHERE decision_id = 'DEC_BOOTSTRAP'"
    ).fetchone()
    assert d_row is not None
    assert d_row["tier"] == 0
    assert d_row["approver_kind"] == "system"
    assert d_row["decided_by"] == "system"
    assert spec_sha in d_row["evidence_refs_json"]

    # Verify launch models seeded
    models = test_ctx.conn.execute("SELECT model_id, role FROM models").fetchall()
    model_ids = {m["model_id"] for m in models}
    assert "EW_HIER_v1" in model_ids
    assert "EW_FLAT_v1" in model_ids
    assert "MOM_ONLY_v1" in model_ids
    assert "IC_SHRUNK_v1" in model_ids

    # Verify launch hypotheses have counts_toward_budget == 0
    h_rows = test_ctx.conn.execute(
        "SELECT count(*) as total, sum(counts_toward_budget) as budget_used FROM hypotheses"
    ).fetchone()
    assert h_rows["total"] > 0
    assert h_rows["budget_used"] == 0

    # Test idempotency: second call succeeds without duplicating rows
    count_before = h_rows["total"]
    res2 = seed(test_ctx, spec_sha256=spec_sha)
    assert res2.status == "ok"
    count_after = test_ctx.conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]
    assert count_after == count_before


def test_launch_exemption_and_trial_counts(test_ctx):
    """Launch exemption does not exclude hypotheses from reported trial counts."""
    spec_sha = "3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f"
    seed(test_ctx, spec_sha256=spec_sha)

    status = budget_status(test_ctx.conn, year=2026, horizon_m=3)
    assert status["budget_used"] == 0
    assert status["budget_remaining"] == 6
    # Trials count includes the launch hypotheses!
    assert status["trials_count"] > 0
    assert status["annual_trials_count"] > 0


def test_first_cutoff_must_be_after_registration(test_ctx):
    """first_oos_as_of knowledge cutoff must be strictly after registration timestamp."""
    spec_sha = "3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f"
    seed(test_ctx, spec_sha256=spec_sha)

    # Registration timestamp is 2026-09-01T09:00:00Z
    # An as_of of 2026-08-31 has cutoff 2026-08-31T18:29:59Z, which is BEFORE registration!
    with pytest.raises(Refused) as exc:
        new_hypothesis(
            test_ctx,
            {
                "family": "momentum",
                "kind": "factor",
                "subject_id": "test_mom@1",
                "title": "Test Momentum",
                "statement": "Test statement",
                "expected_sign": 1,
                "horizon_m": 3,
                "primary_metric": "ic",
                "success_criterion": "ic >= 0.02",
                "failure_criterion": "ic < 0",
                "first_oos_as_of": "2026-08-31",  # Cutoff is before registration!
                "formula": "close / close_20",
                "registered_on": "2026-09-01T09:00:00.000000Z",
                "budget_year": 2026,
            },
        )
    assert "cutoff" in str(exc.value).lower() or "after" in str(exc.value).lower()


def test_annual_seventh_and_family_fourth_refused(test_ctx, golden_cases):
    """Annual 7th and family 4th registrations are refused."""
    gov = golden_cases["governance"]
    assert gov["actor_kind"] == "llm"
    assert gov["tier2_expected_exit"] == 3
    max_year = 6
    max_fam = 3

    spec_sha = "3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f"
    seed(test_ctx, spec_sha256=spec_sha)

    # Register 3 hypotheses in family "momentum" (all count toward budget)
    for i in range(1, max_fam + 1):
        hid = new_hypothesis(
            test_ctx,
            {
                "family": "momentum",
                "kind": "factor",
                "subject_id": f"mom_variant_{i}@1",
                "title": f"Momentum Variant {i}",
                "statement": "Statement",
                "expected_sign": 1,
                "horizon_m": 3,
                "primary_metric": "ic",
                "success_criterion": "ic >= 0.02",
                "failure_criterion": "ic < 0",
                "first_oos_as_of": "2026-09-30",
                "formula": f"close / close_{i}",
                "registered_on": "2026-09-01T09:00:00.000000Z",
                "budget_year": 2026,
            },
        )
        assert hid.startswith("H-2026-")

    # 4th in family "momentum" must be refused!
    with pytest.raises(Refused) as exc:
        new_hypothesis(
            test_ctx,
            {
                "family": "momentum",
                "kind": "factor",
                "subject_id": "mom_variant_4@1",
                "title": "Momentum Variant 4",
                "statement": "Statement",
                "expected_sign": 1,
                "horizon_m": 3,
                "primary_metric": "ic",
                "success_criterion": "ic >= 0.02",
                "failure_criterion": "ic < 0",
                "first_oos_as_of": "2026-09-30",
                "formula": "close / close_4",
                "registered_on": "2026-09-01T09:00:00.000000Z",
                "budget_year": 2026,
            },
        )
    assert "family" in str(exc.value).lower()

    # Now register 3 in family "quality" to bring total annual budget used to 6
    for i in range(1, 4):
        new_hypothesis(
            test_ctx,
            {
                "family": "quality",
                "kind": "factor",
                "subject_id": f"quality_variant_{i}@1",
                "title": f"Quality Variant {i}",
                "statement": "Statement",
                "expected_sign": 1,
                "horizon_m": 3,
                "primary_metric": "ic",
                "success_criterion": "ic >= 0.02",
                "failure_criterion": "ic < 0",
                "first_oos_as_of": "2026-09-30",
                "formula": f"roce / {i}",
                "registered_on": "2026-09-01T09:00:00.000000Z",
                "budget_year": 2026,
            },
        )

    # Now annual budget used is 6. A 7th hypothesis in another family ("value") must be refused!
    with pytest.raises(Refused) as exc:
        new_hypothesis(
            test_ctx,
            {
                "family": "value",
                "kind": "factor",
                "subject_id": "val_variant_1@1",
                "title": "Value Variant 1",
                "statement": "Statement",
                "expected_sign": 1,
                "horizon_m": 3,
                "primary_metric": "ic",
                "success_criterion": "ic >= 0.02",
                "failure_criterion": "ic < 0",
                "first_oos_as_of": "2026-09-30",
                "formula": "ep / 1",
                "registered_on": "2026-09-01T09:00:00.000000Z",
                "budget_year": 2026,
            },
        )
    assert "annual" in str(exc.value).lower() or "budget" in str(exc.value).lower()
