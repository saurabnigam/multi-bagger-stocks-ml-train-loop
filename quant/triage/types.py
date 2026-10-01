"""Shared vocabulary, size caps and the ``triage.proposal/1`` schema check.

Everything here is pure. ``check_schema`` is the authority for the proposal shape (no new
dependency, so no JSON-Schema library); the JSON file under docs/operations mirrors it.
Error strings start with the validator rule they belong to (``V1:`` ... ``V10:``) so the
CLI can print them and tests can match on them.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import date
from typing import Any, Mapping

from quant.db.core import canonical_json

SCHEMA_PROPOSAL = "triage.proposal/1"
SCHEMA_PACK = "triage.pack/1"
SCHEMA_ENVELOPE = "triage.envelope/1"

CAPTURE_KIND = "triage_doc"
CAPTURE_SOURCE_VERSION = "triage/1"
CAPTURE_ID_PREFIX = "TD-"

RULE_ID_PREFIX = "TRIAGE_"
RULE_ID_ENGINE = "TRIAGE_RULES_V1"
RULE_ID_AGENT = "TRIAGE_AGENT_V1"
PROPOSAL_ID_PREFIX = "PT-"

# ------------------------------------------------------------------ size caps (design section 4, 5.4)
MAX_PAYLOAD_BYTES = 8 * 1024          # proposals.payload_json
MAX_EVIDENCE_JSON_BYTES = 2 * 1024    # proposals.evidence_json
MAX_PACK_BYTES = 12 * 1024            # data/triage/packs/<id>.json
MAX_ENVELOPE_BYTES = 8 * 1024         # archived evidence envelope
MAX_QUOTE_WORDS = 25
MAX_EXCERPT_WORDS = 50
MAX_HEADLINE_CHARS = 200
MAX_TEXT_CHARS = 200                  # why_not, missing_evidence and needs_human items
MAX_CLAIM_CHARS = 400
MAX_CLAIMS = 12
MAX_CITATIONS = 8
MAX_ALTERNATIVES = 5
MAX_NEGATIVE_CHECKS = 8
MAX_LIST_ITEMS = 10
MAX_FACTOR_INPUTS = 6
MAX_TYPED_FIELDS = 16

# ------------------------------------------------------------------ closed sets
LABELS = (
    "genuine_event", "demerger", "scheme", "rights", "split_or_bonus_missed_by_vendor",
    "vendor_error_other", "pending_listing", "surveillance_or_series_change", "unknown",
)
VALUE_TRANSFER_LABELS = ("demerger", "scheme", "rights")
PRIMARY_DOC_LABELS = VALUE_TRANSFER_LABELS + ("split_or_bonus_missed_by_vendor",)
CONFIDENCES = ("high", "medium", "low")
ACTIONS = ("resolve", "wait", "none", "recheck")
RESOLVE_KINDS = ("genuine", "demerger", "scheme", "rights", "manual_adj", "split", "bonus")
FACTOR_BASES = ("exchange_spos", "vendor_ratio", "market_implied", "round_ratio")
ANOMALY_KINDS = ("suspected_jump", "index_placeholder")
CLAIM_TYPES = ("fact", "inference")
NEGATIVE_RESULTS = ("none_found", "found", "unknown")     # unknown: the engine could not look, never "clean"
FACTOR_INPUT_NAMES = ("prev_close", "spos_price", "ratio")

#: resolve_kinds a label may carry when action is ``resolve``; labels absent from the map never resolve.
LABEL_RESOLVE_KINDS: dict[str, tuple[str, ...]] = {
    "genuine_event": ("genuine",),
    "demerger": ("demerger", "manual_adj"),
    "scheme": ("scheme",),
    "rights": ("rights",),
    "split_or_bonus_missed_by_vendor": ("split", "bonus"),
    "vendor_error_other": ("manual_adj",),          # a vendor that missed a split entirely is split_or_bonus_missed_by_vendor
}

SOURCE_CLASSES = ("exchange", "regulator", "company", "news", "other")
PRIMARY_SOURCE_CLASSES = ("exchange", "regulator", "company")
RELEVANCE = ("entity", "sector", "context")
PROVENANCE = ("yahoo_actions", "yahoo_news", "sebi_rss", "human_attach", "agent_attach")
PUBLISHED_BASES = ("source", "human_declared", "agent_declared")
PUBLISHED_PRECISIONS = ("timestamp", "date")
SOURCE_STATUSES = ("ok", "empty", "error", "skipped")

#: Words whose presence in an attached document argues against a plain ``genuine_event`` (design rule R3).
DEMERGER_KEYWORDS = ("demerger", "scheme of arrangement", "spin-off", "spin off", "spinoff", "record date")

#: Declared uncertainty of a factor taken as 1/gross (design section 6: 12% covers TMPV, VEDL, ABFRL).
MARKET_IMPLIED_UNCERTAINTY_PCT = 12.0
#: Agent factor versus engine recomputation (design section 7 rule 6).
FACTOR_TOLERANCE = 0.0005

ISIN_RE = re.compile(r"^[A-Z0-9]{12}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_ID_RE = re.compile(r"^TD-[0-9a-f]{16}$")
KEY_RE = re.compile(r"^([A-Z0-9]{12})@(\d{4}-\d{2}-\d{2})$")
_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,12}$")


# ------------------------------------------------------------------ small pure helpers

def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON bytes: the same encoding the ledger and the table hashes use."""
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def subject_key(isin: str, ex_date: str) -> str:
    return f"{isin}@{ex_date}"


def split_key(key: str) -> tuple[str, str]:
    m = KEY_RE.match(key or "")
    if not m or not valid_date(m.group(2)):
        raise ValueError(f"not an <isin>@<ex_date> key: {key!r}")
    return m.group(1), m.group(2)


def valid_date(text: Any) -> bool:
    if not isinstance(text, str) or not DATE_RE.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def word_count(text: str) -> int:
    return len(text.split())


def is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ------------------------------------------------------------------ proposal schema (rule 1, plus rule 4's length cap)

_TOP_REQUIRED = ("schema", "key", "as_of", "producer", "anomaly_kind", "classification", "resolution")
_TOP_OPTIONAL = ("claims", "citations", "negative_checks", "missing_evidence", "needs_human", "usage")


def with_defaults(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Deep copy with the optional sections present. Call after ``check_schema`` passes."""
    p = copy.deepcopy(dict(proposal))
    for name in ("claims", "citations", "negative_checks", "missing_evidence", "needs_human"):
        p.setdefault(name, [])
    p.setdefault("usage", {"n_fetches": 0, "tokens_in": None, "tokens_out": None, "wall_s": 0.0})
    return p


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, msg: str, code: str = "V1") -> None:
        self.items.append(f"{code}:schema:{path}: {msg}")


def _obj(e: _Errors, path: str, value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> bool:
    if not isinstance(value, dict):
        e.add(path, "must be an object")
        return False
    missing = [k for k in required if k not in value]
    extra = [k for k in value if k not in required and k not in optional]
    if missing:
        e.add(path, f"missing {missing}")
    if extra:
        e.add(path, f"unknown keys {extra}")
    return not missing


def _enum(e: _Errors, path: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        e.add(path, f"{value!r} not in {list(allowed)}")


def _text(e: _Errors, path: str, value: Any, cap: int, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        e.add(path, "must be a string" + (" or null" if nullable else ""))
    elif len(value) > cap:
        e.add(path, f"longer than {cap} characters")


def _str_list(e: _Errors, path: str, value: Any, cap_items: int, cap_chars: int) -> None:
    if not isinstance(value, list):
        e.add(path, "must be a list")
        return
    if len(value) > cap_items:
        e.add(path, f"more than {cap_items} items")
    for i, item in enumerate(value):
        _text(e, f"{path}[{i}]", item, cap_chars)


def check_schema(proposal: Any, *, producer_kind: str) -> list[str]:
    """Return every schema, enum and size-cap error ([] when the proposal is well formed).

    ``producer_kind`` is ``"engine"`` or ``"llm"`` and fixes the required ``producer.name`` prefix.
    Cross-checks that need the database, the pack or the archive live in ``validate``.
    """
    e = _Errors()
    if not _obj(e, "$", proposal, _TOP_REQUIRED, _TOP_OPTIONAL):
        return e.items
    p = proposal
    if p["schema"] != SCHEMA_PROPOSAL:
        e.add("schema", f"must be {SCHEMA_PROPOSAL!r}")
    if not isinstance(p["key"], str) or not KEY_RE.match(p["key"]) or not valid_date(p["key"].split("@")[-1]):
        e.add("key", "must look like <ISIN>@<YYYY-MM-DD>")
    if not valid_date(p["as_of"]):
        e.add("as_of", "must be a YYYY-MM-DD date")
    _enum(e, "anomaly_kind", p["anomaly_kind"], ANOMALY_KINDS)

    # producer
    prod = p["producer"]
    if _obj(e, "producer", prod, ("kind", "name", "pack_sha256"), ("prompt_sha256", "self_confidence")):
        _enum(e, "producer.kind", prod["kind"], ("engine", "llm"))
        if prod["kind"] != producer_kind:
            e.add("producer.kind", f"must be {producer_kind!r} for this submission")
        prefix = "llm:" if producer_kind == "llm" else "rules:"
        if not isinstance(prod["name"], str) or not prod["name"].startswith(prefix) or len(prod["name"]) > 80 \
                or len(prod["name"]) == len(prefix):
            e.add("producer.name", f"must start with {prefix!r} and be at most 80 characters")
        if not isinstance(prod["pack_sha256"], str) or not HEX64_RE.match(prod["pack_sha256"]):
            e.add("producer.pack_sha256", "must be 64 lowercase hex characters")
        ps = prod.get("prompt_sha256")
        if ps is not None and (not isinstance(ps, str) or not HEX64_RE.match(ps)):
            e.add("producer.prompt_sha256", "must be null or 64 lowercase hex characters")
        sc = prod.get("self_confidence")
        if sc is not None and not (isinstance(sc, str) and len(sc) <= 20) and not is_number(sc):
            e.add("producer.self_confidence", "must be null, a short string or a number")

    # classification
    cl = p["classification"]
    if _obj(e, "classification", cl, ("label", "confidence", "rule_id", "alternatives"), ("subtype",)):
        _enum(e, "classification.label", cl["label"], LABELS)
        _enum(e, "classification.confidence", cl["confidence"], CONFIDENCES)
        _text(e, "classification.subtype", cl.get("subtype"), 60, nullable=True)
        _text(e, "classification.rule_id", cl["rule_id"], 20, nullable=True)
        alts = cl["alternatives"]
        if not isinstance(alts, list):
            e.add("classification.alternatives", "must be a list")
        else:
            if len(alts) > MAX_ALTERNATIVES:
                e.add("classification.alternatives", f"more than {MAX_ALTERNATIVES} items")
            for i, a in enumerate(alts):
                if _obj(e, f"classification.alternatives[{i}]", a, ("label", "why_not")):
                    _enum(e, f"classification.alternatives[{i}].label", a["label"], LABELS)
                    _text(e, f"classification.alternatives[{i}].why_not", a["why_not"], MAX_TEXT_CHARS)

    # citations (ids are referenced by claims and factor inputs)
    cit_ids: set[str] = set()
    cites = p.get("citations", [])
    if not isinstance(cites, list):
        e.add("citations", "must be a list")
        cites = []
    elif len(cites) > MAX_CITATIONS:
        e.add("citations", f"more than {MAX_CITATIONS} items")
    for i, c in enumerate(cites):
        path = f"citations[{i}]"
        if not _obj(e, path, c, ("id", "capture_id", "quote")):
            continue
        if not isinstance(c["id"], str) or not _ID_RE.match(c["id"]):
            e.add(f"{path}.id", "must be 1-12 letters, digits or underscores")
        elif c["id"] in cit_ids:
            e.add(f"{path}.id", f"duplicate citation id {c['id']!r}")
        else:
            cit_ids.add(c["id"])
        if not isinstance(c["capture_id"], str) or not CAPTURE_ID_RE.match(c["capture_id"]):
            e.add(f"{path}.capture_id", "must look like TD-<16 hex>")
        if not isinstance(c["quote"], str) or not c["quote"].strip():
            e.add(f"{path}.quote", "must be a non-empty string", code="V4")
        elif word_count(c["quote"]) > MAX_QUOTE_WORDS:
            e.add(f"{path}.quote", f"longer than {MAX_QUOTE_WORDS} words", code="V4")

    def ref_ok(ref: Any) -> bool:
        return isinstance(ref, str) and (ref in cit_ids or (ref.startswith("f:") and len(ref) > 2))

    # claims
    claim_ids: set[str] = set()
    claims = p.get("claims", [])
    if not isinstance(claims, list):
        e.add("claims", "must be a list")
        claims = []
    elif len(claims) > MAX_CLAIMS:
        e.add("claims", f"more than {MAX_CLAIMS} items")
    for i, c in enumerate(claims):
        path = f"claims[{i}]"
        if not _obj(e, path, c, ("id", "type", "text", "citations", "depends_on")):
            continue
        if not isinstance(c["id"], str) or not _ID_RE.match(c["id"]):
            e.add(f"{path}.id", "must be 1-12 letters, digits or underscores")
        elif c["id"] in claim_ids:
            e.add(f"{path}.id", f"duplicate claim id {c['id']!r}")
        else:
            claim_ids.add(c["id"])
        _enum(e, f"{path}.type", c["type"], CLAIM_TYPES)
        _text(e, f"{path}.text", c["text"], MAX_CLAIM_CHARS)
        if not isinstance(c["citations"], list) or len(c["citations"]) > 6:
            e.add(f"{path}.citations", "must be a list of at most 6 references")
        else:
            for ref in c["citations"]:
                if not ref_ok(ref):
                    e.add(f"{path}.citations", f"{ref!r} is neither a citation id nor f:<feature>")
            if c["type"] == "fact" and not c["citations"]:
                e.add(f"{path}.citations", "a fact claim needs at least one citation")
        if not isinstance(c["depends_on"], list):
            e.add(f"{path}.depends_on", "must be a list of claim ids")
    for i, c in enumerate(claims):
        if isinstance(c, dict) and isinstance(c.get("depends_on"), list):
            for dep in c["depends_on"]:
                if dep not in claim_ids:
                    e.add(f"claims[{i}].depends_on", f"unknown claim id {dep!r}")

    # resolution
    res = p["resolution"]
    if _obj(e, "resolution", res,
            ("action", "resolve_kind", "factor", "factor_basis", "factor_uncertainty_pct", "needs_force",
             "needs_primary", "recheck_on", "factor_inputs")):
        _enum(e, "resolution.action", res["action"], ACTIONS)
        if res["resolve_kind"] is not None:
            _enum(e, "resolution.resolve_kind", res["resolve_kind"], RESOLVE_KINDS)
        if res["action"] == "resolve" and res["resolve_kind"] is None:
            e.add("resolution.resolve_kind", "action 'resolve' needs a resolve_kind")
        if res["action"] != "resolve" and res["resolve_kind"] is not None:
            e.add("resolution.resolve_kind", "only action 'resolve' may carry a resolve_kind")
        if res["factor"] is not None and not (is_number(res["factor"]) and res["factor"] > 0):
            e.add("resolution.factor", "must be null or a positive finite number")
        if res["factor_basis"] is not None:
            _enum(e, "resolution.factor_basis", res["factor_basis"], FACTOR_BASES)
        fu = res["factor_uncertainty_pct"]
        if fu is not None and not (is_number(fu) and fu >= 0):
            e.add("resolution.factor_uncertainty_pct", "must be null or a non-negative number")
        for flag in ("needs_force", "needs_primary"):
            if not isinstance(res[flag], bool):
                e.add(f"resolution.{flag}", "must be a boolean")
        if res["recheck_on"] is not None and not valid_date(res["recheck_on"]):
            e.add("resolution.recheck_on", "must be null or a YYYY-MM-DD date")
        fi = res["factor_inputs"]
        if not isinstance(fi, list) or len(fi) > MAX_FACTOR_INPUTS:
            e.add("resolution.factor_inputs", f"must be a list of at most {MAX_FACTOR_INPUTS} items")
        else:
            for i, item in enumerate(fi):
                path = f"resolution.factor_inputs[{i}]"
                if _obj(e, path, item, ("name", "value", "citation")):
                    _enum(e, f"{path}.name", item["name"], FACTOR_INPUT_NAMES)
                    if not (is_number(item["value"]) and item["value"] > 0):
                        e.add(f"{path}.value", "must be a positive finite number")
                    if not ref_ok(item["citation"]):
                        e.add(f"{path}.citation", f"{item['citation']!r} is neither a citation id nor f:<feature>")

    # negative checks, free-text lists, usage
    nc = p.get("negative_checks", [])
    if not isinstance(nc, list) or len(nc) > MAX_NEGATIVE_CHECKS:
        e.add("negative_checks", f"must be a list of at most {MAX_NEGATIVE_CHECKS} items")
    else:
        for i, item in enumerate(nc):
            path = f"negative_checks[{i}]"
            if _obj(e, path, item, ("check", "sources", "result")):
                _text(e, f"{path}.check", item["check"], 60)
                _enum(e, f"{path}.result", item["result"], NEGATIVE_RESULTS)
                if not isinstance(item["sources"], list) or len(item["sources"]) > MAX_CITATIONS * 2 \
                        or not all(isinstance(s, str) and CAPTURE_ID_RE.match(s) for s in item["sources"]):
                    e.add(f"{path}.sources", "must be a list of TD-<16 hex> capture ids")
    _str_list(e, "missing_evidence", p.get("missing_evidence", []), MAX_LIST_ITEMS, MAX_TEXT_CHARS)
    _str_list(e, "needs_human", p.get("needs_human", []), MAX_LIST_ITEMS, MAX_TEXT_CHARS)
    usage = p.get("usage", {})
    if _obj(e, "usage", usage, ("n_fetches", "tokens_in", "tokens_out", "wall_s")):
        for name in ("n_fetches", "tokens_in", "tokens_out"):
            v = usage[name]
            if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                e.add(f"usage.{name}", "must be null or a non-negative integer")
        if not (is_number(usage["wall_s"]) and usage["wall_s"] >= 0):
            e.add("usage.wall_s", "must be a non-negative number")

    # size cap last, on whatever parsed
    try:
        size = len(canonical_bytes(p))
    except (TypeError, ValueError):
        size = None
        e.add("$", "is not JSON serialisable")
    if size is not None and size > MAX_PAYLOAD_BYTES:
        e.add("$", f"payload is {size} bytes, cap {MAX_PAYLOAD_BYTES}")
    return e.items
