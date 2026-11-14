#!/usr/bin/env bash
# Run the engine CLI with THIS checkout's code against the data root (docs/operations/month_end.md).
#
#   scripts/q.sh status
#   scripts/q.sh triage list --as-of 2026-11-30
#   scripts/q.sh run monthly
#
# Data root: $MBQ_DATA_ROOT, else routine.env in this checkout, else this checkout. It holds quant.db, data/,
# knowledge/ and ui/. An explicit QUANT_* path variable wins. Interpreter: $QUANT_PYTHON, else routine.env,
# else venv/bin/python, else python3. The exit code is the engine's.
set -euo pipefail

CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -eq 0 ]]; then
  echo "usage: $(basename "$0") <quant command> [arguments]   (for example: $(basename "$0") status)" >&2; exit 64
fi
# Optional local settings (MBQ_DATA_ROOT, QUANT_PYTHON). The environment wins; the file is parsed, never run.
if [[ -f "$CODE_ROOT/scripts/routine_env.sh" ]]; then
  # shellcheck source=scripts/routine_env.sh
  . "$CODE_ROOT/scripts/routine_env.sh"
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

cd "$CODE_ROOT"
exec "$PY" -m quant "$@"
