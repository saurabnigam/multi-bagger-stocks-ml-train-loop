"""Validator for agent proposals (design section 7, rules 1-10).

``validate`` is what ``triage submit`` runs. It never trusts the agent for anything the engine can
recompute: capture times come from the ``captures`` rows, the envelope files are re-hashed, quotes
are matched against the archived text, numbers must appear in a cited quote or be a cited pack
feature, the factor is recomputed, the negative check is re-run over *every* admissible document
(not only the cited ones), and confidence comes from ``grade.grade``. The agent's self-reported
confidence is stored and ignored.

Rule 7 asks for a *verified* primary document (``grade.is_verified_primary``: the class is primary and
a human or the engine archived it), exactly as the grader does. Rule 8 also requires, for a value-transfer
label (demerger, scheme, rights, a split or bonus the vendor missed), that every cited primary document has
``entity_match`` True whatever its ``relevance`` says.

Any failure refuses the whole proposal, and every independent error is reported, not only the
first. Errors are strings that start with the rule they belong to (``V1:`` ... ``V10:``).

Pack features read here (``pack["features"]``, flat): ``gross_factor``, ``n_peers_moved``,
``sector_peer_median_move_pct``, ``market_median_move_pct``, ``vendor_split_rows`` (a list; the
recorded-case name ``yahoo_split_event_in_store_pm5d`` is accepted too), ``vendor_split_candidates`` (vendor splits
within 400 days of the jump, each with ``sessions_from_jump`` and ``compensating_jump``; ``r4_row`` is the one
test of which of them R4 acts on), ``cross_section_count``, ``reversal_prior`` and ``placeholder``. ``series`` is the
security's series at the jump (a string). A missing or null feature is unknown, never zero or false:
a condition that needs it is treated as not shown, and a negative check that needs it reports
``unknown`` rather than ``none_found``.

The engine's negative check (rule 7) has three results per check: ``found``, ``none_found`` (the
engine looked with every input present and saw nothing) and ``unknown`` (an input was missing).
``genuine_event`` needs every check ``none_found``.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from quant.data.actions import JUMP_RATIO_BOUNDS
from quant.errors import Refused
from quant.triage import evidence, grade, pit
from quant.triage.types import (
    FACTOR_TOLERANCE, LABEL_RESOLVE_KINDS, MARKET_IMPLIED_UNCERTAINTY_PCT, MAX_EVIDENCE_JSON_BYTES,
    MAX_PAYLOAD_BYTES, PRIMARY_DOC_LABELS, VALUE_TRANSFER_LABELS, canonical_bytes, check_schema, is_number,
    split_key, with_defaults,
)

_V3_REASONS = {"published_at_missing", "published_at_unparseable", "published_after_cutoff",
               "published_after_capture"}
_NUM_RE = re.compile(r"(?<![A-Za-z_\d])\d+(?:,\d+)*(?:\.\d+)?")
_ENGINE_DEMERGER_RULES = ("R1", "R2", "R3", "R4")
R3_WINDOW_DAYS = 120
R3_GROSS_RANGE = (0.2, 0.9)
R3_PEER_MARKET_GAP_PCT = 3.0
R4_RATIO_TOLERANCE = 0.03
R4_MIN_SESSIONS = 2                        # a vendor split within 1 session of the jump is the vendor's own, correctly dated
R5_MIN_JUMPS = 3                           # jumps on one date across the store (rule R5; also an R7 negative check)
R5_MARKET_QUIET_PCT = 2.0                  # |market median move| under this is a quiet day
R7_WINDOW_DAYS = 5                         # a genuine-event document must be dated in [ex - 5d, ex]
INTEGER_CLAIM_TOLERANCE = 0.005            # "1" matches a non-integer feature only within 0.5%


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    proposal: dict[str, Any] | None = None      # normalised, engine-derived fields filled in (None when refused)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)   # [{capture_id, sha256}] for the store


@dataclass(frozen=True)
class _Cited:
    cid: str            # citation id in the proposal (c1)
    capture_id: str
    row: dict[str, Any]
    env: dict[str, Any]


# ------------------------------------------------------------------ numbers

def numbers_in(text: str) -> list[tuple[float, int]]:
    """Numbers written in ``text`` as (value, decimals). Digits glued to letters (Q3, FY27) are not numbers."""
    out: list[tuple[float, int]] = []
    for tok in _NUM_RE.findall(text or ""):
        clean = tok.replace(",", "")
        out.append((float(clean), len(clean.split(".")[1]) if "." in clean else 0))
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _feature_matches(value: float, written: float, decimals: int) -> bool:
    """The claim writes ``value`` to ``decimals`` places. An integer claim must be near the feature, not merely round to it."""
    if decimals == 0 and not _close(value, round(value)):
        return abs(abs(value) - abs(written)) <= INTEGER_CLAIM_TOLERANCE * abs(value)
    return _close(round(abs(value), decimals), abs(written))


# ------------------------------------------------------------------ pack feature helpers

def _num(features: Mapping[str, Any], name: str) -> float | None:
    v = features.get(name)
    return float(v) if is_number(v) else None


def _vendor_split_value(features: Mapping[str, Any]) -> Any:
    rows = features.get("vendor_split_rows")
    return features.get("yahoo_split_event_in_store_pm5d") if rows is None else rows


def vendor_split_rows(features: Mapping[str, Any]) -> list[Any]:
    """Vendor split rows near the jump. An absent feature gives [] here; ``negative_check`` tells absent from empty."""
    rows = _vendor_split_value(features)
    if isinstance(rows, Mapping):
        return [rows] if rows else []
    return list(rows) if isinstance(rows, (list, tuple)) else []


def r4_target(gross: float | None) -> float | None:
    """The split ratio R4 looks for: 1/gross for a downward jump, gross for an upward one (design D1)."""
    if gross is None or not math.isfinite(gross) or gross <= 0:
        return None
    return 1.0 / gross if gross < 1.0 else gross


def r4_row(candidates: Any, gross: float | None) -> Mapping[str, Any] | None:
    """The vendor split R4 acts on, from the pack's ``vendor_split_candidates``; None when none qualifies.

    A candidate qualifies when it is not within one session of the jump (``sessions_from_jump``), the price store shows
    no compensating jump on its date (``compensating_jump`` is exactly False: unknown is None and does not qualify), and
    its ratio is within ``R4_RATIO_TOLERANCE`` of ``r4_target``. The engine (rules R4) and the agent validator both call
    this, so they cannot disagree about what a misdated vendor split is. Nearest ratio wins, then the earlier date.
    """
    target = r4_target(gross)
    if target is None or not isinstance(candidates, (list, tuple)):
        return None
    ok = []
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        ratio, sessions = row.get("ratio"), row.get("sessions_from_jump")
        if not is_number(ratio) or ratio <= 0 or isinstance(sessions, bool) or not isinstance(sessions, int) \
                or abs(sessions) < R4_MIN_SESSIONS or row.get("compensating_jump") is not False:
            continue
        if abs(float(ratio) - target) / target <= R4_RATIO_TOLERANCE:
            ok.append(row)
    return min(ok, key=lambda r: (abs(float(r["ratio"]) - target) / target, str(r.get("date")))) if ok else None


def vendor_conflict(features: Mapping[str, Any]) -> bool:
    """A vendor-conflict feature: a vendor split (near the jump, or a qualifying R4 split months away), a cross-section of
    jumps, or a reversal."""
    xs = _num(features, "cross_section_count")
    return bool(vendor_split_rows(features)) or r4_row(features.get("vendor_split_candidates"), _num(features, "gross_factor")) is not None \
        or (xs is not None and xs >= R5_MIN_JUMPS) or features.get("reversal_prior") is True


def unreadable_captures(rejected: list[tuple[str, str]] | None) -> list[str]:
    """Capture ids whose file is missing, altered or malformed: what they say is unknown."""
    return [cid for cid, reason in rejected or [] if reason in evidence.INTEGRITY_REJECTS]


#: The checks that look for a vendor artifact in the price data rather than for a corporate action in a document.
#: R7 and the agent validator need them clean; R8 (market stress) keeps its original four blockers.
VENDOR_ARTIFACT_CHECKS = ("cross_section_jumps_present", "reversal_pair_present")


def negative_check(features: Mapping[str, Any], envelopes: list[tuple[str, Mapping[str, Any]]],
                   engine_proposal: Mapping[str, Any] | None,
                   rejected: list[tuple[str, str]] | None = None,
                   archive_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """The engine's own negative check for a ``genuine_event`` claim (design rule R7), as stored checks.

    ``envelopes`` is every admissible document for the anomaly as (capture_id, envelope), cited or not.
    ``rejected`` is ``EvidenceSet.rejected``. A capture that could not be read (file missing, hash mismatch,
    malformed) may have been a demerger document, so the keyword scan is ``unknown``, not ``none_found``.
    ``archive_dir`` lets the scan read private bodies; an envelope that names a body the scan cannot read
    (no archive directory, missing, altered, not text) also makes the keyword check ``unknown``.
    The keyword check (``demerger_keyword_in_docs``) covers demerger, scheme, record date, split, sub-division,
    bonus, rights and entitlement wording and a typed ``action_type`` other than ``genuine``.
    Two checks guard against a vendor artifact that a dated headline would otherwise hide: the same-date
    cross-section (``cross_section_jumps_present``: R5's condition) and a reversal pair (``reversal_pair_present``:
    R6's condition, either side of the jump; the forward half is unknowable on day 0 and is not required).
    Each check is ``found``, ``none_found`` or ``unknown``: a missing feature, a missing pack, a missing
    engine proposal or an unreadable document is ``unknown`` and never counts as evidence of absence (design 5.4).
    """
    scans = [(cid, evidence.scan_corporate_action(env, archive_dir)) for cid, env in envelopes]
    keyword_ids = [cid for cid, sc in scans if sc.found][:16]
    unreadable_bodies = [cid for cid, sc in scans if sc.unreadable]
    unreadable = unreadable_captures(rejected) + unreadable_bodies
    engine_rule = ((engine_proposal or {}).get("classification") or {}).get("rule_id")
    splits = _vendor_split_value(features)
    series = features.get("series")
    xs, market = _num(features, "cross_section_count"), _num(features, "market_median_move_pct")
    reversal_prior, reversal_forward = features.get("reversal_prior"), features.get("reversal_forward")

    def r(check: str, state: str, sources: list[str] | None = None) -> dict[str, Any]:
        return {"check": check, "sources": sources or [], "result": state}

    def known(flag: bool | None, unknown: bool = False) -> str:
        return "unknown" if unknown else "found" if flag else "none_found"

    cross_section = known(xs is not None and xs >= R5_MIN_JUMPS and market is not None
                          and abs(market) < R5_MARKET_QUIET_PCT,
                          xs is None or (xs >= R5_MIN_JUMPS and market is None))
    reversal = known(reversal_prior is True or reversal_forward is True,
                     not isinstance(reversal_prior, bool) and reversal_forward is not True)

    return [
        r("demerger_keyword_in_docs", known(bool(keyword_ids), bool(unreadable) and not keyword_ids),
          keyword_ids or unreadable[:16]),
        r("vendor_split_present", known(bool(vendor_split_rows(features)),
                                        not isinstance(splits, (list, tuple, Mapping)))),
        r("placeholder_present", known(features.get("placeholder") is True,
                                       not isinstance(features.get("placeholder"), bool))),
        r("series_not_eq", known(isinstance(series, str) and series != "EQ", not isinstance(series, str))),
        r("cross_section_jumps_present", cross_section),
        r("reversal_pair_present", reversal),
        r("engine_rule_r1_r4", known(engine_rule in _ENGINE_DEMERGER_RULES, engine_proposal is None)),
    ]


# ------------------------------------------------------------------ the validator

def validate(proposal: Any, *, conn: sqlite3.Connection, as_of: str, pack: Mapping[str, Any] | None,
             engine_proposal: Mapping[str, Any] | None = None, pack_sha256: str | None = None,
             archive_dir: Path | str | None = None) -> ValidationResult:
    """Run rules 1-10 over an agent proposal. ``conn`` is only read; nothing is written."""
    schema_errors = check_schema(proposal, producer_kind="llm")
    if schema_errors:
        return ValidationResult(False, schema_errors)
    p = with_defaults(proposal)
    isin, ex_date = split_key(p["key"])
    errors: list[str] = []
    notes: list[str] = []
    flags: list[str] = []

    if p["as_of"] != as_of:
        errors.append(f"V1:binding: as_of {p['as_of']} differs from the cohort {as_of}")
    if pack_sha256 is not None and p["producer"]["pack_sha256"] != pack_sha256:
        errors.append("V1:binding: producer.pack_sha256 does not match the pack this proposal was made from")
    # The pack and the engine row must belong to this cohort. Their features (vendor rows, series, placeholder,
    # peers, later bars) and the evidence they saw are those of their own cutoff; checking an agent proposal for
    # cohort B against cohort A's pack would run the engine's negative checks on stale inputs.
    build_hint = f"; build for this cohort first ('triage build --as-of {as_of} --isin {isin} --ex-date {ex_date}')"
    if pack is not None:
        if pack.get("as_of") != as_of:
            errors.append(f"V1:binding: the pack was built for cohort {pack.get('as_of')!r}, not {as_of}{build_hint}")
        elif pack.get("cutoff_utc") != pit.cutoff_utc(as_of):
            errors.append(f"V1:binding: the pack's cutoff {pack.get('cutoff_utc')!r} is not the cutoff of {as_of}{build_hint}")
    if engine_proposal is not None and engine_proposal.get("as_of") != as_of:
        errors.append(f"V1:binding: the engine proposal was built for cohort {engine_proposal.get('as_of')!r}, "
                      f"not {as_of}{build_hint}")

    cutoff = pit.cutoff_utc(as_of)
    label = p["classification"]["label"]
    features: Mapping[str, Any] = (pack or {}).get("features") or {}

    cited = _check_citations(conn, p, cutoff, label != "unknown", errors)          # rules 2 and 3
    _check_quotes(p, cited, archive_dir, errors, notes)                              # rule 4
    _check_numbers(p, features, ex_date, as_of, errors)                              # rule 5
    factor, uncertainty, needs_force = _check_factor(p, features, errors)            # rule 6
    cited_envs = [c.env for c in cited.values()]
    admissible = evidence.collect_for_subject(conn, isin=isin, ex_date=ex_date, cutoff=cutoff)
    checks = negative_check(features, [(i.capture_id, i.envelope) for i in admissible.admitted], engine_proposal,
                            admissible.rejected, archive_dir)
    unreadable = unreadable_captures(admissible.rejected)
    if unreadable:
        notes.append("evidence_unreadable:" + ",".join(unreadable[:16]))
    _check_label(p, label, cited_envs, features, checks, errors)                     # rule 7
    _check_entities(p, cited, isin, ex_date, errors, label)                          # rule 8

    # rule 9: confidence from grade(); the agent's own claim is neither read nor trusted
    res = p["resolution"]
    rule_id, has_body, has_primary = _derive_rule(label, res, factor, cited, features, ex_date, archive_dir)
    graded_envs = _r7_documents(cited, ex_date) if label == "genuine_event" else cited_envs
    peers = _num(features, "n_peers_moved")
    confidence = grade.grade(
        label, rule_id=rule_id, evidence_level=grade.evidence_level(graded_envs),
        corroborated=grade.corroborated(None if peers is None else int(peers)),
        has_body_hash=has_body, has_primary_doc=has_primary)

    # rule 10: a disagreement with the engine is stored and flagged, not refused
    if engine_proposal is None:
        notes.append("engine_proposal_absent: no engine proposal to compare the label with")
    else:
        engine_label = (engine_proposal.get("classification") or {}).get("label")
        if engine_label != label:
            flags.append("DISAGREES_WITH_ENGINE")

    if errors:
        return ValidationResult(False, errors, None, flags, notes, [])

    n = p
    n["classification"]["confidence"] = confidence
    n["classification"]["rule_id"] = rule_id
    n["resolution"].update(factor=factor, factor_uncertainty_pct=uncertainty, needs_force=needs_force,
                           needs_primary=res["action"] == "resolve" and label == "vendor_error_other")
    n["negative_checks"] = checks
    refs = sorted(({"capture_id": c.capture_id, "sha256": c.row["sha256"]} for c in cited.values()),
                  key=lambda r: r["capture_id"])
    refs = [r for i, r in enumerate(refs) if i == 0 or r != refs[i - 1]]
    size = len(canonical_bytes(n))
    if size > MAX_PAYLOAD_BYTES:
        errors.append(f"V1:schema:$: normalised payload is {size} bytes, cap {MAX_PAYLOAD_BYTES}")
    if len(canonical_bytes(refs)) > MAX_EVIDENCE_JSON_BYTES:
        errors.append(f"V1:schema:$: evidence list is over {MAX_EVIDENCE_JSON_BYTES} bytes")
    if errors:
        return ValidationResult(False, errors, None, flags, notes, [])
    return ValidationResult(True, [], n, flags, notes, refs)


# ------------------------------------------------------------------ rules 2 and 3

def _check_citations(conn: sqlite3.Connection, p: Mapping[str, Any], cutoff: str, decisive: bool,
                     errors: list[str]) -> dict[str, _Cited]:
    cited: dict[str, _Cited] = {}
    for c in p["citations"]:
        row = evidence.get_capture(conn, c["capture_id"])
        if row is None:
            errors.append(f"V2:capture_missing: citation {c['id']} names {c['capture_id']}, which is not in captures")
            continue
        try:
            loaded = evidence.load(row)
        except Refused as exc:
            errors.append(f"V2:{exc.code}: citation {c['id']} ({c['capture_id']})")
            continue
        ok, reason = pit.admissible(loaded, row, cutoff, decisive=decisive)
        if not ok:
            code = "V3" if reason in _V3_REASONS else "V2"
            errors.append(f"{code}:{reason}: citation {c['id']} ({c['capture_id']})")
            continue
        cited[c["id"]] = _Cited(c["id"], c["capture_id"], row, loaded.data)
    return cited


# ------------------------------------------------------------------ rule 4

def _check_quotes(p: Mapping[str, Any], cited: Mapping[str, _Cited], archive_dir: Path | str | None,
                  errors: list[str], notes: list[str]) -> None:
    for c in p["citations"]:
        item = cited.get(c["id"])
        if item is None:
            continue
        sources = [evidence.norm_text(s) for s in evidence.quote_sources(item.env)]
        body_sha = item.env.get("body_sha256")
        if body_sha:
            read = evidence.read_body(archive_dir, body_sha) if archive_dir is not None else None
            if read is not None and read.status == "ok":
                sources.append(evidence.norm_text(read.text or ""))
            elif read is not None and read.status == "hash_mismatch":
                errors.append(f"V4:body_tampered: citation {c['id']} ({item.capture_id}) body no longer hashes to its name")
            else:
                notes.append(f"body_unavailable:{item.capture_id}")
        quote = evidence.norm_text(c["quote"])
        if not any(evidence.quote_in(quote, s) for s in sources):
            errors.append(f"V4:quote_not_found: citation {c['id']} is not a verbatim excerpt of {item.capture_id}")


# ------------------------------------------------------------------ rule 5

def _supporting_refs(claim: Mapping[str, Any], claims: Mapping[str, Mapping[str, Any]]) -> list[str]:
    refs: list[str] = []
    seen = {claim["id"]}
    stack = [claim]
    while stack:
        cur = stack.pop()
        refs.extend(cur["citations"])
        for dep in cur["depends_on"]:
            if dep not in seen and dep in claims:
                seen.add(dep)
                stack.append(claims[dep])
    return refs


def _check_numbers(p: Mapping[str, Any], features: Mapping[str, Any], ex_date: str, as_of: str,
                   errors: list[str]) -> None:
    quote_numbers = {c["id"]: numbers_in(c["quote"]) for c in p["citations"]}
    claims = {c["id"]: c for c in p["claims"]}
    unknown_reported: set[str] = set()
    for claim in p["claims"]:
        refs = _supporting_refs(claim, claims)
        pool = [n for r in refs if r in quote_numbers for n, _ in quote_numbers[r]]
        feats: list[float] = []
        for r in refs:
            if r.startswith("f:"):
                name = r[2:]
                v = _num(features, name)
                if v is None:
                    if name not in unknown_reported:
                        unknown_reported.add(name)
                        errors.append(f"V5:unknown_feature: f:{name} is not a numeric pack feature")
                else:
                    feats.append(v)
        text = claim["text"].replace(ex_date, " ").replace(as_of, " ")
        for value, decimals in numbers_in(text):
            if any(_close(value, q) for q in pool) or any(_feature_matches(f, value, decimals) for f in feats):
                continue
            errors.append(f"V5:number_unsupported: claim {claim['id']} states {value:g}, which is in no cited quote or feature")


# ------------------------------------------------------------------ rule 6

def _input_supported(item: Mapping[str, Any], quote_numbers: Mapping[str, list[tuple[float, int]]],
                     features: Mapping[str, Any]) -> bool:
    ref, value = item["citation"], float(item["value"])
    if ref.startswith("f:"):
        v = _num(features, ref[2:])
        return v is not None and _close(v, value)
    return any(_close(value, q) for q, _ in quote_numbers.get(ref, []))


def _check_factor(p: Mapping[str, Any], features: Mapping[str, Any],
                  errors: list[str]) -> tuple[float | None, float | None, bool]:
    """Recompute the factor. Returns (factor, uncertainty_pct, needs_force); the agent's factor is only compared."""
    res = p["resolution"]
    action, kind, basis, agent = res["action"], res["resolve_kind"], res["factor_basis"], res["factor"]
    if action != "resolve":
        if agent is not None or res["factor_inputs"]:
            errors.append("V6:factor_without_resolve: a proposal that makes no resolve call carries no factor or inputs")
        return None, None, False
    if kind == "genuine":
        if basis is not None or res["factor_inputs"]:
            errors.append("V6:genuine_has_no_basis: a genuine move carries no factor_basis or factor_inputs")
        if agent is not None and not _close(agent, 1.0):
            errors.append(f"V6:factor_mismatch: genuine means a factor of exactly 1.0, not {agent:g}")
        return 1.0, None, False

    if basis is None:
        errors.append("V6:factor_basis: a resolve call needs a factor_basis")
        return None, None, False
    quote_numbers = {c["id"]: numbers_in(c["quote"]) for c in p["citations"]}
    inputs: dict[str, float] = {}
    for item in res["factor_inputs"]:
        inputs[item["name"]] = float(item["value"])
        if not _input_supported(item, quote_numbers, features):
            errors.append(f"V6:factor_input: {item['name']} {item['value']:g} is not in its cited quote or feature "
                          f"({item['citation']})")
    recomputed: float | None = None
    if basis == "exchange_spos":
        missing = [n for n in ("prev_close", "spos_price") if n not in inputs]
        if missing:
            errors.append(f"V6:factor_input: exchange_spos needs factor_inputs {missing}")
        else:
            recomputed = inputs["prev_close"] / inputs["spos_price"]
    elif basis in ("vendor_ratio", "round_ratio"):
        if "ratio" not in inputs:
            errors.append(f"V6:factor_input: {basis} needs a factor_input named ratio")
        else:
            recomputed = inputs["ratio"]
    else:  # market_implied
        gross = _num(features, "gross_factor")
        if gross is None or gross <= 0:
            errors.append("V6:gross_factor: market_implied needs a gross_factor feature in the pack")
        else:
            recomputed = 1.0 / gross
    if recomputed is None:
        return None, None, False
    if agent is not None and abs(agent - recomputed) / recomputed > FACTOR_TOLERANCE:
        errors.append(f"V6:factor_mismatch: factor {agent:g} differs from the engine recomputation "
                      f"{recomputed:.6g} by more than 0.05%")
    gross = _num(features, "gross_factor")
    lo, hi = JUMP_RATIO_BOUNDS
    needs_force = gross is not None and gross > 0 and not (lo <= gross * recomputed <= hi)
    return round(recomputed, 6), (MARKET_IMPLIED_UNCERTAINTY_PCT if basis == "market_implied" else None), needs_force


# ------------------------------------------------------------------ rules 7 and 8

def _check_label(p: Mapping[str, Any], label: str, cited_envs: list[dict[str, Any]], features: Mapping[str, Any],
                 checks: list[dict[str, Any]], errors: list[str]) -> None:
    # R-6: the same predicate as the grader. A source class the agent declared for its own link never counts.
    if label in PRIMARY_DOC_LABELS and not any(grade.is_verified_primary(e) for e in cited_envs):
        errors.append(f"V7:primary_source_required: label {label} needs a cited exchange, regulator or company "
                      "document that a human or the engine archived; a source class an agent declared for its own "
                      "link does not count")
    if label == "genuine_event":
        failed = [c["check"] for c in checks if c["result"] == "found"]
        unknown = [c["check"] for c in checks if c["result"] == "unknown"]
        if failed:
            errors.append(f"V7:negative_check_failed: genuine_event is blocked by {failed}")
        if unknown:
            errors.append(f"V7:negative_check_unknown: the engine could not run {unknown}; "
                          "a missing input is not evidence of absence")
    if label == "vendor_error_other" and not vendor_conflict(features):
        errors.append("V7:vendor_conflict_required: vendor_error_other needs a vendor split, cross-section or reversal feature")
    res = p["resolution"]
    if res["action"] == "resolve" and res["resolve_kind"] not in LABEL_RESOLVE_KINDS.get(label, ()):
        errors.append(f"V7:resolve_kind: label {label} cannot resolve as {res['resolve_kind']}")


def _check_entities(p: Mapping[str, Any], cited: Mapping[str, _Cited], isin: str, ex_date: str,
                    errors: list[str], label: str | None = None) -> None:
    for c in p["citations"]:
        item = cited.get(c["id"])
        if item is None:
            continue
        subj = item.env.get("subject") or {}
        if subj.get("isin") != isin or subj.get("ex_date") != ex_date:
            errors.append(f"V8:subject_mismatch: citation {c['id']} was archived for "
                          f"{subj.get('isin')}@{subj.get('ex_date')}, not {isin}@{ex_date}")
        if item.env.get("relevance") == "entity":
            match = item.env.get("entity_match")
            if match is False:
                errors.append(f"V8:entity_match_false: citation {c['id']} claims entity relevance but the company "
                              "name or symbol was not found in it")
            elif match is None:
                errors.append(f"V8:entity_match_unverified: citation {c['id']} claims entity relevance but was "
                              "never checked against the company name")
        elif label in PRIMARY_DOC_LABELS and grade.is_primary(item.env) and item.env.get("entity_match") is not True:
            # R-4: a value-transfer label moves a holder's value to another company, so every primary document
            # cited for it must be verified to concern this company, whatever relevance its attacher declared.
            state = "does not name the company" if item.env.get("entity_match") is False else "was never checked"
            errors.append(f"V8:entity_match_required: citation {c['id']} is a primary document cited for a "
                          f"{label} proposal, but it {state}: every primary document behind a value-transfer "
                          "label needs a verified entity match")


# ------------------------------------------------------------------ rule 9 support

def _published_day(env: Mapping[str, Any]) -> date | None:
    value = env.get("published_at")
    if value is None:
        return None
    if env.get("published_at_precision") == "date":
        return date.fromisoformat(value)
    return pit.ist_date(pit.parse_instant(value))


def _r7_documents(cited: Mapping[str, _Cited], ex_date: str) -> list[dict[str, Any]]:
    """Cited envelopes that can fire R7: entity or sector relevance, dated in [ex - 5d, ex] (design 5.2)."""
    ex = date.fromisoformat(ex_date)
    out = []
    for c in cited.values():
        day = _published_day(c.env)
        if c.env.get("relevance") in ("entity", "sector") and day is not None \
                and ex - timedelta(days=R7_WINDOW_DAYS) <= day <= ex:
            out.append(c.env)
    return out


def _derive_rule(label: str, res: Mapping[str, Any], factor: float | None, cited: Mapping[str, _Cited],
                 features: Mapping[str, Any], ex_date: str,
                 archive_dir: Path | str | None = None) -> tuple[str | None, bool, bool]:
    """(rule_id, has_body_hash, has_primary_doc): the engine rule whose conditions this proposal meets.

    None means no rule's conditions are demonstrably met, which grades low. Anything unknown in the
    pack counts as not met. Only a document a human or the engine archived is a primary document here:
    the class an agent declares for its own link is not verified. ``has_body_hash`` is true only when the
    private body exists now and still hashes to the envelope's ``body_sha256``.
    """
    primary = [c.env for c in cited.values() if grade.is_verified_primary(c.env)]
    basis = res["factor_basis"]
    if label in VALUE_TRANSFER_LABELS:
        if basis == "exchange_spos":
            inputs = {i["name"]: float(i["value"]) for i in res["factor_inputs"]}
            for env in primary:
                tf = env.get("typed_fields") or {}
                pc, sp = tf.get("prev_close"), tf.get("spos_price")
                if is_number(pc) and is_number(sp) and "prev_close" in inputs and "spos_price" in inputs \
                        and _close(float(pc), inputs["prev_close"]) and _close(float(sp), inputs["spos_price"]):
                    body = env.get("body_sha256")
                    has_body = bool(body) and archive_dir is not None \
                        and evidence.read_body(archive_dir, body).status == "ok"
                    return "R2", has_body, True
        elif basis == "market_implied":
            gross, peer, market = (_num(features, n) for n in
                                   ("gross_factor", "sector_peer_median_move_pct", "market_median_move_pct"))
            ex = date.fromisoformat(ex_date)
            if gross is not None and R3_GROSS_RANGE[0] <= gross <= R3_GROSS_RANGE[1] \
                    and not vendor_split_rows(features) and peer is not None and market is not None \
                    and abs(peer - market) < R3_PEER_MARKET_GAP_PCT:
                for env in primary:
                    day = _published_day(env)
                    if env.get("relevance") == "entity" and env.get("entity_match") is True \
                            and evidence.mentions_demerger(env, archive_dir) and day is not None \
                            and ex - timedelta(days=R3_WINDOW_DAYS) <= day <= ex:
                        return "R3", False, True
        return None, False, bool(primary)
    if label == "vendor_error_other":
        gross = _num(features, "gross_factor")
        if basis == "vendor_ratio" and gross is not None and gross > 0 and factor is not None:
            candidates = features.get("vendor_split_candidates")
            if isinstance(candidates, (list, tuple)):         # a pack from the current engine: the same test as rule R4
                met, target = r4_row(candidates, gross) is not None, r4_target(gross)
            else:                                             # an older pack or a recorded row: the five-day rows only
                met, target = bool(vendor_split_rows(features)), 1.0 / gross
            if met and abs(factor - target) / target <= R4_RATIO_TOLERANCE:
                return "R4", False, bool(primary)
        return None, False, bool(primary)
    if label == "pending_listing" and features.get("placeholder") is True:
        return "R1", False, bool(primary)
    if label == "genuine_event" and _r7_documents(cited, ex_date):
        return "R7", False, bool(primary)
    return None, False, bool(primary)
