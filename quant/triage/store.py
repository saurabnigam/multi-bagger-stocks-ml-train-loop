"""Proposal store: one ``proposals`` row per proposal, no new table (design section 4).

Rows are ``kind='other'`` with ``rule_id`` ``TRIAGE_RULES_V1`` (engine) or ``TRIAGE_AGENT_V1``
(agent), ``subject_id='triage:<isin>@<ex_date>'``, the proposal as canonical JSON in
``payload_json`` (at most 8 KB) and the cited captures in ``evidence_json`` (at most 2 KB).
Writes go through the RunContext connection; the journal triggers record them. Supersession is a
control update of the older row's ``status``, never an edit of its content.

Building the same proposal twice is a no-op: a new row is written only when the payload or the
evidence list changed.

An llm proposal is never stored on trust: ``write_proposal`` runs ``validate.validate`` itself, with no
way to skip it, and stores what the validator returned (engine-derived confidence, rule, factor and
negative checks; the validated evidence list), not what the caller passed. The pack and the engine
proposal the validator needs are read as ``triage submit`` reads them, unless the caller supplies them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from quant.db.core import canonical_json, update_control
from quant.errors import Refused
from quant.triage import validate as _validate
from quant.triage.types import (
    CAPTURE_ID_RE, HEX64_RE, MAX_EVIDENCE_JSON_BYTES, MAX_PACK_BYTES, MAX_PAYLOAD_BYTES, PROPOSAL_ID_PREFIX,
    RULE_ID_AGENT, RULE_ID_ENGINE, canonical_bytes, check_schema, sha256_hex, split_key,
)

_COLUMNS = ("proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, rule_id, "
            "proposed_by, llm_review, status, decided_on, decided_by, decision_id, md_path")


@dataclass(frozen=True)
class Stored:
    proposal_id: str
    created: bool                     # False: an identical proposal already existed, nothing written
    confirm: str                      # first 8 hex of sha256(payload_json); binds a human decision to this version
    md_path: str
    superseded: list[str] = field(default_factory=list)


def subject_id(isin: str, ex_date: str) -> str:
    return f"triage:{isin}@{ex_date}"


def confirm_code(payload_json: str) -> str:
    """First 8 hex of sha256 of the stored payload (what ``actions-resolve --confirm`` expects)."""
    return sha256_hex(payload_json.encode("utf-8"))[:8]


def pack_bytes(pack: Mapping[str, Any]) -> tuple[str, bytes]:
    """Canonical bytes and sha256 of a pack; refuses a pack over the 12 KB cap."""
    raw = canonical_bytes(pack)
    if len(raw) > MAX_PACK_BYTES:
        raise Refused("triage_cap", f"pack is {len(raw)} bytes, cap {MAX_PACK_BYTES}")
    return sha256_hex(raw), raw


def _evidence_json(evidence: Iterable[Mapping[str, str]]) -> str:
    refs = []
    for item in evidence:
        if set(item) != {"capture_id", "sha256"} or not CAPTURE_ID_RE.match(str(item["capture_id"])) \
                or not HEX64_RE.match(str(item["sha256"])):
            raise Refused("triage_proposal_invalid", f"evidence entry must be {{capture_id, sha256}}: {dict(item)!r}")
        refs.append({"capture_id": item["capture_id"], "sha256": item["sha256"]})
    refs.sort(key=lambda r: r["capture_id"])
    text = canonical_json(refs)
    if len(text.encode("utf-8")) > MAX_EVIDENCE_JSON_BYTES:
        raise Refused("triage_cap", f"evidence_json is {len(text.encode('utf-8'))} bytes, cap {MAX_EVIDENCE_JSON_BYTES}")
    return text


def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _next_seq(conn: sqlite3.Connection) -> int:
    seq = 0
    for (pid,) in conn.execute("SELECT proposal_id FROM proposals WHERE proposal_id LIKE ?",
                               (PROPOSAL_ID_PREFIX + "%",)).fetchall():
        tail = str(pid).rsplit("-", 1)[-1]
        if tail.isdigit():
            seq = max(seq, int(tail))
    return seq + 1


def _load_pack(ctx: Any, engine_row: Mapping[str, Any] | None) -> tuple[Mapping[str, Any] | None, str | None]:
    """(pack, expected_sha256) of the engine proposal's pack file; the pack is None unless it exists and still hashes."""
    if engine_row is None:
        return None, None
    from quant.triage.pack import pack_path               # pack imports this module, so import it lazily

    expected = ((engine_row.get("payload") or {}).get("producer") or {}).get("pack_sha256")
    path = pack_path(ctx.cfg.paths.data_dir, engine_row["proposal_id"])
    if not path.is_file():
        return None, expected
    raw = path.read_bytes()
    if sha256_hex(raw) != expected:
        return None, expected
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, expected
    return (data if isinstance(data, dict) else None), expected


def write_proposal(ctx: Any, proposal: Mapping[str, Any], evidence: Iterable[Mapping[str, str]], *,
                   flags: Iterable[str] = (), pack: Mapping[str, Any] | None = None,
                   pack_sha256: str | None = None, engine_proposal: Mapping[str, Any] | None = None) -> Stored:
    """Write one proposal row (or return the identical existing one).

    ``proposal`` must pass ``check_schema`` for its own producer kind. An llm producer needs an llm
    actor and must pass ``validate.validate``; any failure refuses the write and nothing is stored. An engine
    producer is refused under an llm actor (the engine row is not validated, so it must not be forgeable by the agent). For an
    llm proposal the stored payload and evidence are the validator's, and ``evidence`` is not read.
    ``pack``, ``pack_sha256`` and ``engine_proposal`` default to the anomaly's current engine proposal and
    its intact pack file; with neither, the validator sees no pack (a missing input is unknown, never
    clean). A changed proposal from the same producer supersedes that producer's open row for the same
    subject. ``flags`` (for example ``DISAGREES_WITH_ENGINE``) go to ``llm_review``, together with any the
    validator raises.
    """
    producer_kind = (proposal.get("producer") or {}).get("kind") if isinstance(proposal, Mapping) else None
    if producer_kind not in ("engine", "llm"):
        raise Refused("triage_proposal_invalid", "producer.kind must be 'engine' or 'llm'")
    errors = check_schema(proposal, producer_kind=producer_kind)
    if errors:
        raise Refused("triage_proposal_invalid", "; ".join(errors))
    if producer_kind == "llm" and ctx.actor.kind != "llm":
        raise Refused("triage_actor", f"an llm proposal can only be written by an llm actor, not '{ctx.actor.kind}'")
    if producer_kind == "engine" and ctx.actor.kind not in ("system", "human"):
        # An engine row skips the validator and the rule recomputation, and reads as 'rule R2, high, from system'.
        raise Refused("triage_actor", f"an engine proposal can only be written by the engine (a system actor) or a "
                                      f"human, not '{ctx.actor.kind}'; an llm actor submits with producer.kind 'llm'")
    if producer_kind == "llm":
        isin_, ex_date_ = split_key(proposal["key"])
        if engine_proposal is None or pack is None:
            engine_row = latest_proposal(ctx.conn, isin_, ex_date_, rule_id=RULE_ID_ENGINE)
            if engine_proposal is None and engine_row is not None:
                engine_proposal = engine_row.get("payload")
            if pack is None:
                pack, expected = _load_pack(ctx, engine_row)
                pack_sha256 = pack_sha256 or expected
        verdict = _validate.validate(proposal, conn=ctx.conn, as_of=ctx.as_of, pack=pack,
                                     engine_proposal=engine_proposal, pack_sha256=pack_sha256,
                                     archive_dir=ctx.cfg.paths.archive_dir)
        if not verdict.ok:
            raise Refused("triage_proposal_invalid", "; ".join(verdict.errors))
        proposal, evidence = verdict.proposal, verdict.evidence
        flags = [*flags, *verdict.flags]

    isin, ex_date = split_key(proposal["key"])
    sid = subject_id(isin, ex_date)
    rule_id = RULE_ID_AGENT if producer_kind == "llm" else RULE_ID_ENGINE
    proposed_by = proposal["producer"]["name"] if producer_kind == "llm" else "system"
    payload_json = canonical_json(proposal)
    if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise Refused("triage_cap", f"payload is {len(payload_json.encode('utf-8'))} bytes, cap {MAX_PAYLOAD_BYTES}")
    evidence_json = _evidence_json(evidence)
    review = ",".join(sorted(set(flags))) or None

    conn = ctx.conn
    latest = _dicts(conn.execute(
        f"SELECT {_COLUMNS} FROM proposals WHERE subject_id = ? AND rule_id = ? AND status != 'superseded' "
        "ORDER BY rowid DESC LIMIT 1", (sid, rule_id)))
    if latest and latest[0]["payload_json"] == payload_json and latest[0]["evidence_json"] == evidence_json:
        row = latest[0]
        return Stored(row["proposal_id"], False, confirm_code(row["payload_json"]), row["md_path"], [])

    pid = f"{PROPOSAL_ID_PREFIX}{proposal['as_of']}-{_next_seq(conn):03d}"
    md_path = f"data/triage/sheets/{pid}.md"
    conn.execute(
        "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, "
        "rule_id, proposed_by, llm_review, status, md_path) VALUES (?, ?, ?, 'other', ?, ?, ?, ?, ?, ?, 'proposed', ?)",
        (pid, ctx.run_id, proposal["as_of"], sid, payload_json, evidence_json, rule_id, proposed_by, review, md_path),
    )
    superseded: list[str] = []
    for (old,) in conn.execute(
            "SELECT proposal_id FROM proposals WHERE subject_id = ? AND rule_id = ? AND status = 'proposed' "
            "AND proposal_id != ?", (sid, rule_id, pid)).fetchall():
        update_control(ctx, "proposals", {"proposal_id": old}, {"status": "superseded"})
        superseded.append(old)
    return Stored(pid, True, confirm_code(payload_json), md_path, superseded)


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["payload"] = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        row["payload"] = None
    try:
        row["evidence"] = json.loads(row["evidence_json"])
    except (TypeError, ValueError):
        row["evidence"] = None
    return row


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any] | None:
    rows = _dicts(conn.execute(f"SELECT {_COLUMNS} FROM proposals WHERE proposal_id = ?", (proposal_id,)))
    return _hydrate(rows[0]) if rows else None


def list_proposals(conn: sqlite3.Connection, *, isin: str | None = None, ex_date: str | None = None,
                   status: str | None = None) -> list[dict[str, Any]]:
    """Triage proposals (rule_id starting ``TRIAGE_``), oldest first, optionally for one anomaly or status."""
    sql = f"SELECT {_COLUMNS} FROM proposals WHERE rule_id LIKE 'TRIAGE\\_%' ESCAPE '\\'"
    args: list[Any] = []
    if isin is not None and ex_date is not None:
        sql += " AND subject_id = ?"
        args.append(subject_id(isin, ex_date))
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    return [_hydrate(r) for r in _dicts(conn.execute(sql + " ORDER BY rowid", args))]


def latest_proposal(conn: sqlite3.Connection, isin: str, ex_date: str, *, rule_id: str) -> dict[str, Any] | None:
    """Newest non-superseded proposal of one producer for an anomaly, or None."""
    rows = _dicts(conn.execute(
        f"SELECT {_COLUMNS} FROM proposals WHERE subject_id = ? AND rule_id = ? AND status != 'superseded' "
        "ORDER BY rowid DESC LIMIT 1", (subject_id(isin, ex_date), rule_id)))
    return _hydrate(rows[0]) if rows else None
