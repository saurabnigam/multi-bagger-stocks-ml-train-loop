#!/usr/bin/env bash
set -euo pipefail

# Resolve repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Activate virtualenv if present
if [[ -f "venv/bin/activate" ]]; then
    source venv/bin/activate
fi

echo "Running specification checker..."
python3 docs/spec/check_spec.py

echo "Running full test suite..."
python -m pytest -q

echo "All checks PASSED."
