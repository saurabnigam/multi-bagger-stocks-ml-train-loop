#!/usr/bin/env bash
# Monthly Orchestration Runner Script for Antigravity Quant Engine V2
# NOTE: No cron schedule is automatically installed. Run via system cron or manual trigger.
# NOTE: In accordance with Prime Directives and Master Spec, this runner NEVER pushes to remote automatically.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

export PYTHONPATH=.

echo "Starting monthly quant pipeline run at $(date -u +'%Y-%m-%dT%H:%M:%SZ')..."
python3 -m quant run monthly "$@"

echo "Pipeline execution completed successfully."
