"""Late price admission (MASTER_SPEC R13): a closing price is admissible by its trading date.

When the as_of closes are not all recorded by a cohort's knowledge cutoff, the cohort may be
scored late. Its price bars (dated <= as_of) are then read at a pinned price vintage, the time
of scoring; everything else keeps the cutoff: fundamentals, holdings, attributes, index
membership and human corporate-action approvals. Such a cohort is flagged ``scored_late``,
published with ``is_clean = 0`` and must replay exactly from its stored pin.

Factor code identity hashes the source of ``PriceStore`` methods and ``FactorInputs``, so the
rule lives in a subclass here and none of those functions is edited. ``tri``, ``close_split``,
``close_raw`` and ``volume`` all read through ``_versioned``, so they follow the pin without
being overridden. The rule applies only where a human Tier-2 decision enabling it is recorded in
the same database (:func:`enabled_decision`); without one, nothing here changes any result.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from quant.data.gates import _cfg, record_event
from quant.data.prices import PriceStore

RULE_BARS_AND_SUSPECTS = "bar_date/1"     # bars by date; a jump seen only in late bars is an unresolved suspect
RULE_BARS_ONLY = "bar_date/0"             # bars by date; the strict corporate-action view (pre-change cohorts)
RULES = (RULE_BARS_AND_SUSPECTS, RULE_BARS_ONLY)
DECISION_SUBJECT = "late_price_admission"
LATE_EVENT_CODES = ("COHORT_SCORED_LATE",)


def excluded_late_cohorts_sql(column: str = "cohort_id") -> str:
    """SQL predicate that is true for a cohort no late-price event names (kept out of clean-live evidence).

    The inner ``IS NOT NULL`` matters: ``NOT IN`` over a set holding a NULL is never true.
    """
    codes = ", ".join(f"'{c}'" for c in LATE_EVENT_CODES)
    return (f"{column} NOT IN (SELECT json_extract(detail_json, '$.cohort_id') FROM data_quality_events "
            f"WHERE code IN ({codes}) AND json_extract(detail_json, '$.cohort_id') IS NOT NULL)")


def _as_of_of(cutoff: str) -> str:
    """The as_of date of a knowledge cutoff (as_of 23:59:59.999999 IST expressed in UTC)."""
    return pd.Timestamp(cutoff).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d")


class AdmittingPriceStore(PriceStore):
    """PriceStore that reads the bars of pinned cutoffs at the pin's price vintage.

    Only a ``vintage_at`` equal to a pinned cutoff is changed; every other vintage is passed
    through unchanged, and a store with no pins behaves exactly like :class:`PriceStore`.
    A pinned read never returns a bar dated after the as_of the cutoff belongs to.
    """

    _pins: Optional[Dict[str, Dict[str, str]]] = None      # per instance; open_readonly() builds via __new__

    # ------------------------------------------------------------------ pins
    def _pin_map(self) -> Dict[str, Dict[str, str]]:
        if self._pins is None:
            self._pins = {}
        return self._pins

    def pin(self, cutoff: str, price_vintage: str, rule: str = RULE_BARS_AND_SUSPECTS) -> None:
        """Read the bars of ``cutoff`` at ``price_vintage`` from now on. Idempotent; a different pin raises."""
        if rule not in RULES:
            raise ValueError(f"unknown price admission rule {rule!r}; expected one of {RULES}")
        if not price_vintage > cutoff:
            raise ValueError(f"price vintage {price_vintage} must be after the cutoff {cutoff}")
        pins = self._pin_map()
        new = {"price_vintage": price_vintage, "rule": rule}
        if cutoff in pins and pins[cutoff] != new:
            raise ValueError(f"cutoff {cutoff} is already pinned as {pins[cutoff]}; refusing {new}")
        pins[cutoff] = new

    def pins(self) -> Dict[str, Dict[str, str]]:
        """A copy of the pins: cutoff -> {"price_vintage", "rule"}."""
        return {c: dict(p) for c, p in self._pin_map().items()}

    def _bar_vintage(self, vintage_at: str) -> str:
        pin = self._pin_map().get(vintage_at)
        return pin["price_vintage"] if pin else vintage_at

    def _bar_end(self, vintage_at: str, end: str) -> str:
        """The latest bar date a read may return: the caller's, and for a pinned cutoff never past its as_of."""
        return min(end, _as_of_of(vintage_at)) if vintage_at in self._pin_map() else end

    # ------------------------------------------------------------------ bar reads
    def _versioned(self, security_ids: List[int], start: str, end: str, vintage_at: str, cols: str) -> pd.DataFrame:
        return super()._versioned(security_ids, start, self._bar_end(vintage_at, end), self._bar_vintage(vintage_at), cols)

    def adv_inr(self, security_ids: List[int], as_of: str, vintage_at: str, window: int = 63) -> pd.DataFrame:
        return super().adv_inr(security_ids, self._bar_end(vintage_at, as_of), self._bar_vintage(vintage_at), window)

    def _manifest_rows(self, vintage_at: str) -> list:
        rows = super()._manifest_rows(self._bar_vintage(vintage_at))
        if vintage_at in self._pin_map():
            last = _as_of_of(vintage_at)
            rows = [r for r in rows if r[1] <= last]
        return rows

    # ------------------------------------------------------------------ corporate actions
    def corporate_actions(self, security_ids: List[int], vintage_at: str) -> Dict[int, List[Dict[str, Any]]]:
        """The strict view at the cutoff. Under ``bar_date/1`` an action whose ex-date is known only at the
        price vintage (a jump evidenced by late bars, or an approval recorded after the cutoff with no
        suspect before it) is an unresolved suspect for this cohort: it truncates history at the ex-date."""
        out = super().corporate_actions(security_ids, vintage_at)
        pin = self._pin_map().get(vintage_at)
        if pin is None or pin["rule"] != RULE_BARS_AND_SUSPECTS:
            return out
        late = super().corporate_actions(security_ids, pin["price_vintage"])
        last = _as_of_of(vintage_at)                       # an ex-date after the as_of is outside this cohort's window
        for sid, acts in late.items():
            known = {a["ex_date"] for a in out.get(sid, [])}
            added = [{"ex_date": a["ex_date"], "mode": "truncate"} for a in acts
                     if a["ex_date"] not in known and a["ex_date"] <= last]
            if added:
                out[sid] = sorted(out.get(sid, []) + added, key=lambda a: a["ex_date"])
        return out

    def split_events(self, security_ids: List[int], start: str, end: str, vintage_at: str) -> Dict[int, List[tuple]]:
        """Vendor splits from the pinned price vintage; approved splits (a human decision) from the strict cutoff."""
        if vintage_at not in self._pin_map() or not security_ids:
            return super().split_events(security_ids, start, end, vintage_at)
        ids = [int(x) for x in security_ids]
        end = self._bar_end(vintage_at, end)
        bar_vintage = self._bar_vintage(vintage_at)
        placeholders = ",".join("?" for _ in ids)
        query = f"""
        WITH cand AS (
            SELECT DISTINCT security_id, date FROM prices_daily
            WHERE split_ratio != 1 AND security_id IN ({placeholders})
              AND date > ? AND date <= ? AND observed_at <= ?
        ), ranked AS (
            SELECT p.security_id, p.date, p.split_ratio,
                   ROW_NUMBER() OVER (PARTITION BY p.security_id, p.date ORDER BY p.observed_at DESC) AS rn
            FROM prices_daily p JOIN cand USING (security_id, date)
            WHERE p.observed_at <= ?
        )
        SELECT security_id, date, split_ratio FROM ranked WHERE rn = 1 AND split_ratio != 1
        """
        with self.conn() as p_conn:
            rows = p_conn.execute(query, [*ids, start, end, bar_vintage, bar_vintage]).fetchall()
        out: Dict[int, List[tuple]] = {}
        vendor = {(int(r[0]), str(r[1])) for r in rows}
        for sid, date, ratio in rows:
            out.setdefault(int(sid), []).append((str(date), float(ratio)))
        for sid, acts in PriceStore.corporate_actions(self, ids, vintage_at).items():
            for a in acts:
                if a["mode"] == "split" and start < a["ex_date"] <= end and (sid, a["ex_date"]) not in vendor:
                    out.setdefault(int(sid), []).append((a["ex_date"], float(a["mult"])))
        return {sid: sorted(ev) for sid, ev in out.items()}


# ----------------------------------------------------------------------------- coverage and decision

def coverage(store: PriceStore, security_ids: List[int], as_of: str, vintage_at: str) -> float:
    """Share of ``security_ids`` with a non-null close on ``as_of`` at ``vintage_at``, read strictly (pins ignored).

    No securities gives 0.0: an unknown universe is never complete.
    """
    ids = sorted({int(s) for s in security_ids})
    if not ids:
        return 0.0
    df = PriceStore._versioned(store, ids, as_of, as_of, vintage_at, "close_raw")
    return float(df["close_raw"].notna().sum()) / len(ids) if not df.empty else 0.0


def enabled_decision(conn: sqlite3.Connection) -> Optional[str]:
    """The newest human Tier-2 rule_change decision enabling late admission in this database, or None."""
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE kind = 'rule_change' AND tier = 2 AND subject_id = ? "
        "AND approver_kind = 'human' AND status IN ('approved', 'applied') ORDER BY decided_on DESC, rowid DESC LIMIT 1",
        (DECISION_SUBJECT,),
    ).fetchone()
    return str(row[0]) if row else None


def _threshold(ctx: Any) -> float:
    return float(_cfg(ctx, "gates", "price_coverage", 0.98))     # the value gate G3 reads


@dataclass(frozen=True)
class Admission:
    mode: str                                   # "on_time" | "late" | "blocked"
    coverage_at_cutoff: float
    coverage_at_vintage: Optional[float]
    price_vintage: Optional[str]
    decision_id: Optional[str]
    reason: str


def decide(ctx: Any, as_of: str, cutoff: str, security_ids: List[int]) -> Admission:
    """Whether the as_of closes are on time, admissible late, or still missing.

    ``on_time``: strict coverage at the cutoff meets the G3 threshold. Otherwise ``late`` needs both an
    enabling decision and strict coverage at the clock (the price vintage) meeting the threshold.
    """
    store, thr = ctx.store, _threshold(ctx)
    cov_cut = coverage(store, security_ids, as_of, cutoff)
    if cov_cut >= thr:
        return Admission("on_time", cov_cut, None, None, None, f"as_of closes cover {cov_cut:.2%} at the cutoff")
    decision_id = enabled_decision(ctx.conn)
    now = ctx.clock.iso()
    cov_now = coverage(store, security_ids, as_of, now)
    missing = []
    if decision_id is None:
        missing.append("late price admission is not enabled (no approved human rule_change decision "
                       f"for {DECISION_SUBJECT} in this database)")
    if not now > cutoff:
        missing.append(f"the clock {now} is not past the cutoff")
    elif cov_now < thr:
        missing.append(f"as_of closes are still missing at {now}: {cov_now:.2%} (need {thr:.0%})")
    if missing:
        return Admission("blocked", cov_cut, cov_now, None, decision_id,
                         f"as_of closes cover {cov_cut:.2%} at the cutoff (need {thr:.0%}); " + "; ".join(missing))
    return Admission("late", cov_cut, cov_now, now, decision_id,
                     f"as_of closes cover {cov_cut:.2%} at the cutoff and {cov_now:.2%} at {now}; "
                     f"scored late under decision {decision_id}")


# ----------------------------------------------------------------------------- replay of published cohorts

def published_pins(ctx: Any, store: AdmittingPriceStore, *, write: bool) -> Dict[str, Dict[str, Any]]:
    """Pin every published live cohort that was scored late, so it replays exactly.

    1. A cohort whose ``source_refs_json`` holds ``price_vintage`` is pinned there, under its recorded rule
       (the default rule when that block is not a dict). A stored vintage that is not a string after the cohort's
       cutoff, or whose pin is refused, is skipped; with ``write`` one WARN ``COHORT_PIN_INVALID`` event per cohort.
    2. A cohort published before the change whose as_of closes carry an observation time after its cutoff is
       pinned at ``published_at`` under ``bar_date/0``; this derivation needs the enabling decision, a cohort
       published after its cutoff, strict coverage of its scored securities below the G3 threshold at the
       cutoff and at or above it at ``published_at``. With ``write`` it records one INFO ``COHORT_SCORED_LATE``
       event per such cohort (once). With ``write`` False nothing is written.

    Returns cohort_id -> {"price_vintage", "rule", "basis": "stored" | "derived"}.
    """
    conn, thr = ctx.conn, _threshold(ctx)
    decision_id = enabled_decision(conn)
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute("SELECT cohort_id, as_of, knowledge_cutoff, published_at, source_refs_json FROM cohorts "
                          "WHERE track = 'live' ORDER BY as_of").fetchall():
        cohort_id, as_of, cutoff, published_at = r[0], r[1], r[2], r[3]
        try:
            refs = json.loads(r[4]) if r[4] else {}
        except ValueError:
            refs = {}
        refs = refs if isinstance(refs, dict) else {}
        vintage = refs.get("price_vintage")
        if vintage is not None:
            block = refs.get("late_price_admission")
            rule = block.get("rule", RULE_BARS_AND_SUSPECTS) if isinstance(block, dict) else RULE_BARS_AND_SUSPECTS
            try:
                if not (isinstance(vintage, str) and vintage > cutoff):
                    raise ValueError(f"stored price vintage {vintage!r} is not a time after the cutoff {cutoff}")
                store.pin(cutoff, vintage, rule)
            except (ValueError, TypeError) as exc:
                if write and conn.execute(
                        "SELECT 1 FROM data_quality_events WHERE code = 'COHORT_PIN_INVALID' "
                        "AND json_extract(detail_json, '$.cohort_id') = ? LIMIT 1", (cohort_id,)).fetchone() is None:
                    record_event(ctx, code="COHORT_PIN_INVALID", severity="WARN",
                                 detail={"cohort_id": cohort_id, "value": vintage, "rule": rule, "error": str(exc)})
                continue
            out[cohort_id] = {"price_vintage": vintage, "rule": rule, "basis": "stored"}
            continue
        if decision_id is None or not published_at > cutoff:
            continue
        sids = [int(x[0]) for x in conn.execute("SELECT DISTINCT security_id FROM scores WHERE cohort_id = ?", (cohort_id,))]
        if not sids or coverage(store, sids, as_of, cutoff) >= thr or coverage(store, sids, as_of, published_at) < thr:
            continue
        store.pin(cutoff, published_at, RULE_BARS_ONLY)
        out[cohort_id] = {"price_vintage": published_at, "rule": RULE_BARS_ONLY, "basis": "derived"}
        if write and conn.execute(
                "SELECT 1 FROM data_quality_events WHERE code = 'COHORT_SCORED_LATE' "
                "AND json_extract(detail_json, '$.cohort_id') = ? LIMIT 1", (cohort_id,)).fetchone() is None:
            record_event(ctx, code="COHORT_SCORED_LATE", severity="INFO",
                         detail={"cohort_id": cohort_id, "price_vintage": published_at, "rule": RULE_BARS_ONLY,
                                 "basis": "derived", "decision_id": decision_id})
    return out
