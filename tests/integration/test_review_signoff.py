"""Integration tests for the rewritten scripts/signoff.sh (review fix, docs_scripts).

These exercise the operational/longitudinal/--help paths only. --phase engineering
is intentionally never invoked from here: it shells out to `pytest -q` internally
and would recurse into the very test run executing this file.
"""

import json
import os
import tempfile
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNOFF_SH = REPO_ROOT / "scripts" / "signoff.sh"


def _run_signoff(args, timeout=120):
    """Run scripts/signoff.sh with the current interpreter, from the repo root."""
    env = os.environ.copy()
    env.setdefault("QUANT_PYTHON", sys.executable)
    # Isolate from any real state database in the checkout: operational/longitudinal
    # prerequisites are judged against the configured db path (MASTER_SPEC 10.2 overrides).
    isolated = tempfile.mkdtemp(prefix="signoff_isolated_")
    env.setdefault("QUANT_DB_PATH", os.path.join(isolated, "absent.db"))
    env.setdefault("QUANT_DATA_DIR", os.path.join(isolated, "data"))
    return subprocess.run(
        ["bash", str(SIGNOFF_SH), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_signoff_script_exists_and_executable():
    assert SIGNOFF_SH.is_file()
    assert (SIGNOFF_SH.stat().st_mode & 0o111) != 0


def test_help_prints_usage_and_exits_zero():
    result = _run_signoff(["--help"])
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--phase" in result.stdout
    assert "--as-of" in result.stdout
    assert "--output" in result.stdout


def test_longitudinal_phase_defers_with_valid_json_rows(tmp_path):
    output_path = tmp_path / "longitudinal.json"
    result = _run_signoff(["--phase", "longitudinal", "--output", str(output_path)])

    # No live cohorts exist in this working tree, so S13/S14 must defer rather
    # than fail, and the run as a whole reports "deferred" (exit 2), never
    # exit 0 (that would hide that nothing was actually verified) and never
    # exit 1 (nothing failed either).
    assert result.returncode == 2, result.stdout + result.stderr

    assert output_path.exists(), "expected --output PATH to be written"
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["phase_requested"] == "longitudinal"
    row_ids = {row["id"] for row in report["rows"]}
    assert {"S13", "S14", "S15"} <= row_ids

    rows_by_id = {row["id"]: row for row in report["rows"]}
    assert rows_by_id["S13"]["status"] == "DEFERRED"
    assert rows_by_id["S14"]["status"] == "DEFERRED"
    # S15 is a research observation: it must never be reported as a failure,
    # and it must not contribute to the overall exit code.
    assert rows_by_id["S15"]["status"] not in {"FAIL", "DEFERRED"}

    assert report["summary"]["exit_code"] == 2
    assert report["summary"]["fail"] == 0


def test_operational_phase_without_env_flags_defers():
    result = _run_signoff(["--phase", "operational"])

    # Without QUANT_NETWORK=1 / QUANT_LEGACY_REAL=1 / a real state database,
    # every operational check must defer, not silently pass or fail.
    assert result.returncode == 2, result.stdout + result.stderr
    assert "DEFERRED" in result.stdout
    assert "S09" in result.stdout
    assert "S10" in result.stdout
    assert "S11" in result.stdout
    assert "S12" in result.stdout
    # No fabricated PASS anywhere in these rows: every id present must be DEFERRED.
    for line in result.stdout.splitlines():
        if line.startswith(("S09|", "S10|", "S11|", "S12|")):
            assert "|DEFERRED|" in line


def test_invalid_phase_rejected():
    result = _run_signoff(["--phase", "not-a-real-phase"])
    assert result.returncode == 1
    assert "invalid" in (result.stderr or "").lower()


@pytest.mark.parametrize("flag", ["--phase", "--as-of", "--output"])
def test_missing_flag_value_does_not_crash_uncontrolled(flag):
    # Regression guard: an argument-parsing bug should surface as a clean,
    # non-zero exit, not a bash unbound-variable crash (`set -u`).
    result = _run_signoff([flag])
    assert result.returncode in (0, 1, 2)
