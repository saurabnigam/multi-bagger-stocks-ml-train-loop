"""Confidence grading: design table 5.3, one function for the engine and for agent proposals.

Evidence levels: **P** primary document (exchange, regulator or company) with a ``published_at``,
entity relevance and a verified entity match; **Ps** primary document with sector relevance
declared by the attacher; **N** any other dated evidence (news, other, or a primary document
whose entity match is unverified). Undated evidence counts for nothing. A document an agent attached
(provenance ``agent_attach`` or basis ``agent_declared``) is link-only: the agent chose its source class
and date, so it is level **A**, which never grades above low even with corroboration. The agent's job is
to find documents a human then attaches; only a human or the engine earns medium. Corroboration **C2** is
at least two of the top-5 same-sector peers moving 5% or more in the stock's direction; peers are
not point-in-time, so C2 never decides alone.

A self-reported confidence is never an input here.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from quant.triage.types import LABELS, PRIMARY_SOURCE_CLASSES, VALUE_TRANSFER_LABELS

CORROBORATION_MIN_PEERS = 2


def is_primary(envelope: Mapping[str, Any]) -> bool:
    return envelope.get("source_class") in PRIMARY_SOURCE_CLASSES


def is_agent_declared(envelope: Mapping[str, Any]) -> bool:
    """The agent, not a human or the engine, supplied this envelope's class and date."""
    return envelope.get("provenance") == "agent_attach" or envelope.get("published_at_basis") == "agent_declared"


def is_verified_primary(envelope: Mapping[str, Any]) -> bool:
    """A primary-class document that a human or the engine archived. Only this earns R2, R3, P or Ps."""
    return is_primary(envelope) and not is_agent_declared(envelope)


def envelope_level(envelope: Mapping[str, Any]) -> str | None:
    """Evidence level of one envelope: ``P``, ``Ps``, ``N``, ``A`` (agent-declared) or None (undated)."""
    if envelope.get("published_at") is None:
        return None
    if is_agent_declared(envelope):
        return "A"
    if is_verified_primary(envelope):
        if envelope.get("relevance") == "entity" and envelope.get("entity_match") is True:
            return "P"
        if envelope.get("relevance") == "sector":
            return "Ps"
    return "N"


def evidence_level(envelopes: Iterable[Mapping[str, Any]]) -> str | None:
    """Best level across envelopes (P over Ps over N over A); None when nothing dated is given."""
    best: str | None = None
    order = {"P": 3, "Ps": 2, "N": 1, "A": 0}
    for env in envelopes:
        lvl = envelope_level(env)
        if lvl is not None and (best is None or order[lvl] > order[best]):
            best = lvl
    return best


def corroborated(n_peers_moved: int | None) -> bool:
    """C2. An unknown peer count (None) is not corroboration."""
    return n_peers_moved is not None and n_peers_moved >= CORROBORATION_MIN_PEERS


def grade(label: str, *, rule_id: str | None = None, evidence_level: str | None = None,
          corroborated: bool = False, has_body_hash: bool = False, has_primary_doc: bool = False) -> str:
    """Return ``high``, ``medium`` or ``low`` for a label (design table 5.3).

    ``rule_id`` is the rule that produced the proposal (``R1``..``R8``; for an agent proposal, the
    rule whose conditions the validator re-derived). ``has_body_hash`` means the deciding document
    came from ``--file`` (its body hash is on the envelope).
    """
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}")
    if label in VALUE_TRANSFER_LABELS:
        if rule_id == "R2":
            return "high" if has_body_hash else "medium"
        return "medium" if rule_id == "R3" else "low"
    if label == "genuine_event":
        if rule_id == "R8":
            return "low"
        if evidence_level == "P":
            return "high" if corroborated else "medium"
        if evidence_level in ("Ps", "N") and corroborated:
            return "medium"
        return "low"
    if label == "vendor_error_other":
        return "medium" if rule_id == "R4" else "low"
    if label == "pending_listing":
        if rule_id == "R1":
            return "high" if has_primary_doc else "medium"
        return "low"
    return "low"
