"""Anomaly features: what the price store, the universe and the evidence archive say about one jump.

``Features`` is the only input the rules read besides the archived documents. It has two
constructors. :class:`Reader` plus :func:`build` read the stores (read-only, vintage
``cutoff``, bars up to the ``as_of`` date), and :func:`from_recorded` adapts the recorded
price-only rows of the labelled eval set. Both give the same shape, so the rules cannot tell
them apart.

Every field that can be unknown is ``None`` when unknown and never defaulted to zero, empty or
false: a missing peer set, a store with no bar on the ex-date, an unreadable membership table and
a day-0 anomaly with no later bars all stay distinguishable from a measured zero. ``flat`` turns a
``Features`` into the flat mapping that goes into the pack (and that the agent validator reads).

Nothing here writes to a database, reads a factor, or looks at a bar dated after ``as_of`` or
observed after the cutoff. Peers come from ``sector_map``, which is one current backfill and not
point-in-time, so every pack says ``peer_set_pit: false`` and peers only ever corroborate.
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant.data.actions import JUMP_RATIO_BOUNDS
from quant.triage import pit
from quant.triage.types import canonical_bytes, sha256_hex

IST = ZoneInfo("Asia/Kolkata")

#: Session offsets inspected around the ex-date for a second jump (reversal) and for sizing the window.
NEARBY_SESSIONS = 7
#: Calendar days either side of the ex-date in which a vendor split counts as "near" the jump.
VENDOR_SPLIT_WINDOW_DAYS = 5
#: Bars after the ex-date needed before later-bar features are treated as available (design: "day-5").
POST_BARS_MIN = 5
#: A placeholder event counts for a jump if its day is in [ex - 45 days, ex + 15 days]. An event recorded much
#: later belongs to a later listing, not to an old jump.
PLACEHOLDER_LOOKBACK_DAYS = 45
PLACEHOLDER_LOOKAHEAD_DAYS = 15
PEER_TOP_N = 5
PEER_CORR_SESSIONS = 120
PEER_CORR_MIN_OBS = 60
PEER_MOVE_THRESHOLD = 0.05
_BAR_COLS = "close_raw, dividend_raw, split_ratio, volume_raw, observed_at"


# ------------------------------------------------------------------ small helpers

def jump_ratio_from(cfg: Any) -> float:
    """The threshold ``actions.detect`` reads: ``[returns].jump_ratio``, default 1.40."""
    return float(getattr(getattr(cfg, "returns", None), "jump_ratio", JUMP_RATIO_BOUNDS[1]))


def is_jump(gross: float, jump_ratio: float) -> bool:
    """Same test as ``actions.detect``: a finite gross factor outside [1/jump_ratio, jump_ratio]."""
    return bool(math.isfinite(gross) and (gross > jump_ratio or gross < 1.0 / jump_ratio))


def _r(x: float | None, places: int = 6) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), places)


def _day(text: str) -> date:
    return date.fromisoformat(text[:10])


def ist_day(iso: str) -> str:
    """IST calendar date of a stored UTC timestamp."""
    return pit.ist_date(pit.parse_instant(iso)).isoformat()


# ------------------------------------------------------------------ the feature record

@dataclass(frozen=True)
class Features:
    isin: str
    ex_date: str
    as_of: str
    anomaly_kind: str = "suspected_jump"
    symbol: str | None = None
    security_id: int | None = None
    gross_factor: float | None = None
    volume_ratio: float | None = None
    next_day_close_ratio: float | None = None       # needs a bar after the ex-date
    fwd5_close_ratio: float | None = None           # needs five bars after the ex-date
    n_post_bars: int | None = None                  # bars in (ex_date, as_of]; None = not measured
    series: str | None = None
    placeholder: bool | None = None
    sector_group: str | None = None
    n_peers: int | None = None
    sector_peer_median_move_pct: float | None = None
    market_median_move_pct: float | None = None
    n_peers_moved: int | None = None
    vendor_split_rows: tuple[Mapping[str, Any], ...] | None = None    # None = unknown, () = none seen
    cross_section_count: int | None = None
    #: (session offset from the ex-date, date, gross) of other jumps of this security; offset < 0 is before.
    nearby_jumps: tuple[tuple[int, str, float], ...] = ()
    peer_set_pit: bool = False

    @property
    def post_bars_available(self) -> bool:
        return self.n_post_bars is not None and self.n_post_bars >= POST_BARS_MIN

    @property
    def move_pct(self) -> float | None:
        return None if self.gross_factor is None else _r((self.gross_factor - 1.0) * 100.0, 4)


# ------------------------------------------------------------------ vendor rows and the flat mapping

_SPLIT_KEY = re.compile(r"^split_(\d+)_date$")


def yahoo_split_rows(items: Iterable[Any], ex_date: str) -> list[dict[str, Any]]:
    """Split rows from archived Yahoo ``actions`` envelopes within the vendor window of the ex-date.

    Reads the envelope shape ``sources.actions_result`` writes: provenance ``yahoo_actions`` and
    ``typed_fields`` ``split_<k>_date`` / ``split_<k>_ratio`` pairs (ratio as the vendor gives it, new
    shares per old). A pair with a missing, renamed or non-numeric half is ignored: that is unknown,
    never a split.
    """
    out: list[dict[str, Any]] = []
    ex = _day(ex_date)
    for it in items:
        env = it.envelope
        if env.get("provenance") != "yahoo_actions":
            continue
        tf = env.get("typed_fields") or {}
        for key, when in tf.items():
            m = _SPLIT_KEY.match(key)
            ratio = tf.get(f"split_{m.group(1)}_ratio") if m else None
            if not m or isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) \
                    or ratio <= 0 or not isinstance(when, str):
                continue
            try:
                gap = abs((_day(when) - ex).days)
            except ValueError:
                continue
            if gap <= VENDOR_SPLIT_WINDOW_DAYS:
                out.append({"date": when[:10], "ratio": round(float(ratio), 6), "source": "yahoo_actions",
                            "capture_id": it.capture_id})
    return out


def merged_vendor_rows(f: Features, items: Iterable[Any] = ()) -> list[dict[str, Any]] | None:
    """Store rows plus Yahoo rows, sorted; None when neither source gave an answer (unknown, not empty)."""
    yahoo = yahoo_split_rows(items, f.ex_date)
    if f.vendor_split_rows is None and not yahoo:
        return None
    rows = [dict(r) for r in (f.vendor_split_rows or ())] + yahoo
    rows.sort(key=lambda r: (str(r.get("date")), str(r.get("source")), float(r.get("ratio") or 0)))
    return rows


def _reversal(f: Features, lo: float, hi: float, *, forward: bool) -> bool | None:
    if f.gross_factor is None:
        return None
    for offset, _when, g in f.nearby_jumps:
        if (offset > 0) == forward and offset != 0 and lo <= f.gross_factor * g <= hi:
            return True
    if forward and not f.post_bars_available:
        return None
    return False


def flat(f: Features, items: Iterable[Any] = (), *, product_range: tuple[float, float] = (0.95, 1.05)) -> dict[str, Any]:
    """The flat pack ``features`` mapping (keys the validator reads, plus context for the reviewer)."""
    rows = merged_vendor_rows(f, items)
    ratio = None
    if rows and f.gross_factor and f.gross_factor > 0:
        target = 1.0 / f.gross_factor
        other = [r for r in rows if r.get("date") != f.ex_date and r.get("ratio")]
        if other:
            ratio = min(other, key=lambda r: (abs(float(r["ratio"]) - target) / target, str(r["date"])))["ratio"]
    lo, hi = product_range
    return {
        "gross_factor": _r(f.gross_factor),
        "move_pct": f.move_pct,
        "volume_ratio": _r(f.volume_ratio, 4),
        "next_day_close_ratio": _r(f.next_day_close_ratio, 4),
        "fwd5_close_ratio": _r(f.fwd5_close_ratio, 4),
        "n_post_bars": f.n_post_bars,
        "post_bars_available": f.post_bars_available,
        "series": f.series,
        "placeholder": f.placeholder,
        "sector_group": f.sector_group,
        "n_peers": f.n_peers,
        "n_peers_moved": f.n_peers_moved,
        "sector_peer_median_move_pct": _r(f.sector_peer_median_move_pct, 4),
        "market_median_move_pct": _r(f.market_median_move_pct, 4),
        "vendor_split_rows": rows,
        "vendor_split_ratio": ratio,
        "cross_section_count": f.cross_section_count,
        "reversal_prior": _reversal(f, lo, hi, forward=False),
        "reversal_forward": _reversal(f, lo, hi, forward=True),
        "peer_set_pit": f.peer_set_pit,
    }


# ------------------------------------------------------------------ placeholder events (state database)

def placeholder_events(conn: sqlite3.Connection, cutoff: str) -> list[dict[str, Any]] | None:
    """``INDEX_PLACEHOLDER`` data-quality events recorded at or before the cutoff, oldest first.

    Filtered on ``created_at`` and not on the event's ``as_of``, which is the run's as_of and can be
    any date. None when the table cannot be read (unknown, not "no placeholder").
    """
    import json

    try:
        cur = conn.execute(
            "SELECT event_id, created_at, security_id, detail_json FROM data_quality_events "
            "WHERE code = 'INDEX_PLACEHOLDER' ORDER BY created_at, event_id")
        raw = cur.fetchall()
    except sqlite3.Error:
        return None
    cut = pit.parse_instant(cutoff)
    out: list[dict[str, Any]] = []
    for event_id, created_at, security_id, detail_json in raw:
        try:
            when = pit.parse_instant(created_at)
        except ValueError:
            continue
        if when > cut:
            continue
        try:
            detail = json.loads(detail_json) if detail_json else {}
        except ValueError:
            detail = {}
        out.append({"event_id": event_id, "created_at": created_at, "day": ist_day(created_at),
                    "security_id": security_id, "detail": detail if isinstance(detail, dict) else {}})
    return out


def placeholder_window(ex_date: str) -> tuple[str, str]:
    """Inclusive ``(first, last)`` event day that can attach to a jump on ``ex_date``."""
    ex = _day(ex_date)
    return ((ex - timedelta(days=PLACEHOLDER_LOOKBACK_DAYS)).isoformat(),
            (ex + timedelta(days=PLACEHOLDER_LOOKAHEAD_DAYS)).isoformat())


def placeholder_for(events: Sequence[Mapping[str, Any]] | None, *, security_id: int | None, isin: str,
                    symbol: str | None, ex_date: str) -> bool | None:
    """Does a placeholder event in the window point at this security (itself or as the parent of a DUMMY row)?

    The window is the event's IST day in ``[ex - 45 days, ex + 15 days]``. None means the events could not be
    read; an empty answer inside the window is False.
    """
    if events is None:
        return None
    floor, ceiling = placeholder_window(ex_date)
    for ev in events:
        if not floor <= ev["day"] <= ceiling:
            continue
        detail = ev["detail"]
        if (security_id is not None and ev["security_id"] == security_id) \
                or detail.get("placeholder_isin") == isin \
                or (symbol is not None and detail.get("parent_symbol") == symbol):
            return True
    return False


# ------------------------------------------------------------------ store reader

@dataclass(frozen=True)
class CrossSection:
    day: str
    gross: Mapping[int, float]       # security_id -> one-day gross total-return factor on ``day``
    n_jumps: int
    market_median_move_pct: float | None


@dataclass(frozen=True)
class SubjectBars:
    frame: pd.DataFrame
    sha256: str                      # of the bars read, so a pack names its exact price inputs
    last_bar_date: str | None
    n_bars: int


class Reader:
    """Read-only access to the price store and the state database at one cohort's cutoff.

    Bars are read with ``vintage_at = cutoff`` and never past the ``as_of`` date, so bars added
    later (dated later or observed later) cannot change anything returned here. Cross-sections and
    sector maps are cached for the life of the reader, which is one ``build --all`` pass.
    """

    def __init__(self, state_conn: sqlite3.Connection, store: Any, as_of: str, *, jump_ratio: float,
                 cutoff: str | None = None):
        self.conn = state_conn
        self.store = store
        self.as_of = as_of
        self.cutoff = cutoff or pit.cutoff_utc(as_of)
        self.jump_ratio = float(jump_ratio)
        self._ids: list[int] | None = None
        self._xs: dict[str, CrossSection] = {}
        self._sectors: dict[int, list[tuple[str, str | None, str]]] | None = None
        self._events: list[dict[str, Any]] | None | bool = False

    # -- price store
    def security_ids(self) -> list[int]:
        if self._ids is None:
            with self.store.conn() as pc:
                self._ids = [int(r[0]) for r in pc.execute(
                    "SELECT DISTINCT security_id FROM prices_daily WHERE security_id > 0 ORDER BY security_id")]
        return self._ids

    def _bars(self, sids: Sequence[int], start: str, end: str, cols: str) -> pd.DataFrame:
        frames = []
        for i in range(0, len(sids), 400):
            frames.append(self.store._versioned([int(s) for s in sids[i:i + 400]], start, end, self.cutoff, cols))
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def cross_section(self, day: str) -> CrossSection:
        """Every security's one-day gross factor on ``day`` (needs a bar on ``day`` and one before it)."""
        if day in self._xs:
            return self._xs[day]
        start = (_day(day) - timedelta(days=10)).isoformat()
        df = self._bars(self.security_ids(), start, day, "close_raw, dividend_raw, split_ratio")
        gross: dict[int, float] = {}
        if not df.empty:
            for sid, g in df.groupby("security_id"):
                g = g.sort_values("date")
                if len(g) < 2 or str(g["date"].iloc[-1]) != day:
                    continue
                prev = float(g["close_raw"].iloc[-2])
                last = g.iloc[-1]
                with np.errstate(divide="ignore", invalid="ignore"):
                    val = float(last["split_ratio"]) * (float(last["close_raw"]) + float(last["dividend_raw"])) / prev
                if prev > 0 and math.isfinite(val):
                    gross[int(sid)] = val
        median = float(np.median(list(gross.values()))) if gross else None
        xs = CrossSection(day, gross, sum(1 for v in gross.values() if is_jump(v, self.jump_ratio)),
                          None if median is None else _r((median - 1.0) * 100.0, 4))
        self._xs[day] = xs
        return xs

    def subject_bars(self, sid: int, ex_date: str) -> SubjectBars:
        start = (_day(ex_date) - timedelta(days=30)).isoformat()
        end = min(self.as_of, (_day(ex_date) + timedelta(days=20)).isoformat())
        df = self._bars([sid], start, end, _BAR_COLS)
        if df.empty:
            return SubjectBars(df, sha256_hex(canonical_bytes([])), None, 0)
        df = df.sort_values("date").reset_index(drop=True)
        rows = [[str(r.date), float(r.close_raw), float(r.dividend_raw), float(r.split_ratio),
                 None if pd.isna(r.volume_raw) else float(r.volume_raw), str(r.observed_at)] for r in df.itertuples()]
        return SubjectBars(df, sha256_hex(canonical_bytes(rows)), str(df["date"].iloc[-1]), len(df))

    # -- state database
    def _sector_rows(self) -> dict[int, list[tuple[str, str | None, str]]]:
        if self._sectors is None:
            out: dict[int, list[tuple[str, str | None, str]]] = {}
            try:
                cur = self.conn.execute(
                    "SELECT security_id, valid_from, valid_to, sector_group FROM sector_map WHERE observed_at <= ? "
                    "ORDER BY security_id, valid_from", (self.cutoff,))
                for sid, vf, vt, grp in cur.fetchall():
                    out.setdefault(int(sid), []).append((str(vf), None if vt is None else str(vt), str(grp)))
            except sqlite3.Error:
                out = {}
            self._sectors = out
        return self._sectors

    def sector_group(self, sid: int, day: str) -> str | None:
        """Group valid on ``day``; else the latest known group (the map is one current backfill)."""
        rows = self._sector_rows().get(int(sid))
        if not rows:
            return None
        valid = [r for r in rows if r[0] <= day and (r[1] is None or r[1] > day)]
        return (valid or rows)[-1][2]

    def series_at(self, sid: int, day: str) -> str | None:
        """Exchange series from the latest universe snapshot on or before ``day``; None when unknown."""
        try:
            row = self.conn.execute(
                "SELECT series FROM universe_membership WHERE security_id = ? AND as_of <= ? AND observed_at <= ? "
                "ORDER BY as_of DESC, observed_at DESC LIMIT 1", (int(sid), day, self.cutoff)).fetchone()
        except sqlite3.Error:
            return None
        return None if row is None or row[0] is None else str(row[0])

    def placeholder_events(self) -> list[dict[str, Any]] | None:
        if self._events is False:
            self._events = placeholder_events(self.conn, self.cutoff)
        return self._events      # type: ignore[return-value]

    # -- peers
    def peers(self, sid: int, day: str, stock_gross: float | None) -> tuple[str | None, int | None, float | None, int | None]:
        """(sector_group, n_peers with a bar that day, peer median move %, top-5 correlated peers that moved 5%+)."""
        grp = self.sector_group(sid, day)
        if grp is None:
            return None, None, None, None
        peer_ids = [p for p in self.security_ids() if p != sid and self.sector_group(p, day) == grp]
        xs = self.cross_section(day)
        moves = [xs.gross[p] - 1.0 for p in peer_ids if p in xs.gross]
        if not moves:
            return grp, 0, None, None
        median = _r(float(np.median(moves)) * 100.0, 4)
        if stock_gross is None or stock_gross == 1.0:
            return grp, len(moves), median, None
        top = self._top_correlated(sid, peer_ids, day)
        if top is None:
            return grp, len(moves), median, None
        up = stock_gross > 1.0
        moved = sum(1 for p in top if p in xs.gross
                    and ((xs.gross[p] - 1.0 >= PEER_MOVE_THRESHOLD) if up else (xs.gross[p] - 1.0 <= -PEER_MOVE_THRESHOLD)))
        return grp, len(moves), median, moved

    def _top_correlated(self, sid: int, peer_ids: Sequence[int], day: str) -> list[int] | None:
        """Top-5 peers by correlation of daily returns over the sessions before ``day``; None if nothing to rank."""
        if not peer_ids:
            return None
        start = (_day(day) - timedelta(days=200)).isoformat()
        end = (_day(day) - timedelta(days=1)).isoformat()
        df = self._bars([sid, *peer_ids], start, end, "close_raw, dividend_raw, split_ratio")
        if df.empty:
            return None
        rets: dict[int, pd.Series] = {}
        for s, g in df.groupby("security_id"):
            g = g.sort_values("date")
            prev = g["close_raw"].shift(1)
            with np.errstate(divide="ignore", invalid="ignore"):
                r = g["split_ratio"] * (g["close_raw"] + g["dividend_raw"]) / prev - 1.0
            rets[int(s)] = pd.Series(r.to_numpy(), index=g["date"].to_numpy())
        if sid not in rets:
            return None
        wide = pd.DataFrame(rets).replace([np.inf, -np.inf], np.nan).sort_index().tail(PEER_CORR_SESSIONS)
        ranked: list[tuple[float, int]] = []
        for p in peer_ids:
            if p not in wide:
                continue
            pair = wide[[sid, p]].dropna()
            if len(pair) >= PEER_CORR_MIN_OBS:
                c = pair[sid].corr(pair[p])
                if math.isfinite(c):
                    ranked.append((-float(c), int(p)))
        if not ranked:
            return None
        ranked.sort()
        return [p for _, p in ranked[:PEER_TOP_N]]


# ------------------------------------------------------------------ building features from the stores

def build(reader: Reader, anomaly: Any) -> Features:
    """Features for one scanned anomaly (``scan.Anomaly``). Missing inputs stay unknown."""
    events = reader.placeholder_events()
    sid = anomaly.security_id
    placeholder = True if anomaly.kind == "index_placeholder" else placeholder_for(
        events, security_id=sid, isin=anomaly.isin, symbol=anomaly.symbol, ex_date=anomaly.ex_date)
    series = reader.series_at(sid, anomaly.ex_date) if sid is not None else None
    base = dict(isin=anomaly.isin, ex_date=anomaly.ex_date, as_of=reader.as_of, anomaly_kind=anomaly.kind,
                symbol=anomaly.symbol, security_id=sid, placeholder=placeholder, series=series)
    if anomaly.kind == "index_placeholder" or sid is None:
        return Features(**base)

    bars = reader.subject_bars(sid, anomaly.ex_date)
    df = bars.frame
    gross = anomaly.gross_factor
    volume_ratio = next_day = fwd5 = None
    n_post: int | None = None
    vendor_rows: tuple[dict[str, Any], ...] | None = None
    nearby: list[tuple[int, str, float]] = []
    dates = [] if df.empty else [str(d) for d in df["date"]]
    if anomaly.ex_date in dates:
        i = dates.index(anomaly.ex_date)
        c = df["close_raw"].to_numpy(float)
        d = df["dividend_raw"].to_numpy(float)
        sp = df["split_ratio"].to_numpy(float)
        vol = df["volume_raw"].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            g = np.full(len(c), np.nan)
            g[1:] = sp[1:] * (c[1:] + d[1:]) / c[:-1]
        if i >= 1 and math.isfinite(g[i]):
            gross = float(g[i])
        prior = vol[max(0, i - 20):i]
        prior = prior[np.isfinite(prior) & (prior > 0)]
        if len(prior) and np.isfinite(vol[i]):
            volume_ratio = float(vol[i] / np.median(prior))
        n_post = len(c) - 1 - i
        if n_post >= 1 and c[i] > 0:
            next_day = float(c[i + 1] / c[i])
        if n_post >= 5 and c[i] > 0:
            fwd5 = float(c[i + 5] / c[i])
        for k in range(1, len(c)):
            if k != i and abs(k - i) <= NEARBY_SESSIONS and is_jump(float(g[k]), reader.jump_ratio):
                nearby.append((k - i, dates[k], float(g[k])))
        ex = _day(anomaly.ex_date)
        vendor_rows = tuple(
            {"date": dates[k], "ratio": round(float(sp[k]), 6), "source": "price_store"}
            for k in range(len(c))
            if abs(sp[k] - 1.0) > 1e-9 and abs((_day(dates[k]) - ex).days) <= VENDOR_SPLIT_WINDOW_DAYS)

    xs = reader.cross_section(anomaly.ex_date)
    grp, n_peers, peer_median, moved = reader.peers(sid, anomaly.ex_date, gross)
    return Features(
        **base, gross_factor=gross, volume_ratio=volume_ratio, next_day_close_ratio=next_day, fwd5_close_ratio=fwd5,
        n_post_bars=n_post, sector_group=grp, n_peers=n_peers, sector_peer_median_move_pct=peer_median,
        market_median_move_pct=xs.market_median_move_pct if xs.gross else None, n_peers_moved=moved,
        vendor_split_rows=vendor_rows, cross_section_count=xs.n_jumps if xs.gross else None,
        nearby_jumps=tuple(nearby))


def price_vintage(reader: Reader, f: Features) -> dict[str, Any]:
    """Names the exact price inputs behind a pack: the cutoff, the subject bars read and the last bar seen."""
    if f.security_id is None or f.anomaly_kind == "index_placeholder":
        return {"vintage_at": reader.cutoff, "subject_bars_sha256": None, "n_bars": 0, "last_bar_date": None}
    bars = reader.subject_bars(f.security_id, f.ex_date)
    return {"vintage_at": reader.cutoff, "subject_bars_sha256": bars.sha256, "n_bars": bars.n_bars,
            "last_bar_date": bars.last_bar_date}


# ------------------------------------------------------------------ recorded rows (eval set, day-0 and day-5)

def from_recorded(case: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], *, post_bars_available: bool,
                  placeholder: bool = False, series: str | None = None, as_of: str | None = None) -> Features:
    """Adapt one recorded row (``recorded_cases.json``) to ``Features``. Labels are never read.

    ``post_bars_available=False`` is the cohort evening (day 0): the next-day and five-day ratios are
    hidden and a later jump of the same security is not seen. True is day 5: they are shown. Jumps
    of the same security before the ex-date and jumps on the same date are known either way.
    ``cases`` is the whole recorded set (the cross-section and reversal features look across it).
    """
    ex = case["ex_date"]
    real = [c for c in cases if c.get("origin") != "synthetic"]
    same_day = sum(1 for c in real if c["ex_date"] == ex) if case.get("origin") != "synthetic" else None
    nearby: list[tuple[int, str, float]] = []
    for other in real:
        if other is case or other["symbol"] != case["symbol"] or other["ex_date"] == ex:
            continue
        a, b = np.datetime64(ex), np.datetime64(other["ex_date"])
        sessions = max(1, int(np.busday_count(min(a, b), max(a, b))))      # weekday count; no holiday calendar
        offset = sessions if b > a else -sessions
        if abs(offset) <= NEARBY_SESSIONS and (offset < 0 or post_bars_available):
            nearby.append((offset, other["ex_date"], float(other["gross_factor"])))
    rows = tuple({"date": str(e[0]), "ratio": float(e[1]), "source": "price_store"}
                 for e in (case.get("yahoo_split_event_in_store_pm5d") or ())
                 if isinstance(e, (list, tuple)) and len(e) >= 2)
    return Features(
        isin=case["isin"], ex_date=ex, as_of=as_of or ex, symbol=case.get("symbol"),
        gross_factor=float(case["gross_factor"]), volume_ratio=case.get("volume_ratio_vs_20d_median"),
        next_day_close_ratio=case.get("next_day_close_ratio") if post_bars_available else None,
        fwd5_close_ratio=case.get("fwd5_close_ratio") if post_bars_available else None,
        n_post_bars=POST_BARS_MIN if post_bars_available else 0, series=series, placeholder=placeholder,
        sector_group=case.get("sector_group"), n_peers=case.get("n_sector_peers"),
        sector_peer_median_move_pct=case.get("sector_peer_median_move_pct"),
        market_median_move_pct=case.get("market_median_move_pct"), n_peers_moved=None,
        vendor_split_rows=rows, cross_section_count=same_day, nearby_jumps=tuple(sorted(nearby)))
