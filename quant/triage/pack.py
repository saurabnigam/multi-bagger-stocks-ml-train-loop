"""Assemble the evidence pack and the engine proposal for one anomaly (design 5.4, 6).

``build_one`` is pure with respect to storage: it reads the state database and the price store,
loads archived envelopes, runs the rules and returns the pack, the proposal and the evidence list.
It writes nothing. Writing is the caller's job, in the design's order: pack file first, then the
``proposals`` row (``store.write_proposal``), then the sheet.

Replayable and idempotent. The pack holds no wall-clock time. Evidence comes from ``captures`` rows
stamped at or before the cutoff (``evidence.collect_for_subject``), bars come from
``vintage_at = cutoff`` and stop at the ``as_of`` date, and every list is ordered. So building the
same anomaly twice gives the same pack hash and the same proposal, ``store.write_proposal`` then
writes no second row, and bars added later (dated later or observed later) change nothing. A new
capture or a changed feature changes the pack, and the proposal supersedes the old row.

Missing is not absent: ``source_status`` records per source whether it answered (``ok``), answered
with nothing (``empty``), failed (``error``) or was never run (``skipped``). A source that did not
run is never read as evidence that nothing exists.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from quant.errors import Refused
from quant.triage import evidence, features as feat, pit, rules
from quant.triage.store import pack_bytes
from quant.triage.types import (
    MAX_CITATIONS, MAX_EVIDENCE_JSON_BYTES, MAX_PACK_BYTES, PROVENANCE, SCHEMA_PACK, SOURCE_STATUSES, canonical_bytes,
    check_schema,
)

MAX_EVIDENCE_ITEMS = 16
_MAX_QUOTE_WORDS = 25
_REJECT_LISTED = 8


@dataclass(frozen=True)
class Built:
    key: str
    anomaly: Any
    features: feat.Features
    decision: rules.Decision
    pack: dict[str, Any]
    pack_sha256: str
    pack_raw: bytes
    proposal: dict[str, Any]
    evidence: list[dict[str, str]]          # [{capture_id, sha256}] for ``store.write_proposal``
    items: tuple[Any, ...]                  # admitted evidence items
    rejected: tuple[tuple[str, str], ...]


# ------------------------------------------------------------------ pack

def _published_key(it: Any) -> str:
    return str(it.envelope.get("published_at") or "")


def _order(items: Sequence[Any], cited: Sequence[str]) -> list[Any]:
    """Cited first, then dated before undated, newest publication first, then capture id (stable sorts, last key first)."""
    out = sorted(items, key=lambda it: it.capture_id)
    out.sort(key=_published_key, reverse=True)
    out.sort(key=lambda it: not it.decisive_ok)
    rank = {cid: i for i, cid in enumerate(cited)}
    out.sort(key=lambda it: rank.get(it.capture_id, len(rank)))
    return out


def _summary(it: Any, *, urls: bool = True, excerpt_chars: int | None = None, headline: bool = True) -> dict[str, Any]:
    env = it.envelope
    excerpt = env.get("excerpt")
    if excerpt is not None and excerpt_chars is not None:
        excerpt = excerpt[:excerpt_chars]
    out = {
        "capture_id": it.capture_id, "sha256": it.sha256, "captured_at": it.captured_at,
        "provenance": env.get("provenance"), "source_class": env.get("source_class"),
        "relevance": env.get("relevance"), "entity_match": env.get("entity_match"),
        "title": env.get("title"), "excerpt": excerpt,
        "published_at": env.get("published_at"), "published_at_basis": env.get("published_at_basis"),
        "published_at_precision": env.get("published_at_precision"),
        "typed_fields": env.get("typed_fields") or {}, "has_body": bool(env.get("body_sha256")),
        "decisive_ok": bool(it.decisive_ok),
    }
    if headline:
        out["headline"] = env.get("headline")
    if urls:
        out["url"] = (env.get("url") or None) and env["url"][:160]
    return out


def collected_status(conn: sqlite3.Connection, key: str, cutoff: str) -> dict[str, str]:
    """Per source, the state ``triage collect`` last recorded for this anomaly at or before the cutoff.

    Only ``ok`` answers leave an envelope, so an ``empty`` or ``error`` answer is otherwise invisible to the pack and
    would read as ``skipped``. Collect writes it to its run notes (``notes_json['collect'][key][source]['status']``);
    this reads it back. The latest run wins. A run that cannot be read is ignored: that source stays unknown.
    """
    try:
        rows = conn.execute("SELECT started_at, notes_json FROM runs WHERE kind = 'triage' AND notes_json IS NOT NULL "
                            "ORDER BY started_at, run_id").fetchall()
    except sqlite3.Error:
        return {}
    cut = pit.parse_instant(cutoff)
    out: dict[str, str] = {}
    for started_at, notes_json in rows:
        try:
            if pit.parse_instant(started_at) > cut:
                continue
            per_source = ((json.loads(notes_json) or {}).get("collect") or {}).get(key)
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(per_source, dict):
            continue
        for name, rec in per_source.items():
            status = rec.get("status") if isinstance(rec, dict) else None
            if name in PROVENANCE and status in SOURCE_STATUSES:
                out[name] = status
    return out


def source_status(f: feat.Features, items: Sequence[Any], rejected: Sequence[tuple[str, str]],
                  vintage: Mapping[str, Any], collected: Mapping[str, str] | None = None) -> dict[str, str]:
    """Per-source answer state. A source nobody ran, or that left nothing to record, is ``skipped``, never ``empty``.

    ``collected`` is ``collected_status``: a source that was asked and answered ``empty`` or ``error`` leaves no
    envelope, so that state comes from the collect run's notes. An archived envelope always wins.
    """
    collected = collected or {}
    out: dict[str, str] = {}
    for prov in PROVENANCE:
        envs = sorted((i for i in items if i.envelope.get("provenance") == prov), key=lambda i: (i.captured_at, i.capture_id))
        if envs:
            out[prov] = str(envs[-1].envelope.get("source_status") or "ok")
        else:
            out[prov] = collected[prov] if collected.get(prov) in ("empty", "error") else "skipped"
    if f.anomaly_kind == "index_placeholder" or f.security_id is None:
        out["price_store"] = "skipped"
    else:
        out["price_store"] = "ok" if f.gross_factor is not None and vintage.get("n_bars", 0) >= 2 else "empty"
    out["universe"] = "ok" if f.series is not None else "empty"
    out["sector_map"] = "ok" if f.n_peers else "empty"
    unreadable = [cid for cid, reason in rejected if reason in evidence.INTEGRITY_REJECTS]
    out["evidence_archive"] = "error" if unreadable else "ok"
    return out


_DETAIL_LEVELS = ((True, None, True), (False, None, True), (False, 160, True), (False, 80, False))


def _fits(pack: Mapping[str, Any], chosen: Sequence[Any]) -> bool:
    refs = [{"capture_id": i.capture_id, "sha256": i.sha256} for i in chosen]
    return len(canonical_bytes(pack)) <= MAX_PACK_BYTES and len(canonical_bytes(refs)) <= MAX_EVIDENCE_JSON_BYTES


def _fit(pack: dict[str, Any], ordered: Sequence[Any]) -> dict[str, Any]:
    """Fill ``pack['evidence']`` from the priority-ordered items within the 12 KB and 2 KB caps.

    Detail is shed before documents: URLs, then long excerpts, then headlines. Only when the
    smallest form of every document still does not fit are documents dropped from the tail, and the
    count is recorded in ``evidence_omitted``.
    """
    top = list(ordered[:MAX_EVIDENCE_ITEMS])
    for urls, excerpt_chars, headline in _DETAIL_LEVELS:
        pack["evidence"] = [_summary(i, urls=urls, excerpt_chars=excerpt_chars, headline=headline) for i in top]
        pack["evidence_omitted"] = len(ordered) - len(top)
        if _fits(pack, top):
            return pack
    for n in range(len(top) - 1, -1, -1):
        pack["evidence"] = pack["evidence"][:n]
        pack["evidence_omitted"] = len(ordered) - n
        if _fits(pack, top[:n]):
            return pack
    pack["evidence"], pack["evidence_omitted"] = [], len(ordered)
    if not _fits(pack, []):
        raise Refused("triage_pack_too_large", f"the pack exceeds {MAX_PACK_BYTES} bytes with no documents in it")
    return pack


# ------------------------------------------------------------------ proposal

def _quote(env: Mapping[str, Any]) -> str:
    text = (env.get("excerpt") or env.get("title") or "").split()
    return " ".join(text[:_MAX_QUOTE_WORDS]) or "document"


def _proposal(f: feat.Features, anomaly: Any, d: rules.Decision, pack_sha: str, by_id: Mapping[str, Any]) -> dict[str, Any]:
    cids: list[str] = []

    def cite(ref: str) -> str:
        if ref.startswith("f:"):
            return ref
        if ref not in cids:
            cids.append(ref)
        return f"c{cids.index(ref) + 1}"

    for ref in d.cited:
        cite(ref)
    claims: list[dict[str, Any]] = []
    fact_ids: list[str] = []
    for kind, text, refs in d.claims:
        cid = f"k{len(claims) + 1}"
        claims.append({"id": cid, "type": kind, "text": text, "citations": [cite(r) for r in refs][:6],
                       "depends_on": list(fact_ids) if kind == "inference" else []})
        if kind == "fact":
            fact_ids.append(cid)
    inputs = [{"name": name, "value": value, "citation": cite(ref)} for name, value, ref in d.factor_inputs]
    cids = [c for c in cids if c in by_id][:MAX_CITATIONS]
    citations = [{"id": f"c{i + 1}", "capture_id": cid, "quote": _quote(by_id[cid].envelope)} for i, cid in enumerate(cids)]
    known = {c["id"] for c in citations}
    for claim in claims:                     # a reference past the citation cap is dropped, not dangled
        claim["citations"] = [r for r in claim["citations"] if r.startswith("f:") or r in known]
        if claim["type"] == "fact" and not claim["citations"]:
            claim["type"] = "inference"
    inputs = [i for i in inputs if i["citation"].startswith("f:") or i["citation"] in known]
    return {
        "schema": "triage.proposal/1", "key": anomaly.key, "as_of": f.as_of,
        "producer": {"kind": "engine", "name": rules.ENGINE_NAME, "prompt_sha256": None, "pack_sha256": pack_sha,
                     "self_confidence": None},
        "anomaly_kind": anomaly.kind,
        "classification": {"label": d.label, "subtype": d.subtype, "confidence": d.confidence, "rule_id": d.rule_id,
                           "alternatives": [{"label": lab, "why_not": why[:200]} for lab, why in d.alternatives]},
        "resolution": {"action": d.action, "resolve_kind": d.resolve_kind,
                       "factor": None if d.factor is None else round(float(d.factor), 6),
                       "factor_basis": d.factor_basis, "factor_uncertainty_pct": d.factor_uncertainty_pct,
                       "needs_force": d.needs_force, "needs_primary": d.needs_primary, "recheck_on": d.recheck_on,
                       "factor_inputs": inputs},
        "claims": claims, "citations": citations, "negative_checks": [dict(c) for c in d.negative_checks],
        "missing_evidence": list(d.missing_evidence), "needs_human": list(d.needs_human),
        "usage": {"n_fetches": 0, "tokens_in": None, "tokens_out": None, "wall_s": 0.0},
    }


# ------------------------------------------------------------------ build

def build_one(conn: sqlite3.Connection, reader: feat.Reader, anomaly: Any, *,
              archive_dir: Path | str | None = None) -> Built:
    """Features, admissible evidence, rules, pack and engine proposal for one anomaly. Writes nothing."""
    cutoff = reader.cutoff
    f = feat.build(reader, anomaly)
    evset = evidence.collect_for_subject(conn, isin=anomaly.isin, ex_date=anomaly.ex_date, cutoff=cutoff)
    decision = rules.classify(f, evset.admitted, rejected=evset.rejected, archive_dir=archive_dir)
    ordered = _order(evset.admitted, decision.cited)
    vintage = feat.price_vintage(reader, f)
    pack: dict[str, Any] = {
        "schema": SCHEMA_PACK, "rules_version": rules.RULES_VERSION,
        "subject": {"key": anomaly.key, "isin": anomaly.isin, "ex_date": anomaly.ex_date, "symbol": anomaly.symbol,
                    "security_id": anomaly.security_id, "anomaly_kind": anomaly.kind},
        "as_of": f.as_of, "cutoff_utc": cutoff, "price_store_vintage": vintage,
        "features": feat.flat(f, evset.admitted, product_range=rules.R6_PRODUCT_RANGE),
        "source_status": source_status(f, evset.admitted, evset.rejected, vintage,
                                       collected_status(conn, anomaly.key, cutoff)),
        "rejected_evidence": [{"capture_id": cid, "reason": reason} for cid, reason in sorted(evset.rejected)[:_REJECT_LISTED]],
    }
    _fit(pack, ordered)
    sha, raw = pack_bytes(pack)
    included = {e["capture_id"] for e in pack["evidence"]}
    by_id = {i.capture_id: i for i in evset.admitted if i.capture_id in included}
    proposal = _proposal(f, anomaly, decision, sha, by_id)
    problems = check_schema(proposal, producer_kind="engine")
    if problems:
        raise Refused("triage_proposal_invalid", "engine proposal failed its own schema: " + "; ".join(problems[:5]))
    refs = [{"capture_id": e["capture_id"], "sha256": e["sha256"]} for e in pack["evidence"]]
    return Built(anomaly.key, anomaly, f, decision, pack, sha, raw, proposal, refs, tuple(evset.admitted),
                 tuple(evset.rejected))


def pack_path(data_dir: Path | str, proposal_id: str) -> Path:
    return Path(data_dir) / "triage" / "packs" / f"{proposal_id}.json"


def write_pack_file(data_dir: Path | str, proposal_id: str, built: Built) -> Path:
    """Write ``data/triage/packs/<proposal_id>.json``. Idempotent; a different file under the same id is refused."""
    path = pack_path(data_dir, proposal_id)
    if path.exists():
        if path.read_bytes() != built.pack_raw:
            raise Refused("triage_pack_conflict", f"{path} exists with different content")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(built.pack_raw)
    tmp.replace(path)
    return path
