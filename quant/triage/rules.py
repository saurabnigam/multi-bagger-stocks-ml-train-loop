"""The deterministic rule classifier (design section 5.2): pure functions over ``Features`` and documents.

``classify`` runs the rules in a fixed order and the first match wins. The order is
R1 placeholder or non-EQ series, R2 exchange SPOS, R3 demerger document, R4 vendor split, R7
genuine-event document, R8 market stress, R5 cross-section, R6 reversal, then ``unknown``. The
numbering follows the design table's names; the evaluation order is the table's order, not the number's.
R4 reads vendor splits dated up to 400 days from the jump (``features.VENDOR_SPLIT_LOOKBACK_DAYS``), not only the five
days R3's negative check reads; ``validate.r4_row`` holds the test and the agent validator calls it too.
R1 has two halves (an index placeholder, and a series other than EQ at the jump). Both label
``pending_listing`` and run first, as design section 5.2 says. A non-EQ series on a stock with an SPOS
or demerger document therefore waits instead of proposing a factor; that is fail-safe and is the one
place a reviewer may want a different order (an owner ruling, not a code default).

R7 runs before R5 and R6, so its negative check also fails on a same-date cross-section and on a reversal pair
(``validate.VENDOR_ARTIFACT_CHECKS``): a dated headline, or a link an agent attached, cannot hide a vendor error. The
check reads private document bodies too, and an unreadable body makes it ``unknown``. R8 keeps its original blockers.

Nothing here reads a database, the clock or the network. Every tunable is a module constant under
``RULES_VERSION``; there are no config keys, so the policy hash is unchanged. Confidence comes only
from ``grade.grade``. A condition whose input is unknown (a missing peer set, a store with no
series, an unreadable capture) is treated as not met, and the ``genuine_event`` negative check
reports ``unknown`` rather than ``none_found`` for it. That is the same code the agent validator
runs, so the engine and the validator cannot disagree about what "no demerger evidence" means.

Day 0: on the cohort evening the ex-date is the last bar, so later-bar features are unknown. The
only rule that needs later bars is the forward half of R6 (reversal). When no rule fires and later
bars are missing, the result is ``unknown`` with ``recheck_on`` set to the next session. The suspect
stays unresolved and truncated, which fails safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from quant.data.actions import JUMP_RATIO_BOUNDS
from quant.triage import evidence, features as feat, grade, pit, validate
from quant.triage.types import (
    MARKET_IMPLIED_UNCERTAINTY_PCT, FACTOR_TOLERANCE, MAX_LIST_ITEMS, MAX_TEXT_CHARS, is_number,
)
from quant.triage.validate import VENDOR_ARTIFACT_CHECKS, negative_check, unreadable_captures

RULES_VERSION = "triage-rules/1"
ENGINE_NAME = f"rules:{RULES_VERSION}"

# ------------------------------------------------------------------ tunables (design 5.2)
R1_RECHECK_DAYS = 7
R3_WINDOW_DAYS = 120                   # a demerger document is dated in [ex - 120d, ex]
R3_GROSS_RANGE = (0.2, 0.9)
R3_PEER_MARKET_GAP_PCT = 3.0           # |peer median - market median| must be under this
R4_RATIO_TOLERANCE = 0.03              # vendor ratio within 3% of 1/gross (downward jump) or of gross (upward jump)
R4_MIN_SESSIONS = validate.R4_MIN_SESSIONS   # the split must be more than one session from the jump
R4_LOOKBACK_DAYS = feat.VENDOR_SPLIT_LOOKBACK_DAYS   # and within 400 days of it; R3 keeps the five-day window
R5_MIN_JUMPS = validate.R5_MIN_JUMPS   # jumps on one date across the store (shared with the R7 negative check)
R5_MARKET_QUIET_PCT = validate.R5_MARKET_QUIET_PCT
R5_ROUND_RATIOS = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)
R5_RATIO_TOLERANCE = 0.03
R6_PRODUCT_RANGE = (0.95, 1.05)        # gross factors of the pair multiply to about 1
R6_WINDOW_SESSIONS = feat.NEARBY_SESSIONS
R7_WINDOW_DAYS = 5                     # a genuine-event document is dated in [ex - 5d, ex]
R8_MARKET_MOVE_PCT = 4.0
RULE_ORDER = ("R1", "R2", "R3", "R4", "R7", "R8", "R5", "R6")
RECHECK_RULES = ("R5", "R8")           # price-only calls that a later reversal bar could overturn
_SPOS_ACTIONS = ("demerger", "scheme", "rights")
_MAX_CITED = 3


# ------------------------------------------------------------------ the result record

@dataclass(frozen=True)
class Decision:
    """What the rules conclude. ``claims`` refs are capture ids (``TD-...``) or ``f:<feature>``; ``pack`` renumbers them."""
    label: str
    rule_id: str | None
    confidence: str
    subtype: str | None = None
    action: str = "none"
    resolve_kind: str | None = None
    factor: float | None = None
    factor_basis: str | None = None
    factor_uncertainty_pct: float | None = None
    needs_force: bool = False
    needs_primary: bool = False
    recheck_on: str | None = None
    factor_inputs: tuple[tuple[str, float, str], ...] = ()            # (name, value, ref)
    alternatives: tuple[tuple[str, str], ...] = ()                    # (label, why_not)
    claims: tuple[tuple[str, str, tuple[str, ...]], ...] = ()         # (fact|inference, text, refs)
    negative_checks: tuple[Mapping[str, Any], ...] = ()
    missing_evidence: tuple[str, ...] = ()
    needs_human: tuple[str, ...] = ()
    cited: tuple[str, ...] = ()                                       # capture ids the decision rests on


@dataclass(frozen=True)
class _Ctx:
    f: feat.Features
    items: tuple[Any, ...]
    flat: Mapping[str, Any]
    rejected: tuple[tuple[str, str], ...]
    archive_dir: Path | str | None
    warnings: list[str] = field(default_factory=list)      # reviewer notes from rules that declined to fire

    @property
    def decisive(self) -> list[Any]:
        return [i for i in self.items if i.decisive_ok]


# ------------------------------------------------------------------ helpers

def next_session(as_of: str) -> str:
    """Next weekday after ``as_of``. There is no holiday calendar here, so a holiday makes this a day early."""
    d = date.fromisoformat(as_of) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def published_day(env: Mapping[str, Any]) -> date | None:
    value = env.get("published_at")
    if value is None:
        return None
    if env.get("published_at_precision") == "date":
        return date.fromisoformat(value)
    return pit.ist_date(pit.parse_instant(value))


def _in_window(env: Mapping[str, Any], ex_date: str, days: int) -> bool:
    day = published_day(env)
    ex = date.fromisoformat(ex_date)
    return day is not None and ex - timedelta(days=days) <= day <= ex


def _needs_force(gross: float | None, factor: float | None) -> bool:
    if gross is None or factor is None or gross <= 0:
        return False
    lo, hi = JUMP_RATIO_BOUNDS
    return not (lo <= gross * factor <= hi)


def _cap(texts: Sequence[str]) -> tuple[str, ...]:
    return tuple(t[:MAX_TEXT_CHARS] for t in texts[:MAX_LIST_ITEMS])


def _fmt_move(x: float | None) -> str:
    return "unknown" if x is None else f"{x:+.2f}%"


def _gross_claim(f: feat.Features) -> tuple[str, str, tuple[str, ...]]:
    return ("fact", f"One-day total-return factor on {f.ex_date} is {f.gross_factor:.4f}.", ("f:gross_factor",))


def _primary_ids(ctx: _Ctx) -> tuple[str, ...]:
    return tuple(i.capture_id for i in ctx.decisive if grade.is_verified_primary(i.envelope))[:_MAX_CITED]


# ------------------------------------------------------------------ rules

def _r1_placeholder(c: _Ctx) -> Decision | None:
    """R1: an index placeholder row points at this security, or it trades in a series other than EQ. Runs first."""
    f = c.f
    is_placeholder = f.placeholder is True or f.anomaly_kind == "index_placeholder"
    if not is_placeholder and not (isinstance(f.series, str) and f.series != "EQ"):
        return None
    primary = _primary_ids(c)
    if is_placeholder:
        subtype = "index_placeholder"
        claims = (("fact", "An index placeholder row (DUMMY symbol or invalid ISIN) points at this security.", ("f:placeholder",)),
                  ("inference", "A pending spin-off listing explains the price step; wait for the new listing.", ("f:placeholder",)))
        need = "exchange or company notice naming the listing"
    else:
        subtype = "series_not_eq"
        claims = (("fact", f"The security trades in series {f.series}, not EQ.", ("f:series",)),
                  ("inference", "A series change or pending listing may explain the price step; wait for an exchange notice.",
                   ("f:series",)))
        need = "exchange notice naming the series change or the listing"
    return Decision(
        "pending_listing", "R1", grade.grade("pending_listing", rule_id="R1", has_primary_doc=bool(primary)),
        subtype=subtype,
        action="wait", recheck_on=(date.fromisoformat(f.as_of) + timedelta(days=R1_RECHECK_DAYS)).isoformat(),
        claims=claims, missing_evidence=_cap(() if primary else (need,)), cited=primary)


def _spos_candidates(c: _Ctx) -> tuple[list[tuple[Any, str, float]], list[str]]:
    cands: list[tuple[Any, str, float]] = []
    notes: list[str] = []
    for it in c.decisive:
        env = it.envelope
        if not grade.is_verified_primary(env) or env.get("relevance") != "entity" or env.get("entity_match") is not True:
            continue
        tf = env.get("typed_fields") or {}
        pc, sp = tf.get("prev_close"), tf.get("spos_price")
        if not (is_number(pc) and is_number(sp)) or pc <= 0 or sp <= 0:
            continue
        action = tf.get("action_type", "demerger")
        if action not in _SPOS_ACTIONS:
            notes.append(f"{it.capture_id}: action_type {action!r} is not one of {list(_SPOS_ACTIONS)}")
            continue
        if pc / sp <= 1.0:
            notes.append(f"{it.capture_id}: prev_close / spos_price is {pc / sp:.4f}, not above 1; check the circular")
            continue
        cands.append((it, action, pc / sp))
    return cands, notes


def _body_verifies(c: _Ctx, it: Any) -> bool:
    """True when the item carries a body hash and the archived body file reads back intact."""
    body = it.envelope.get("body_sha256")
    return bool(body) and c.archive_dir is not None and evidence.read_body(c.archive_dir, body).status == "ok"


def _r2_spos(c: _Ctx) -> Decision | None:
    cands, notes = _spos_candidates(c)
    c.warnings.extend(notes)
    if not cands:
        return None
    first_factor, first_action = cands[0][2], cands[0][1]
    if any(a != first_action or abs(fac - first_factor) / first_factor > FACTOR_TOLERANCE for _, a, fac in cands):
        return _unknown(c, extra_needs=["conflicting SPOS documents: " + ", ".join(i.capture_id for i, _, _ in cands)])
    it, action, factor = next((x for x in cands if _body_verifies(c, x[0])), cands[0])
    env, tf = it.envelope, it.envelope["typed_fields"]
    has_body = _body_verifies(c, it)
    factor = round(factor, 6)
    g = c.f.gross_factor
    alternatives: tuple[tuple[str, str], ...] = ()
    if g is not None and g > 0:
        gap = (1.0 / g - factor) / factor * 100.0
        alternatives = ((action, f"market-implied factor {1.0 / g:.4f} (1/gross) differs from the exchange-derived "
                                 f"{factor:.4f} by {gap:+.1f}%"),)
    return Decision(
        action, "R2", grade.grade(action, rule_id="R2", has_body_hash=has_body), action="resolve", resolve_kind=action,
        factor=factor, factor_basis="exchange_spos", needs_force=_needs_force(g, factor),
        factor_inputs=(("prev_close", float(tf["prev_close"]), it.capture_id), ("spos_price", float(tf["spos_price"]), it.capture_id)),
        alternatives=alternatives,
        claims=(("fact", f"The exchange circular gives previous close {tf['prev_close']:g} and special pre-open price "
                         f"{tf['spos_price']:g}.", (it.capture_id,)),
                ("inference", f"Factor is previous close divided by special pre-open price: {factor:.4f}.", (it.capture_id,))),
        missing_evidence=_cap(() if has_body else ("circular body file (attach with --file) to reach high confidence",)),
        cited=(it.capture_id,))


def _r3_demerger_doc(c: _Ctx) -> Decision | None:
    f, fl = c.f, c.flat
    g, peer, market = f.gross_factor, f.sector_peer_median_move_pct, f.market_median_move_pct
    rows = fl["vendor_split_rows"]
    if g is None or not (R3_GROSS_RANGE[0] <= g <= R3_GROSS_RANGE[1]) or rows != [] or peer is None or market is None \
            or abs(peer - market) >= R3_PEER_MARKET_GAP_PCT:
        return None
    for it in c.decisive:
        env = it.envelope
        if grade.is_verified_primary(env) and env.get("relevance") == "entity" and env.get("entity_match") is True \
                and evidence.mentions_demerger(env, c.archive_dir) and _in_window(env, f.ex_date, R3_WINDOW_DAYS):
            factor = round(1.0 / g, 6)
            return Decision(
                "demerger", "R3", grade.grade("demerger", rule_id="R3"), action="resolve", resolve_kind="demerger",
                factor=factor, factor_basis="market_implied", factor_uncertainty_pct=MARKET_IMPLIED_UNCERTAINTY_PCT,
                needs_force=_needs_force(g, factor),
                claims=(("fact", "A primary document about this company mentions a demerger or scheme and is dated "
                                 f"within {R3_WINDOW_DAYS} days before the ex-date.", (it.capture_id,)),
                        _gross_claim(f),
                        ("fact", f"Sector peers moved {_fmt_move(peer)} and the market {_fmt_move(market)} that day.",
                         ("f:sector_peer_median_move_pct", "f:market_median_move_pct")),
                        ("inference", f"The loss is company-specific, not sector or market; 1/gross is {factor:.4f}.",
                         ("f:gross_factor",))),
                missing_evidence=_cap(("exchange special pre-open price (prev_close and spos_price) to replace "
                                       "the market-implied factor",)),
                cited=(it.capture_id,))
    return None


def _r4_unknown_notes(candidates: Any, gross: float | None) -> list[str]:
    """Reviewer notes for a vendor split that matches the jump but whose compensating-jump test could not run."""
    target = validate.r4_target(gross)
    if target is None or not isinstance(candidates, (list, tuple)):
        return []
    notes = []
    for row in candidates:
        ratio, sessions = row.get("ratio"), row.get("sessions_from_jump")
        if not is_number(ratio) or ratio <= 0 or row.get("compensating_jump") is not None \
                or abs(float(ratio) - target) / target > R4_RATIO_TOLERANCE \
                or (isinstance(sessions, int) and abs(sessions) < R4_MIN_SESSIONS):
            continue
        why = row.get("compensating_jump_why")
        if why == "no_price_bar_on_split_date":
            notes.append(f"R4: the vendor split dated {row.get('date')} (ratio {float(ratio):g}) matches the jump, but the price "
                         "store has no price bar on that date, so a cancelling jump cannot be ruled out; R4 abstains")
        else:
            notes.append(f"R4: the vendor split dated {row.get('date')} (ratio {float(ratio):g}) matches the jump, but its "
                         f"compensating-jump test could not run ({why or 'unknown'}); R4 abstains")
    return notes


def _r4_vendor_split(c: _Ctx) -> Decision | None:
    """R4: a vendor split dated on another day explains the jump (design D1).

    The split may be dated up to ``features.VENDOR_SPLIT_LOOKBACK_DAYS`` (400) from the jump. It must be more than one
    session from the jump date, its ratio within 3% of 1/gross (downward jump) or of gross (upward jump), and the price
    store must show no compensating jump on its date, which needs a price bar on that date: without one the answer is
    unknown and R4 abstains with a note. "Within one session" counts the security's own bars, not weekdays.
    ``validate.r4_row`` holds the test; the agent validator calls it too.

    The call is ``manual_adj``, never ``split``. The vendor's real share change is already in the store at the vendor's own
    date, and a ``split`` record would also feed ``PriceStore.split_events`` (the EPS share basis in factors/growth.py), so a
    second split would count it twice. A downward jump takes the vendor ratio as the factor; an upward jump takes 1/ratio,
    so ``gross * factor`` is near 1 either way. The factor input stays the vendor's ratio.
    """
    f, fl = c.f, c.flat
    g = f.gross_factor
    candidates = fl.get("vendor_split_candidates")
    row = validate.r4_row(candidates, g)
    if row is None:
        c.warnings.extend(_r4_unknown_notes(candidates, g))
        return None
    target = validate.r4_target(g)
    ratio = round(float(row["ratio"]), 6)
    factor = validate.r4_factor(ratio, g)
    ref = row.get("capture_id") or "f:vendor_split_ratio"
    sessions = abs(int(row["sessions_from_jump"]))
    return Decision(
        "vendor_error_other", "R4", grade.grade("vendor_error_other", rule_id="R4"), subtype="misdated_adjustment",
        action="resolve", resolve_kind="manual_adj", factor=factor, factor_basis="vendor_ratio",
        needs_force=_needs_force(g, factor), needs_primary=True, factor_inputs=(("ratio", ratio, ref),),
        claims=(_gross_claim(f),
                ("fact", f"The vendor records a split of ratio {ratio:g} dated {row['date']}, {sessions} sessions from the "
                         f"ex-date {f.ex_date}, and the price store shows no jump on that date that cancels this one.", (ref,)),
                ("inference", f"The vendor applied the split on the wrong day; its ratio is within "
                              f"{R4_RATIO_TOLERANCE:.0%} of {target:.4f} ({'1/gross' if g < 1.0 else 'gross'}). The real share "
                              "change is already in the store at the vendor's date, so the call is a manual return adjustment, "
                              "not a second split.",
                 ("f:gross_factor", ref))),
        missing_evidence=_cap(("exchange or company document confirming the split date",
                               f"review the vendor split dated {row['date']} for a mirror-image jump")),
        cited=(row["capture_id"],) if row.get("capture_id") else ())


def _genuine_docs(c: _Ctx) -> list[Any]:
    """Documents that can carry R7: dated in [ex - 5d, ex], entity relevance with a verified match, or sector relevance."""
    out = []
    for it in c.decisive:
        env = it.envelope
        rel = env.get("relevance")
        ok = (rel == "entity" and env.get("entity_match") is True) or rel == "sector"
        if ok and _in_window(env, c.f.ex_date, R7_WINDOW_DAYS):
            out.append(it)
    return out


def _negative_checks(c: _Ctx) -> list[dict[str, Any]]:
    # R1 to R4 did not match when this runs, so the last check is none_found by construction.
    return negative_check(c.flat, [(i.capture_id, i.envelope) for i in c.items],
                          {"classification": {"rule_id": None}}, list(c.rejected), c.archive_dir)


def _r7_genuine_doc(c: _Ctx) -> Decision | None:
    docs = _genuine_docs(c)
    if not docs:
        return None
    checks = _negative_checks(c)
    if any(ch["result"] != "none_found" for ch in checks):
        return None
    f = c.f
    level = grade.evidence_level([d.envelope for d in docs])
    conf = grade.grade("genuine_event", rule_id="R7", evidence_level=level,
                       corroborated=grade.corroborated(f.n_peers_moved))
    cited = tuple(d.capture_id for d in docs[:_MAX_CITED])
    peers = "" if f.n_peers_moved is None else f" {f.n_peers_moved} of the closest sector peers moved 5% or more the same way."
    return Decision(
        "genuine_event", "R7", conf, action="resolve", resolve_kind="genuine", factor=1.0,
        claims=(("fact", f"A document dated within {R7_WINDOW_DAYS} days before the ex-date covers this company or its sector.",
                 cited),
                ("inference", "No corporate-action wording, vendor split, same-date cross-section, reversal pair, placeholder or non-EQ series was found (negative checks).", ()),
                ("inference", "The move is a real price reaction to that news, not a corporate action." + peers, cited)),
        negative_checks=tuple(checks),
        missing_evidence=_cap(() if level == "P" else ("primary document naming the company",)),
        cited=cited)


def _blocked_by_found(c: _Ctx) -> bool:
    """R8's blockers: counter-evidence in documents, a vendor split, a placeholder or a non-EQ series. The two
    vendor-artifact checks (cross-section, reversal) block R7 only: a reversal inside a crash is still market stress."""
    return any(ch["result"] == "found" for ch in _negative_checks(c) if ch["check"] not in VENDOR_ARTIFACT_CHECKS)


def _r8_market_stress(c: _Ctx) -> Decision | None:
    f = c.f
    g, mkt = f.gross_factor, f.market_median_move_pct
    if g is None or mkt is None or abs(mkt) < R8_MARKET_MOVE_PCT or g == 1.0 or (g > 1.0) != (mkt > 0):
        return None
    if _blocked_by_found(c):
        return None
    return Decision(
        "genuine_event", "R8", grade.grade("genuine_event", rule_id="R8"), subtype="market_stress",
        action="resolve", resolve_kind="genuine", factor=1.0,
        claims=(_gross_claim(f),
                ("fact", f"The market median moved {_fmt_move(mkt)} that day, the same direction.", ("f:market_median_move_pct",)),
                ("inference", "A market-wide move of this size explains the jump; no corporate-action evidence was found.",
                 ("f:gross_factor", "f:market_median_move_pct"))),
        missing_evidence=_cap(("primary document confirming no corporate action",)))


def _r5_cross_section(c: _Ctx) -> Decision | None:
    f = c.f
    g, n, mkt = f.gross_factor, f.cross_section_count, f.market_median_move_pct
    if g is None or g <= 0 or n is None or n < R5_MIN_JUMPS or mkt is None or abs(mkt) >= R5_MARKET_QUIET_PCT:
        return None
    ratio = None
    if g < 1.0:
        target = 1.0 / g
        near = [r for r in R5_ROUND_RATIOS if abs(r - target) / target <= R5_RATIO_TOLERANCE]
        ratio = min(near, key=lambda r: abs(r - target)) if near else None
    base = dict(subtype="misdated_adjustment", needs_primary=True,
                claims=(_gross_claim(f),
                        ("fact", f"{n} securities jumped on {f.ex_date} while the market median moved {_fmt_move(mkt)}.",
                         ("f:cross_section_count", "f:market_median_move_pct")),
                        ("inference", "Many unrelated securities stepping on one quiet day points to a vendor adjustment error.",
                         ("f:cross_section_count",))),
                missing_evidence=_cap(("vendor action record or exchange document for the ex-date",)))
    conf = grade.grade("vendor_error_other", rule_id="R5")
    if ratio is None:
        return Decision("vendor_error_other", "R5", conf, action="none", **base)
    # manual_adj, not split: the vendor already records the real share change on its own date, and a
    # split row would also feed split_events (EPS share basis) and count that change twice.
    return Decision("vendor_error_other", "R5", conf, action="resolve", resolve_kind="manual_adj", factor=ratio,
                    factor_basis="round_ratio", needs_force=_needs_force(g, ratio), **base)


def _r6_reversal(c: _Ctx) -> Decision | None:
    f = c.f
    if f.gross_factor is None:
        return None
    lo, hi = R6_PRODUCT_RANGE
    for offset, when, g in f.nearby_jumps:
        if abs(offset) > R6_WINDOW_SESSIONS or offset == 0 or not (lo <= f.gross_factor * g <= hi):
            continue
        side = "before" if offset < 0 else "after"
        return Decision(
            "vendor_error_other", "R6", grade.grade("vendor_error_other", rule_id="R6"), subtype="one_bar_glitch",
            action="none",
            claims=(_gross_claim(f),
                    ("fact", f"The same security jumped by {g:.4f} on {when}, {abs(offset)} session(s) {side}; "
                             f"the product of the two factors is {f.gross_factor * g:.4f}.", ("f:gross_factor",)),
                    ("inference", "A jump that reverses within days is a bad vendor bar, not a corporate action; "
                                  "there is no single factor to apply.", ("f:gross_factor",))),
            missing_evidence=_cap(("review both dates; resolve each as genuine only if the bars are real",)))
    return None


_RULES = {"R1": _r1_placeholder, "R2": _r2_spos, "R3": _r3_demerger_doc, "R4": _r4_vendor_split,
          "R7": _r7_genuine_doc, "R8": _r8_market_stress, "R5": _r5_cross_section, "R6": _r6_reversal}


# ------------------------------------------------------------------ default and entry point

def _awaits_later_bars(f: feat.Features) -> bool:
    """Day 0 or day 1-4 with the ex-date bar in the store: later bars may still arrive. ``n_post_bars`` None means
    the ex-date bar is not in the store at all, and a later bar would not help."""
    return f.anomaly_kind == "suspected_jump" and f.n_post_bars is not None and not f.post_bars_available


def _unknown(c: _Ctx, extra_needs: Sequence[str] = ()) -> Decision:
    f, fl = c.f, c.flat
    missing: list[str] = []
    found = [ch for ch in _negative_checks(c) if ch["result"] == "found"]
    if found:                                     # counter-evidence first
        missing.append("resolve the counter-evidence: " + ", ".join(ch["check"] for ch in found))
    unreadable = unreadable_captures(list(c.rejected))
    for ch in _negative_checks(c):                 # a body that could not be read is as unknown as a capture that could not
        if ch["check"] == "demerger_keyword_in_docs" and ch["result"] == "unknown":
            unreadable += [cid for cid in ch["sources"] if cid not in unreadable]
    if unreadable:
        missing.append("unreadable evidence, re-attach: " + ", ".join(unreadable[:4]))
    if f.gross_factor is None and f.anomaly_kind == "suspected_jump":
        missing.append("price bars for the ex-date (the store has none)")
    if not any(grade.is_verified_primary(i.envelope) for i in c.decisive):
        missing.append("dated primary document (exchange circular, company filing or regulator paper) for the ex-date")
    if f.gross_factor is not None and f.gross_factor < 1.0:
        missing.append("exchange special pre-open price (prev_close and spos_price) if this is a demerger, scheme or rights event")
    if f.market_median_move_pct is None or f.sector_peer_median_move_pct is None:
        missing.append("peer and market moves for the ex-date (not available from the store)")
    if fl["vendor_split_rows"] is None:
        missing.append("vendor split record near the ex-date")
    if f.series is None:
        missing.append("exchange series at the ex-date")
    recheck = None
    if _awaits_later_bars(f):
        missing.append("bars after the ex-date; a reversal or vendor correction may follow")
        recheck = next_session(f.as_of)
    return Decision("unknown", None, "low", action="none", recheck_on=recheck, missing_evidence=_cap(missing),
                    needs_human=_cap([*extra_needs, *c.warnings]))


def classify(f: feat.Features, items: Sequence[Any] = (), *, rejected: Sequence[tuple[str, str]] = (),
             archive_dir: Path | str | None = None) -> Decision:
    """Classify one anomaly. ``items`` are admissible ``evidence.EvidenceItem`` objects for it.

    ``rejected`` is ``EvidenceSet.rejected``: captures that could not be admitted. An unreadable one
    makes the demerger-keyword check ``unknown``. ``archive_dir`` lets R2 confirm that a document body
    really exists before it grants high confidence.
    """
    items = tuple(items)
    c = _Ctx(f, items, feat.flat(f, items, product_range=R6_PRODUCT_RANGE), tuple(rejected), archive_dir)
    for rule_id in RULE_ORDER:
        d = _RULES[rule_id](c)
        if d is not None:
            if d.rule_id in RECHECK_RULES and d.recheck_on is None and _awaits_later_bars(f):
                d = replace(d, recheck_on=next_session(f.as_of))
            return replace(d, needs_human=_cap([*d.needs_human, *c.warnings])) if c.warnings else d
    return _unknown(c)
