"""Tests for WS09.03: Approval, ratification and prospective changes (C09)."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pytest

from quant.cli import main as cli_main
from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.knowledge.adr import check as adr_check, write as adr_write
from quant.knowledge.bootstrap import seed
from quant.knowledge.proposals import apply as apply_proposals, approve, authorize, draft, ratify, reject
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
    db_path = tmp_path / "test_proposals.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=knowledge_dir)

    clock = FrozenTestClock("2026-09-01T09:00:00.000000Z")
    actor = Actor(kind="system", name="cli")

    ctx = RunContext(
        as_of="2026-09-01",
        kind="proposal",
        track="live",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    # Initialize runs and bootstrap
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-01', 'proposal', 'live', 1, '2026-09-01T09:00:00.000000Z', 'ok', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:1', '2026-09-01', 'live', '2026-09-01T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-01T09:00:00.000000Z', '2026-09-01T09:00:00.000000Z', 1, 1)"
        )

    seed(ctx, spec_sha256="3e90bb1623d9d47c53bc23531a77f591feec6f55bd5bc54bc58487437dcdbb2f")
    return ctx


def test_llm_actor_plus_human_prefix_refused(test_ctx):
    """LLM actor claiming human prefix is refused with exit code 3."""
    # Insert a proposal
    with test_ctx.conn:
        test_ctx.conn.execute(
            "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, rule_id, proposed_by, status, md_path) "
            "VALUES ('P-2026-09-01', 1, '2026-09-01', 'promote_factor', 'mom_12_1@1', '{}', '{}', 'rule_1', 'system', 'proposed', 'p.md')"
        )

    # Context with actor_kind='llm' but by='human:alice'
    test_ctx.actor = Actor(kind="llm", name="human:alice")
    with pytest.raises(Refused) as exc:
        approve(test_ctx, proposal_id="P-2026-09-01", note="Attempted impersonation")
    assert "human" in str(exc.value).lower() or "llm" in str(exc.value).lower()

    # Verify CLI exits with code 3 (governance golden case human_prefix_expected_exit)
    db_file = str(test_ctx.cfg.paths.db)
    rc = cli_main(["kb", "approve", "P-2026-09-01", "--actor-kind", "llm", "--by", "human:alice", "--db-path", db_file])
    assert rc == 3


def test_llm_actor_tier2_refused(test_ctx, golden_cases):
    """LLM actor attempting to approve Tier 2 proposal is refused with exit code 3."""
    gov = golden_cases["governance"]
    expected_exit = gov["tier2_expected_exit"]  # 3

    # Insert a Tier 2 proposal (promote_model)
    with test_ctx.conn:
        test_ctx.conn.execute(
            "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, rule_id, proposed_by, status, md_path) "
            "VALUES ('P-2026-09-02', 1, '2026-09-01', 'promote_model', 'IC_SHRUNK_v1', '{}', '{}', 'rule_1', 'system', 'proposed', 'p.md')"
        )

    test_ctx.actor = Actor(kind="llm", name="llm:gemini-flash")
    with pytest.raises(Refused) as exc:
        approve(test_ctx, proposal_id="P-2026-09-02", note="LLM trying Tier 2")
    assert "tier 2" in str(exc.value).lower() or "tier2" in str(exc.value).lower()

    # CLI exit code check
    db_file = str(test_ctx.cfg.paths.db)
    rc = cli_main(["kb", "approve", "P-2026-09-02", "--actor-kind", "llm", "--by", "llm:gemini-flash", "--db-path", db_file])
    assert rc == expected_exit


def test_tier1_criteria_all_true_yields_provisional(test_ctx, golden_cases):
    """Tier 1 proposal approved by LLM yields provisional status and writes ADR."""
    gov = golden_cases["governance"]
    expected_status = gov["tier1_initial_status"]  # "provisional"

    with test_ctx.conn:
        test_ctx.conn.execute(
            "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, rule_id, proposed_by, status, md_path) "
            "VALUES ('P-2026-09-03', 1, '2026-09-01', 'promote_factor', 'roce@1', '{}', '{}', 'rule_1', 'system', 'proposed', 'p.md')"
        )

    test_ctx.actor = Actor(kind="llm", name="llm:gemini-flash")
    did = approve(test_ctx, proposal_id="P-2026-09-03", note="LLM approved provisional")
    assert did.startswith("D-")

    d_row = test_ctx.conn.execute(
        "SELECT status, approver_kind, tier, adr_path FROM decisions WHERE decision_id = ?",
        (did,),
    ).fetchone()
    assert d_row["status"] == expected_status
    assert d_row["tier"] == 1
    assert d_row["approver_kind"] == "llm"

    # Verify ADR was written to disk
    adr_file = Path(d_row["adr_path"])
    assert adr_file.exists()
    content = adr_file.read_text(encoding="utf-8")
    assert "provisional" in content.lower()
    assert "roce@1" in content


def test_human_ratifies_provisional_decision(test_ctx):
    """Human ratifies a provisional decision, moving status to approved."""
    with test_ctx.conn:
        test_ctx.conn.execute(
            "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, rule_id, proposed_by, status, md_path) "
            "VALUES ('P-2026-09-04', 1, '2026-09-01', 'promote_factor', 'eps_growth_3y@1', '{}', '{}', 'rule_1', 'system', 'proposed', 'p.md')"
        )

    test_ctx.actor = Actor(kind="llm", name="llm:gemini-flash")
    did = approve(test_ctx, proposal_id="P-2026-09-04", note="LLM approved")

    # Now human ratifies
    test_ctx.actor = Actor(kind="human", name="human:saurabh")
    res = ratify(test_ctx, decision_id=did, note="Human co-signature confirmed")
    assert res.status == "ok"

    d_row = test_ctx.conn.execute(
        "SELECT status, ratified_by, ratified_on FROM decisions WHERE decision_id = ?",
        (did,),
    ).fetchone()
    assert d_row["status"] == "approved"
    assert d_row["ratified_by"] == "human:saurabh"
    assert d_row["ratified_on"] is not None


def test_expiry_appends_reversion_and_prospective_model_version(test_ctx):
    """Unratified provisional decision expiring after 60 days appends reversion decision and prospective model version."""
    # Seed past provisional decision decided 65 days ago
    past_ts = "2026-06-25T09:00:00.000000Z"
    did = "D-2026-06-01"
    adr_path = str(test_ctx.cfg.paths.knowledge_dir / "decisions" / f"ADR-{did}.md")
    Path(adr_path).parent.mkdir(parents=True, exist_ok=True)
    Path(adr_path).write_text(f"# ADR {did}\nProvisional promote factor roce@1", encoding="utf-8")

    with test_ctx.conn:
        test_ctx.conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) "
            "VALUES (?, 'promote_factor', 1, 'roce@1', 'Promote Roce', 'ctx', '[]', 'approve', '[]', ?, 'llm:gemini-flash', 'llm', 'provisional', ?, 'sha1')",
            (did, past_ts, adr_path),
        )
        test_ctx.conn.execute(
            "UPDATE factor_registry SET status = 'active' WHERE factor_id = 'roce@1'"
        )

    # Call apply at as_of 2026-09-01 (68 days later > 60 days expiry)
    test_ctx.clock = FrozenTestClock("2026-09-01T09:00:00.000000Z")
    res = apply_proposals(test_ctx, as_of="2026-09-01")
    assert res.status == "ok"

    # Verify old decision is now reverted
    old_d = test_ctx.conn.execute(
        "SELECT status FROM decisions WHERE decision_id = ?", (did,)
    ).fetchone()
    assert old_d["status"] == "reverted"

    # Verify reversion decision was created
    rev_d = test_ctx.conn.execute(
        "SELECT * FROM decisions WHERE kind = 'reversion'"
    ).fetchone()
    assert rev_d is not None
    assert rev_d["approver_kind"] == "system"

    # Verify factor was reverted
    f_stat = test_ctx.conn.execute(
        "SELECT status FROM factor_registry WHERE factor_id = 'roce@1'"
    ).fetchone()["status"]
    assert f_stat in ("shadow", "probation")

    # Verify ADR exists for reversion
    rev_adr = Path(rev_d["adr_path"])
    assert rev_adr.exists()


def test_adr_exists_for_every_applied_decision(test_ctx):
    """Verify adr.check confirms an ADR file exists on disk for every decision."""
    # Initially DEC_BOOTSTRAP exists from seed
    report = adr_check(test_ctx.conn, test_ctx.cfg.paths.knowledge_dir)
    assert report.passed
