#!/usr/bin/env bash
# Snapshot the files git does not hold: the state database, the daily price store and the raw archives.
# Usage: scripts/backup_state.sh [--dry-run] [DEST_DIR]
#   DEST_DIR defaults to $QUANT_BACKUP_DIR, else "mb-backups" next to the repository folder.
#   KEEP (default 6) is the number of snapshots to keep, counting the new one.
#   QUANT_LOCK_WAIT_S (default 900) is how long to wait for the state write lock.
# Sources: QUANT_DB_PATH, QUANT_PRICES_DB_PATH, QUANT_ARCHIVE_DIR, else quant.db, data/prices_daily.sqlite,
# data/archive under the data root: MBQ_DATA_ROOT (environment or routine.env), else the repository root.
# The script never writes inside the repository or the data root.
# The two databases are copied while holding the engine's state write lock, each through the SQLite backup
# API from a read-only connection, so the copies are consistent with each other and the sources are not
# opened for writing. If the interpreter cannot import the quant package the script fails; it never falls
# back to a plain copy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -r "$ROOT/scripts/routine_env.sh" ]]; then
    # shellcheck source=routine_env.sh
    source "$ROOT/scripts/routine_env.sh"
    load_routine_env "$ROOT"
fi
DRY=0
DEST_ARG=""
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "backup_state: unknown option $a" >&2; exit 2 ;;
        *) [[ -z "$DEST_ARG" ]] || { echo "backup_state: only one DEST_DIR is allowed" >&2; exit 2; }; DEST_ARG="$a" ;;
    esac
done

die() { echo "backup_state: $*" >&2; exit 1; }

abs_from_root() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$ROOT" "$1" ;; esac; }

check_plain() {   # check_plain WHAT VALUE : refuse a single quote or a newline
    case "$2" in
        *"'"*) die "a single quote in $1 is not supported: $2" ;;
        *$'\n'*) die "a newline in $1 is not supported" ;;
    esac
}

# Absolute, symlink-resolved path of something that may not exist yet (deepest existing ancestor + rest).
resolve_path() {
    local p rest="" base
    case "$1" in /*) p="$1" ;; *) p="$PWD/$1" ;; esac
    case "/$p/" in */../*) die "destination must not contain '..': $1" ;; esac
    while [[ ! -d "$p" ]]; do
        rest="/$(basename "$p")$rest"
        base="$(dirname "$p")"
        [[ "$base" != "$p" ]] || break
        p="$base"
    done
    printf '%s%s' "$(cd "$p" && pwd -P)" "$rest"
}

DATA_ROOT="${MBQ_DATA_ROOT:-$ROOT}"
SRC_DB="$(abs_from_root "${QUANT_DB_PATH:-$DATA_ROOT/quant.db}")"
SRC_PRICES="$(abs_from_root "${QUANT_PRICES_DB_PATH:-$DATA_ROOT/data/prices_daily.sqlite}")"
SRC_ARCHIVE="$(abs_from_root "${QUANT_ARCHIVE_DIR:-$DATA_ROOT/data/archive}")"
KEEP="${KEEP:-6}"
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "KEEP must be a positive integer, got '$KEEP'"
LOCK_WAIT="${QUANT_LOCK_WAIT_S:-900}"

DEST_RAW="${DEST_ARG:-${QUANT_BACKUP_DIR:-$(dirname "$ROOT")/mb-backups}}"
check_plain "the destination" "$DEST_RAW"
DEST="$(resolve_path "$DEST_RAW")"
check_plain "the destination" "$DEST"
case "$DEST/" in
    "$ROOT"/*) die "destination $DEST is inside the repository $ROOT; choose a folder outside it" ;;
esac
if [[ -n "${MBQ_DATA_ROOT:-}" && -d "$MBQ_DATA_ROOT" ]]; then
    DATA_REAL="$(cd "$MBQ_DATA_ROOT" && pwd -P)"
    case "$DEST/" in
        "$DATA_REAL"/*) die "destination $DEST is inside the data root $DATA_REAL; choose a folder outside it" ;;
    esac
fi

if [[ -n "${QUANT_PYTHON:-}" ]]; then PY="$QUANT_PYTHON"
elif [[ -x "$ROOT/venv/bin/python" ]]; then PY="$ROOT/venv/bin/python"
else PY="python3"; fi
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1

[[ -f "$SRC_DB" ]] || die "state database not found: $SRC_DB"
[[ -f "$SRC_PRICES" ]] || die "price store not found: $SRC_PRICES"
[[ -d "$SRC_ARCHIVE" ]] || die "archive directory not found: $SRC_ARCHIVE"
DB_NAME="$(basename "$SRC_DB").gz"
PRICES_NAME="$(basename "$SRC_PRICES").gz"
[[ "$DB_NAME" != "$PRICES_NAME" ]] || die "the two databases share a file name ($DB_NAME); rename one"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SNAP="$DEST/snapshot-$STAMP"
PARTIAL="$DEST/.snapshot-$STAMP.partial"
[[ ! -e "$SNAP" ]] || die "snapshot already exists: $SNAP"

# Snapshots this script made: snapshot-<stamp> holding an INVENTORY.json, oldest first, one per line.
list_own() {
    local d
    [[ -d "$DEST" ]] || return 0
    for d in "$DEST"/snapshot-????????T??????Z; do
        if [[ -d "$d" && -f "$d/INVENTORY.json" ]]; then echo "$d"; fi
    done | sort
}

# prune_oldest TOTAL : delete the oldest of our snapshots until TOTAL is at most KEEP; never $SNAP.
# $1 is the number of snapshots there will be once the new one is in place. With DRY=1 only report.
prune_oldest() {
    local total="$1" excess i=0 d list
    excess=$(( total - KEEP ))
    [[ "$excess" -gt 0 ]] || return 0
    list="$(list_own)"
    while IFS= read -r d; do
        [[ -n "$d" && "$d" != "$SNAP" ]] || continue
        [[ "$i" -lt "$excess" ]] || break
        i=$(( i + 1 ))
        if [[ "$DRY" -eq 1 ]]; then echo "  would delete older snapshot $d"
        else rm -rf "$d"; echo "backup_state: deleted older snapshot $d"; fi
    done <<< "$list"
}
count_own() { local list; list="$(list_own)"; if [[ -z "$list" ]]; then echo 0; else printf '%s\n' "$list" | wc -l | tr -d ' '; fi; }

if [[ "$DRY" -eq 1 ]]; then
    echo "backup_state: dry run, nothing is written"
    echo "  would create $SNAP"
    echo "    $DB_NAME            <- read-only SQLite backup of $SRC_DB, under the state write lock"
    echo "    $PRICES_NAME   <- read-only SQLite backup of $SRC_PRICES, under the same lock"
    echo "    archive.tar.gz      <- $SRC_ARCHIVE"
    echo "    INVENTORY.json      (name, bytes, sha256 per file; sources; UTC time; git commit)"
    prune_oldest $(( $(count_own) + 1 ))
    echo "  keep newest $KEEP snapshots"
    exit 0
fi

sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
bytes_of() { wc -c < "$1" | tr -d ' '; }
json_str() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

"$PY" -c "import quant.db.lock" >/dev/null 2>&1 \
    || die "cannot import the quant package with $PY (PYTHONPATH=$PYTHONPATH); refusing to copy without the state write lock"

mkdir -p "$DEST"
cleanup() { [[ -d "$PARTIAL" ]] && rm -rf "$PARTIAL"; return 0; }
trap cleanup EXIT
mkdir "$PARTIAL"

# Copy both databases under the state write lock; read-only source connections; then gzip (no timestamp).
"$PY" - "$LOCK_WAIT" "$PARTIAL" "$SRC_DB" "$DB_NAME" "$SRC_PRICES" "$PRICES_NAME" <<'PYEOF' || die "database copy failed (see the message above)"
import gzip, os, shutil, sqlite3, sys
from urllib.parse import quote

wait_s, partial, state_db, state_name, prices_db, prices_name = sys.argv[1:7]
from quant.db import lock

try:
    handle = lock.acquire(state_db, {"actor": "system:backup", "kind": "system", "command": "backup_state.sh"},
                          float(wait_s))
except lock.StateLocked as exc:
    print(f"backup_state: could not get the state write lock within {wait_s}s: {exc}", file=sys.stderr)
    sys.exit(3)
try:
    for src, name in ((state_db, state_name), (prices_db, prices_name)):
        raw = os.path.join(partial, ".copy.sqlite")
        ro = sqlite3.connect("file:" + quote(os.path.abspath(src)) + "?mode=ro", uri=True, timeout=30)
        dest = sqlite3.connect(raw)
        try:
            ro.backup(dest)
        finally:
            dest.close()
            ro.close()
        with open(raw, "rb") as fin, open(os.path.join(partial, name), "wb") as fout:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fout, compresslevel=9, mtime=0) as gz:
                shutil.copyfileobj(fin, gz)
        os.remove(raw)
finally:
    lock.release(handle)
PYEOF

tar -czf "$PARTIAL/archive.tar.gz" -C "$(dirname "$SRC_ARCHIVE")" "$(basename "$SRC_ARCHIVE")" \
    || die "tar failed for $SRC_ARCHIVE"

GIT_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
{
    echo "{"
    echo "  \"created_utc\": \"$CREATED\","
    echo "  \"git_commit\": \"$GIT_COMMIT\","
    echo "  \"sources\": {"
    echo "    \"state_db\": \"$(json_str "$SRC_DB")\","
    echo "    \"prices_db\": \"$(json_str "$SRC_PRICES")\","
    echo "    \"archive_dir\": \"$(json_str "$SRC_ARCHIVE")\""
    echo "  },"
    echo "  \"files\": ["
    first=1
    for name in "$DB_NAME" "$PRICES_NAME" "archive.tar.gz"; do
        [[ "$first" -eq 1 ]] || echo ","
        first=0
        printf '    {"name": "%s", "bytes": %s, "sha256": "%s"}' \
            "$(json_str "$name")" "$(bytes_of "$PARTIAL/$name")" "$(sha256_of "$PARTIAL/$name")"
    done
    echo ""
    echo "  ]"
    echo "}"
} > "$PARTIAL/INVENTORY.json"

# Put the new snapshot in place first; only then prune, so the new one is never lost.
mv "$PARTIAL" "$SNAP"
prune_oldest "$(count_own)"
echo "backup_state: wrote $SNAP"
for name in "$DB_NAME" "$PRICES_NAME" archive.tar.gz INVENTORY.json; do
    echo "  $name $(bytes_of "$SNAP/$name") bytes"
done
echo "backup_state: copy $DEST to another machine or cloud storage yourself; this script writes only locally"
