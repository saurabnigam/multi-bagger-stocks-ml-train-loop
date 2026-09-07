from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from quant.config import load as load_config
from quant.types import (
    Actor,
    Check,
    CheckReport,
    Clock,
    CriteriaCheck,
    Draft,
    FrozenClock,
    HacResult,
    Result,
    SystemClock,
    World,
)
from quant.errors import (
    Blocked,
    ImmutableConflict,
    LookaheadError,
    QuantError,
    Refused,
    SourceChanged,
)


def test_config_loads_canonical_keys_and_paths():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config()
    
    # Check top-level / nested sections
    assert cfg.spec_revision == 2
    assert cfg.schema_version == 1
    assert cfg.learning.weight_units == 10000
    assert cfg.yahoo.accessor_sleep_s == 0.5
    assert cfg.yahoo.batch_size == 25
    assert cfg.budget.hypotheses_per_year == 6
    assert cfg.costs.fixed_bps_one_way == 12.0
    
    # Paths resolve from repo root
    assert cfg.paths.db.is_absolute()
    assert cfg.paths.db == repo_root / "quant.db"
    assert cfg.paths.legacy_db == repo_root / "quant_engine.db"
    assert cfg.paths.prices_db == repo_root / "data/prices_daily.sqlite"
    assert cfg.paths.data_dir == repo_root / "data"
    assert cfg.paths.knowledge_dir == repo_root / "knowledge"
    assert cfg.paths.ui_dir == repo_root / "ui"
    assert cfg.paths.archive_dir == repo_root / "data/archive"
    
    # Policy hash exists and is non-empty hex
    assert isinstance(cfg.policy_sha256, str)
    assert len(cfg.policy_sha256) == 64


def test_path_override_does_not_alter_policy_hash(tmp_path):
    cfg1 = load_config()
    cfg2 = cfg1.with_paths(
        db=tmp_path / "custom.db",
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
    )
    assert cfg2.paths.db == tmp_path / "custom.db"
    assert cfg2.policy_sha256 == cfg1.policy_sha256


def test_frozen_clock_preserves_utc_microseconds():
    c = json.loads(
        Path("docs/spec/contracts/golden_cases.json").read_text()
    )["cases"]["pit_cutoff"]
    cutoff_str = c["cutoff"]  # "2026-09-30T18:29:59.999999Z"
    clock = FrozenClock(cutoff_str)
    now = clock.now()
    
    assert now.tzinfo == timezone.utc
    assert now.microsecond == 999999
    assert now.year == 2026
    assert now.month == 9
    assert now.day == 30
    assert now.hour == 18
    assert now.minute == 29
    assert now.second == 59
    assert clock.iso() == cutoff_str


def test_system_clock_returns_aware_utc():
    clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None
    assert now.tzinfo == timezone.utc


def test_actor_identity():
    a = Actor(kind="human", name="OWNER")
    assert a.by == "human:OWNER"
    assert a.kind == "human"
    assert a.name == "OWNER"
    
    a_llm = Actor(kind="llm", name="gemini-flash")
    assert a_llm.by == "llm:gemini-flash"


def test_check_report_passed_and_deferred():
    c_pass = Check(
        id="G1", status="PASS", observed=500, expected=480, reason="ok", blocking=True
    )
    c_def = Check(
        id="G4", status="DEFERRED", observed=None, expected=None, reason="first month", blocking=True
    )
    c_nonblock_fail = Check(
        id="W1", status="FAIL", observed=20, expected=10, reason="warning", blocking=False
    )
    report = CheckReport(checks=[c_pass, c_def, c_nonblock_fail])
    assert report.passed is True
    assert report.deferred == ["G4"]
    
    c_block_fail = Check(
        id="G2", status="FAIL", observed=70, expected=62, reason="stale", blocking=True
    )
    report_fail = CheckReport(checks=[c_pass, c_block_fail])
    assert report_fail.passed is False


def test_error_hierarchy_and_fields():
    assert issubclass(Blocked, QuantError)
    assert issubclass(Refused, QuantError)
    assert issubclass(LookaheadError, QuantError)
    assert issubclass(ImmutableConflict, QuantError)
    assert issubclass(SourceChanged, QuantError)
    
    err = Blocked(code="calendar_missing", detail="Missing 2026 sessions")
    assert err.code == "calendar_missing"
    assert err.detail == "Missing 2026 sessions"
    
    c_err = ImmutableConflict(table="daily_predictions", key={"id": 1})
    assert c_err.table == "daily_predictions"
    assert c_err.key == {"id": 1}
