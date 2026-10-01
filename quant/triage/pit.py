"""Point-in-time admissibility of triage evidence (design section 8).

One function, :func:`admissible`, decides whether an archived document may inform a cohort. It is
used by ``build``, ``submit`` and ``show``. The capture time comes only from the ``captures`` row
(never from a pack, a proposal or the envelope file), the envelope file must hash to the row's
``sha256``, and ``published_at`` is never defaulted: a missing date leaves the document as
context and blocks any decisive claim.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from quant.errors import Refused
from quant.triage.types import CAPTURE_KIND, DATE_RE, SCHEMA_ENVELOPE, canonical_bytes, sha256_hex, valid_date

IST = ZoneInfo("Asia/Kolkata")


def cutoff_utc(as_of: str) -> str:
    """The cohort's knowledge cutoff: as_of 23:59:59.999999 IST, as a UTC ISO string."""
    from quant.run import knowledge_cutoff

    return knowledge_cutoff(as_of)


def parse_instant(text: str) -> datetime:
    """Parse an ISO timestamp that carries a timezone (``Z`` or an offset) into aware UTC."""
    if not isinstance(text, str):
        raise ValueError(f"not a timestamp: {text!r}")
    dt = datetime.fromisoformat(text.strip())
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return dt.astimezone(timezone.utc)


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def normalise_published_at(text: str) -> tuple[str, str]:
    """Return ``(value, precision)``: a plain date stays a date, a timestamp becomes UTC.

    A date-time without a timezone is refused: its instant is ambiguous and a wrong guess could
    move a document across the cutoff.
    """
    t = (text or "").strip()
    if not t:
        raise ValueError("empty published_at")
    if DATE_RE.match(t):
        if not valid_date(t):
            raise ValueError(f"not a calendar date: {text!r}")
        return t, "date"
    try:
        dt = parse_instant(t)
    except ValueError as exc:
        raise ValueError(f"published_at needs a plain YYYY-MM-DD date or a timestamp with timezone: {text!r}") from exc
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "timestamp"


def assert_open(clock: Any, as_of: str) -> None:
    """Refuse once the cohort's cutoff has passed (``collect``, ``attach``, ``build``, ``submit``)."""
    cutoff = parse_instant(cutoff_utc(as_of))
    now = clock.now()
    if now > cutoff:
        raise Refused(
            "triage_window_closed",
            f"as_of {as_of}: cutoff {cutoff_utc(as_of)} has passed at {now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}; "
            "anything captured now is invisible to that cohort",
        )


def _unwrap(envelope: Any) -> tuple[Mapping[str, Any], str]:
    """(data, sha256 of the bytes it came from). A plain mapping is hashed as it would be archived."""
    if hasattr(envelope, "data") and hasattr(envelope, "sha256"):
        return envelope.data, envelope.sha256
    return envelope, sha256_hex(canonical_bytes(envelope))


def admissible(envelope: Any, capture_row: Mapping[str, Any] | None, cutoff: str, *,
               decisive: bool = False) -> tuple[bool, str]:
    """Is this archived envelope admissible for a cohort with this knowledge cutoff?

    ``envelope`` is a loaded envelope (``evidence.load``) or a plain mapping; ``capture_row`` is its
    ``captures`` row. Returns ``(ok, reason)``. ``reason`` is ``ok`` or ``ok_undated`` when admitted.
    ``decisive=True`` is for evidence behind a non-``unknown`` claim: it also needs a ``published_at``.
    Checks run in a fixed order and the first failure is reported.
    """
    if capture_row is None:
        return False, "capture_missing"
    data, file_sha = _unwrap(envelope)
    if capture_row["kind"] != CAPTURE_KIND:
        return False, "not_triage_doc"
    try:
        captured = parse_instant(capture_row["captured_at"])
        cut = parse_instant(cutoff)
    except ValueError:
        return False, "captured_at_unparseable"
    if captured > cut:
        return False, "captured_after_cutoff"
    if file_sha != capture_row["sha256"]:
        return False, "envelope_hash_mismatch"
    if data.get("schema") != SCHEMA_ENVELOPE:
        return False, "envelope_invalid"

    published = data.get("published_at")
    if published is None:
        if decisive:
            return False, "published_at_missing"
        return True, "ok_undated"

    try:
        value, precision = normalise_published_at(published)
    except ValueError:
        return False, "published_at_unparseable"
    if value != published or precision != data.get("published_at_precision"):
        return False, "published_at_unparseable"

    if precision == "date":
        pub_day = date.fromisoformat(value)
        if pub_day > ist_date(cut):
            return False, "published_after_cutoff"
        if pub_day > ist_date(captured):
            return False, "published_after_capture"
    else:
        pub = parse_instant(value)
        if pub > cut:
            return False, "published_after_cutoff"
        if pub > captured:
            return False, "published_after_capture"
    return True, "ok"

