#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Interpreter resolution (MASTER_SPEC 10.1/10.5): scripts/resolve_python.sh: $QUANT_PYTHON, else this checkout's venv, else the main working directory's venv, else python3.
# This must match scripts/signoff.sh and monthly_cron.sh so all three agree on which
# Python actually runs the engine.
PY="$(bash "${REPO_ROOT}/scripts/resolve_python.sh")"

export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Using interpreter: ${PY}"

echo "Running specification checker..."
"${PY}" docs/spec/check_spec.py

echo "Running full test suite..."
"${PY}" -m pytest -q

echo "All checks PASSED."
