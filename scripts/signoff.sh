#!/usr/bin/env bash
set -euo pipefail

echo "========================================================"
echo "    ANTIGRAVITY QUANT ENGINE V2 — PHASED SIGN-OFF"
echo "========================================================"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Stage 1: Engineering Acceptance
echo ""
echo "--- STAGE 1: ENGINEERING ACCEPTANCE ---"
python3 docs/spec/check_spec.py
PYTHONPATH=. venv/bin/pytest -q
echo "Engineering Acceptance: PASS"

# Stage 2: Operational Acceptance
echo ""
echo "--- STAGE 2: OPERATIONAL ACCEPTANCE ---"
# Check frozen legacy database SHA256 matches specification baseline
EXPECTED_LEGACY_SHA="03fe228b8fc90c63e8deddd33d1f9308693972af931aec7000c6870a34cb48a8"
if [ -f "quant_engine.db" ]; then
    ACTUAL_LEGACY_SHA=$(shasum -a 256 quant_engine.db | awk '{print $1}')
    if [ "$ACTUAL_LEGACY_SHA" != "$EXPECTED_LEGACY_SHA" ]; then
        echo "ERROR: quant_engine.db SHA mismatch! Expected $EXPECTED_LEGACY_SHA, got $ACTUAL_LEGACY_SHA"
        exit 1
    fi
    echo "Legacy frozen database SHA verified: $ACTUAL_LEGACY_SHA (PASS)"
fi

# Run read-only legacy migration check on isolated DB
ISOLATED_DB="/tmp/quant_isolated_signoff.db"
rm -f "$ISOLATED_DB"
PYTHONPATH=. venv/bin/python3 -c "
from pathlib import Path
from quant.db.core import connect, apply_schema
from quant.migrate.legacy import build_sample, run
from quant.config import load
from quant.types import Actor, FrozenClock
from quant.run import RunContext

sample_db = Path('/tmp/sample_signoff.db')
if sample_db.exists():
    sample_db.unlink()
build_sample(Path('quant_engine.db'), sample_db, ['360ONE.NS', 'ABB.NS'])

conn = connect('$ISOLATED_DB')
apply_schema(conn, kind='state')
with conn:
    conn.execute(
        'INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) '
        'VALUES (1, \'2026-09-03\', \'migration\', \'legacy\', 1, \'2026-09-03T18:30:00.000000Z\', \'ok\', \'sha1\', \'c_sha\', \'cfg_sha\', \'reg_sha\')'
    )
cfg = load().with_paths(db='$ISOLATED_DB')
clock = FrozenClock('2026-09-03T18:30:00.000000Z')
actor = Actor(kind='system', name='migration')
ctx = RunContext(as_of='2026-09-03', kind='migration', track='legacy', cfg=cfg, clock=clock, actor=actor)
ctx.conn = conn
ctx.run_id = 1
res = run(ctx=ctx, legacy_db_path=sample_db)
assert res.status == 'ok', f'Legacy migration failed on isolated DB: {res.status}'
sample_db.unlink(missing_ok=True)
"
rm -f "$ISOLATED_DB"
echo "Isolated DB migration & schema check: PASS"
echo "Operational Acceptance: PASS"

# Stage 3: Longitudinal Acceptance
echo ""
echo "--- STAGE 3: LONGITUDINAL ACCEPTANCE ---"
echo "Live forward-horizon maturity: DEFERRED (Awaiting 12-month forward live cohort maturity; zero live performance claimed at handoff)"
echo "Longitudinal Acceptance: DEFERRED"

echo ""
echo "========================================================"
echo "SUMMARY:"
echo "  Engineering:   PASS"
echo "  Operational:   PASS"
echo "  Longitudinal:  DEFERRED"
echo "========================================================"
