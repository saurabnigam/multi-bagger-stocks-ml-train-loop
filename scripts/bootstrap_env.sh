#!/usr/bin/env bash
# Create or repair the ONE shared Python environment: venv/ in the main working directory (the checkout
# that owns .git). Git worktrees share it through scripts/resolve_python.sh; no agent keeps a private copy.
#
#   scripts/bootstrap_env.sh            create venv/ if missing, install the pinned versions
#   scripts/bootstrap_env.sh --check    exit 0 if the environment matches requirements.lock, else 1
#
# Versions come from requirements.lock (exact pins the engine is validated on). BASE_PYTHON chooses the
# interpreter that creates the venv (default: python3.14, else python3). Installing downloads from PyPI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
MAIN="$ROOT"; [[ -n "$COMMON" ]] && MAIN="$(dirname "$COMMON")"
VENV="$MAIN/venv"
LOCK="$ROOT/requirements.lock"
[[ -f "$LOCK" ]] || { echo "bootstrap_env: $LOCK not found" >&2; exit 1; }

matches() {
  [[ -x "$VENV/bin/python" ]] || return 1
  diff <(grep -v '^#' "$LOCK" | sed '/^$/d' | sort) \
       <("$VENV/bin/python" -m pip freeze --all 2>/dev/null | grep -Ev '^(pip|setuptools|wheel)==' | sort) >/dev/null
}

if [[ "${1:-}" == "--check" ]]; then
  if matches; then echo "bootstrap_env: $VENV matches requirements.lock"; exit 0; fi
  echo "bootstrap_env: $VENV is missing or differs from requirements.lock; run scripts/bootstrap_env.sh" >&2; exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  BASE="${BASE_PYTHON:-}"
  if [[ -z "$BASE" ]]; then BASE="$(command -v python3.14 || command -v python3)"; fi
  echo "bootstrap_env: creating $VENV with $BASE"
  "$BASE" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$LOCK"
if matches; then echo "bootstrap_env: $VENV ready ($("$VENV/bin/python" --version))"; else
  echo "bootstrap_env: installed, but the environment still differs from requirements.lock" >&2; exit 1; fi
