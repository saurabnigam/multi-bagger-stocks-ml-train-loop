from pathlib import Path
import subprocess
import urllib.request
import pytest
import requests

from quant.cli import main as cli_main, register as cli_register
from quant.errors import Blocked, Refused
from tests.synthetic import make_world


def test_network_disabled_by_default():
    # Attempting an outbound HTTP call should be blocked by our autouse network guard
    with pytest.raises(Exception, match="(?i)network"):
        urllib.request.urlopen("http://example.com", timeout=1)

    with pytest.raises(Exception, match="(?i)network"):
        requests.get("http://example.com", timeout=1)


def test_synthetic_world_byte_identical_repetition(tmp_path):
    p1 = tmp_path / "w1"
    p2 = tmp_path / "w2"
    w1 = make_world(p1, seed=0, months=48)
    w2 = make_world(p2, seed=0, months=48)

    assert w1.months == w2.months
    assert w1.security_ids == w2.security_ids
    assert len(w1.security_ids) == 60
    assert len(w1.months) == 48

    # Check sessions DataFrame identical
    assert w1.sessions.equals(w2.sessions)
    assert w1.events == w2.events


def test_cli_help_and_dispatch():
    # CLI help returns 0
    assert cli_main(["--help"]) == 0
    assert cli_main(["db", "--help"]) == 0

    # Test error code mappings
    def raise_blocked(args):
        raise Blocked("g1_failed", "Not enough constituents")

    def raise_refused(args):
        raise Refused("policy_violation", "Illegal human actor")

    def raise_unexpected(args):
        raise RuntimeError("Something exploded")

    cli_register("testgroup", "blockcmd", raise_blocked, "Test blocked cmd")
    cli_register("testgroup", "refusecmd", raise_refused, "Test refuse cmd")
    cli_register("testgroup", "failcmd", raise_unexpected, "Test fail cmd")

    assert cli_main(["testgroup", "blockcmd"]) == 2
    assert cli_main(["testgroup", "refusecmd"]) == 3
    assert cli_main(["testgroup", "failcmd"]) == 1


def test_check_script_exists_and_executable():
    repo_root = Path(__file__).resolve().parents[2]
    check_sh = repo_root / "scripts/check.sh"
    assert check_sh.is_file()
    assert (check_sh.stat().st_mode & 0o111) != 0  # Executable
