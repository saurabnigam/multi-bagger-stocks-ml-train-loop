"""Enumerate the anomalies triage should look at (design 5.1, amendment F2).

An anomaly is a measured one-day jump, an unresolved ``suspected`` row, or an index placeholder.
``measure_jumps`` repeats ``actions.detect``'s arithmetic on the price store, without writing, so
the two always agree on what a jump is (a test compares them on the actions CLI fixture world).

Window (F2, narrowed by D4). The default window lists every measured jump that is not yet resolved in
``corporate_actions`` and whose ex-date is in the trailing 13 months of ``as_of`` (the lookback
``data actions-list`` uses for its in-window flag), every unresolved ``suspected`` row in the same 13
months, anything with an open proposal (status ``proposed`` or ``wait``) whatever its date, and index
placeholders. An older unresolved suspect row with no open proposal is hidden: it is counted in
``ScanResult.hidden`` (so a list can say how many there are and how to show them) and ``since``
widens the window to include it. ``since`` never narrows the window. An explicit ``select`` by ISIN and
ex-date still finds a hidden row. Resolution is read as of the cutoff, so a decision recorded later does
not hide an anomaly from an earlier cohort.

A placeholder event for a security that also has a listed jump is folded into that jump (its
features say ``placeholder: true``) instead of being listed twice, when ``features.placeholder_links``
says so: by the parent symbol or security, with the event's day in ``[ex - 45, ex + 15]``, or by ISIN
suffix (``DUM545A01024`` for ``INE545A01024``), with the event's day in ``[ex - 45, ex + 60]``. The ISIN
suffix link applies whatever ``parent_symbol`` says (null, the parent's symbol, or another one). An event
outside every jump's window is listed on its own.

A folded placeholder is also returned in ``ScanResult.folded`` with the key it would have had as a
standalone anomaly, so a build can supersede an open proposal that an earlier scan made for that key.

The scan is read-only: it takes a state connection and a price store and writes nothing.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable, Sequence

import pandas as pd

from quant.triage import features as feat, pit, store as triage_store
from quant.triage.types import RULE_ID_ENGINE, subject_key

WINDOW_MONTHS = 13
KIND_JUMP = "suspected_jump"
KIND_PLACEHOLDER = "index_placeholder"
OPEN_STATUSES = ("proposed", "wait")     # an anomaly with one of these has an open proposal and stays in the default window


@dataclass(frozen=True)
class Jump:
    security_id: int
    ex_date: str
    gross_factor: float           # unrounded, as ``actions.detect`` computes it
    observed_at: str              # of the bar that shows the jump


@dataclass(frozen=True)
class Anomaly:
    kind: str                     # suspected_jump | index_placeholder
    isin: str | None
    ex_date: str
    security_id: int | None
    symbol: str | None
    gross_factor: float | None
    observed_at: str | None
    source: str                   # measured | suspect_row | measured+suspect_row | placeholder_event
    in_window: bool
    status: str = "new"           # new | proposed | wait | rejected (still open) | resolved
    proposal_id: str | None = None

    @property
    def key(self) -> str:
        return subject_key(self.isin or "", self.ex_date)


@dataclass(frozen=True)
class Folded:
    """A placeholder event that a listed jump claims. ``key`` is what it was listed as before it folded."""
    key: str                      # <isin or placeholder isin>@<first-seen day>: a standalone anomaly's key
    isin: str
    first_seen: str               # the IST day the engine first captured the placeholder
    placeholder_symbol: str | None
    placeholder_isin: str | None
    parent_key: str               # the listed jump that claims it


@dataclass(frozen=True)
class ScanResult:
    as_of: str
    cutoff: str
    window_start: str
    anomalies: list[Anomaly]
    n_measured: int
    n_suspect_rows: int
    #: Unresolved suspect rows older than the window with no open proposal (D4). Not in ``anomalies``.
    hidden: list[Anomaly] = field(default_factory=list)
    #: Placeholders folded into a listed jump (X6); their standalone proposals are superseded by the next build.
    folded: list[Folded] = field(default_factory=list)

    @property
    def n_hidden(self) -> int:
        return len(self.hidden)

    @property
    def hidden_since(self) -> str | None:
        """Earliest ex-date among the hidden rows: ``--since`` this date shows them all."""
        return min((a.ex_date for a in self.hidden), default=None)


def default_window_start(as_of: str) -> str:
    """Start of the trailing 13 months, the same arithmetic ``actions.list_suspects`` uses."""
    return (pd.Timestamp(as_of) - pd.DateOffset(months=WINDOW_MONTHS)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ measuring jumps

def measure_jumps(store: Any, security_ids: Sequence[int], *, since: str, through: str, vintage_at: str,
                  jump_ratio: float) -> list[Jump]:
    """Every one-day jump with ``since <= ex_date <= through`` in bars observed at or before ``vintage_at``.

    The arithmetic is ``actions.detect``'s: ``split * (close + dividend) / previous close`` outside
    ``[1/jump_ratio, jump_ratio]``. ``detect`` also skips keys already in ``corporate_actions`` and
    writes suspect rows; this function does neither, so it shows every jump including resolved ones.
    """
    import numpy as np

    sids = sorted({int(x) for x in security_ids})
    if not sids:
        return []
    start = (pd.Timestamp(since) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    out: list[Jump] = []
    for i in range(0, len(sids), 100):
        df = store._versioned(sids[i:i + 100], start, through, vintage_at, "close_raw, dividend_raw, split_ratio, observed_at")
        for sid, g in df.groupby("security_id"):
            g = g.sort_values("date")
            c = g["close_raw"].to_numpy(float)
            d = g["dividend_raw"].to_numpy(float)
            sp = g["split_ratio"].to_numpy(float)
            if len(c) < 2:
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                f = sp[1:] * (c[1:] + d[1:]) / c[:-1]
            bad = np.where(np.isfinite(f) & ((f > jump_ratio) | (f < 1.0 / jump_ratio)))[0] + 1
            dates = g["date"].to_numpy(str)
            obs = g["observed_at"].to_numpy(str)
            for k in bad:
                if dates[k] >= since:
                    out.append(Jump(int(sid), str(dates[k]), float(f[k - 1]), str(obs[k])))
    out.sort(key=lambda j: (j.security_id, j.ex_date))
    return out


# ------------------------------------------------------------------ state database reads

def _identities(conn: sqlite3.Connection, sids: Iterable[int]) -> dict[int, tuple[str | None, str | None]]:
    out: dict[int, tuple[str | None, str | None]] = {}
    for sid in sorted(set(sids)):
        row = conn.execute("SELECT isin FROM securities WHERE security_id = ?", (sid,)).fetchone()
        sym = conn.execute("SELECT nse_symbol FROM symbol_history WHERE security_id = ? ORDER BY valid_from DESC LIMIT 1",
                           (sid,)).fetchone()
        out[sid] = (None if row is None else row[0], None if sym is None else sym[0])
    return out


def _action_rows(conn: sqlite3.Connection, cutoff: str) -> tuple[dict[tuple[int, str], bool], dict[tuple[int, str], tuple[str, float | None]]]:
    """(resolved keys, suspect rows) from ``corporate_actions`` as known at the cutoff."""
    resolved: dict[tuple[int, str], bool] = {}
    suspects: dict[tuple[int, str], tuple[str, float | None]] = {}
    cur = conn.execute("SELECT security_id, ex_date, kind, decision_id, observed_at, note FROM corporate_actions "
                       "WHERE observed_at <= ? ORDER BY security_id, ex_date, observed_at", (cutoff,))
    for sid, ex, kind, decision_id, observed_at, note in cur.fetchall():
        key = (int(sid), str(ex))
        if decision_id is not None:
            resolved[key] = True
        if kind == "suspected":
            gross = None
            try:
                gross = float(json.loads(note).get("gross_factor")) if note else None
            except (ValueError, TypeError, AttributeError):
                gross = None
            suspects.setdefault(key, (str(observed_at), gross))
    return resolved, suspects


def _proposal_status(conn: sqlite3.Connection, isin: str | None, ex_date: str) -> tuple[str, str | None]:
    if isin is None:
        return "new", None
    row = triage_store.latest_proposal(conn, isin, ex_date, rule_id=RULE_ID_ENGINE)
    if row is None:
        return "new", None
    action = ((row.get("payload") or {}).get("resolution") or {}).get("action")
    if row.get("status") == "approved":
        return "resolved", row["proposal_id"]
    if row.get("status") == "rejected":
        # A rejection resolves nothing: the anomaly is still truncated and has no corporate_actions row.
        return "rejected", row["proposal_id"]
    return ("wait" if action == "wait" else "proposed"), row["proposal_id"]


# ------------------------------------------------------------------ the scan

def scan(conn: sqlite3.Connection, store: Any, as_of: str, *, since: str | None = None,
         security_ids: Sequence[int] | None = None, jump_ratio: float,
         include_resolved: bool = False) -> ScanResult:
    """List the anomalies for the cohort ``as_of``. Reads ``conn`` and ``store``; writes nothing.

    ``jump_ratio`` has no default: pass ``features.jump_ratio_from(cfg)`` so the threshold is the one
    ``actions.detect`` reads. ``security_ids`` None scans every security in the price store; a caller that
    wants the tracked universe only passes those ids.
    """
    cutoff = pit.cutoff_utc(as_of)
    window_start = default_window_start(as_of)
    if since is not None and since < window_start:
        window_start = since
    sids = list(security_ids) if security_ids is not None else feat.Reader(conn, store, as_of, jump_ratio=jump_ratio, cutoff=cutoff).security_ids()
    measured = measure_jumps(store, sids, since=window_start, through=as_of, vintage_at=cutoff, jump_ratio=jump_ratio)
    resolved, suspects = _action_rows(conn, cutoff)
    wanted = {int(s) for s in sids}

    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for j in measured:
        key = (j.security_id, j.ex_date)
        merged[key] = dict(gross=j.gross_factor, observed_at=j.observed_at, source="measured", in_window=True)
    for key, (observed_at, gross) in suspects.items():
        if key in resolved and not include_resolved:
            continue
        if security_ids is not None and key[0] not in wanted:
            continue
        if key in merged:
            merged[key]["source"] = "measured+suspect_row"
        else:
            merged[key] = dict(gross=gross, observed_at=observed_at, source="suspect_row",
                               in_window=key[1] >= window_start)
    if not include_resolved:
        merged = {k: v for k, v in merged.items() if k not in resolved}

    ids = _identities(conn, (k[0] for k in merged))
    anomalies: list[Anomaly] = []
    for (sid, ex), v in merged.items():
        isin, symbol = ids[sid]
        status, pid = ("resolved", None) if (sid, ex) in resolved else _proposal_status(conn, isin, ex)
        anomalies.append(Anomaly(KIND_JUMP, isin, ex, sid, symbol, v["gross"], v["observed_at"], v["source"],
                                 v["in_window"], status, pid))

    hidden = [a for a in anomalies if a.source == "suspect_row" and not a.in_window and a.status not in OPEN_STATUSES]
    shown = [a for a in anomalies if a not in hidden]
    standalone, folded = _placeholder_anomalies(conn, cutoff, shown)
    shown.extend(standalone)
    shown.sort(key=lambda a: (a.ex_date, a.isin or "", a.kind))
    hidden.sort(key=lambda a: (a.ex_date, a.isin or "", a.kind))
    folded.sort(key=lambda f: (f.parent_key, f.key))
    return ScanResult(as_of, cutoff, window_start, shown, len(measured), len(suspects), hidden, folded)


def _placeholder_anomalies(conn: sqlite3.Connection, cutoff: str,
                           jumps: Sequence[Anomaly]) -> tuple[list[Anomaly], list[Folded]]:
    """(standalone anomalies, folded placeholders): one anomaly per placeholder (its first event in the lookback),
    unless a listed jump claims it, and then it is a ``Folded`` record instead.

    ``jumps`` are the listed jump anomalies. The event is claimed (folded into the jump) when
    ``features.placeholder_links`` says it points at that jump's security, the same test
    ``features.placeholder_for`` applies, so a folded placeholder is exactly one the jump's features report.
    """
    events = feat.placeholder_events(conn, cutoff)
    if not events:
        return [], []
    floor = (pd.Timestamp(pit.ist_date(pit.parse_instant(cutoff)).isoformat())
             - timedelta(days=feat.PLACEHOLDER_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    first: dict[tuple[Any, Any], dict[str, Any]] = {}
    for ev in events:
        if ev["day"] < floor:
            continue
        first.setdefault((ev["security_id"], ev["detail"].get("placeholder_isin")), ev)
    out: list[Anomaly] = []
    folded: list[Folded] = []
    for (sid, _placeholder_isin), ev in first.items():
        isin, symbol = (None, None) if sid is None else _identities(conn, [int(sid)])[int(sid)]
        isin = isin or ev["detail"].get("placeholder_isin")
        parent = next((j for j in jumps if j.kind == KIND_JUMP and feat.placeholder_links(
            ev, security_id=j.security_id, isin=j.isin, symbol=j.symbol, ex_date=j.ex_date)), None)
        if parent is not None:
            if isin is not None:
                folded.append(Folded(subject_key(isin, ev["day"]), isin, ev["day"], ev["detail"].get("placeholder_symbol"),
                                     ev["detail"].get("placeholder_isin"), parent.key))
            continue
        if isin is None:
            continue
        status, pid = _proposal_status(conn, isin, ev["day"])
        out.append(Anomaly(KIND_PLACEHOLDER, isin, ev["day"], None if sid is None else int(sid), symbol, None,
                           ev["created_at"], "placeholder_event", True, status, pid))
    return out, folded


def select(result: ScanResult, *, isin: str | None = None, ex_date: str | None = None) -> list[Anomaly]:
    """Anomalies matching an ISIN and ex-date (either may be None). An explicit pick also finds a hidden row."""
    return [a for a in (*result.anomalies, *result.hidden)
            if (isin is None or a.isin == isin) and (ex_date is None or a.ex_date == ex_date)]

