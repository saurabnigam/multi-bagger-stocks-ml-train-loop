#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Interpreter resolution (MASTER_SPEC 10.1/10.5): $QUANT_PYTHON, else venv/bin/python, else python3.
# This must match scripts/signoff.sh and monthly_cron.sh so all three agree on which
# Python actually runs the engine.
if [[ -n "${QUANT_PYTHON:-}" ]]; then
    PY="${QUANT_PYTHON}"
elif [[ -x "venv/bin/python" ]]; then
    PY="venv/bin/python"
else
    PY="python3"
fi

export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Using interpreter: ${PY}"

echo "Running specification checker..."
"${PY}" docs/spec/check_spec.py

echo "Running full test suite..."
"${PY}" -m pytest -q

echo "All checks PASSED."
