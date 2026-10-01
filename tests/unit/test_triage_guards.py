"""WP1 of evidence triage: governance guards and the read-only price store.

Triage proposes; only a human resolves a corporate action. These tests pin the code
refusals that back that rule (llm actors in ``actions.add`` / ``actions.clear``, any actor
approving a ``TRIAGE_`` proposal through ``proposals.approve``), the proposal-bound
``actions.resolve`` (``proposal_id`` + ``confirm``), ``PriceStore.open_readonly`` and the
factor code identity, which this work package must not move (amendment F3). Fixture shape
follows tests/unit/test_wp_actions_cli.py.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data import actions
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.factors import registry as factor_registry
from quant.knowledge import proposals
from quant.run import RunContext
from quant.types import Actor, FrozenClock

EVIDENCE_AT = "2026-09-11T10:06:00.000000Z"
CLOCK_AT = "2026-09-14T13:58:30.000000Z"
ISIN = "INE000000001"
EX_DATE = "2025-10-14"
GROSS = 0.6
FACTOR = 600.0 / 360.0
HUMAN = Actor(kind="human", name="owner")
LLM = Actor(kind="llm", name="llm:agent")
SYSTEM = Actor(kind="system", name="cron")


# ----------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here.

    Keeps this file well inside the triage suite's 10 s budget (amendment F7).
    """
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


_TEMPLATE: dict[str, Path] = {}


@pytest.fixture(scope="module", autouse=True)
def _state_template(tmp_path_factory):
    """One schema build for the module; every ``_world`` copies it (about 30 ms saved per test)."""
    db = tmp_path_factory.mktemp("guards_template") / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()                                                                 # WAL checkpointed: one file
    _TEMPLATE["db"] = db
    yield
    _TEMPLATE.clear()


def test_the_state_template_equals_a_fresh_apply_schema_build(tmp_path):
    """Every guard world copies the template. It must hold the schema, tables and journal triggers, a fresh build gives."""
    def schema(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return sorted((r[0], r[1], r[2], r[3]) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        finally:
            conn.close()

    fresh = tmp_path / "fresh.db"
    conn = connect(fresh)
    apply_schema(conn, kind="state")
    conn.close()
    assert schema(_TEMPLATE["db"]) == schema(fresh) and len(schema(fresh)) > 50


def _world(tmp_path):
    db = tmp_path / "state.db"
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_TEMPLATE["db"], db)
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d",
                                    archive_dir=tmp_path / "a", knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    return cfg, db


def _ctx(cfg, actor=HUMAN, as_of="2026-09-11"):
    return RunContext(as_of=as_of, kind="test", track="live", cfg=cfg, clock=FrozenClock(CLOCK_AT), actor=actor)


def _series():
    dates = pd.bdate_range("2025-06-02", "2026-09-11").strftime("%Y-%m-%d")
    close = np.full(len(dates), 600.0)
    close[list(dates).index(EX_DATE):] = 360.0
    return dates, close


def _seed_suspect(cfg):
    dates, close = _series()
    with _ctx(cfg) as ctx:
        ctx.conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, ?, 'P', '2025-01-01', '2026-09-11', 'listed')", (ISIN,))
        ctx.conn.execute(
            "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
            "VALUES (1, 'TESTCO', 'TESTCO.NS', '2025-01-01', 'test')")
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                          {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        assert actions.detect(ctx, [1], since="2025-01-01").counts["suspected"] == 1


def _payload(label="demerger", kind="demerger", factor=FACTOR) -> str:
    return json.dumps({
        "schema": "triage.proposal/1",
        "classification": {"label": label, "confidence": "medium", "rule_id": "R3"},
        "resolution": {"action": "resolve", "resolve_kind": kind, "factor": factor},
    }, sort_keys=True, separators=(",", ":"))


def _confirm(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:8]


def _add_proposal(cfg, payload_json, *, pid="PT-2026-09-01", status="proposed", kind="other",
                  rule_id="TRIAGE_RULES_V1", subject=None, proposed_by="system"):
    with _ctx(cfg, SYSTEM) as ctx:
        ctx.conn.execute(
            "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, "
            "evidence_json, rule_id, proposed_by, status, md_path) VALUES (?, ?, '2026-09-11', ?, ?, ?, '[]', ?, ?, ?, ?)",
            (pid, ctx.run_id, kind, subject or f"triage:{ISIN}@{EX_DATE}", payload_json, rule_id,
             proposed_by, status, f"data/triage/sheets/{pid}.md"))
    return pid


def _resolve(cfg, *, actor=HUMAN, kind="demerger", factor=FACTOR, **kw):
    with _ctx(cfg, actor) as ctx:
        return actions.resolve(ctx, isin=ISIN, ex_date=EX_DATE, kind=kind, factor=factor,
                               evidence="BSE filing: scheme of arrangement", **kw)


def _rows(db, sql, params=()):
    conn = connect(db, readonly=True)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _decision(cfg, *, approver_kind, status, decision_id="D-TEST-01"):
    """A decision the actor kind could legitimately hold today (the llm bridge that exists)."""
    with _ctx(cfg, SYSTEM) as ctx:
        ctx.conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
            "evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) "
            "VALUES (?, 'data_fix', 1, ?, 't', 'c', '[]', 'd', '[]', ?, ?, ?, ?, 'adr.md', 'x')",
            (decision_id, ISIN, CLOCK_AT, f"{approver_kind}:x", approver_kind, status))
    return decision_id


# ----------------------------------------------------------- add / clear refuse llm actors

def test_add_refuses_llm_actor_even_with_a_matching_provisional_decision(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    did = _decision(cfg, approver_kind="llm", status="provisional")   # passes _validate_decision today

    with pytest.raises(Refused) as exc:
        with _ctx(cfg, LLM) as ctx:
            actions.add(ctx, isin=ISIN, ex_date=EX_DATE, kind="manual_adj", factor=1.0, decision_id=did)
    assert exc.value.code == "governance"
    assert _rows(db, "SELECT count(*) FROM corporate_actions WHERE decision_id IS NOT NULL") == [(0,)]


def test_clear_refuses_llm_actor_even_with_a_matching_provisional_decision(tmp_path):
    cfg, db = _world(tmp_path)
    did = _decision(cfg, approver_kind="llm", status="provisional")
    with _ctx(cfg, SYSTEM) as ctx:
        ctx.conn.execute(
            "INSERT INTO data_quality_events (run_id, as_of, code, severity, detail_json, created_at) "
            "VALUES (?, '2026-09-11', 'SUSPECTED_CORPORATE_ACTIONS', 'WARN', '{}', ?)", (ctx.run_id, CLOCK_AT))
        event_id = ctx.conn.execute("SELECT max(event_id) FROM data_quality_events").fetchone()[0]

    with pytest.raises(Refused) as exc:
        with _ctx(cfg, LLM) as ctx:
            actions.clear(ctx, event_id=event_id, decision_id=did)
    assert exc.value.code == "governance"
    assert _rows(db, "SELECT resolved_by FROM data_quality_events WHERE event_id = ?", (event_id,)) == [(None,)]


def test_add_and_clear_still_work_for_a_human_with_a_human_decision(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    did = _decision(cfg, approver_kind="human", status="approved")
    with _ctx(cfg, HUMAN) as ctx:
        actions.add(ctx, isin=ISIN, ex_date=EX_DATE, kind="manual_adj", factor=FACTOR, decision_id=did)
    assert _rows(db, "SELECT kind, decision_id FROM corporate_actions WHERE decision_id IS NOT NULL") == \
        [("manual_adj", did)]


# ------------------------------------------------ proposals.approve refuses TRIAGE_ rules

@pytest.mark.parametrize("actor", [HUMAN, LLM, SYSTEM], ids=["human", "llm", "system"])
def test_approve_refuses_triage_proposal_for_every_actor_kind(tmp_path, actor):
    cfg, db = _world(tmp_path)
    for rule_id in ("TRIAGE_RULES_V1", "TRIAGE_AGENT_V1"):
        pid = _add_proposal(cfg, _payload(), pid=f"P-{rule_id}", rule_id=rule_id)
        with pytest.raises(Refused) as exc:
            with _ctx(cfg, actor) as ctx:
                proposals.approve(ctx, pid)
        assert exc.value.code == "governance"
        assert "TRIAGE_" in exc.value.detail
    assert _rows(db, "SELECT count(*) FROM decisions") == [(0,)]
    assert _rows(db, "SELECT status FROM proposals ORDER BY proposal_id") == [("proposed",), ("proposed",)]


def test_approve_still_works_for_a_non_triage_proposal(tmp_path):
    cfg, db = _world(tmp_path)
    pid = _add_proposal(cfg, "{}", pid="P-2026-09-09", kind="data_fix", rule_id="some_rule", subject="x")
    with _ctx(cfg, HUMAN) as ctx:
        did = proposals.approve(ctx, pid)
    assert _rows(db, "SELECT status, decision_id FROM proposals WHERE proposal_id = ?", (pid,)) == [("approved", did)]


# ------------------------------------------------ proposals.reject refuses llm actors on TRIAGE_ rules

@pytest.mark.parametrize("rule_id", ["TRIAGE_RULES_V1", "TRIAGE_AGENT_V1"])
def test_reject_refuses_an_llm_actor_on_a_triage_proposal(tmp_path, rule_id):
    cfg, db = _world(tmp_path)
    pid = _add_proposal(cfg, _payload(), rule_id=rule_id)
    with pytest.raises(Refused) as exc:
        with _ctx(cfg, LLM) as ctx:
            proposals.reject(ctx, pid, note="not a demerger")
    assert exc.value.code == "governance"
    assert "TRIAGE_" in exc.value.detail
    assert _rows(db, "SELECT count(*) FROM decisions") == [(0,)]
    assert _rows(db, "SELECT status, decision_id FROM proposals WHERE proposal_id = ?", (pid,)) == \
        [("proposed", None)]


def test_reject_refuses_every_actor_kind_on_a_triage_proposal_and_points_to_triage_reject(tmp_path):
    """GOV-3: 'kb reject' skips the reason code that 'triage reject' requires, and a system actor passed the llm-only
    guard. A triage proposal is closed only by 'triage reject' (human, reason-coded)."""
    cfg, db = _world(tmp_path)
    pids = [_add_proposal(cfg, _payload(), pid=f"P-{rule_id}", rule_id=rule_id)
            for rule_id in ("TRIAGE_RULES_V1", "TRIAGE_AGENT_V1")]
    for pid in pids:
        for actor in (HUMAN, SYSTEM, LLM):
            with pytest.raises(Refused) as exc:
                with _ctx(cfg, actor) as ctx:
                    proposals.reject(ctx, pid, note="closing it")
            assert exc.value.code == "governance", (pid, actor.kind)
            assert "TRIAGE_" in exc.value.detail and "triage reject" in exc.value.detail
    assert _rows(db, "SELECT count(*) FROM decisions") == [(0,)]
    assert _rows(db, "SELECT status, decision_id, decided_by FROM proposals ORDER BY proposal_id") == \
        [("proposed", None, None)] * 2


def test_reject_still_works_for_an_llm_actor_on_a_non_triage_proposal(tmp_path):
    cfg, db = _world(tmp_path)
    pid = _add_proposal(cfg, "{}", pid="P-2026-09-09", kind="data_fix", rule_id="some_rule", subject="x")
    with _ctx(cfg, LLM) as ctx:
        did = proposals.reject(ctx, pid)
    assert _rows(db, "SELECT status, decision_id FROM proposals WHERE proposal_id = ?", (pid,)) == [("rejected", did)]


# --------------------------------------------------- resolve bound to a proposal version

def test_confirm_code_is_first_8_hex_of_sha256_of_payload_json():
    p = _payload()
    assert actions.proposal_confirm_code(p) == _confirm(p)
    assert len(actions.proposal_confirm_code(p)) == 8


def test_resolve_with_proposal_stores_link_approves_proposal_and_records_accepted(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)

    res = _resolve(cfg, proposal_id=pid, confirm=_confirm(payload))

    did = res.details["decision_id"]
    assert res.details["proposal_id"] == pid
    assert res.details["triage_outcome"] == "accepted"
    assert _rows(db, "SELECT proposal_id, status, approver_kind FROM decisions WHERE decision_id = ?", (did,)) == \
        [(pid, "approved", "human")]
    assert _rows(db, "SELECT status, decision_id, decided_by FROM proposals WHERE proposal_id = ?", (pid,)) == \
        [("approved", did, "human:owner")]
    # the proposal status change goes through the journaled control-update path
    assert _rows(db, "SELECT count(*) FROM ledger_events WHERE table_name = 'proposals' AND operation = 'update'") == [(1,)]
    (context,) = _rows(db, "SELECT context FROM decisions WHERE decision_id = ?", (did,))[0]
    assert pid in context and "accepted" in context
    adr = pathlib.Path(res.details["adr_path"]).read_text()
    assert pid in adr and "accepted" in adr
    assert _rows(db, "SELECT kind, decision_id FROM corporate_actions WHERE decision_id IS NOT NULL") == \
        [("demerger", did)]


@pytest.mark.parametrize("factor_scale, outcome", [
    (1.00, "accepted"),
    (1.015, "accepted"),      # within 2%
    (0.985, "accepted"),
    (1.03, "modified"),       # more than 2% above the proposal
    (0.97, "modified"),       # more than 2% below the proposal
])
def test_resolve_outcome_follows_the_2_percent_factor_band(tmp_path, factor_scale, outcome):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)

    res = _resolve(cfg, factor=FACTOR * factor_scale, proposal_id=pid, confirm=_confirm(payload))

    assert res.details["triage_outcome"] == outcome
    (context,) = _rows(db, "SELECT context FROM decisions WHERE decision_id = ?", (res.details["decision_id"],))[0]
    assert outcome in context


def test_resolve_records_modified_when_the_chosen_kind_differs_from_the_proposal(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload(label="demerger", kind="demerger")
    pid = _add_proposal(cfg, payload)

    res = _resolve(cfg, kind="scheme", factor=FACTOR, proposal_id=pid, confirm=_confirm(payload))

    assert res.details["triage_outcome"] == "modified"
    (context,) = _rows(db, "SELECT context FROM decisions WHERE decision_id = ?", (res.details["decision_id"],))[0]
    assert "modified" in context and "scheme" in context


def test_resolve_genuine_matches_a_genuine_proposal_with_or_without_a_factor(tmp_path):
    for n, factor in enumerate((1.0, None)):
        cfg, db = _world(tmp_path / f"w{n}")
        _seed_suspect(cfg)
        payload = _payload(label="genuine_event", kind="genuine", factor=factor)
        pid = _add_proposal(cfg, payload)
        res = _resolve(cfg, kind="genuine", factor=1.0, proposal_id=pid, confirm=_confirm(payload))
        assert res.details["triage_outcome"] == "accepted"


def test_resolve_records_no_call_when_the_proposal_made_no_resolve_call(tmp_path):
    """An abstain/wait proposal has nothing to accept or modify: it must not inflate the override rate."""
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload(label="unknown", kind=None, factor=None)
    pid = _add_proposal(cfg, payload)
    res = _resolve(cfg, proposal_id=pid, confirm=_confirm(payload))
    assert res.details["triage_outcome"] == "no_call"
    assert _rows(db, "SELECT proposal_id FROM decisions WHERE decision_id = ?", (res.details["decision_id"],)) == [(pid,)]
    assert _rows(db, "SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("approved",)]
    context = _rows(db, "SELECT context FROM decisions WHERE decision_id = ?", (res.details["decision_id"],))[0][0]
    assert "no_call" in context and "modified" not in context


def test_resolve_confirm_is_case_insensitive(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)
    res = _resolve(cfg, proposal_id=pid, confirm=_confirm(payload).upper())
    assert res.details["triage_outcome"] == "accepted"


def _assert_nothing_written(db, pid="PT-2026-09-01", status="proposed"):
    assert _rows(db, "SELECT count(*) FROM decisions") == [(0,)]
    assert _rows(db, "SELECT count(*) FROM corporate_actions WHERE decision_id IS NOT NULL") == [(0,)]
    assert _rows(db, "SELECT status, decision_id FROM proposals WHERE proposal_id = ?", (pid,)) == [(status, None)]


def test_resolve_refuses_a_wrong_confirm_and_writes_nothing(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)
    wrong = "0" * 8 if _confirm(payload) != "0" * 8 else "f" * 8

    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid, confirm=wrong)
    assert exc.value.code == "confirm_mismatch"
    _assert_nothing_written(db)


def test_resolve_refuses_a_confirm_for_a_superseded_payload_version(tmp_path):
    """--confirm binds to the exact payload: the code of an older version does not open a newer one."""
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    old, new = _payload(factor=FACTOR), _payload(factor=FACTOR * 1.10)
    pid = _add_proposal(cfg, new)
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid, confirm=_confirm(old))
    assert exc.value.code == "confirm_mismatch"
    _assert_nothing_written(db)


def test_resolve_refuses_proposal_id_without_confirm_and_confirm_without_proposal_id(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid)
    assert exc.value.code == "confirm_required"
    with pytest.raises(Refused) as exc:
        _resolve(cfg, confirm=_confirm(payload))
    assert exc.value.code == "confirm_without_proposal"
    _assert_nothing_written(db)


@pytest.mark.parametrize("status", ["approved", "rejected", "superseded", "expired"])
def test_resolve_refuses_a_proposal_that_is_not_open(tmp_path, status):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload, status=status)
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid, confirm=_confirm(payload))
    assert exc.value.code == "proposal_not_open"
    assert _rows(db, "SELECT count(*) FROM decisions") == [(0,)]


def test_resolve_refuses_unknown_proposal_and_unreadable_payload(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id="PT-NOPE", confirm="abcd1234")
    assert exc.value.code == "proposal_missing"

    bad = "not json"
    pid = _add_proposal(cfg, bad)
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid, confirm=_confirm(bad))
    assert exc.value.code == "proposal_payload_invalid"
    _assert_nothing_written(db)


def test_resolve_refuses_a_proposal_for_another_security_or_ex_date(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload, subject=f"triage:{ISIN}@2025-10-15")
    with pytest.raises(Refused) as exc:
        _resolve(cfg, proposal_id=pid, confirm=_confirm(payload))
    assert exc.value.code == "proposal_subject_mismatch"
    _assert_nothing_written(db)


def test_resolve_with_proposal_still_refuses_non_human_actors(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    payload = _payload()
    pid = _add_proposal(cfg, payload)
    for actor in (LLM, SYSTEM):
        with pytest.raises(Refused) as exc:
            _resolve(cfg, actor=actor, proposal_id=pid, confirm=_confirm(payload))
        assert exc.value.code == "governance"
    _assert_nothing_written(db)


def test_resolve_without_proposal_is_unchanged(tmp_path):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    res = _resolve(cfg)
    did = res.details["decision_id"]
    assert _rows(db, "SELECT proposal_id FROM decisions WHERE decision_id = ?", (did,)) == [(None,)]
    assert "triage_outcome" not in res.details
    (context,) = _rows(db, "SELECT context FROM decisions WHERE decision_id = ?", (did,))[0]
    assert "accepted" not in context and "modified" not in context


# ------------------------------------------------------------------ read-only price store

def _seeded_price_db(cfg):
    _seed_suspect(cfg)
    return pathlib.Path(cfg.paths.prices_db)


def _stat(path):
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def test_open_readonly_reads_and_leaves_the_file_untouched(tmp_path):
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    before = _stat(path)

    store = PriceStore.open_readonly(path, allow_unadjusted=True)
    closes = store.close_raw([1], "2025-06-01", "2026-09-11", "2099-01-01T00:00:00.000000Z")
    assert len(closes) == len(_series()[0]) and closes[1].iloc[-1] == 360.0
    assert store.latest_date([1]) == "2026-09-11"
    assert store.split_events([1], "2025-06-01", "2026-09-11", "2099-01-01T00:00:00.000000Z") == {}
    with store.conn() as c:
        assert c.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == len(closes)

    assert _stat(path) == before
    assert sorted(p.name for p in path.parent.glob("p.db*")) == ["p.db"]       # no -journal / -wal / -shm left


def test_open_readonly_rejects_writes(tmp_path):
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    before = _stat(path)
    store = PriceStore.open_readonly(path, allow_unadjusted=True)

    with pytest.raises(sqlite3.OperationalError):
        with store.conn() as c:
            c.execute("DELETE FROM prices_daily")
    with pytest.raises(sqlite3.OperationalError):
        with store.conn() as c:
            c.execute("CREATE TABLE sneaky (x)")
    norm = pd.DataFrame({"date": ["2026-09-14"], "close": [361.0], "volume": 1.0})
    with pytest.raises(sqlite3.OperationalError):
        with _ctx(cfg) as ctx:
            store.ingest(ctx, norm, {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})

    assert _stat(path) == before
    with PriceStore.open_readonly(path, allow_unadjusted=True).conn() as c:
        assert c.execute("SELECT count(*) FROM prices_daily").fetchone()[0] == len(_series()[0])


def test_open_readonly_never_creates_tables_or_files(tmp_path):
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    before = _stat(empty)
    store = PriceStore.open_readonly(empty, allow_unadjusted=True)
    with store.conn() as c:
        assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert _stat(empty) == before

    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        PriceStore.open_readonly(missing, allow_unadjusted=True)
    assert not missing.exists()


def test_open_readonly_does_not_commit(tmp_path, monkeypatch):
    """The store's conn() context manager commits; the read-only one must not."""
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    commits = []
    real = sqlite3.connect

    class _Spy(sqlite3.Connection):
        def commit(self):
            commits.append(1)
            return super().commit()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **{**k, "factory": _Spy}))
    vintage = "2099-01-01T00:00:00.000000Z"
    PriceStore(path, state_conn=None).close_raw([1], "2025-06-01", "2026-09-11", vintage)
    assert commits, "control: the read-write store commits, so the spy sees commits"
    commits.clear()

    store = PriceStore.open_readonly(path, allow_unadjusted=True)
    store.close_raw([1], "2025-06-01", "2026-09-11", vintage)
    assert commits == []


def test_open_readonly_accepts_a_state_connection_for_corporate_actions(tmp_path):
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    state = connect(db, readonly=True)
    try:
        store = PriceStore.open_readonly(path, state_conn=state)
        acts = store.corporate_actions([1], "2099-01-01T00:00:00.000000Z")
        assert acts == {1: [{"ex_date": EX_DATE, "mode": "truncate"}]}
    finally:
        state.close()


def test_open_readonly_adjusted_series_see_the_truncate_action_only_with_a_state_connection(tmp_path):
    """Adjusted accessors depend on ``state_conn``; a store without one silently ignores every action."""
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    vintage = "2099-01-01T00:00:00.000000Z"
    args = ([1], "2025-06-01", "2026-09-11", vintage)
    full = len(_series()[0])
    ex_pos = list(_series()[0]).index(EX_DATE)

    bare = PriceStore.open_readonly(path, allow_unadjusted=True)
    assert bare.has_action_source is False
    raw_tri = bare.tri(*args)[1]
    assert raw_tri.notna().all() and raw_tri.min() == pytest.approx(60.0)   # the 40% jump reads as a real return

    state = connect(db, readonly=True)
    try:
        store = PriceStore.open_readonly(path, state_conn=state)
        assert store.has_action_source is True
        tri = store.tri(*args)
        assert len(tri) == full and tri[1].iloc[:ex_pos].isna().all()      # history before the ex-date is truncated
        assert tri[1].iloc[ex_pos:].eq(100.0).all()
    finally:
        state.close()


def test_require_action_source_fails_loudly_without_a_state_connection(tmp_path):
    cfg, db = _world(tmp_path)
    path = _seeded_price_db(cfg)
    with pytest.raises(Refused) as exc:
        PriceStore.open_readonly(path, allow_unadjusted=True).require_action_source()
    assert exc.value.code == "action_source_missing"

    state = connect(db, readonly=True)
    try:
        store = PriceStore.open_readonly(path, state_conn=state)
        assert store.require_action_source() is store
    finally:
        state.close()


# ------------------------------------------------------------------- factor code identity

# Computed from the unmodified code (git d65416d) BEFORE this work package edited any file.
# PriceStore.open_readonly and the guards in actions/proposals add code but must not move any
# factor's code identity (amendment F3): a moved hash would force a version bump of every factor.
BASELINE_FACTOR_CODE_SHA = {
    "accruals:1": "4862183ed28b130c0cae11e3c92d4f073a94e0f76f90710dfde0946dad4b4c23",
    "beta_252:1": "7efc6e76b2b873e550e3d47cb0a3aff79c710b92e2482233ba8ac631278e3067",
    "book_to_price:1": "72e2476ed437c11eeb7d77f234bff50bc1f6f966d455e749d466ced9c716cad4",
    "cash_conversion_3y:1": "e88ebadd9b63f2990103000fbc80b5a83c863b0611fa18486ce1ecc66196bae2",
    "dc_flag:1": "214dc8ba8ab01ee0bf854c0fd60e92db1e121826dde3a332dc5ce68df3752944",
    "dist_52w_high:1": "ef9f970e52024629995fc7dc5fa6043083629357cf1d1af5c6d6b33616e49a80",
    "div_yield:1": "1166e265e425cfa9186fd2cb454f05d0253cc2936028c4923f87ab1aacb77bb4",
    "earn_mom:1": "f0a1f3eb80edaf8b8d39595ba8acf9111b5d6f59c2cef29f51efaa13d290c6fb",
    "earnings_yield:1": "486176341966d4d949a3e4ceebf27d31295f4c72375c9434f3911350be210f96",
    "eps_growth_3y:1": "392498386af21e1e08073f757209e23c81c26bca2e1dafde6838bb6f4344be52",
    "fcf_yield:1": "7c5960595b51533fc1422d5860abbe4f1d5b2d26c0d0f33ec63e82e9a6d8d308",
    "inst_hold_chg_3m:1": "13c5e0da49801d6875635f75f115a6920a9e0f10bbec6932a00624a42260e503",
    "leverage:1": "11e14151c7e4fe22c4f21ffc5e7f26140beb6910f82411144d809c03b4c33e8b",
    "liq:1": "dab19ac19d1ee6d9c561efd3509885aa3df74b5fd49175ae15b1c9a13bee66bb",
    "max_ret_21:1": "92b0558ca6e69417cf3e3254ee6557eda51143730ff9dc66a3d6096a6ff71482",
    "mom_12_1:1": "33ef6d2ebd66d3e60effed87da3ca93f319817d23bb6a58b40a0e7fd12d6bdab",
    "mom_6_1:1": "fca1a76826cc0ff109f2857d412bc4b616c64231fbc09a110c09f7efa8ac5763",
    "rev_1m:1": "1396184be9bc22e0136159c9ed4086fe960cf3fe22fe14280c60aba1efc453a9",
    "rev_growth_3y:1": "ef2514d960290c699e83d893a318f5e4729d43e6d1feeda6942dfc08756374f8",
    "roce:1": "9212964c18dbc2435dc220f0efbff3d4c5b7ec3a7ded0e8aff41df5faa1dcd6b",
    "roe_stability_3y:1": "6bd51397d5df8ed82ed5d66dc28ab5f742428cbad9f83b77bc5e3ecbb71fba2c",
    "size:1": "cabeb5e82de842f4a51d6775fbe281cfced3e18f75bc95de337013fe03ffb60b",
    "trend_200:1": "d07c4d42157eade371ab79da7f14ba4a29ab3c375fed3abcc222be59eb102c42",
    "vol_252:1": "6c71717013942f9223f8db314dd4aa8a0c8d03337f71a3c49c268e49925e85e2",
}


def test_factor_code_identity_is_unchanged_for_every_launch_spec():
    specs = factor_registry.launch_specs()
    current = {f"{s.name}:{s.version}": factor_registry._calc_code_sha(s) for s in specs}
    assert set(current) == set(BASELINE_FACTOR_CODE_SHA), "launch spec set changed; recompute the baseline deliberately"
    moved = sorted(k for k in current if current[k] != BASELINE_FACTOR_CODE_SHA[k])
    assert not moved, f"factor code identity moved for {moved}"
