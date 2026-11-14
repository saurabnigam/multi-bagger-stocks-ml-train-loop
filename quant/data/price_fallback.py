"""Closing prices from several vendors, with fallbacks (MASTER_SPEC R18).

Yahoo stays the first source (history and corporate-action adjustments). For the as-of session and the
sessions just before it, members that still have no close are filled from the exchanges' official
end-of-day files: NSE first, then BSE. The files come from an inbox folder the owner puts them in
(``mode = "manual"``, the default: the engine never touches the network) or, when the owner sets
``mode = "auto"``, are fetched directly.

Rules that never bend:

* A stored close is never overwritten: only securities with no close for the date are written.
* Every file is a registered capture with its own kind; ``observed_at`` of a filled row is the capture's
  ``captured_at``, the time the engine first saw the bytes (the fetch time, or the time an inbox file was
  read, never a file's own modification time). Gate G3 and the cutoff rules are unchanged: a close seen
  after the cutoff is still late.
* The chain is enabled per database by a human Tier-2 decision (subject ``price_fallback_sources``).
  The decision does not choose the mode; the config does.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from quant.data.gates import record_event
from quant.data.prices import PriceStore, normalize_source
from quant.errors import Refused
from quant.types import Clock, Result

DECISION_SUBJECT = "price_fallback_sources"
MODES = ("manual", "auto")
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
COLUMNS = ["isin", "symbol", "series", "date", "open", "high", "low", "close", "volume"]
REQUIRED = ["TradDt", "FinInstrmTp", "ISIN", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"]
_CHUNK = 500
# What a bad source can raise while its file is read, unzipped, fetched or parsed: none of it escapes fill_missing.
_SOURCE_ERRORS = (requests.RequestException, OSError, Refused, zipfile.BadZipFile, zipfile.LargeZipFile,
                  UnicodeDecodeError, KeyError)


class SourceUnavailable(Exception):
    """The file is not published (HTTP 404) or not in the inbox yet."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str                                  # template with {YYYYMMDD}
    kind: str                                 # captures.kind
    headers: Dict[str, str]
    series: Tuple[str, ...]
    zipped: bool                              # the official file is a zip holding one CSV
    same_exchange: bool                       # the same exchange as the primary vendor's closes (Yahoo's .NS quotes are NSE's)

    def url_for(self, session: str) -> str:
        return self.url.format(YYYYMMDD=session.replace("-", ""))

    def file_name(self, session: str) -> str:
        return self.url_for(session).rsplit("/", 1)[-1]

    def inbox_names(self, session: str) -> List[str]:
        """Official file name first, then the already unzipped form (NSE only)."""
        name = self.file_name(session)
        return [name, name[:-4]] if self.zipped else [name]

    def parse(self, raw: bytes, session: str, zipped: Optional[bool] = None) -> pd.DataFrame:
        """Equity rows (FinInstrmTp STK) of one session as ``COLUMNS``; see the module rules.

        Refused('wrong_session') when the file's TradDt is not ``session`` (or is blank throughout); Refused('missing_column') when a
        required column is absent. ``zipped`` overrides the source default (an unzipped NSE file in the inbox).
        """
        if self.zipped if zipped is None else zipped:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise Refused("bad_zip", f"{self.name}: the zip holds no CSV")
                data = zf.read(names[0])
        else:
            data = raw
        text = data.decode("utf-8-sig")
        try:
            df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise Refused("bad_csv", f"{self.name}: {exc}") from exc
        df.columns = [str(c).strip() for c in df.columns]
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            raise Refused("missing_column", f"{self.name}: missing column(s) {missing}")
        df = df.apply(lambda col: col.str.strip())
        dates = {d for d in df["TradDt"] if d}
        if not dates or dates != {session}:
            what = f"file is for {sorted(dates)}" if dates else "file carries no trade date"
            raise Refused("wrong_session", f"{self.name}: {what}, wanted {session}")
        df = df[df["FinInstrmTp"] == "STK"].copy()
        close = pd.to_numeric(df["ClsPric"], errors="coerce")
        df = df[np.isfinite(close) & (close > 0)].copy()
        df["close"] = close[df.index]
        rank = {s: i for i, s in enumerate(self.series)}
        df["_rank"] = df["SctySrs"].map(rank)
        df = df[df["_rank"].notna() & (df["ISIN"] != "")].copy()
        df = df.sort_values("_rank", kind="stable").drop_duplicates("ISIN", keep="first").sort_index()
        out = pd.DataFrame({
            "isin": df["ISIN"], "symbol": df["TckrSymb"], "series": df["SctySrs"], "date": session,
            "open": pd.to_numeric(df["OpnPric"], errors="coerce"),
            "high": pd.to_numeric(df["HghPric"], errors="coerce"),
            "low": pd.to_numeric(df["LwPric"], errors="coerce"),
            "close": df["close"],
            "volume": pd.to_numeric(df["TtlTradgVol"], errors="coerce"),
        })
        for c in ("open", "high", "low"):
            out[c] = out[c].fillna(out["close"])
        out["volume"] = out["volume"].fillna(0.0)
        return out[COLUMNS].reset_index(drop=True)


SOURCES: Dict[str, Source] = {
    "nse_udiff": Source(
        name="nse_udiff",
        url="https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip",
        kind="nse_udiff_bhavcopy", headers={"User-Agent": _UA},
        series=("EQ", "BE", "BZ", "RR", "IV"), zipped=True, same_exchange=True),     # RR, IV: REITs, InvITs
    "bse_udiff": Source(
        name="bse_udiff",
        url="https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{YYYYMMDD}_F_0000.CSV",
        kind="bse_udiff_bhavcopy", headers={"User-Agent": _UA, "Referer": "https://www.bseindia.com/"},
        series=("A", "B", "T", "X", "XT", "Z", "M", "MT", "IF"), zipped=False, same_exchange=False),   # IF: REITs
}


# ----------------------------------------------------------------------------- config

def _ps(cfg: Any, key: str, default: Any) -> Any:
    sec = getattr(cfg, "price_sources", None)
    return getattr(sec, key, default) if sec is not None else default


def configured_sources(cfg: Any) -> List[str]:
    return [str(s) for s in _ps(cfg, "fallback", ["nse_udiff", "bse_udiff"])]


def mode_of(cfg: Any) -> str:
    mode = str(_ps(cfg, "mode", "manual"))
    if mode not in MODES:
        raise Refused("bad_mode", f"[price_sources] mode must be one of {MODES}, got {mode!r}")
    return mode


def inbox_dir(cfg: Any) -> Path:
    """The inbox folder, under the configured data directory."""
    return Path(cfg.paths.data_dir) / str(_ps(cfg, "inbox_subdir", "inbox/prices"))


# ----------------------------------------------------------------------------- network

def _http_fetch(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    """The default fetch (auto mode only). No cookies are kept: ``requests.get`` opens no session."""
    r = requests.get(url, headers=headers, timeout=timeout)
    return r.status_code, r.content


class ExchangeFileClient:
    """Fetches an exchange's official end-of-day file. ``fetch`` is injectable: (url, headers, timeout) -> (status, bytes)."""

    def __init__(self, cfg: Any, clock: Clock, fetch: Optional[Callable[..., Tuple[int, bytes]]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.cfg, self.clock = cfg, clock
        self._fetch, self._sleep = fetch, sleep

    def get(self, source: str, session: str) -> Tuple[bytes, str]:
        if source not in SOURCES:
            raise Refused("unknown_source", f"price source {source!r} is not in the registry {list(SOURCES)}")
        src = SOURCES[source]
        url = src.url_for(session)
        timeout = float(_ps(self.cfg, "http_timeout_s", 30))
        retries = int(_ps(self.cfg, "http_retries", 2))
        fetch = self._fetch or _http_fetch
        sleep = self._sleep or time.sleep
        for attempt in range(retries + 1):
            try:
                status, body = fetch(url, dict(src.headers), timeout)
                if status == 404:
                    raise SourceUnavailable(f"{source}: {url} is not published (HTTP 404)")
                if status != 200:
                    raise requests.HTTPError(f"HTTP {status} for {url}")
                return body, self.clock.iso()
            except (requests.RequestException, OSError):          # OSError: timeouts, resets, refused connections
                if attempt == retries:
                    raise
                sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")


# ----------------------------------------------------------------------------- decision

def enabled_decision(conn: sqlite3.Connection) -> Optional[str]:
    """The newest human Tier-2 rule_change decision enabling the fallback chain in this database, or None."""
    row = conn.execute(
        "SELECT decision_id FROM decisions WHERE kind = 'rule_change' AND tier = 2 AND subject_id = ? "
        "AND approver_kind = 'human' AND status IN ('approved', 'applied') ORDER BY decided_on DESC, rowid DESC LIMIT 1",
        (DECISION_SUBJECT,),
    ).fetchone()
    return str(row[0]) if row else None


# ----------------------------------------------------------------------------- helpers

def _chunks(items: List[Any], n: int = _CHUNK):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _isin_map(conn: sqlite3.Connection, ids: List[int]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for part in _chunks(ids):
        marks = ",".join("?" for _ in part)
        for sid, isin in conn.execute(f"SELECT security_id, isin FROM securities WHERE security_id IN ({marks})", part):
            out[int(sid)] = str(isin)
    return out


def _closes(store: PriceStore, ids: List[int], session: str, vintage_at: str) -> Dict[int, float]:
    """Securities with a close for ``session`` visible at ``vintage_at`` (strict read), and that close."""
    df = store.close_raw(ids, session, session, vintage_at)
    if df is None or df.empty:
        return {}
    row = df.iloc[0]
    return {int(s): float(v) for s, v in row.items() if pd.notna(v)}


def _sessions(store: PriceStore, ids: List[int], through: str, n: int, vintage_at: str) -> List[str]:
    start = (pd.Timestamp(through) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    df = store.close_raw(ids, start, through, vintage_at)
    dates: List[str] = []
    if df is not None and not df.empty:
        dates = sorted((str(d) for d in df.index[df.notna().any(axis=1)] if str(d) <= through), reverse=True)[:n]
    return sorted(set(dates) | {through}, reverse=True)


def _find_inbox_file(inbox: Path, src: Source, session: str) -> Optional[Path]:
    if not inbox.is_dir():
        return None
    files = {p.name.lower(): p for p in sorted(inbox.iterdir()) if p.is_file()}
    for name in src.inbox_names(session):
        if name.lower() in files:
            return files[name.lower()]
    return None


def _first_seen_of_rows(store: PriceStore, capture_id: str) -> Optional[str]:
    """The earliest ``observed_at`` of the price rows that already carry ``capture_id`` (None when there are none)."""
    with store.conn() as p_conn:
        row = p_conn.execute("SELECT MIN(observed_at) FROM prices_daily WHERE capture_id = ?", (capture_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _archive(ctx: Any, store: PriceStore, src: Source, session: str, raw: bytes, fetched_at: str, version: str,
             zipped: bool) -> Tuple[str, str]:
    """Archive the bytes and register the capture; returns (capture_id, sha256).

    A capture row that exists is never changed. A missing row is registered with ``captured_at`` = ``fetched_at``,
    unless price rows with this capture_id already exist in the price store (an orphan: the row was lost in a
    staging rollback): then the earliest ``observed_at`` of those rows is the true first-seen time. A new row is
    also registered with ``ctx.keep_after_rollback`` so that a rollback of the run's staging does not take it away
    from price rows that are already committed in the separate price store.
    """
    sha = hashlib.sha256(raw).hexdigest()
    capture_id = f"cap_{src.kind}_{sha[:16]}"
    d = Path(ctx.cfg.paths.archive_dir) / "captures" / "prices_fallback"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{capture_id}.{'zip' if zipped else 'csv'}.gz"
    if not path.exists():
        with gzip.open(path, "wb") as f:
            f.write(raw)
    meta = d / f"{capture_id}.json"
    if not meta.exists():
        meta.write_text(json.dumps({"capture_id": capture_id, "fetched_at": fetched_at, "source": src.name, "url": version,
                                    "session": session, "sha256": sha}, indent=2), encoding="utf-8")
    if ctx.conn.execute("SELECT 1 FROM captures WHERE capture_id = ?", (capture_id,)).fetchone() is None:
        captured_at = _first_seen_of_rows(store, capture_id) or fetched_at
        values = (capture_id, captured_at, src.kind, str(path), sha, version, ctx.run_id)
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", values)
        keep = getattr(ctx, "keep_after_rollback", None)
        if cur.rowcount == 1 and keep is not None:
            keep("captures", pd.DataFrame([dict(zip(
                ("capture_id", "captured_at", "kind", "archive_path", "sha256", "source_version", "run_id"), values))]),
                ["capture_id"])
    return capture_id, sha


def _move_aside(path: Path, sha: str, folder: str) -> None:
    """Move an inbox file to ``<inbox>/<folder>/`` (``done`` or ``failed``); a name clash adds the sha prefix."""
    dest = path.parent / folder
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / path.name
    if target.exists():
        target = dest / f"{path.stem}.{sha[:8]}{path.suffix}"
    shutil.move(str(path), str(target))


# ----------------------------------------------------------------------------- the chain

def fill_missing(ctx: Any, store: PriceStore, security_ids: List[int], through: str,
                 client: Optional[ExchangeFileClient] = None) -> Result:
    """Fill closes that are missing for ``through`` and the sessions before it from the official files.

    See the module docstring for the rules. Returns ``Result(status="disabled")`` without a human decision.
    The first configured source is read for every session (its file is also a cross-check of the stored closes);
    later sources only while something is still missing. A problem with a source never raises: a file that
    cannot be read (OSError, a bad zip, a refused file, a failed fetch) is a WARN ``PRICE_FALLBACK_SOURCE_FAILED``
    event and the chain goes on; an inbox file that does not parse is moved to ``<inbox>/failed/``. In auto mode a
    source whose fetch got no HTTP answer at all is skipped for the remaining sessions of the call. An unpublished
    or absent file is only reported in the Result. An unknown source name or mode is a Refused, before any write.
    """
    if enabled_decision(ctx.conn) is None:
        return Result(status="disabled")
    cfg = ctx.cfg
    mode = mode_of(cfg)
    names = configured_sources(cfg)
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        raise Refused("unknown_source", f"price source(s) {unknown} are not in the registry {list(SOURCES)}")
    n_sessions = int(_ps(cfg, "fallback_sessions", 3))
    warn = float(_ps(cfg, "disagreement_warn", 0.005))
    warn_cross = float(_ps(cfg, "disagreement_warn_cross_exchange", 0.02))
    inbox = inbox_dir(cfg)
    ids = sorted({int(s) for s in security_ids})
    isin_of = _isin_map(ctx.conn, ids)
    sid_of = {v: k for k, v in isin_of.items()}

    entries: List[Dict[str, Any]] = []
    used: List[str] = []
    final_missing: Dict[str, int] = {}
    absent: List[Dict[str, Any]] = []
    filled_total = disagreements = failed = 0
    state: Dict[str, Optional[ExchangeFileClient]] = {"client": client}

    dead: set = set()                       # sources whose fetch failed at connection level: skipped for the rest of this call
    for session in _sessions(store, ids, through, n_sessions, ctx.clock.iso()):
        have = _closes(store, ids, session, ctx.clock.iso())
        missing = [s for s in ids if s not in have]
        for pos, name in enumerate(names):
            if pos > 0 and not missing:
                break
            src = SOURCES[name]
            entry: Dict[str, Any] = {"session": session, "source": name, "status": "", "filled": 0,
                                     "still_missing": len(missing), "compared": 0, "disagreements": 0}
            entries.append(entry)
            if name in dead:
                entry["status"] = "skipped"                               # reported once, when it failed
                continue
            origin: Optional[Path] = None
            fetching = False
            try:
                origin = _find_inbox_file(inbox, src, session)
                if origin is not None:
                    raw = origin.read_bytes()
                    fetched_at = ctx.clock.iso()                      # first seen now, never the file's mtime
                    version, zipped = f"manual-file:{origin.name}", origin.name.lower().endswith(".zip")
                elif mode == "manual":
                    entry["status"] = "absent"
                    if session == through:
                        absent.append({"source": name, "session": session, "file_name": src.file_name(session),
                                       "url": src.url_for(session), "inbox": str(inbox)})
                    continue
                else:
                    if state["client"] is None:
                        state["client"] = ExchangeFileClient(cfg, ctx.clock)
                    fetching = True
                    raw, fetched_at = state["client"].get(name, session)
                    version, zipped = src.url_for(session), src.zipped
            except SourceUnavailable:
                entry["status"] = "unavailable"
                failed += 1
                continue
            except _SOURCE_ERRORS as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                failed += 1
                if fetching and not isinstance(exc, requests.HTTPError):
                    dead.add(name)                                    # no answer at all (not an HTTP status): do not retry per session
                record_event(ctx, code="PRICE_FALLBACK_SOURCE_FAILED", severity="WARN",
                             detail={"date": session, "source": name, "error": str(exc)})
                continue
            try:
                df = src.parse(raw, session, zipped)
            except _SOURCE_ERRORS as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                failed += 1
                detail: Dict[str, Any] = {"date": session, "source": name, "error": str(exc)}
                if origin is not None:                                # a file that does not parse is not read again
                    try:
                        _move_aside(origin, hashlib.sha256(raw).hexdigest(), "failed")
                        detail["moved_to"] = "failed"
                    except OSError as move_exc:
                        detail["move_error"] = str(move_exc)
                record_event(ctx, code="PRICE_FALLBACK_SOURCE_FAILED", severity="WARN", detail=detail)
                continue

            capture_id, sha = _archive(ctx, store, src, session, raw, fetched_at, version, zipped)
            entry["capture_id"] = capture_id
            vend = df.set_index("isin")
            # cross-check against closes stored before this source is applied; another exchange has its own tolerance
            tol = warn if src.same_exchange else warn_cross
            bad: List[Dict[str, Any]] = []
            compared = 0
            for sid, stored in have.items():
                isin = isin_of.get(sid)
                if isin in vend.index and stored > 0:
                    compared += 1
                    vc = float(vend.at[isin, "close"])
                    rel = abs(vc - stored) / stored
                    if rel > tol:
                        bad.append({"security_id": sid, "isin": isin, "stored_close": stored, "vendor_close": vc, "rel_diff": rel})
            if bad:
                bad.sort(key=lambda r: (-r["rel_diff"], r["security_id"]))
                record_event(ctx, code="VENDOR_CLOSE_DISAGREEMENT", severity="WARN",
                             detail={"date": session, "source": name, "count": len(bad), "compared": compared,
                                     "tolerance": tol, "sample": bad[:10]})
            entry["compared"], entry["disagreements"] = compared, len(bad)
            disagreements += len(bad)
            # fill: only securities with no close for the date
            frames = []
            for sid in missing:
                isin = isin_of.get(sid)
                if isin not in vend.index:
                    continue
                v = vend.loc[isin]
                one = pd.DataFrame([{"date": session, "open": v["open"], "high": v["high"], "low": v["low"],
                                     "close": v["close"], "volume": v["volume"]}])
                norm = normalize_source(one, {"security_id": sid, "observed_at": fetched_at, "capture_id": capture_id,
                                              "source_sha256": sha, "close_basis": "raw", "volume_basis": "delivered",
                                              "dividend_basis": "delivered"})
                norm["yahoo_close"] = np.nan                           # not Yahoo values
                norm["yahoo_adj_close"] = np.nan
                frames.append(norm)
            filled = 0
            if frames:
                res = store.reconcile(ctx, pd.concat(frames, ignore_index=True))
                filled = int(res.counts.get("accepted", 0))
            if filled:
                have = _closes(store, ids, session, ctx.clock.iso())
                missing = [s for s in ids if s not in have]
                if name not in used:
                    used.append(name)
                record_event(ctx, code="PRICE_FALLBACK_USED", severity="INFO",
                             detail={"date": session, "source": name, "kind": src.kind, "capture_id": capture_id,
                                     "filled": filled, "still_missing": len(missing)})
            filled_total += filled
            entry.update(status="filled" if filled else "checked", filled=filled, still_missing=len(missing))
            # Last step for a file: only after the rows are reconciled, the capture is registered and the events are
            # recorded. Anything that raised above leaves the file in the inbox for the next run.
            if origin is not None:
                _move_aside(origin, sha, "done")
        final_missing[session] = len(missing)

    needed = absent if final_missing.get(through, 0) > 0 else []
    return Result(
        status="ok",
        counts={"sessions": len(final_missing), "filled": filled_total, "still_missing": final_missing.get(through, 0),
                "disagreements": disagreements, "sources_used": len(used), "sources_failed": failed},
        details={"mode": mode, "through": through, "sessions": entries, "sources_used": used,
                 "needed_files": needed if mode == "manual" else []},
    )


def summary(res: Result) -> Dict[str, Any]:
    """The counts of a fill_missing Result for a caller's details (status only when disabled)."""
    out: Dict[str, Any] = {"status": res.status}
    out.update(res.counts)
    if res.details:
        out["mode"] = res.details.get("mode")
        out["sources_used_names"] = list(res.details.get("sources_used", []))
    return out
