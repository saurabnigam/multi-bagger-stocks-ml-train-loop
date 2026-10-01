"""Evidence archive: envelope files plus ``captures`` rows (design sections 3, 4 and 8).

An envelope is a small JSON document (provenance, title, headline, ``published_at``, typed
fields, ...). ``register`` writes it content-addressed under ``<archive_dir>/triage/<sha256>.json``
and records one ``captures`` row (``kind='triage_doc'``, ``capture_id='TD-<sha256[:16]>'``) whose
``sha256`` is the hash of that file. Re-registering an identical envelope reuses the existing
row, so the first-observed time is kept.

``captured_at`` is ``ctx.clock`` and nothing else: no function here accepts a capture time, and
an envelope carrying a time-of-capture key is refused. Bodies (human ``--file`` only) live in a
git-ignored private directory, addressed by their hash; the envelope keeps only the hash.
Journaling is done by the state-table triggers, never by this module.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from quant.errors import Refused
from quant.triage import pit
from quant.triage.types import (
    CAPTURE_ID_PREFIX, CAPTURE_KIND, CAPTURE_SOURCE_VERSION, DEMERGER_KEYWORDS, HEX64_RE,
    ISIN_RE, MAX_ENVELOPE_BYTES, MAX_EXCERPT_WORDS, MAX_HEADLINE_CHARS, MAX_TYPED_FIELDS, PROVENANCE,
    PUBLISHED_BASES, RELEVANCE, SCHEMA_ENVELOPE, SOURCE_CLASSES, SOURCE_STATUSES, canonical_bytes,
    sha256_hex, valid_date, word_count,
)

_ENV_REQUIRED = ("provenance", "subject", "title", "source_class", "relevance")
_ENV_OPTIONAL = (
    "schema", "url", "headline", "published_at", "published_at_basis", "published_at_precision",
    "relevance_basis", "excerpt", "typed_fields", "body_sha256", "entity_match", "source_status",
)
_FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
# Reasons a capture could not be read or trusted. Its subject is then unknown, so it cannot be ruled out.
INTEGRITY_REJECTS = frozenset({"envelope_file_missing", "envelope_hash_mismatch", "envelope_invalid",
                               "captured_at_unparseable", "published_at_unparseable"})
_CAPTURE_COLUMNS = "capture_id, captured_at, kind, archive_path, sha256, source_version, run_id"


# ------------------------------------------------------------------ envelope

def normalise_envelope(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Validate caller-supplied fields and return the exact envelope that will be archived.

    Refuses unknown keys (this is what keeps a capture time out of the file), over-long text and
    an undated document that claims a publication basis. ``published_at`` is normalised; it is
    never defaulted.
    """
    errors: list[str] = []
    if not isinstance(fields, Mapping):
        raise Refused("triage_envelope_invalid", "envelope fields must be a mapping")
    missing = [k for k in _ENV_REQUIRED if k not in fields]
    extra = [k for k in fields if k not in _ENV_REQUIRED and k not in _ENV_OPTIONAL]
    if missing:
        errors.append(f"missing {missing}")
    if extra:
        errors.append(f"unknown keys {extra}")
    if missing:
        raise Refused("triage_envelope_invalid", "; ".join(errors))

    def text(name: str, cap: int, *, nullable: bool = True) -> str | None:
        v = fields.get(name)
        if v is None:
            if not nullable:
                errors.append(f"{name} is required")
            return None
        if not isinstance(v, str) or (not nullable and not v.strip()):
            errors.append(f"{name} must be a non-empty string" if not nullable else f"{name} must be a string or null")
            return None
        if len(v) > cap:
            errors.append(f"{name} longer than {cap} characters")
        return v

    if fields.get("schema", SCHEMA_ENVELOPE) != SCHEMA_ENVELOPE:
        errors.append(f"schema must be {SCHEMA_ENVELOPE!r}")
    if fields["provenance"] not in PROVENANCE:
        errors.append(f"provenance {fields['provenance']!r} not in {list(PROVENANCE)}")
    if fields["source_class"] not in SOURCE_CLASSES:
        errors.append(f"source_class {fields['source_class']!r} not in {list(SOURCE_CLASSES)}")
    if fields["relevance"] not in RELEVANCE:
        errors.append(f"relevance {fields['relevance']!r} not in {list(RELEVANCE)}")
    status = fields.get("source_status", "ok")
    if status not in SOURCE_STATUSES:
        errors.append(f"source_status {status!r} not in {list(SOURCE_STATUSES)}")

    subject = fields["subject"]
    if not isinstance(subject, Mapping) or set(subject) != {"isin", "ex_date"} \
            or not isinstance(subject["isin"], str) or not ISIN_RE.match(subject["isin"]) \
            or not valid_date(subject["ex_date"]):
        errors.append("subject must be {isin: <12 char ISIN>, ex_date: YYYY-MM-DD}")
        subject = {"isin": None, "ex_date": None}

    title = text("title", 300, nullable=False)
    headline = text("headline", MAX_HEADLINE_CHARS)
    url = text("url", 500)
    relevance_basis = text("relevance_basis", 120)
    excerpt = text("excerpt", 800)
    if excerpt is not None and word_count(excerpt) > MAX_EXCERPT_WORDS:
        errors.append(f"excerpt longer than {MAX_EXCERPT_WORDS} words")

    published = fields.get("published_at")
    basis = fields.get("published_at_basis")
    precision = fields.get("published_at_precision")
    if published is None:
        if basis is not None or precision is not None:
            errors.append("published_at_basis and published_at_precision must be null without published_at")
        basis = precision = None
    else:
        try:
            published, derived = pit.normalise_published_at(published)
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
            derived = None
        if basis not in PUBLISHED_BASES:
            errors.append(f"published_at_basis {basis!r} not in {list(PUBLISHED_BASES)} (required with published_at)")
        if precision is not None and derived is not None and precision != derived:
            errors.append(f"published_at_precision {precision!r} contradicts the value ({derived})")
        precision = derived

    typed = fields.get("typed_fields") or {}
    if not isinstance(typed, Mapping) or len(typed) > MAX_TYPED_FIELDS:
        errors.append(f"typed_fields must be an object with at most {MAX_TYPED_FIELDS} entries")
        typed = {}
    else:
        for k, v in typed.items():
            scalar = isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200)
            if not isinstance(k, str) or not _FIELD_KEY_RE.match(k) or not scalar:
                errors.append(f"typed_fields[{k!r}] must be a short string, number or boolean under a simple key")

    body = fields.get("body_sha256")
    if body is not None and not (isinstance(body, str) and HEX64_RE.match(body)):
        errors.append("body_sha256 must be null or 64 lowercase hex characters")
    match = fields.get("entity_match")
    if match is not None and not isinstance(match, bool):
        errors.append("entity_match must be null, true or false")

    if errors:
        raise Refused("triage_envelope_invalid", "; ".join(errors))
    envelope = {
        "schema": SCHEMA_ENVELOPE,
        "provenance": fields["provenance"],
        "subject": {"isin": subject["isin"], "ex_date": subject["ex_date"]},
        "url": url,
        "title": title,
        "headline": headline,
        "published_at": published,
        "published_at_basis": basis,
        "published_at_precision": precision,
        "source_class": fields["source_class"],
        "relevance": fields["relevance"],
        "relevance_basis": relevance_basis,
        "excerpt": excerpt,
        "typed_fields": dict(typed),
        "body_sha256": body,
        "entity_match": match,
        "source_status": status,
    }
    if len(canonical_bytes(envelope)) > MAX_ENVELOPE_BYTES:
        raise Refused("triage_envelope_invalid", f"envelope larger than {MAX_ENVELOPE_BYTES} bytes")
    return envelope


def norm_text(s: str) -> str:
    """Whitespace- and case-normalised text, used for quote matching."""
    return " ".join(s.casefold().split())


def quote_in(quote: str, text: str) -> bool:
    """Is ``quote`` a verbatim excerpt of ``text`` (both already normalised) on token boundaries?

    A quote that starts or ends with a letter or digit must not start or end inside a longer token or
    number: ``660.75`` is not an excerpt of ``1660.75``, and ``75`` is not one of ``660.75``.
    """
    if not quote:
        return False
    before = r"(?<![\w])(?<!\d[.,])" if quote[0].isalnum() else ""
    after = r"(?![\w])(?![.,]\d)" if quote[-1].isalnum() else ""
    return re.search(before + re.escape(quote) + after, text) is not None


def quote_sources(envelope: Mapping[str, Any]) -> list[str]:
    """The envelope fields a quote may be drawn from (besides a private body)."""
    return [s for s in (envelope.get("headline"), envelope.get("title"), envelope.get("excerpt")) if s]


#: How much of a private body the keyword scan reads (the same bound ``triage attach`` uses for the entity match).
MAX_BODY_SCAN_CHARS = 500_000

#: Wording of a corporate action other than a demerger. A document that uses it must not be read as plain news
#: (design 5.2, R7 negative check). Whole words only, so "copyrights" and "bonuses" do not match.
_ACTION_TERMS_RE = re.compile(
    r"(?<![a-z0-9])(?:split|sub[- ]?division|bonus|rights|entitlement|ex-bonus|ex-split|ex-rights)(?![a-z0-9])")


@dataclass(frozen=True)
class KeywordScan:
    """Result of scanning one document. ``unreadable`` means a body exists that could not be read, so what it says
    is unknown. ``found`` wins over ``unreadable``: one hit is enough to block a genuine call."""
    found: bool
    unreadable: bool


def scannable_text(envelope: Mapping[str, Any], archive_dir: Path | str | None) -> tuple[str, bool]:
    """(normalised text to search, body_unreadable).

    The text is headline, title and excerpt, plus the private body when the envelope names one and it reads back
    intact (at most ``MAX_BODY_SCAN_CHARS`` characters). An envelope with a ``body_sha256`` whose body cannot be
    read (no archive directory, file missing, altered, not UTF-8 text) is flagged unreadable, never treated as clean.
    """
    hay = " ".join(norm_text(s) for s in quote_sources(envelope))
    sha = envelope.get("body_sha256")
    if not sha:
        return hay, False
    read = read_body(archive_dir, sha) if archive_dir is not None else None
    if read is None or read.status != "ok":
        return hay, True
    return hay + " " + norm_text((read.text or "")[:MAX_BODY_SCAN_CHARS]), False


def scan_demerger(envelope: Mapping[str, Any], archive_dir: Path | str | None = None) -> KeywordScan:
    """Demerger wording (``DEMERGER_KEYWORDS``) in the text fields and the private body (rule R3's trigger)."""
    hay, unreadable = scannable_text(envelope, archive_dir)
    found = any(k in hay for k in DEMERGER_KEYWORDS)
    return KeywordScan(found, unreadable and not found)


def scan_corporate_action(envelope: Mapping[str, Any], archive_dir: Path | str | None = None) -> KeywordScan:
    """Any corporate-action wording: demerger terms, split, sub-division, bonus, rights, entitlement, or a typed
    ``action_type`` other than ``genuine``. This is the R7 negative check; R3 keeps ``scan_demerger``."""
    hay, unreadable = scannable_text(envelope, archive_dir)
    typed = (envelope.get("typed_fields") or {}).get("action_type")
    found = any(k in hay for k in DEMERGER_KEYWORDS) or _ACTION_TERMS_RE.search(hay) is not None \
        or (isinstance(typed, str) and typed.strip().casefold() not in ("", "genuine"))
    return KeywordScan(found, unreadable and not found)


def mentions_demerger(envelope: Mapping[str, Any], archive_dir: Path | str | None = None) -> bool:
    return scan_demerger(envelope, archive_dir).found


# ------------------------------------------------------------------ archive and capture rows

@dataclass(frozen=True)
class Registered:
    capture_id: str
    sha256: str
    captured_at: str
    archive_path: str
    reused: bool


@dataclass(frozen=True)
class LoadedEnvelope:
    data: dict[str, Any]
    sha256: str          # of the file bytes as found on disk, not of the parsed content
    path: Path


@dataclass(frozen=True)
class EvidenceItem:
    capture_id: str
    sha256: str
    captured_at: str     # from the captures row
    envelope: dict[str, Any]
    decisive_ok: bool    # has a published_at that passes the point-in-time checks
    reason: str          # 'ok' or 'ok_undated'


@dataclass(frozen=True)
class EvidenceSet:
    admitted: list[EvidenceItem]
    rejected: list[tuple[str, str]]     # (capture_id, reason)


@dataclass(frozen=True)
class BodyRead:
    text: str | None
    status: str          # ok | missing | hash_mismatch | undecodable


def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def get_capture(conn: sqlite3.Connection, capture_id: str) -> dict[str, Any] | None:
    rows = _dicts(conn.execute(f"SELECT {_CAPTURE_COLUMNS} FROM captures WHERE capture_id = ?", (capture_id,)))
    return rows[0] if rows else None


def _write_content_addressed(path: Path, raw: bytes, sha: str) -> None:
    if path.exists():
        if sha256_hex(path.read_bytes()) != sha:
            raise Refused("triage_archive_tampered", f"{path} exists but does not hash to its own name")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def register(ctx: Any, fields: Mapping[str, Any]) -> Registered:
    """Archive one envelope and record (or reuse) its capture row. ``captured_at`` is ``ctx.clock``.

    Order: file first (content-addressed, idempotent), row second. An orphan file is harmless.
    """
    envelope = normalise_envelope(fields)
    raw = canonical_bytes(envelope)
    sha = sha256_hex(raw)
    capture_id = CAPTURE_ID_PREFIX + sha[:16]
    path = Path(ctx.cfg.paths.archive_dir) / "triage" / f"{sha}.json"
    _write_content_addressed(path, raw, sha)

    existing = get_capture(ctx.conn, capture_id)
    if existing is not None:
        if existing["sha256"] != sha:
            raise Refused("triage_capture_collision", f"{capture_id} already names different content")
        return Registered(capture_id, sha, existing["captured_at"], existing["archive_path"], True)
    captured_at = ctx.clock.iso()
    ctx.conn.execute(
        f"INSERT INTO captures ({_CAPTURE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (capture_id, captured_at, CAPTURE_KIND, str(path), sha, CAPTURE_SOURCE_VERSION, ctx.run_id),
    )
    return Registered(capture_id, sha, captured_at, str(path), False)


def load(row: Mapping[str, Any]) -> LoadedEnvelope:
    """Read the archived envelope a capture row points at. Hash checking is ``pit.admissible``'s job."""
    path = Path(row["archive_path"])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise Refused("envelope_file_missing", f"{path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = {}
    return LoadedEnvelope(data if isinstance(data, dict) else {}, sha256_hex(raw), path)


def collect_for_subject(conn: sqlite3.Connection, *, isin: str, ex_date: str, cutoff: str) -> EvidenceSet:
    """Every envelope admissible for ``isin@ex_date`` at ``cutoff``, plus why the others were not.

    Undated envelopes are admitted as context (``decisive_ok=False``). A capture stamped after the
    cutoff is never loaded, so later edits to its file cannot matter. Items are ordered by capture
    time, then id.
    """
    cur = conn.execute(
        f"SELECT {_CAPTURE_COLUMNS} FROM captures WHERE kind = ? ORDER BY captured_at, capture_id",
        (CAPTURE_KIND,),
    )
    cut = pit.parse_instant(cutoff)
    admitted: list[EvidenceItem] = []
    rejected: list[tuple[str, str]] = []
    for row in _dicts(cur):
        cid = row["capture_id"]
        try:
            captured = pit.parse_instant(row["captured_at"])
        except ValueError:
            rejected.append((cid, "captured_at_unparseable"))
            continue
        if captured > cut:
            rejected.append((cid, "captured_after_cutoff"))
            continue
        try:
            loaded = load(row)
        except Refused as exc:
            rejected.append((cid, exc.code))
            continue
        ok, reason = pit.admissible(loaded, row, cutoff)
        if not ok:
            rejected.append((cid, reason))
            continue
        subj = loaded.data.get("subject") or {}
        if subj.get("isin") != isin or subj.get("ex_date") != ex_date:
            continue
        decisive_ok, _ = pit.admissible(loaded, row, cutoff, decisive=True)
        admitted.append(EvidenceItem(cid, row["sha256"], row["captured_at"], loaded.data, decisive_ok, reason))
    return EvidenceSet(admitted, rejected)


# ------------------------------------------------------------------ private bodies

def _body_path(archive_dir: Path | str, sha: str) -> Path:
    return Path(archive_dir) / "triage_private" / sha


def store_body(archive_dir: Path | str, data: bytes) -> str:
    """Copy a document body to the private directory (content-addressed) and return its hash."""
    sha = sha256_hex(data)
    path = _body_path(archive_dir, sha)
    _write_content_addressed(path, data, sha)
    return sha


def read_body(archive_dir: Path | str, sha: str) -> BodyRead:
    """Read a private body, checking it still hashes to its name and decodes as UTF-8 text."""
    if not isinstance(sha, str) or not HEX64_RE.match(sha):
        return BodyRead(None, "missing")
    path = _body_path(archive_dir, sha)
    try:
        raw = path.read_bytes()
    except OSError:
        return BodyRead(None, "missing")
    if sha256_hex(raw) != sha:
        return BodyRead(None, "hash_mismatch")
    try:
        return BodyRead(raw.decode("utf-8"), "ok")
    except UnicodeDecodeError:
        return BodyRead(None, "undecodable")
