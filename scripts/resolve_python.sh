#!/usr/bin/env bash
# The one rule for "which Python runs the engine" (MASTER_SPEC 10.1). Every script uses it, so every
# checkout and every agent runs the same environment:
#   1. $QUANT_PYTHON, when set
#   2. venv/bin/python in this checkout
#   3. venv/bin/python in the main working directory (the checkout that owns .git): a git worktree
#      has no venv of its own and shares that one
#   4. python3
# Create the shared environment with scripts/bootstrap_env.sh.
#
# Usage: PY="$(scripts/resolve_python.sh)", or source this file and call: resolve_python ROOT_DIR
resolve_python() {
  local root="$1" common main
  if [[ -n "${QUANT_PYTHON:-}" ]]; then printf '%s\n' "$QUANT_PYTHON"; return 0; fi
  if [[ -x "$root/venv/bin/python" ]]; then printf '%s\n' "$root/venv/bin/python"; return 0; fi
  common="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$common" ]]; then
    main="$(dirname "$common")"
    if [[ "$main" != "$root" && -x "$main/venv/bin/python" ]]; then printf '%s\n' "$main/venv/bin/python"; return 0; fi
  fi
  printf '%s\n' python3
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  resolve_python "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
