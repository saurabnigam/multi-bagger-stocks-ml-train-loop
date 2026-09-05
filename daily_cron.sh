#!/bin/bash
# V16 AI Quant Engine Automated Pipeline
#
# Order matters:
#   1. harness    - record today's snapshot with the CURRENT weights (this is what
#                   makes the stored final_score genuinely out-of-sample)
#   2. optimizer  - learn only from transitions it has not seen before (idempotent:
#                   re-running on the same data is a no-op)
#   3. ui export
#   4. health checks (non-zero exit if the snapshot has unit/bound errors)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "Starting V16 Pipeline: $(date)  (repo: $REPO_DIR)"

if [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

python db_setup.py
python harness_v16_learning.py
python weight_optimizer.py
python update_ui_v16.py
python eval_portfolio_health.py

echo "V16 Pipeline Complete: $(date)"
