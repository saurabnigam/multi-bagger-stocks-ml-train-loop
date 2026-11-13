"""`kb decide`: recording the human Tier-2 rule change that enables late price admission (MASTER_SPEC R13).

The library function and the CLI command are exercised against a tmp_path state database
(tests/conftest.py already points every default path at a temp dir, so the knowledge
directory for ADRs is temporary too).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from quant.cli import main
from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.run import RunContext
from quant.types import Actor, FrozenClock

SUBJECT = "late_price_admission"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    conn = connect(path)
    apply_schema(conn, kind="state")
    conn.close()
    return path


def _decide(db, *, actor_kind="human", by="human:owner", subject=SUBJECT, as_of="2026-11-14", kind="rule_change"):
    return main(["kb", "decide", "--kind", kind, "--subject", subject, "--title", "Admit closes by trading date",
                 "--evidence", "MASTER_SPEC R13; docs/analysis addendum", "--actor-kind", actor_kind, "--by", by,
                 "--db-path", str(db), "--as-of", as_of])


def _rows(db):
    conn = connect(db, readonly=True)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM decisions WHERE kind = 'rule_change'").fetchall()]
    finally:
        conn.close()


def test_human_records_the_decision_and_its_adr(db, capsys):
    from quant.data.price_admission import enabled_decision

    assert _decide(db) == 0
    out = capsys.readouterr().out
    (row,) = _rows(db)
    assert row["decision_id"] == "D-2026-11-01" and row["decision_id"] in out
    assert (row["kind"], row["tier"], row["subject_id"], row["status"], row["approver_kind"]) == \
        ("rule_change", 2, SUBJECT, "approved", "human")
    assert row["effective_from"] == "2026-11-14" and row["decided_by"].endswith("owner")
    assert "MASTER_SPEC R13" in row["evidence_refs_json"]
    adr = Path(row["adr_path"])
    assert adr.is_file() and str(adr) in out and "Admit closes by trading date" in adr.read_text(encoding="utf-8")

    # the ADR check is clean, and the engine now sees the rule as enabled
    assert main(["kb", "check", "--db-path", str(db)]) == 0
    conn = connect(db, readonly=True)
    try:
        assert enabled_decision(conn) == row["decision_id"]
    finally:
        conn.close()


@pytest.mark.parametrize("actor_kind,by", [("llm", "llm:assistant"), ("system", "system:cli")])
def test_non_human_actors_are_refused(db, capsys, actor_kind, by):
    assert _decide(db, actor_kind=actor_kind, by=by) == 3
    assert "governance" in capsys.readouterr().err
    assert _rows(db) == []


def test_unknown_subject_is_refused(db, capsys):
    assert _decide(db, subject="gate_override_everything") == 3
    assert "late_price_admission" in capsys.readouterr().err        # names what is allowed
    assert _rows(db) == []


def test_a_second_decision_for_the_subject_is_refused_and_names_the_first(db, capsys):
    assert _decide(db) == 0
    capsys.readouterr()
    assert _decide(db, as_of="2026-11-15") == 3
    assert "D-2026-11-01" in capsys.readouterr().err
    assert len(_rows(db)) == 1


def test_missing_arguments_and_other_kinds_are_usage_errors(db, capsys):
    assert main(["kb", "decide", "--kind", "rule_change", "--actor-kind", "human", "--by", "human:o",
                 "--db-path", str(db)]) == 1
    assert _decide(db, kind="promote_factor") == 1
    assert _rows(db) == []


def test_library_refuses_non_human_and_unknown_subject(db):
    from quant.knowledge.decisions import ALLOWED_RULE_SUBJECTS, record_rule_change

    assert ALLOWED_RULE_SUBJECTS == {SUBJECT}
    cfg = load_config().with_paths(db=db)
    for kind in ("llm", "system"):
        with pytest.raises(Refused) as exc:
            with RunContext(as_of="2026-11-14", kind="decide", track="live", cfg=cfg,
                            clock=FrozenClock("2026-11-14T10:00:00.000000Z"), actor=Actor(kind=kind, name="x")) as ctx:
                record_rule_change(ctx, subject_id=SUBJECT, title="t", context="c", evidence="e")
        assert exc.value.code == "governance"
    with pytest.raises(Refused):
        with RunContext(as_of="2026-11-14", kind="decide", track="live", cfg=cfg,
                        clock=FrozenClock("2026-11-14T10:00:00.000000Z"), actor=Actor(kind="human", name="o")) as ctx:
            record_rule_change(ctx, subject_id="other", title="t", context="c", evidence="e")
    assert _rows(db) == []
