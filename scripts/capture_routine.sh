#!/usr/bin/env bash
# Month-end capture routine (docs/operations/month_end.md). Safe to run any day, any number of times:
# every mode first checks whether there is anything to do.
#
#   capture_routine.sh prices        download closing prices for today's session if they are not complete
#   capture_routine.sh fundamentals  universe + fundamentals capture when the last one is old, or tonight is month-end
#   capture_routine.sh status        print the state of captures, coverage and what needs a person
#
# Code is this checkout. Data is $MBQ_DATA_ROOT (default: this checkout): quant.db, data/, knowledge/, ui/.
# Optional local settings: routine.env in the root of this checkout (MBQ_DATA_ROOT and QUANT_PYTHON only; see
# scripts/routine_env.sh). A variable already set in the environment wins over the file.
# Never resolves a corporate action, never runs the monthly pipeline, never pushes.
#
# Rehearsals: MBQ_REHEARSAL=1 together with MBQ_TODAY=YYYY-MM-DD and/or MBQ_HOUR=HH pretends it is that India
# date and hour. Without MBQ_REHEARSAL=1 those two variables are ignored with one warning. The "not before
# 16:00 India time" guard applies whenever the date being captured is the real current India date.
#
# Exit codes: 0 done or nothing to do; 1 failure or no database yet (prices, fundamentals); 10 closes still
# incomplete (run again later); 64 bad usage. `status` exits 0 whatever it could not read.
set -euo pipefail

MODE="${1:-}"
CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -r "$CODE_ROOT/scripts/routine_env.sh" ]]; then
  # shellcheck source=routine_env.sh
  source "$CODE_ROOT/scripts/routine_env.sh"
  load_routine_env "$CODE_ROOT"
fi
DATA_ROOT="${MBQ_DATA_ROOT:-$CODE_ROOT}"
if [[ -n "${QUANT_PYTHON:-}" ]]; then PY="$QUANT_PYTHON"
elif [[ -x "$CODE_ROOT/venv/bin/python" ]]; then PY="$CODE_ROOT/venv/bin/python"
else PY="python3"; fi

export PYTHONPATH="$CODE_ROOT" PYTHONDONTWRITEBYTECODE=1
export QUANT_DB_PATH="${QUANT_DB_PATH:-$DATA_ROOT/quant.db}"
export QUANT_PRICES_DB_PATH="${QUANT_PRICES_DB_PATH:-$DATA_ROOT/data/prices_daily.sqlite}"
export QUANT_DATA_DIR="${QUANT_DATA_DIR:-$DATA_ROOT/data}"
export QUANT_UI_DIR="${QUANT_UI_DIR:-$DATA_ROOT/ui}"
export QUANT_KNOWLEDGE_DIR="${QUANT_KNOWLEDGE_DIR:-$DATA_ROOT/knowledge}"
export QUANT_ARCHIVE_DIR="${QUANT_ARCHIVE_DIR:-$DATA_ROOT/data/archive}"
export QUANT_LEGACY_DB_PATH="${QUANT_LEGACY_DB_PATH:-$DATA_ROOT/quant_engine.db}"
export QUANT_LOCK_WAIT_S="${QUANT_LOCK_WAIT_S:-900}"      # wait up to 15 minutes for another writer

# The session date and time in India. MBQ_TODAY / MBQ_HOUR count only inside a rehearsal (MBQ_REHEARSAL=1).
REAL_TODAY="$(TZ=Asia/Kolkata date +%F)"
REAL_HOUR="$(TZ=Asia/Kolkata date +%H)"
if [[ "${MBQ_REHEARSAL:-}" == "1" ]]; then
  TODAY="${MBQ_TODAY:-$REAL_TODAY}"
  HOUR="${MBQ_HOUR:-$REAL_HOUR}"
else
  TODAY="$REAL_TODAY"; HOUR="$REAL_HOUR"
  if [[ -n "${MBQ_TODAY:-}" || -n "${MBQ_HOUR:-}" ]]; then
    echo "capture_routine: warning: MBQ_TODAY and MBQ_HOUR are ignored unless MBQ_REHEARSAL=1 is also set" >&2
  fi
fi
MIN_FRESH_DAYS="${MBQ_FUNDAMENTALS_MAX_AGE_DAYS:-3}"
ACTOR=(--actor-kind system --by system:scheduler)

LOG_DIR="$QUANT_DATA_DIR/logs"
LOG="$LOG_DIR/routine_${TODAY:0:7}.log"
say() { mkdir -p "$LOG_DIR"; printf '%s %s\n' "$(TZ=Asia/Kolkata date '+%F %T IST')" "$*" | tee -a "$LOG"; }
q() { ( cd "$CODE_ROOT" && "$PY" -m quant "$@" ); }
# True while the real India clock says the session for $TODAY has not closed (NSE closes 15:30 IST).
session_open() { [[ "$TODAY" == "$REAL_TODAY" && "$((10#$HOUR))" -lt 16 ]]; }

# Facts, as shell assignments F_<name>='value' (quoted by the interpreter). The calendar part needs no
# database. The database part is read-only, uses the engine's own functions and takes no lock; if it cannot
# be read, F_db_error says why.
# `facts cov` also counts today's closes against the universe (slower: loads the full engine).
facts() {
  ( cd "$CODE_ROOT" && "$PY" - "$TODAY" "${1:-}" <<'PYEOF'
import calendar, datetime, json, os, sqlite3, sys

today = sys.argv[1]
want_cov = sys.argv[2] == "cov"
out = {"today": today}
d = datetime.date.fromisoformat(today)
out["weekday"] = d.isoweekday()
last = datetime.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
while last.isoweekday() > 5:
    last -= datetime.timedelta(days=1)
out["month_end_weekday"] = last.isoformat()          # ignores exchange holidays; see the runbook
out["is_month_end_evening"] = (d == last)
try:
    from zoneinfo import ZoneInfo
    from quant.config import load as load_config

    cfg = load_config()
    if not os.path.isfile(cfg.paths.db):
        raise FileNotFoundError(f"no database at {cfg.paths.db}")
    conn = sqlite3.connect(f"file:{cfg.paths.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for kind in ("nifty500", "yahoo_bundle", "yahoo_prices"):
            r = conn.execute("SELECT max(captured_at) FROM captures WHERE kind = ?", (kind,)).fetchone()
            out[f"last_{kind}"] = r[0]
        live = conn.execute("SELECT cohort_id, as_of, is_clean FROM cohorts WHERE track = 'live' "
                            "ORDER BY as_of DESC LIMIT 1").fetchone()
        out["latest_live_id"] = live["cohort_id"] if live else None
        out["latest_live_as_of"] = live["as_of"] if live else None
        out["latest_live_clean"] = live["is_clean"] if live else None
        now = datetime.datetime.now(datetime.timezone.utc)
        lb = out["last_yahoo_bundle"]
        out["fundamentals_age_days"] = None
        out["fundamentals_done_after_1600_today"] = False
        if lb:
            when = datetime.datetime.fromisoformat(lb.replace("Z", "+00:00"))
            out["fundamentals_age_days"] = round((now - when).total_seconds() / 86400, 2)
            ist = when.astimezone(ZoneInfo("Asia/Kolkata"))
            out["fundamentals_done_after_1600_today"] = (ist.date() == d and ist.hour >= 16)
        try:
            if not want_cov:
                raise RuntimeError("coverage not requested")
            from quant.run import knowledge_cutoff
            from quant.data.price_admission import coverage
            from quant.data.prices import PriceStore
            from quant.data.universe import members_at
            members = members_at(conn, knowledge_cutoff(today), index_name="NIFTY500")
            sids = sorted(int(x) for x in members["security_id"].unique())
            store = PriceStore.open_readonly(cfg.paths.prices_db, state_conn=conn)
            cov = coverage(store, sids, today, now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
            out.update(members=len(sids), coverage=round(cov, 4), with_close=round(cov * len(sids)))
        except Exception as exc:                          # a fresh store, or no universe yet
            out.update(members=0, coverage=0.0, with_close=0, coverage_error=f"{type(exc).__name__}: {exc}"[:160])
        try:
            out["threshold"] = float(getattr(cfg.gates, "price_coverage", 0.98))
        except Exception:
            out["threshold"] = 0.98
    finally:
        conn.close()
except Exception as exc:                                  # no database, no tables, or the engine cannot be imported
    out["db_error"] = f"{type(exc).__name__}: {exc}"[:200]
import shlex
for k, v in out.items():
    print(f"F_{k}={shlex.quote('' if v is None else str(v))}")
PYEOF
  )
}
less_than() { awk -v a="${1:-0}" -v b="${2:-0}" 'BEGIN { exit !(a + 0 < b + 0) }'; }
# percent 0.98 -> 98% ; 0.975 -> 97.5% (whole number when it is one, else one decimal)
percent() { awk -v t="${1:-0}" 'BEGIN { p = t * 100; r = int(p + 0.5); d = p - r; if (d < 0) d = -d
  if (d < 1e-9) printf "%d%%", r; else printf "%.1f%%", p }'; }

# The newest snapshot-YYYYMMDDTHHMMSSZ folder holding INVENTORY.json in the backup folder, and how old it is.
# Reads only; prints "last backup: ..." and, when there is none or it is too old, one ACTION NEEDED line.
backup_line() {
  local dir="${QUANT_BACKUP_DIR:-$(dirname "$CODE_ROOT")/mb-backups}" max="${MBQ_BACKUP_MAX_AGE_DAYS:-35}" d newest="" age=""
  for d in "$dir"/snapshot-????????T??????Z; do
    [[ -d "$d" && -f "$d/INVENTORY.json" ]] || continue
    [[ "$(basename "$d")" =~ ^snapshot-[0-9]{8}T[0-9]{6}Z$ ]] || continue
    newest="$(basename "$d")"                       # the glob is sorted, so the last match is the newest
  done
  if [[ -n "$newest" ]]; then
    age="$(awk -v stamp="${newest#snapshot-}" -v now="$(date -u +%s)" 'BEGIN {
      y = substr(stamp, 1, 4) + 0; m = substr(stamp, 5, 2) + 0; d = substr(stamp, 7, 2) + 0
      hh = substr(stamp, 10, 2) + 0; mi = substr(stamp, 12, 2) + 0; ss = substr(stamp, 14, 2) + 0
      if (m <= 2) y -= 1
      era = int(y / 400); yoe = y - era * 400
      doy = int((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1
      doe = yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy
      days = era * 146097 + doe - 719468
      secs = days * 86400 + hh * 3600 + mi * 60 + ss
      a = int((now - secs) / 86400); if (a < 0) a = 0
      print a }')" || age=""
    echo "last backup:             $newest (${age:-?} days ago)"
  else
    echo "last backup:             none"
  fi
  if [[ -z "$newest" || -z "$age" ]] || (( age > max )); then
    echo "ACTION NEEDED: no backup in the last ${max} days. Run scripts/backup_state.sh (it copies the price store and raw archive, which git does not hold)."
  fi
}

at_least() { awk -v a="${1:-0}" -v b="${2:-1}" 'BEGIN { exit !(a + 0 >= b + 0) }'; }

# load_facts MESSAGE [cov] : run facts and apply its assignments; on failure say MESSAGE and exit 1 (or return 1).
load_facts() {
  local out
  if out="$(facts "${2:-}")"; then eval "$out"; return 0; fi
  if [[ -n "$1" ]]; then say "$1"; exit 1; fi
  return 1
}

# prices and fundamentals need a state database that exists and has its tables: one clear line, exit 1.
require_db() {   # require_db MODE
  if [[ ! -f "$QUANT_DB_PATH" ]]; then
    say "$1: no database yet at $QUANT_DB_PATH; run the first capture by hand (python -m quant data capture)"; exit 1
  fi
  load_facts "$1: cannot read the state (the interpreter $PY failed)"        # quick: no coverage
  if [[ -n "${F_db_error:-}" ]]; then
    say "$1: the state database cannot be read (${F_db_error:-})"; exit 1
  fi
}

case "$MODE" in
  prices)
    require_db prices
    if [[ "${F_weekday:-0}" -gt 5 ]]; then say "prices: $TODAY is a weekend; no session"; exit 0; fi
    # A bar fetched while the market is open is not a close.
    if session_open; then say "prices: before 16:00 IST; the session has not closed"; exit 0; fi
    load_facts "prices: cannot read the state (the interpreter $PY failed)" cov
    THR="${F_threshold:-}"
    if at_least "${F_coverage:-}" "$THR"; then
      say "prices: closes for $TODAY already complete (${F_with_close:-} of ${F_members:-}, need ${THR})"; exit 0
    fi
    say "prices: closes for $TODAY at ${F_with_close:-} of ${F_members:-}; downloading"
    q prices update --through "$TODAY" "${ACTOR[@]}" >>"$LOG" 2>&1 || { say "prices: update FAILED (see $LOG)"; exit 1; }
    load_facts "prices: cannot read the state after the download" cov
    if at_least "${F_coverage:-}" "$THR"; then
      say "prices: closes for $TODAY complete after download (${F_with_close:-} of ${F_members:-})"; exit 0
    fi
    say "prices: closes for $TODAY still incomplete (${F_with_close:-} of ${F_members:-}, need ${THR}); run again later"
    exit 10
    ;;
  fundamentals)
    require_db fundamentals
    AGE="${F_fundamentals_age_days:-}"; EVE="${F_is_month_end_evening:-}"
    if [[ "$EVE" == "True" ]]; then
      # Month-end evening: capture once, after the close. A capture that finished after 16:00 IST today is enough.
      if [[ "${F_fundamentals_done_after_1600_today:-}" == "True" ]]; then
        say "fundamentals: month-end evening and the last capture finished after 16:00 IST today (${F_last_yahoo_bundle:-}); nothing to do"; exit 0
      fi
    elif [[ -n "$AGE" ]] && less_than "$AGE" "$MIN_FRESH_DAYS"; then
      say "fundamentals: last capture is ${AGE} days old (limit ${MIN_FRESH_DAYS}); nothing to do"; exit 0
    fi
    if session_open && [[ "${F_weekday:-9}" -le 5 ]]; then
      say "fundamentals: before 16:00 IST on a session day; the capture also reads today's prices, so it waits"; exit 0
    fi
    say "fundamentals: capturing (last ${AGE:-never} days ago; month-end evening: $EVE); about 60-75 minutes"
    q universe capture "${ACTOR[@]}" >>"$LOG" 2>&1 || { say "fundamentals: universe capture FAILED (see $LOG)"; exit 1; }
    q data capture "${ACTOR[@]}" >>"$LOG" 2>&1 || { say "fundamentals: data capture FAILED (see $LOG)"; exit 1; }
    say "fundamentals: capture complete"
    ;;
  status)
    set +e      # a sub-step that cannot be read is reported, never fatal
    load_facts "" cov || true
    echo "date (India):            $TODAY"
    echo "month-end session:       ${F_month_end_weekday:-}   tonight is the cohort evening: ${F_is_month_end_evening:-}"
    if [[ ! -f "$QUANT_DB_PATH" ]]; then
      echo "state database:          no database yet at $QUANT_DB_PATH"
      echo "write lock:              no database yet"
    elif [[ -n "${F_db_error:-}" ]]; then
      echo "state database:          cannot be read (${F_db_error:-})"
      echo "write lock:              unknown"
    else
      echo "last universe capture:   ${F_last_nifty500:-}"
      echo "last fundamentals:       ${F_last_yahoo_bundle:-}   (${F_fundamentals_age_days:-} days ago)"
      echo "last price download:     ${F_last_yahoo_prices:-}"
      echo "closes for $TODAY:  ${F_with_close:-} of ${F_members:-}   (need $(percent "${F_threshold:-}"))"
      if [[ -z "${F_latest_live_id:-}" ]]; then
        echo "latest live list:        none yet"
      elif [[ "${F_latest_live_clean:-}" == "1" ]]; then
        echo "latest live list:        ${F_latest_live_id} (as of ${F_latest_live_as_of:-}, clean)"
      else
        echo "latest live list:        ${F_latest_live_id} (as of ${F_latest_live_as_of:-}, scored late or not clean)"
      fi
      RAW="$(q status 2>/dev/null)" || RAW=""       # `quant status` takes no actor flags
      # One line from the JSON key "write_lock": "free" or {"actor","kind","pid","started_at"}.
      LOCK_LINE="$(printf '%s' "$RAW" | "$PY" -c 'import json, sys
try:
    d, _ = json.JSONDecoder().raw_decode(sys.stdin.read().lstrip())
    w = d["write_lock"]
    if w == "free":
        print("write lock: free")
    else:
        print("write lock: held by %s (%s, pid %s) since %s" % (w.get("actor"), w.get("kind"), w.get("pid"), w.get("started_at")))
except Exception:
    print("write lock: unknown")' 2>/dev/null)" || LOCK_LINE=""
      echo "${LOCK_LINE:-write lock: unknown}"
      MONTH_END="${F_month_end_weekday:-}"
      TRIAGE="$(q triage list --as-of "$MONTH_END" --json "${ACTOR[@]}" 2>/dev/null)" || TRIAGE=""
      OPEN="$(printf '%s' "$TRIAGE" | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin); rows = d.get("anomalies", d.get("rows", [])) if isinstance(d, dict) else d
    print(sum(1 for r in rows if str(r.get("status", "")).lower() in ("new", "proposed")))
except Exception:
    print("?")' 2>/dev/null)" || OPEN="?"
      [[ "$OPEN" =~ ^[0-9]+$ ]] || OPEN="?"
      echo "price-jump flags open:   $OPEN"
      if [[ "$OPEN" != "0" && "$OPEN" != "?" ]]; then
        echo "ACTION NEEDED: $OPEN price-jump flag(s) await a person (python -m quant triage list --as-of $MONTH_END)."
      fi
    fi
    backup_line
    if [[ "${F_is_month_end_evening:-}" == "True" ]]; then
      echo "ACTION NEEDED: tonight is the cohort evening. Review the open price-jump flags and record approvals before 23:59 IST."
    fi
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") prices|fundamentals|status" >&2; exit 64
    ;;
esac
