"""
Shared configuration for the Quant Engine.

Every script previously hard-coded an absolute path into a private scratch
directory, which meant a fresh clone (or an agent following AGENTS.md) could
not run anything. All paths are now resolved relative to this file, with an
environment-variable override for tests and CI.
"""
import os

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# Override with QUANT_DB_PATH=/some/other/quant_engine.db
DB_PATH = os.environ.get('QUANT_DB_PATH', os.path.join(REPO_DIR, 'quant_engine.db'))

UI_DIR = os.environ.get('QUANT_UI_DIR', os.path.join(REPO_DIR, 'ui'))

# Minimum number of scored stocks for a snapshot to count as a full-universe run.
FULL_UNIVERSE_MIN = 100

# A forward move larger than this over a ~1-month holding period is treated as
# an unadjusted corporate action (split / bonus / demerger) rather than a real
# return. ZFCVINDIA.NS logged -84% in June 2026, which was a 6:1 split.
CORPORATE_ACTION_ABS_RETURN = 0.60
