"""Evidence triage core (WP2): evidence archive, proposal store, confidence grader, static guard.

Design: docs from the triage design, sections 3-5, 8 and the work package WP2. These tests use a
disposable state database under tmp_path, a FrozenClock and no network. The cohort is as_of
2026-10-30 (Friday); its knowledge cutoff is 2026-10-30T18:29:59.999999Z (23:59:59.999999 IST).
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import re
import shutil

import pytest

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.run import RunContext
from quant.triage import evidence, grade, store, types
from quant.types import Actor, FrozenClock

AS_OF = "2026-10-30"
CUTOFF = "2026-10-30T18:29:59.999999Z"
ISIN = "INE000000001"
EX = "2026-10-29"
KEY = f"{ISIN}@{EX}"
NOON = "2026-10-30T06:30:00.000000Z"


@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here (amendment F7)."""
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("triage_core") / "template.db"
    conn = connect(path)
    apply_schema(conn, kind="state")
    conn.close()
    return path


class World:
    def __init__(self, root: Path, template: Path):
        self.root = root
        shutil.copy(template, root / "state.db")
        self.cfg = load_config().with_paths(
            db=root / "state.db", prices_db=root / "p.db", data_dir=root / "d",
            archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")

    def run(self, now: str = NOON, actor_kind: str = "system", name: str = "pytest", as_of: str = AS_OF):
        clock = FrozenClock(now)
        ctx = RunContext(as_of=as_of, kind="triage", track="live", cfg=self.cfg, clock=clock,
                         actor=Actor(kind=actor_kind, name=name))
        return ctx


@pytest.fixture
def world(tmp_path, template_db):
    return World(tmp_path, template_db)


def env_fields(**over):
    base = dict(
        provenance="human_attach", subject={"isin": ISIN, "ex_date": EX},
        url="https://example.test/circular", title="Board approves demerger scheme",
        published_at="2026-10-28", published_at_basis="human_declared",
        source_class="company", relevance="entity", relevance_basis="token_match",
        excerpt="Board approves demerger scheme of arrangement", entity_match=True)
    base.update(over)
    return base


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def engine_proposal(**over):
    p = {
        "schema": "triage.proposal/1", "key": KEY, "as_of": AS_OF,
        "producer": {"kind": "engine", "name": "rules:triage-rules/1", "prompt_sha256": None,
                     "pack_sha256": "a" * 64, "self_confidence": None},
        "anomaly_kind": "suspected_jump",
        "classification": {"label": "unknown", "subtype": None, "confidence": "low", "rule_id": None,
                           "alternatives": []},
        "resolution": {"action": "none", "resolve_kind": None, "factor": None, "factor_basis": None,
                       "factor_uncertainty_pct": None, "needs_force": False, "needs_primary": False,
                       "recheck_on": None, "factor_inputs": []},
        "claims": [], "citations": [], "negative_checks": [], "missing_evidence": ["primary document"],
        "needs_human": [], "usage": {"n_fetches": 0, "tokens_in": None, "tokens_out": None, "wall_s": 0.0},
    }
    for k, v in over.items():
        p[k] = v
    return p


# ------------------------------------------------------------------ evidence.register

def test_register_takes_no_timestamp_parameter():
    params = set(inspect.signature(evidence.register).parameters)
    assert params == {"ctx", "fields"}
    for name in inspect.signature(evidence.store_body).parameters:
        assert "time" not in name and "captured" not in name


def test_register_stamps_captured_at_from_ctx_clock(world):
    with world.run(now="2026-10-30T07:08:09.123456Z") as ctx:
        reg = evidence.register(ctx, env_fields())
        conn = ctx.conn
        row = rows(conn, "SELECT * FROM captures WHERE capture_id = ?", (reg.capture_id,))[0]
    assert reg.captured_at == "2026-10-30T07:08:09.123456Z" == row["captured_at"]
    assert row["kind"] == "triage_doc" and row["source_version"] == "triage/1"
    assert row["capture_id"] == "TD-" + row["sha256"][:16]
    assert reg.reused is False


def test_register_rejects_a_smuggled_capture_time(world):
    with world.run() as ctx:
        for bad in ("captured_at", "captured", "registered_at"):
            with pytest.raises(Refused) as ei:
                evidence.register(ctx, env_fields(**{bad: "2020-01-01T00:00:00.000000Z"}))
            assert ei.value.code == "triage_envelope_invalid"
        assert rows(ctx.conn, "SELECT * FROM captures WHERE kind = 'triage_doc'") == []


def test_envelope_file_hash_equals_capture_sha256(world):
    with world.run() as ctx:
        reg = evidence.register(ctx, env_fields())
        row = rows(ctx.conn, "SELECT * FROM captures WHERE capture_id = ?", (reg.capture_id,))[0]
    path = Path(row["archive_path"])
    assert path == world.cfg.paths.archive_dir / "triage" / f"{row['sha256']}.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"] == reg.sha256
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == "triage.envelope/1"
    assert doc["published_at"] == "2026-10-28" and doc["published_at_precision"] == "date"
    assert "captured_at" not in doc


def test_reregistering_identical_envelope_reuses_the_row_and_first_observed_time(world):
    with world.run(now="2026-10-30T06:00:00.000000Z") as ctx:
        first = evidence.register(ctx, env_fields())
    with world.run(now="2026-10-30T09:00:00.000000Z") as ctx:
        again = evidence.register(ctx, env_fields())
        n = rows(ctx.conn, "SELECT count(*) AS n FROM captures WHERE kind = 'triage_doc'")[0]["n"]
    assert again.reused is True
    assert again.capture_id == first.capture_id
    assert again.captured_at == "2026-10-30T06:00:00.000000Z"
    assert n == 1


def test_a_changed_envelope_is_a_new_capture(world):
    with world.run() as ctx:
        a = evidence.register(ctx, env_fields())
        b = evidence.register(ctx, env_fields(title="Board approves something else"))
    assert a.capture_id != b.capture_id


def test_capture_is_journaled_by_trigger_not_by_library(world):
    with world.run() as ctx:
        reg = evidence.register(ctx, env_fields())
        events = rows(ctx.conn, "SELECT table_name, operation, run_id FROM ledger_events WHERE table_name = 'captures'")
        run_id = ctx.run_id
    assert events == [{"table_name": "captures", "operation": "insert", "run_id": run_id}]
    assert reg.capture_id.startswith("TD-")


def test_register_refuses_a_capture_id_that_already_names_other_content(world):
    fields = env_fields()
    sha = types.sha256_hex(types.canonical_bytes(evidence.normalise_envelope(fields)))
    with world.run() as ctx:
        ctx.conn.execute(
            "INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
            "VALUES (?, ?, 'triage_doc', 'x', ?, 'triage/1', ?)",
            ("TD-" + sha[:16], NOON, "0" * 64, ctx.run_id))
        with pytest.raises(Refused) as ei:
            evidence.register(ctx, fields)
    assert ei.value.code == "triage_capture_collision"


def test_register_heals_a_missing_envelope_file_but_refuses_a_tampered_one(world):
    with world.run() as ctx:
        reg = evidence.register(ctx, env_fields())
        path = Path(reg.archive_path)
        path.unlink()
        healed = evidence.register(ctx, env_fields())
        assert healed.reused is True and path.exists()
        path.write_bytes(path.read_bytes().replace(b"demerger", b"dividend"))
        with pytest.raises(Refused) as ei:
            evidence.register(ctx, env_fields())
    assert ei.value.code == "triage_archive_tampered"


def test_undated_envelope_keeps_null_published_at(world):
    with world.run() as ctx:
        reg = evidence.register(ctx, env_fields(published_at=None, published_at_basis=None))
        row = evidence.get_capture(ctx.conn, reg.capture_id)
        loaded = evidence.load(row)
    assert loaded.data["published_at"] is None
    assert loaded.data["published_at_basis"] is None and loaded.data["published_at_precision"] is None


@pytest.mark.parametrize("over,why", [
    ({"headline": "x" * 201}, "headline over 200 chars"),
    ({"excerpt": " ".join(["w"] * 51)}, "excerpt over 50 words"),
    ({"source_class": "rumour"}, "bad source_class"),
    ({"relevance": "gossip"}, "bad relevance"),
    ({"published_at": "2026-10-28", "published_at_basis": None}, "dated without basis"),
    ({"published_at": None, "published_at_basis": "source"}, "basis without date"),
    ({"published_at": "2026-10-28T10:00:00"}, "timestamp without timezone"),
    ({"published_at": "yesterday", "published_at_basis": "source"}, "unparseable date"),
    ({"entity_match": "yes"}, "entity_match not a bool"),
    ({"body_sha256": "abc"}, "bad body hash"),
    ({"surprise": 1}, "unknown key"),
    ({"subject": {"isin": "bad", "ex_date": EX}}, "bad isin"),
    ({"typed_fields": {"k": [1]}}, "typed field not scalar"),
])
def test_envelope_validation_refuses(world, over, why):
    with world.run() as ctx:
        with pytest.raises(Refused) as ei:
            evidence.register(ctx, env_fields(**over))
    assert ei.value.code == "triage_envelope_invalid", why


def test_timestamp_published_at_is_normalised_to_utc(world):
    with world.run() as ctx:
        reg = evidence.register(ctx, env_fields(published_at="2026-10-28T10:00:00+05:30", published_at_basis="source"))
        loaded = evidence.load(evidence.get_capture(ctx.conn, reg.capture_id))
    assert loaded.data["published_at"] == "2026-10-28T04:30:00.000000Z"
    assert loaded.data["published_at_precision"] == "timestamp"


def test_body_store_roundtrip_and_integrity(world):
    arch = world.cfg.paths.archive_dir
    sha = evidence.store_body(arch, "Board approves the demerger. Record date is 14 Oct.".encode("utf-8"))
    assert (arch / "triage_private" / sha).exists()
    assert evidence.read_body(arch, sha).status == "ok"
    assert "record date" in evidence.read_body(arch, sha).text.lower()
    assert evidence.read_body(arch, "f" * 64).status == "missing"
    (arch / "triage_private" / sha).write_bytes(b"changed")
    assert evidence.read_body(arch, sha).status == "hash_mismatch"
    bin_sha = evidence.store_body(arch, b"\xff\xfe\x00\x81")
    assert evidence.read_body(arch, bin_sha).status == "undecodable"


# ------------------------------------------------------------------ store

def _ev(n=1):
    return [{"capture_id": "TD-" + f"{i:016x}", "sha256": f"{i:064x}"} for i in range(1, n + 1)]


def test_store_writes_one_row_with_the_documented_shape(world):
    with world.run() as ctx:
        stored = store.write_proposal(ctx, engine_proposal(), [])
        row = rows(ctx.conn, "SELECT * FROM proposals WHERE proposal_id = ?", (stored.proposal_id,))[0]
    assert stored.created is True
    assert re.fullmatch(r"PT-2026-10-30-\d{3}", stored.proposal_id)
    assert row["kind"] == "other" and row["rule_id"] == "TRIAGE_RULES_V1" and row["proposed_by"] == "system"
    assert row["subject_id"] == f"triage:{KEY}" and row["status"] == "proposed" and row["as_of"] == AS_OF
    assert row["md_path"] == f"data/triage/sheets/{stored.proposal_id}.md"
    assert json.loads(row["payload_json"])["key"] == KEY
    assert row["evidence_json"] == "[]" and row["created_run_id"] == ctx.run_id


def test_store_is_idempotent_and_supersedes_only_on_change(world):
    with world.run() as ctx:
        a = store.write_proposal(ctx, engine_proposal(), [])
        b = store.write_proposal(ctx, engine_proposal(), [])
        assert b.created is False and b.proposal_id == a.proposal_id
        assert rows(ctx.conn, "SELECT count(*) AS n FROM proposals")[0]["n"] == 1
        changed = engine_proposal(missing_evidence=["primary document", "peer data"])
        c = store.write_proposal(ctx, changed, [])
        assert c.created is True and c.proposal_id != a.proposal_id and c.superseded == [a.proposal_id]
        status = {r["proposal_id"]: r["status"] for r in rows(ctx.conn, "SELECT proposal_id, status FROM proposals")}
        d = store.write_proposal(ctx, changed, [])
    assert status == {a.proposal_id: "superseded", c.proposal_id: "proposed"}
    assert d.created is False and d.proposal_id == c.proposal_id


def test_store_supersedes_within_the_same_producer_only(world):
    llm = engine_proposal()
    llm["producer"] = {"kind": "llm", "name": "llm:test-model", "prompt_sha256": None,
                       "pack_sha256": "a" * 64, "self_confidence": "high"}
    with world.run() as ctx:
        eng = store.write_proposal(ctx, engine_proposal(), [])
    with world.run(actor_kind="llm", name="test-model") as ctx:
        ag = store.write_proposal(ctx, llm, [], flags=["DISAGREES_WITH_ENGINE"])
        got = {r["proposal_id"]: r for r in rows(ctx.conn, "SELECT * FROM proposals")}
    assert got[eng.proposal_id]["status"] == "proposed"
    assert got[ag.proposal_id]["rule_id"] == "TRIAGE_AGENT_V1"
    assert got[ag.proposal_id]["proposed_by"] == "llm:test-model"
    assert got[ag.proposal_id]["llm_review"] == "DISAGREES_WITH_ENGINE"


def test_store_refuses_llm_producer_under_a_non_llm_actor(world):
    p = engine_proposal()
    p["producer"] = {"kind": "llm", "name": "llm:m", "prompt_sha256": None, "pack_sha256": "a" * 64,
                     "self_confidence": None}
    for kind in ("human", "system"):
        with world.run(actor_kind=kind) as ctx:
            with pytest.raises(Refused) as ei:
                store.write_proposal(ctx, p, [])
            assert ei.value.code == "triage_actor"
            assert rows(ctx.conn, "SELECT * FROM proposals") == []


def test_store_refuses_an_engine_proposal_from_an_llm_actor(world):
    """GOV-2: producer.kind 'engine' skips the validator, so only the engine (system) or a human may write one."""
    forged = engine_proposal(classification={"label": "demerger", "subtype": None, "confidence": "high",
                                             "rule_id": "R2", "alternatives": []})
    with world.run(actor_kind="llm", name="m") as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, forged, [])
        assert ei.value.code == "triage_actor" and "engine" in ei.value.detail
        assert rows(ctx.conn, "SELECT * FROM proposals") == []
    for kind in ("system", "human"):
        with world.run(actor_kind=kind) as ctx:
            assert store.write_proposal(ctx, engine_proposal(), []).created is True
            break


def test_store_refuses_an_llm_proposal_checked_against_another_cohorts_engine_row_and_pack(world):
    """ADV-PIT-1 at the store: the validator the store runs itself must see this cohort's engine row and pack."""
    old = "2026-09-30"
    pack = {"schema": "triage.pack/1", "as_of": old, "cutoff_utc": "2026-09-30T18:29:59.999999Z",
            "features": {"gross_factor": 0.6}, "evidence": []}
    sha, raw = store.pack_bytes(pack)
    producer = {"kind": "engine", "name": "rules:triage-rules/1", "prompt_sha256": None, "pack_sha256": sha,
                "self_confidence": None}
    with world.run(as_of=old) as ctx:
        eng = store.write_proposal(ctx, engine_proposal(as_of=old, producer=producer), [])
    pack_file = world.cfg.paths.data_dir / "triage" / "packs" / f"{eng.proposal_id}.json"
    pack_file.parent.mkdir(parents=True)
    pack_file.write_bytes(raw)
    llm = engine_proposal()
    llm["producer"] = {"kind": "llm", "name": "llm:m", "prompt_sha256": None, "pack_sha256": sha, "self_confidence": None}
    with world.run(actor_kind="llm", name="m") as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, llm, [])
        assert ei.value.code == "triage_proposal_invalid" and "V1:binding" in ei.value.detail
        assert "build for this cohort first" in ei.value.detail
        assert rows(ctx.conn, "SELECT * FROM proposals WHERE rule_id = 'TRIAGE_AGENT_V1'") == []


def test_store_refuses_a_schema_invalid_proposal(world):
    bad = engine_proposal()
    bad["classification"]["label"] = "moonshot"
    with world.run() as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, bad, [])
    assert ei.value.code == "triage_proposal_invalid"


def test_size_caps_payload_evidence_and_pack(world):
    assert (types.MAX_PAYLOAD_BYTES, types.MAX_EVIDENCE_JSON_BYTES, types.MAX_PACK_BYTES) == (8192, 2048, 12288)
    fat = engine_proposal(missing_evidence=["x" * 200] * 10, needs_human=["y" * 200] * 10)
    fat["claims"] = [{"id": f"k{i}", "type": "inference", "text": "z" * 400, "citations": [], "depends_on": []}
                     for i in range(12)]
    assert len(types.canonical_bytes(fat)) > 8192
    with world.run() as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, fat, [])
        assert ei.value.code == "triage_proposal_invalid"
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, engine_proposal(), _ev(20))
        assert ei.value.code == "triage_cap"
        assert rows(ctx.conn, "SELECT * FROM proposals") == []
        ok = store.write_proposal(ctx, engine_proposal(), _ev(6))
        ev = rows(ctx.conn, "SELECT evidence_json FROM proposals WHERE proposal_id = ?", (ok.proposal_id,))[0]
    assert len(ev["evidence_json"].encode()) <= 2048
    # pack: exactly at the cap passes, one byte more is refused
    base = {"schema": "triage.pack/1", "pad": ""}
    room = 12288 - len(types.canonical_bytes(base))
    base["pad"] = "p" * room
    assert len(types.canonical_bytes(base)) == 12288
    sha, raw = store.pack_bytes(base)
    assert len(raw) == 12288 and sha == hashlib.sha256(raw).hexdigest()
    base["pad"] += "p"
    with pytest.raises(Refused) as ei:
        store.pack_bytes(base)
    assert ei.value.code == "triage_cap"


def test_confirm_code_is_the_first_eight_hex_of_the_payload_hash(world):
    with world.run() as ctx:
        stored = store.write_proposal(ctx, engine_proposal(), [])
        row = store.get_proposal(ctx.conn, stored.proposal_id)
    assert stored.confirm == hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()[:8]
    assert store.confirm_code(row["payload_json"]) == stored.confirm
    assert row["payload"]["key"] == KEY


def test_confirm_code_matches_the_one_actions_resolve_expects():
    from quant.data import actions
    fn = getattr(actions, "proposal_confirm_code", None)
    if fn is None:
        pytest.skip("quant.data.actions.proposal_confirm_code is not present in this tree")
    for text in ('{"a":1}', "", "unicode \u20b9 payload"):
        assert store.confirm_code(text) == fn(text)


def test_store_lookup_helpers(world):
    with world.run() as ctx:
        a = store.write_proposal(ctx, engine_proposal(), [])
        other = engine_proposal(key=f"INE000000002@{EX}")
        b = store.write_proposal(ctx, other, [])
        assert [r["proposal_id"] for r in store.list_proposals(ctx.conn, isin=ISIN, ex_date=EX)] == [a.proposal_id]
        assert store.latest_proposal(ctx.conn, "INE000000002", EX, rule_id="TRIAGE_RULES_V1")["proposal_id"] == b.proposal_id
        assert store.latest_proposal(ctx.conn, "INE000000003", EX, rule_id="TRIAGE_RULES_V1") is None
        assert store.get_proposal(ctx.conn, "PT-nope") is None


# ------------------------------------------------------------------ grade(): design table 5.3, one test per cell

VT = ["demerger", "scheme", "rights"]


@pytest.mark.parametrize("label", VT)
def test_grade_value_transfer_high_is_exchange_spos_with_a_body_hash(label):
    assert grade.grade(label, rule_id="R2", has_body_hash=True) == "high"


@pytest.mark.parametrize("label", VT)
def test_grade_value_transfer_medium_is_spos_from_url_only(label):
    assert grade.grade(label, rule_id="R2", has_body_hash=False) == "medium"


@pytest.mark.parametrize("label", VT)
def test_grade_value_transfer_medium_is_r3(label):
    assert grade.grade(label, rule_id="R3") == "medium"
    assert grade.grade(label, rule_id="R3", has_body_hash=True) == "medium"


@pytest.mark.parametrize("label", VT)
def test_grade_value_transfer_low_is_price_only(label):
    assert grade.grade(label) == "low"
    assert grade.grade(label, corroborated=True, evidence_level="P") == "low"


def test_grade_genuine_high_is_primary_and_corroborated():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="P", corroborated=True) == "high"


def test_grade_genuine_medium_primary_alone():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="P") == "medium"


def test_grade_genuine_medium_sector_primary_with_corroboration():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="Ps", corroborated=True) == "medium"


def test_grade_genuine_medium_news_with_corroboration():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="N", corroborated=True) == "medium"


def test_grade_genuine_low_sector_primary_alone():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="Ps") == "low"


def test_grade_genuine_low_news_alone():
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="N") == "low"


def test_grade_genuine_low_corroboration_alone():
    assert grade.grade("genuine_event", corroborated=True) == "low"
    assert grade.grade("genuine_event") == "low"


def test_grade_genuine_low_market_stress_even_with_corroboration():
    assert grade.grade("genuine_event", rule_id="R8") == "low"
    assert grade.grade("genuine_event", rule_id="R8", corroborated=True) == "low"
    assert grade.grade("genuine_event", rule_id="R8", evidence_level="P", corroborated=True) == "low"


def test_grade_vendor_high_does_not_exist_in_v1():
    for rule in (None, "R4", "R5", "R6"):
        for body in (False, True):
            for lvl in (None, "P", "Ps", "N"):
                assert grade.grade("vendor_error_other", rule_id=rule, has_body_hash=body,
                                   evidence_level=lvl, corroborated=True) != "high"


def test_grade_vendor_medium_is_r4():
    assert grade.grade("vendor_error_other", rule_id="R4") == "medium"


@pytest.mark.parametrize("rule", ["R5", "R6", None])
def test_grade_vendor_low_is_r5_r6(rule):
    assert grade.grade("vendor_error_other", rule_id=rule) == "low"


def test_grade_pending_high_is_r1_plus_primary_doc():
    assert grade.grade("pending_listing", rule_id="R1", has_primary_doc=True) == "high"


def test_grade_pending_medium_is_r1():
    assert grade.grade("pending_listing", rule_id="R1") == "medium"


def test_grade_pending_low_without_r1():
    assert grade.grade("pending_listing") == "low"
    assert grade.grade("pending_listing", has_primary_doc=True) == "low"


@pytest.mark.parametrize("label", ["unknown", "surveillance_or_series_change", "split_or_bonus_missed_by_vendor"])
def test_grade_unlisted_labels_are_always_low(label):
    assert grade.grade(label, rule_id="R2", has_body_hash=True, evidence_level="P", corroborated=True,
                       has_primary_doc=True) == "low"


def test_grade_rejects_an_unknown_label():
    with pytest.raises(ValueError):
        grade.grade("moonshot")


def test_evidence_level_helper():
    def e(**o):
        d = {"source_class": "regulator", "published_at": "2026-10-01", "relevance": "entity", "entity_match": True}
        d.update(o)
        return d
    assert grade.evidence_level([e()]) == "P"
    assert grade.evidence_level([e(relevance="sector", entity_match=False)]) == "Ps"
    assert grade.evidence_level([e(source_class="news")]) == "N"
    assert grade.evidence_level([e(source_class="other", relevance="context")]) == "N"
    assert grade.evidence_level([e(entity_match=False)]) == "N"      # entity claimed but not token-verified
    assert grade.evidence_level([e(entity_match=None)]) == "N"
    assert grade.evidence_level([e(published_at=None)]) is None       # undated never counts
    assert grade.evidence_level([]) is None
    assert grade.evidence_level([e(source_class="news"), e(relevance="sector"), e()]) == "P"
    assert grade.evidence_level([e(source_class="news"), e(relevance="sector")]) == "Ps"


@pytest.mark.parametrize("marker", [{"provenance": "agent_attach"}, {"published_at_basis": "agent_declared"}])
def test_agent_declared_evidence_never_lifts_genuine_above_low(marker):
    # An agent chose this link's class and date, so even with peer corroboration it stays low:
    # medium prints a ready resolve command, and only a human or the engine may earn that.
    env = {"source_class": "news", "published_at": "2026-10-01", "relevance": "entity", "entity_match": True, **marker}
    assert grade.evidence_level([env]) == "A"
    assert grade.grade("genuine_event", rule_id="R7", evidence_level="A", corroborated=True) == "low"
    human = {"source_class": "news", "published_at": "2026-10-01", "relevance": "entity", "entity_match": True}
    assert grade.evidence_level([env, human]) == "N"


def test_corroboration_helper_needs_two_moving_peers_and_keeps_unknown_apart():
    assert grade.corroborated(2) and grade.corroborated(5)
    assert not grade.corroborated(1) and not grade.corroborated(0)
    assert not grade.corroborated(None)


# ------------------------------------------------------------------ static guard (design section 8)

TRIAGE_DIR = Path(__file__).resolve().parents[2] / "quant" / "triage"
FORBIDDEN_IMPORTS = ("quant.factors", "quant.model")
FORBIDDEN_NAMES = {"ledger_events", "scores", "factor_values"}
WRITE_TABLES = {"corporate_actions", "scores", "factor_values", "ledger_events"}
WRITE_SQL = re.compile(r"\b(insert|update|delete|replace)\b[^;]*\b(corporate_actions|scores|factor_values|ledger_events)\b", re.I | re.S)
WORD = re.compile(r"\b(ledger_events|scores|factor_values)\b")


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


ACTION_WRITES = ("add", "clear", "resolve")


def _actions_aliases(tree) -> set[str]:
    """Every local name bound to quant.data.actions (plain, aliased, absolute or relative import)."""
    names = {"actions"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "quant.data.actions" and a.asname:
                    names.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[-1] == "data" and (node.level > 0 or mod in ("data", "quant.data")):
                for a in node.names:
                    if a.name == "actions":
                        names.add(a.asname or a.name)
    return names


def static_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    docs = _docstring_nodes(tree)
    aliases = _actions_aliases(tree)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(FORBIDDEN_IMPORTS):
                    bad.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith(FORBIDDEN_IMPORTS):
                bad.append(f"from {mod} import")
            if mod == "quant":
                for a in node.names:
                    if a.name in ("factors", "model"):
                        bad.append(f"from quant import {a.name}")
            if node.level > 0:     # relative: `from . import factors`, `from ..model.x import y`
                head = mod.split(".")[0]
                if head in ("factors", "model"):
                    bad.append(f"from {'.' * node.level}{mod} import")
                for a in node.names:
                    if not mod and a.name in ("factors", "model"):
                        bad.append(f"from {'.' * node.level} import {a.name}")
            if mod == "quant.data.actions" or (node.level > 0 and mod == "data.actions"):
                for a in node.names:
                    if a.name in ACTION_WRITES:
                        bad.append(f"from {mod} import {a.name}")
            if any(a.name == "*" for a in node.names):
                bad.append(f"from {mod or '.'} import *")
            for a in node.names:
                if a.name == "commit":
                    bad.append("import commit")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            bad.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES:
                bad.append(f"attribute {node.attr}")
            if node.attr == "commit":
                bad.append("commit")
            if node.attr in ACTION_WRITES:
                owner = node.value
                if isinstance(owner, ast.Name) and owner.id in aliases:
                    bad.append(f"actions.{node.attr}")
                if isinstance(owner, ast.Attribute) and owner.attr == "actions":    # quant.data.actions.resolve
                    bad.append(f"actions.{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if WORD.search(node.value):
                bad.append(f"string {node.value[:40]!r}")
            if WRITE_SQL.search(node.value):
                bad.append(f"write sql {node.value[:40]!r}")
        elif isinstance(node, ast.With):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Name) and ce.id == "conn":
                    bad.append("with conn")
                if isinstance(ce, ast.Attribute) and ce.attr == "conn":
                    bad.append("with ctx.conn")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name == "commit":
                bad.append("commit(")
            if name in ("append_rows", "update_control", "executemany") and node.args:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value in WRITE_TABLES:
                        bad.append(f"{name}({arg.value!r})")
    return bad


def test_static_guard_detects_every_forbidden_pattern():
    cases = {
        "import quant.factors.registry": "import",
        "from quant.model import ew_hier": "from quant.model",
        "from quant import factors": "from quant import factors",
        "x = 'ledger_events'": "string",
        "conn.execute('INSERT INTO corporate_actions VALUES (1)')": "write sql",
        "conn.execute('update scores set x=1')": "string",
        "ctx.conn.commit()": "commit",
        "with conn:\n    pass": "with conn",
        "with ctx.conn:\n    pass": "with ctx.conn",
        "append_rows(ctx, 'corporate_actions', df, ['k'])": "append_rows",
        "update_control(ctx, 'scores', {}, {})": "update_control",
        "from quant.data.actions import resolve": "resolve",
        "actions.add(ctx)": "actions.add",
        "q = factor_values": "name",
        # bypasses found in review: relative imports, aliases, bare commit, wildcard imports
        "from . import factors": "relative factors",
        "from .. import model": "relative model",
        "from ..factors import registry": "relative factors module",
        "from ..model.ew_hier import run": "relative model module",
        "from quant.data import actions as a\na.resolve(ctx)": "alias resolve",
        "from quant.data import actions as a\na.add(ctx)": "alias add",
        "from quant.data import actions as a\na.clear(ctx)": "alias clear",
        "import quant.data.actions as act\nact.resolve(ctx)": "import alias resolve",
        "from ..data import actions as a\na.resolve(ctx)": "relative alias resolve",
        "from quant.data import actions\nactions.resolve(ctx)": "unaliased resolve",
        "quant.data.actions.resolve(ctx)": "dotted resolve",
        "commit()": "bare commit",
        "from sqlite3 import commit": "imported commit",
        "from quant.data.actions import *": "wildcard actions",
        "from quant.triage.types import *": "any wildcard",
    }
    for src, _ in cases.items():
        assert static_violations(src), f"not detected: {src}"
    assert static_violations('"""Docstring that mentions scores."""\nx = 1') == []
    assert static_violations("conn.execute('SELECT * FROM corporate_actions WHERE isin = ?', (i,))") == []
    # reads through the same module, sibling relative imports and a `commit` word in a string stay legal
    assert static_violations("from quant.data import actions as a\nx = a.detect(conn)") == []
    assert static_violations("from . import evidence\nfrom .types import LABELS") == []
    assert static_violations("msg = 'nothing to commit'") == []


def test_nothing_under_quant_triage_breaks_the_static_rules():
    files = sorted(TRIAGE_DIR.glob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "types.py", "pit.py", "evidence.py", "store.py",
                                        "validate.py", "grade.py"}
    problems = {f.name: static_violations(f.read_text(encoding="utf-8"), f.name) for f in files}
    assert {k: v for k, v in problems.items() if v} == {}


def test_an_agent_attached_primary_document_grades_as_agent_evidence_not_as_a_primary_source():
    def e(**o):
        d = {"source_class": "exchange", "published_at": "2026-10-01", "relevance": "entity", "entity_match": True,
             "provenance": "human_attach", "published_at_basis": "source"}
        d.update(o)
        return d
    assert grade.evidence_level([e()]) == "P"
    assert grade.evidence_level([e(provenance="agent_attach")]) == "A"
    assert grade.evidence_level([e(published_at_basis="agent_declared")]) == "A"
    assert grade.evidence_level([e(provenance="agent_attach", relevance="sector", entity_match=False)]) == "A"
    assert grade.is_primary(e(provenance="agent_attach")) is True          # the declared class is still the class
    assert grade.is_verified_primary(e()) is True
    assert grade.is_verified_primary(e(provenance="agent_attach")) is False
    assert grade.is_verified_primary(e(published_at_basis="agent_declared")) is False
    assert grade.is_verified_primary(e(source_class="news")) is False
