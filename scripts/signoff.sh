#!/usr/bin/env bash
# V2 Quant Engine phased sign-off runner (MASTER_SPEC 11; TEST_AND_VERIFICATION_PLAN
# section 8, rows S01-S15).
#
# This script runs real checks and reports their real subprocess exit codes and
# elapsed times. It never prints a status string that was not derived from an
# actual command's outcome: PASS/FAIL come from a real exit code, and DEFERRED
# means the check's stated prerequisite (an env flag, a real database, enough
# live cohorts) was absent, so the check was not attempted at all.
#
# Usage: scripts/signoff.sh [--phase engineering|operational|longitudinal|all]
#                            [--as-of DATE] [--output PATH]
#
# Exit codes: 0 = every requested check passed and none was deferred.
#             1 = at least one requested check failed.
#             2 = no failures, but at least one requested check was deferred.
# (S15 is a research observation row and never affects the exit code, per
# TEST_AND_VERIFICATION_PLAN section 8.)

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

PHASE="engineering"
AS_OF=""
OUTPUT_PATH=""

usage() {
    cat <<'EOF'
Usage: scripts/signoff.sh [--phase engineering|operational|longitudinal|all] [--as-of DATE] [--output PATH]

Runs the V2 phased sign-off checks (TEST_AND_VERIFICATION_PLAN.md section 8, rows
S01-S15) and prints one markdown row per check, plus a JSON report.

Options:
  --phase PHASE   engineering (default) | operational | longitudinal | all
  --as-of DATE    ISO date (YYYY-MM-DD) passed to date-sensitive checks
                  (a real monthly run, verify pit/leakage). Optional.
  --output PATH   also write the JSON report to PATH.
  --help          show this help and exit 0.

Interpreter resolution: $QUANT_PYTHON, else venv/bin/python, else python3.

Exit codes:
  0  every requested check passed; none was deferred.
  1  at least one requested check failed.
  2  no failures, but at least one requested check was deferred
     (its prerequisite - an env flag, a real database, enough live
     cohorts - was absent, so it was not attempted).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --phase)
            if [[ $# -lt 2 ]]; then
                echo "signoff.sh: --phase requires a value" >&2
                exit 1
            fi
            PHASE="$2"
            shift 2
            ;;
        --as-of)
            if [[ $# -lt 2 ]]; then
                echo "signoff.sh: --as-of requires a value" >&2
                exit 1
            fi
            AS_OF="$2"
            shift 2
            ;;
        --output)
            if [[ $# -lt 2 ]]; then
                echo "signoff.sh: --output requires a value" >&2
                exit 1
            fi
            OUTPUT_PATH="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "signoff.sh: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

case "${PHASE}" in
    engineering|operational|longitudinal|all) ;;
    *)
        echo "signoff.sh: invalid --phase '${PHASE}' (want engineering|operational|longitudinal|all)" >&2
        exit 1
        ;;
esac

# Interpreter resolution (must match scripts/check.sh and monthly_cron.sh):
# $QUANT_PYTHON, else venv/bin/python, else python3.
if [[ -n "${QUANT_PYTHON:-}" ]]; then
    PY="${QUANT_PYTHON}"
elif [[ -x "venv/bin/python" ]]; then
    PY="venv/bin/python"
else
    PY="python3"
fi

export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

echo "V2 sign-off: phase=${PHASE} interpreter=${PY} as-of=${AS_OF:-<none>}" >&2

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/quant_signoff.XXXXXX")"
cleanup() { rm -rf "${TMP_ROOT}"; }
trap cleanup EXIT

ROWS_RAW="${TMP_ROOT}/rows.raw"
: > "${ROWS_RAW}"

declare -a MD_ROWS
MD_ROWS+=("id|phase|status|command|exit_code|elapsed_s|observed|reason")
MD_ROWS+=("---|---|---|---|---|---|---|---")

# ---------------------------------------------------------------- helpers

# run_check CMD...  ->  sets LAST_EXIT, LAST_ELAPSED, LAST_OUTPUT from a real
# subprocess invocation. Never fabricates a result.
run_check() {
    echo "  running: $*" >&2
    SECONDS=0
    LAST_OUTPUT="$("$@" 2>&1)"
    LAST_EXIT=$?
    LAST_ELAPSED=${SECONDS}
}

# summarize TEXT -> last non-blank line, capped at 300 chars (for the
# "observed" column; full output already streamed to stderr above).
summarize() {
    local text="$1"
    local line
    line="$(printf '%s\n' "${text}" | awk 'NF{last=$0} END{print last}')"
    if [[ ${#line} -gt 300 ]]; then
        line="${line:0:297}..."
    fi
    printf '%s' "${line}"
}

# record_row id phase status command exit_code elapsed_s observed reason
record_row() {
    local id="$1" phase="$2" status="$3" command="$4" exit_code="$5" elapsed_s="$6" observed="$7" reason="$8"
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1e' \
        "${id}" "${phase}" "${status}" "${command}" "${exit_code}" "${elapsed_s}" "${observed}" "${reason}" \
        >> "${ROWS_RAW}"
    local md_command="${command//|/\/}"
    local md_observed="${observed//|/\/}"
    local md_reason="${reason//|/\/}"
    MD_ROWS+=("${id}|${phase}|${status}|${md_command}|${exit_code}|${elapsed_s}|${md_observed}|${md_reason}")
}

status_from_exit() {
    [[ "$1" -eq 0 ]] && echo "PASS" || echo "FAIL"
}

# ---------------------------------------------------------- helper scripts
# These run inside $TMP_ROOT (never under the repository) so isolated checks
# cannot mutate real project state. They are generated here, not committed,
# because this module group owns only scripts/signoff.sh.

cat > "${TMP_ROOT}/check_s04.py" <<'PYEOF'
"""S04: isolated `db init` + schema FK/immutability checks."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

work_dir = Path(sys.argv[1])
py = sys.argv[2]
work_dir.mkdir(parents=True, exist_ok=True)
db_path = work_dir / "state.db"

env = os.environ.copy()
env["QUANT_DB_PATH"] = str(db_path)

result = subprocess.run([py, "-m", "quant", "db", "init"], env=env, capture_output=True, text=True)
print(result.stdout.strip())
if result.returncode != 0:
    print(result.stderr.strip(), file=sys.stderr)
    sys.exit(1)

if not db_path.exists():
    print(f"db init reported success but {db_path} does not exist")
    sys.exit(1)

conn = sqlite3.connect(str(db_path))
try:
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    table_count = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    immutable_triggers = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'immutable_%'"
    ).fetchone()[0]
    journal_triggers = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'quant_journal_%'"
    ).fetchone()[0]
finally:
    conn.close()

print(
    f"tables={table_count} immutable_triggers={immutable_triggers} "
    f"journal_triggers={journal_triggers} fk_violations={len(fk_violations)}"
)

if fk_violations:
    print(f"foreign key violations: {fk_violations}")
    sys.exit(1)
if immutable_triggers == 0:
    print("no immutability triggers found in sqlite_master")
    sys.exit(1)
if table_count == 0:
    print("no tables found after db init")
    sys.exit(1)
sys.exit(0)
PYEOF

cat > "${TMP_ROOT}/check_s05.py" <<'PYEOF'
"""S05: isolated `db export` -> `db rebuild` -> `db verify`, seeded through a
RunContext, entirely inside a temp directory with an isolated config.toml
(absolute paths) so nothing touches the repository's real state or ledger."""
import os
import re
import subprocess
import sys
from pathlib import Path

work_dir = Path(sys.argv[1])
py = sys.argv[2]
repo_root = Path(sys.argv[3])

work_dir.mkdir(parents=True, exist_ok=True)
db_path = work_dir / "state.db"
data_dir = work_dir / "data"
data_dir.mkdir(parents=True, exist_ok=True)

real_toml = (repo_root / "config" / "quant.toml").read_text(encoding="utf-8")
isolated_toml = re.sub(r'(?m)^db\s*=.*$', f'db = "{db_path.as_posix()}"', real_toml, count=1)
isolated_toml = re.sub(r'(?m)^data_dir\s*=.*$', f'data_dir = "{data_dir.as_posix()}"', isolated_toml, count=1)
config_path = work_dir / "quant_isolated.toml"
config_path.write_text(isolated_toml, encoding="utf-8")

sys.path.insert(0, str(repo_root))
from quant.config import load as load_config  # noqa: E402
from quant.db.core import apply_schema, append_rows, connect  # noqa: E402
from quant.run import RunContext  # noqa: E402
from quant.types import Actor, FrozenClock  # noqa: E402

import pandas as pd  # noqa: E402

cfg = load_config(str(config_path))
if Path(cfg.paths.db) != db_path or Path(cfg.paths.data_dir) != data_dir:
    print("isolated config.toml did not repoint db/data_dir as expected")
    sys.exit(1)

conn = connect(cfg.paths.db)
apply_schema(conn, kind="state")
conn.close()

clock = FrozenClock("2026-09-30T18:30:00.000000Z")
actor = Actor(kind="system", name="signoff")
with RunContext(as_of="2026-09-30", kind="signoff_smoke", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
    seed = pd.DataFrame([{
        "security_id": 1,
        "isin": "SYNSIGNOFF01",
        "name": "Signoff Synthetic",
        "first_seen": "2026-01-01",
        "last_seen": "2026-09-30",
        "status": "listed",
    }])
    append_rows(ctx, "securities", seed, ["security_id"])
    ctx.checkpoint()


def run_cli(*args):
    result = subprocess.run(
        [py, "-m", "quant", *args, "--config", str(config_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    print(f"$ quant {' '.join(args)} --config {config_path.name}")
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode


rebuilt_db = work_dir / "rebuilt.db"
rc_export = run_cli("db", "export")
rc_rebuild = run_cli("db", "rebuild", "--output", str(rebuilt_db))
rc_verify = run_cli("db", "verify")

ok = rc_export == 0 and rc_rebuild == 0 and rc_verify == 0 and rebuilt_db.exists()
print(
    f"export_exit={rc_export} rebuild_exit={rc_rebuild} verify_exit={rc_verify} "
    f"rebuilt_db_exists={rebuilt_db.exists()}"
)
sys.exit(0 if ok else 1)
PYEOF

cat > "${TMP_ROOT}/check_real_state.py" <<'PYEOF'
"""Reports whether a real state database exists and how many live cohorts it
holds, so operational/longitudinal checks can decide PASS/DEFERRED honestly."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from quant.config import load  # noqa: E402

cfg = load()
db_path = Path(cfg.paths.db)
present = db_path.exists()
live_cohorts = 0
if present:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            live_cohorts = conn.execute("SELECT count(*) FROM cohorts WHERE track='live'").fetchone()[0]
        except sqlite3.Error:
            live_cohorts = 0
        finally:
            conn.close()
    except sqlite3.Error:
        live_cohorts = 0

print(f"REAL_DB_PATH={db_path}")
print(f"REAL_DB_PRESENT={'1' if present else '0'}")
print(f"LIVE_COHORT_COUNT={live_cohorts}")
PYEOF

cat > "${TMP_ROOT}/check_s15.py" <<'PYEOF'
"""S15: research observation only. Reports whatever rank_ic evidence exists;
never asserts a required outcome and never affects the sign-off exit code."""
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
try:
    rows = conn.execute(
        "SELECT track, metric, value, status, as_of FROM evaluations "
        "WHERE metric='rank_ic' ORDER BY as_of DESC LIMIT 5"
    ).fetchall()
except sqlite3.Error as exc:
    print(f"no evaluations table / no rank_ic rows available: {exc}")
    sys.exit(0)
finally:
    conn.close()

if not rows:
    print("no rank_ic evaluations recorded yet; nothing to observe")
    sys.exit(0)

for track, metric, value, status, as_of in rows:
    v = value if value is not None else float("nan")
    sign = "positive" if v > 0 else ("negative" if v < 0 else "zero")
    print(f"as_of={as_of} track={track} metric={metric} sign={sign} value={value} status={status}")
sys.exit(0)
PYEOF

cat > "${TMP_ROOT}/aggregate.py" <<'PYEOF'
"""Combines recorded rows into the JSON report and computes the real exit
code (0/1/2) per TEST_AND_VERIFICATION_PLAN section 8. S15 (status
OBSERVATION) never counts toward failure or deferral."""
import json
import sys
from datetime import datetime, timezone

rows_raw_path, phase_requested, as_of, interpreter, output_path = sys.argv[1:6]

text = open(rows_raw_path, "r", encoding="utf-8").read()
rows = []
for rec in text.split("\x1e"):
    if not rec.strip():
        continue
    fields = rec.split("\x1f")
    if len(fields) != 8:
        continue
    rid, phase, status, command, exit_code, elapsed_s, observed, reason = fields
    rows.append({
        "id": rid,
        "phase": phase,
        "status": status,
        "command": command,
        "exit_code": None if exit_code == "" else int(exit_code),
        "elapsed_s": float(elapsed_s) if elapsed_s != "" else None,
        "observed": observed,
        "reason": reason,
        "artifact_refs": [],
    })

fail_rows = [r for r in rows if r["status"] == "FAIL"]
deferred_rows = [r for r in rows if r["status"] == "DEFERRED"]

if fail_rows:
    exit_code = 1
elif deferred_rows:
    exit_code = 2
else:
    exit_code = 0

report = {
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    "phase_requested": phase_requested,
    "as_of": as_of or None,
    "interpreter": interpreter,
    "rows": rows,
    "summary": {
        "total": len(rows),
        "pass": sum(1 for r in rows if r["status"] == "PASS"),
        "fail": len(fail_rows),
        "deferred": len(deferred_rows),
        "observation": sum(1 for r in rows if r["status"] == "OBSERVATION"),
        "exit_code": exit_code,
    },
}

payload = json.dumps(report, indent=2)
if output_path:
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(payload + "\n")

print(payload)
sys.exit(exit_code)
PYEOF

# --------------------------------------------------------------- S01-S08

run_engineering_checks() {
    run_check "${PY}" docs/spec/check_spec.py
    record_row "S01" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} docs/spec/check_spec.py" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "spec checks pass; canonical copies unchanged"

    run_check "${PY}" -m pytest -q
    record_row "S02" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} -m pytest -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "all default tests pass; measured runtime; legacy modules included"

    run_check "${PY}" -m pytest test_quant_math.py test_optimizer.py -q
    record_row "S03" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} -m pytest test_quant_math.py test_optimizer.py -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "unchanged legacy suite passes"

    run_check "${PY}" "${TMP_ROOT}/check_s04.py" "${TMP_ROOT}/s04" "${PY}"
    record_row "S04" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "isolated db init + PRAGMA foreign_key_check + immutability trigger count" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "exact canonical tables/constraints, no magic count"

    run_check "${PY}" "${TMP_ROOT}/check_s05.py" "${TMP_ROOT}/s05" "${PY}" "${ROOT_DIR}"
    record_row "S05" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "isolated db export -> db rebuild -> db verify, seeded via RunContext" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "table hashes/control-state journal match"

    run_check "${PY}" -m pytest tests/integration/test_ws11_01.py -q
    record_row "S06" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} -m pytest tests/integration/test_ws11_01.py -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "synthetic complete/retry/concurrent/crash run; P1-P12 and relevant T1-T10 pass"

    local ws09_files=(tests/unit/test_ws09_*.py)
    run_check "${PY}" -m pytest "${ws09_files[@]}" -q
    record_row "S07" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} -m pytest tests/unit/test_ws09_*.py -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "identity, tiers, prospective versions and expiry behavior pass"

    run_check "${PY}" -m pytest tests/integration/test_ws11_02.py -q
    record_row "S08" "engineering" "$(status_from_exit "${LAST_EXIT}")" \
        "${PY} -m pytest tests/integration/test_ws11_02.py -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
        "$(summarize "${LAST_OUTPUT}")" "8 tabs, empty/blocked states, no errors/network dependencies"
}

# --------------------------------------------------------------- S09-S12

run_operational_checks() {
    eval "$("${PY}" "${TMP_ROOT}/check_real_state.py" "${ROOT_DIR}")"

    if [[ "${QUANT_NETWORK:-}" == "1" ]]; then
        run_check "${PY}" -m pytest -m network -q
        record_row "S09" "operational" "$(status_from_exit "${LAST_EXIT}")" \
            "${PY} -m pytest -m network -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "captured source bases/units and actual archive manifest verified"
    else
        record_row "S09" "operational" "DEFERRED" "${PY} -m pytest -m network -q" "" "0" \
            "not run" "prerequisite absent: set QUANT_NETWORK=1 to run real adapter tests"
    fi

    if [[ "${QUANT_LEGACY_REAL:-}" == "1" ]]; then
        run_check "${PY}" -m pytest -m legacy_real -q
        record_row "S10" "operational" "$(status_from_exit "${LAST_EXIT}")" \
            "${PY} -m pytest -m legacy_real -q" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "real source unchanged; reconciliation and repeat no-op pass"
    else
        record_row "S10" "operational" "DEFERRED" "${PY} -m pytest -m legacy_real -q" "" "0" \
            "not run" "prerequisite absent: set QUANT_LEGACY_REAL=1 to run the real migration suite"
    fi

    if [[ "${REAL_DB_PRESENT:-0}" == "1" ]]; then
        # bash 3.2 (macOS default) raises "unbound variable" on an EMPTY
        # array expansion under `set -u`, so branch instead of building a
        # possibly-empty --as-of args array.
        if [[ -n "${AS_OF}" ]]; then
            run_check "${PY}" -m quant run monthly --as-of "${AS_OF}"
        else
            run_check "${PY}" -m quant run monthly
        fi
        local ec="${LAST_EXIT}"
        local status
        if [[ "${ec}" -eq 0 ]]; then
            status="PASS"
        elif [[ "${ec}" -eq 2 ]]; then
            status="DEFERRED"
        else
            status="FAIL"
        fi
        record_row "S11" "operational" "${status}" \
            "${PY} -m quant run monthly${AS_OF:+ --as-of ${AS_OF}}" "${ec}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "eligible pre-cutoff captures; actual successful cohort; or DEFERRED if calendar/capture prerequisite absent"

        run_check "${PY}" -m quant db size
        record_row "S12" "operational" "$(status_from_exit "${LAST_EXIT}")" \
            "${PY} -m quant db size" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "measured targets and exceeded budgets listed; no invented timings"
    else
        record_row "S11" "operational" "DEFERRED" "${PY} -m quant run monthly" "" "0" \
            "not run" "prerequisite absent: no real state database present at ${REAL_DB_PATH:-configured db path}"
        record_row "S12" "operational" "DEFERRED" "${PY} -m quant db size" "" "0" \
            "not run" "prerequisite absent: no real state database present at ${REAL_DB_PATH:-configured db path}"
    fi
}

# --------------------------------------------------------------- S13-S15

run_longitudinal_checks() {
    eval "$("${PY}" "${TMP_ROOT}/check_real_state.py" "${ROOT_DIR}")"

    if [[ "${LIVE_COHORT_COUNT:-0}" -ge 3 ]]; then
        run_check "${PY}" -m quant verify pit --months 3
        record_row "S13" "longitudinal" "$(status_from_exit "${LAST_EXIT}")" \
            "${PY} -m quant verify pit --months 3" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "available 3 cohorts identical; missing cohorts DEFERRED"
    else
        record_row "S13" "longitudinal" "DEFERRED" "${PY} -m quant verify pit --months 3" "" "0" \
            "not run" "prerequisite absent: fewer than 3 live cohorts exist (found ${LIVE_COHORT_COUNT:-0})"
    fi

    if [[ "${LIVE_COHORT_COUNT:-0}" -ge 3 && -n "${AS_OF}" ]]; then
        run_check "${PY}" -m quant verify leakage --as-of "${AS_OF}"
        record_row "S14" "longitudinal" "$(status_from_exit "${LAST_EXIT}")" \
            "${PY} -m quant verify leakage --as-of ${AS_OF}" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "prerequisites and outcomes reported; insufficient labels DEFERRED"
    else
        local reason="prerequisite absent: fewer than 3 live cohorts exist (found ${LIVE_COHORT_COUNT:-0})"
        [[ "${LIVE_COHORT_COUNT:-0}" -ge 3 && -z "${AS_OF}" ]] && reason="prerequisite absent: pass --as-of DATE to run the real leakage check"
        record_row "S14" "longitudinal" "DEFERRED" "${PY} -m quant verify leakage --as-of DATE" "" "0" \
            "not run" "${reason}"
    fi

    # S15 is a research observation: it always reports whatever is available
    # and never affects the exit code.
    if [[ "${REAL_DB_PRESENT:-0}" == "1" ]]; then
        run_check "${PY}" "${TMP_ROOT}/check_s15.py" "${REAL_DB_PATH}"
        record_row "S15" "research" "OBSERVATION" \
            "inspect evaluations for observed rank_ic sign/band/track" "${LAST_EXIT}" "${LAST_ELAPSED}" \
            "$(summarize "${LAST_OUTPUT}")" "report observed sign/band/track; no required positive outcome"
    else
        record_row "S15" "research" "OBSERVATION" \
            "inspect evaluations for observed rank_ic sign/band/track" "" "0" \
            "no live evaluations available yet" "report observed sign/band/track; no required positive outcome"
    fi
}

# ------------------------------------------------------------------- run

case "${PHASE}" in
    engineering)
        run_engineering_checks
        ;;
    operational)
        run_operational_checks
        ;;
    longitudinal)
        run_longitudinal_checks
        ;;
    all)
        run_engineering_checks
        run_operational_checks
        run_longitudinal_checks
        ;;
esac

printf '%s\n' "${MD_ROWS[@]}"

"${PY}" "${TMP_ROOT}/aggregate.py" "${ROWS_RAW}" "${PHASE}" "${AS_OF}" "${PY}" "${OUTPUT_PATH}"
FINAL_EXIT=$?

exit "${FINAL_EXIT}"
