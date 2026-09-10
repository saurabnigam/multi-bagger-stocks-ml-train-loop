#!/usr/bin/env bash
# Monthly Orchestration Runner Script for the V2 Quant Engine (MASTER_SPEC 10.5).
#
# NOTE: No cron schedule is automatically installed by this repository. An owner
# schedules this script (system cron, launchd, etc.) as an explicit deployment
# action; running it manually first is expected before any such scheduling.
# NOTE: This runner NEVER pushes to remote automatically. Any caller-supplied
# --push is refused below, not forwarded, regardless of who invokes this script.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

# Interpreter resolution (must match scripts/check.sh and scripts/signoff.sh):
# $QUANT_PYTHON, else venv/bin/python, else python3.
if [[ -n "${QUANT_PYTHON:-}" ]]; then
    PY="${QUANT_PYTHON}"
elif [[ -x "venv/bin/python" ]]; then
    PY="venv/bin/python"
else
    PY="python3"
fi

export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

# Refuse an explicit --push outright: automated/scheduled runs must never push
# (AGENTS.md prime directive, MASTER_SPEC 10.5). A human reviews and pushes by hand.
for arg in "$@"; do
    if [[ "${arg}" == "--push" ]]; then
        echo "monthly_cron.sh: refusing --push; scheduled/automated runs never push to remote." >&2
        echo "Review the run's output and push manually if it looks correct." >&2
        exit 1
    fi
done

LOG_DIR="${ROOT_DIR}/data/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/monthly_$(date -u +'%Y%m%dT%H%M%SZ').log"

LOCK_DIR="${ROOT_DIR}/data/.locks"
mkdir -p "${LOCK_DIR}"
LOCK_FILE="${LOCK_DIR}/monthly_cron.lock"

run_pipeline() {
    echo "Starting monthly quant pipeline run at $(date -u +'%Y-%m-%dT%H:%M:%SZ') using ${PY}..."
    "${PY}" -m quant run monthly "$@"
    echo "Pipeline execution completed successfully."
}

# Concurrency guard: a second overlapping invocation (e.g. a slow prior run still
# settling orders when cron fires again) must not race the same state database.
# Prefer flock (atomic, releases automatically on crash); fall back to a mkdir
# lock on platforms without flock (e.g. stock macOS).
if command -v flock >/dev/null 2>&1; then
    exec 200>"${LOCK_FILE}"
    if ! flock -n 200; then
        echo "monthly_cron.sh: another run already holds ${LOCK_FILE}; exiting without change." | tee -a "${LOG_FILE}" >&2
        exit 1
    fi
    run_pipeline "$@" 2>&1 | tee -a "${LOG_FILE}"
else
    LOCK_MARKER="${LOCK_FILE}.d"
    if ! mkdir "${LOCK_MARKER}" 2>/dev/null; then
        echo "monthly_cron.sh: another run already holds ${LOCK_MARKER}; exiting without change." | tee -a "${LOG_FILE}" >&2
        exit 1
    fi
    trap 'rmdir "${LOCK_MARKER}" 2>/dev/null || true' EXIT
    run_pipeline "$@" 2>&1 | tee -a "${LOG_FILE}"
fi
