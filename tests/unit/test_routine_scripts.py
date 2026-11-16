"""scripts/capture_routine.sh and scripts/backup_state.sh, run as subprocesses against tmp worlds.

Offline: the scripts get a QUANT_PYTHON wrapper that forwards to this interpreter but fakes every command that
would touch the network (`prices update`, `universe capture`, `data capture`) and, when asked, `status` and
`triage list`. The wrapper logs each call, so a test can prove a download was or was not attempted.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.db import lock as dblock
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock

REPO = Path(__file__).resolve().parents[2]
ROUTINE = REPO / "scripts" / "capture_routine.sh"
BACKUP = REPO / "scripts" / "backup_state.sh"

PAST_TUE = "2026-11-10"          # a session day in the past
PAST_SUN = "2026-11-08"
MONTH_END = "2026-10-30"         # last weekday of October 2026 (31 Oct is a Saturday)
NIFTY_AT = "2026-10-29T10:00:00.000000Z"
OBSERVED = "2026-11-10T11:00:00.000000Z"

WRAPPER = """#!/bin/bash
# forwards to the real interpreter; fakes what would use the network
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *"prices update"*) exit "${FAKE_PRICES_RC:-99}" ;;
  *"universe capture"*|*"data capture"*) exit "${FAKE_CAPTURE_RC:-0}" ;;
  *"-m quant status"*)
    if [[ -n "${FAKE_STATUS_FILE:-}" ]]; then cat "$FAKE_STATUS_FILE"; exit "${FAKE_STATUS_RC:-0}"; fi ;;
  *"-m quant triage list"*)
    if [[ -n "${FAKE_TRIAGE_FILE:-}" ]]; then cat "$FAKE_TRIAGE_FILE"; exit "${FAKE_TRIAGE_RC:-0}"; fi
    if [[ -n "${FAKE_TRIAGE_RC:-}" ]]; then exit "$FAKE_TRIAGE_RC"; fi ;;
esac
exec "$REAL_PY" "$@"
"""


# ----------------------------------------------------------------------------- helpers

def _india_today() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()


def _is_weekday(day: str) -> bool:
    return datetime.date.fromisoformat(day).isoweekday() <= 5


def _env(tmp_path: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MBQ_", "QUANT_", "FAKE_"))}
    wrapper = tmp_path / "pywrap.sh"
    if not wrapper.exists():
        wrapper.write_text(WRAPPER)
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    env.update(QUANT_PYTHON=str(wrapper), REAL_PY=sys.executable, FAKE_LOG=str(tmp_path / "calls.log"),
               PYTHONDONTWRITEBYTECODE="1")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _routine(tmp_path: Path, mode: str, root: Path, **extra):
    env = _env(tmp_path, MBQ_DATA_ROOT=root, **extra)
    return subprocess.run(["bash", str(ROUTINE), mode], env=env, capture_output=True, text=True, timeout=120,
                          cwd=str(tmp_path))


def _rehearse(tmp_path, mode, root, today, hour=None, **extra):
    extra = dict(MBQ_REHEARSAL=1, MBQ_TODAY=today, **extra)
    if hour is not None:
        extra["MBQ_HOUR"] = hour
    return _routine(tmp_path, mode, root, **extra)


def make_world(root: Path, *, close_date: str = PAST_TUE, members: int = 5, with_close: int = 5,
               bundle_at: str | None = None, cohort_clean: int | None = None) -> Path:
    """A data root with the engine's own schema: `members` NIFTY500 securities, `with_close` of them with a bar."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    db = root / "quant.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=root / "data" / "prices_daily.sqlite", data_dir=root / "data",
                                   archive_dir=root / "data" / "archive", knowledge_dir=root / "knowledge",
                                   ui_dir=root / "ui")
    with RunContext(as_of=close_date, kind="test", track="live", cfg=cfg,
                    clock=FrozenClock("2026-11-10T12:00:00.000000Z"), actor=Actor(kind="human", name="owner")) as ctx:
        for sid in range(1, members + 1):
            ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                             "VALUES (?, ?, ?, '2025-01-01', '2026-11-10', 'listed')", (sid, f"INE{sid:09d}", f"S{sid}"))
            ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                             "VALUES (?, ?, ?, '2025-01-01', 'test')", (sid, f"S{sid}", f"S{sid}.NS"))
            ctx.conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, "
                             "nse_sector, series, source) VALUES ('2026-10-29', ?, ?, 'NIFTY500', ?, 'Industrials', 'EQ', 'nse_csv')",
                             (NIFTY_AT, sid, f"S{sid}"))
        ctx.conn.execute("INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                         "VALUES ('cap_n500', ?, 'nifty500', 'x', ?, 'test', ?)", (NIFTY_AT, "0" * 64, ctx.run_id))
        if bundle_at:
            ctx.conn.execute("INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                             "VALUES ('cap_bundle', ?, 'yahoo_bundle', 'x', ?, 'test', ?)", (bundle_at, "1" * 64, ctx.run_id))
        if cohort_clean is not None:
            ctx.conn.execute(
                "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
                "source_refs_json, published_at, generated_at, is_clean, run_id) "
                "VALUES ('live:2026-10-30', '2026-10-30', 'live', '2026-10-30T10:00:00.000000Z', 'd', 'm', '{}', "
                "'2026-10-30T12:00:00.000000Z', '2026-10-30T12:00:00.000000Z', ?, ?)", (cohort_clean, ctx.run_id))
        store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        for sid in range(1, with_close + 1):
            store.ingest(ctx, pd.DataFrame({"date": [close_date], "close": [100.0], "volume": [1e5]}),
                         {"security_id": sid, "close_basis": "raw", "observed_at": OBSERVED})
    return root


def _utc_ago(days: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture(scope="module")
def world_complete(tmp_path_factory):
    return make_world(tmp_path_factory.mktemp("complete"), with_close=5, bundle_at=_utc_ago(1))


@pytest.fixture(scope="module")
def world_partial(tmp_path_factory):
    return make_world(tmp_path_factory.mktemp("partial"), with_close=3, bundle_at=_utc_ago(10))


# ----------------------------------------------------------------------------- capture_routine.sh: prices

def test_unknown_mode_prints_usage_and_exits_64(tmp_path):
    r = _routine(tmp_path, "bogus", tmp_path / "empty")
    assert r.returncode == 64 and "usage:" in r.stderr
    r = subprocess.run(["bash", str(ROUTINE)], env=_env(tmp_path, MBQ_DATA_ROOT=tmp_path / "empty"),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 64 and "usage:" in r.stderr


def test_prices_on_a_weekend_says_no_session_and_downloads_nothing(tmp_path, world_complete):
    r = _rehearse(tmp_path, "prices", world_complete, PAST_SUN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no session" in r.stdout
    assert not [c for c in _calls(tmp_path) if "prices update" in c]


def test_prices_before_1600_on_the_real_date_waits(tmp_path, world_partial):
    today = _india_today()
    if not _is_weekday(today):
        pytest.skip("the weekend check runs before the 16:00 guard, so a weekend cannot show the guard")
    r = _rehearse(tmp_path, "prices", world_partial, today, hour=10)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "before 16:00 IST" in r.stdout
    assert not [c for c in _calls(tmp_path) if "prices update" in c]


# ----------------------------------------------------------------------------- the scenarios that load the whole engine
# Each of these runs the script's full path (about 2 s of imports). They run side by side once per module.

def _scenarios(tmp_path_factory, world_complete, world_partial):
    month_end_world = make_world(tmp_path_factory.mktemp("monthend"), close_date=MONTH_END, bundle_at=_utc_ago(1))
    held_world = tmp_path_factory.mktemp("held") / "w"
    shutil.copytree(world_complete, held_world)
    scratch = {n: tmp_path_factory.mktemp(n) for n in
               ("complete", "fail", "partial0", "normal", "held", "fake_a", "fake_b")}
    obj = {"x": 1, "write_lock": {"actor": "system:backup", "kind": "system", "pid": 77, "started_at": "T0"}}
    triage_two = {"anomalies": [{"status": "proposed"}, {"status": "new"}, {"status": "approved"}]}
    fa = dict(FAKE_STATUS_FILE=_json_file(scratch["fake_a"], "s.json", obj),
              FAKE_TRIAGE_FILE=_json_file(scratch["fake_a"], "t.json", triage_two))
    fb = dict(FAKE_STATUS_FILE=_json_file(scratch["fake_b"], "s.json", "not json at all"), FAKE_TRIAGE_RC=1)
    # a past date at 10:00 would be blocked if the guard applied to it
    return {
        "complete": lambda: _rehearse(scratch["complete"], "prices", world_complete, PAST_TUE, hour=10),
        "fail": lambda: _rehearse(scratch["fail"], "prices", world_partial, PAST_TUE),
        "partial0": lambda: _rehearse(scratch["partial0"], "prices", world_partial, PAST_TUE, FAKE_PRICES_RC=0),
        "normal": lambda: _rehearse(scratch["normal"], "status", world_complete, PAST_TUE, FAKE_TRIAGE_RC=1),
        "held": lambda: _rehearse(scratch["held"], "status", held_world, PAST_TUE, FAKE_TRIAGE_RC=1),
        "fake_a": lambda: _rehearse(scratch["fake_a"], "status", world_complete, PAST_TUE, **fa),
        "fake_b": lambda: _rehearse(scratch["fake_b"], "status", month_end_world, MONTH_END, **fb),
    }, scratch, held_world


@pytest.fixture(scope="module")
def heavy(tmp_path_factory, world_complete, world_partial):
    from concurrent.futures import ThreadPoolExecutor
    jobs, scratch, held_world = _scenarios(tmp_path_factory, world_complete, world_partial)
    handle = dblock.acquire(held_world / "quant.db", {"actor": "human:tester", "kind": "human"}, 0)
    try:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(fn) for name, fn in jobs.items()}
            results = {name: f.result() for name, f in futures.items()}
    finally:
        dblock.release(handle)
    return results, scratch


def test_prices_already_complete_exits_0_without_a_download(heavy):
    results, scratch = heavy
    r = results["complete"]                      # the wrapper exits 99 on "prices update": exit 0 proves none was tried
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already complete" in r.stdout and "5 of 5" in r.stdout and "before 16:00" not in r.stdout
    assert not [c for c in _calls(scratch["complete"]) if "prices update" in c]


def test_prices_incomplete_attempts_the_download_and_fails_or_exits_10(heavy):
    results, scratch = heavy
    r = results["fail"]                          # the download fails (wrapper exit 99)
    assert r.returncode == 1 and "update FAILED" in r.stdout
    assert [c for c in _calls(scratch["fail"]) if "prices update" in c]
    r = results["partial0"]                      # the download "succeeds", still 3 of 5
    assert r.returncode == 10 and "still incomplete" in r.stdout and "3 of 5" in r.stdout
    assert [c for c in _calls(scratch["partial0"]) if "prices update --through 2026-11-10" in c]


def test_status_on_a_normal_world_reports_coverage_and_a_free_lock(heavy):
    r = heavy[0]["normal"]
    assert r.returncode == 0, r.stdout + r.stderr
    assert "closes for 2026-11-10:  5 of 5" in r.stdout
    assert "write lock: free" in r.stdout        # needs `quant status` to print the "write_lock" key
    assert "(need 98%)" in r.stdout and "0.98" not in r.stdout                                      # C3
    assert "latest live list:        none yet" in r.stdout


def test_status_names_the_holder_of_a_held_lock(heavy):
    r = heavy[0]["held"]
    assert r.returncode == 0, r.stdout + r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("write lock:"))
    assert line.startswith(f"write lock: held by human:tester (human, pid {os.getpid()}) since "), line


def test_status_formats_the_write_lock_object_and_counts_open_flags(heavy):
    r = heavy[0]["fake_a"]
    assert r.returncode == 0, r.stdout + r.stderr
    assert [ln for ln in r.stdout.splitlines() if ln.startswith("write lock:")] == \
        ["write lock: held by system:backup (system, pid 77) since T0"]
    assert "price-jump flags open:   2" in r.stdout and "ACTION NEEDED: 2 price-jump flag(s)" in r.stdout


def test_status_survives_every_sub_step_failing_and_a_failed_triage_is_exactly_a_question_mark(heavy):
    r = heavy[0]["fake_b"]                       # month-end evening; status output is not JSON; triage exits 1
    assert r.returncode == 0, r.stdout + r.stderr
    assert [ln for ln in r.stdout.splitlines() if ln.startswith("write lock:")] == ["write lock: unknown"]
    assert [ln for ln in r.stdout.splitlines() if ln.startswith("price-jump flags open:")] == ["price-jump flags open:   ?"]
    assert "price-jump flag(s)" not in r.stdout and "ACTION NEEDED: ?" not in r.stdout
    assert "tonight is the cohort evening: True" in r.stdout
    assert "ACTION NEEDED: tonight is the cohort evening" in r.stdout


# ----------------------------------------------------------------------------- capture_routine.sh: fundamentals

def test_fundamentals_nothing_to_do_when_the_last_capture_is_young(tmp_path, world_complete):
    r = _rehearse(tmp_path, "fundamentals", world_complete, PAST_TUE)       # bundle 1 day old, limit 3, not month-end
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to do" in r.stdout
    assert not [c for c in _calls(tmp_path) if "capture" in c]


def test_fundamentals_captures_when_the_last_capture_is_old(tmp_path, world_partial):
    # bundle 10 days old; hour=10 would be blocked if the guard applied to a past date
    r = _rehearse(tmp_path, "fundamentals", world_partial, PAST_TUE, hour=10)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "capture complete" in r.stdout and "before 16:00" not in r.stdout
    calls = _calls(tmp_path)
    assert any("universe capture" in c for c in calls) and any("data capture" in c for c in calls)


def test_fundamentals_waits_before_1600_on_the_real_date_and_runs_after(tmp_path, world_partial):
    today = _india_today()
    if not _is_weekday(today):
        pytest.skip("the guard only applies on a session day")
    r = _rehearse(tmp_path, "fundamentals", world_partial, today, hour=10)         # stale bundle, but the session is open
    assert r.returncode == 0 and "before 16:00 IST" in r.stdout
    assert not [c for c in _calls(tmp_path) if "capture" in c]
    r = _rehearse(tmp_path, "fundamentals", world_partial, today, hour=17)
    assert r.returncode == 0 and "capture complete" in r.stdout
    assert any("universe capture" in c for c in _calls(tmp_path))


@pytest.mark.parametrize("bundle_at, captures", [
    ("2026-10-30T11:00:00.000000Z", False),    # 16:30 IST on the evening: finished after 16:00, skip
    ("2026-10-30T10:31:00.000000Z", False),    # 16:01 IST: skip
    ("2026-10-30T10:29:00.000000Z", True),     # 15:59 IST: before 16:00, capture again
    ("2026-10-29T11:00:00.000000Z", True),     # after 16:00 but the day before
])
def test_fundamentals_on_the_month_end_evening(tmp_path, bundle_at, captures):
    root = make_world(tmp_path / "w", close_date=MONTH_END, bundle_at=bundle_at)
    r = _rehearse(tmp_path, "fundamentals", root, MONTH_END)
    assert r.returncode == 0, r.stdout + r.stderr
    attempted = any("universe capture" in c for c in _calls(tmp_path))
    assert attempted is captures, r.stdout
    assert ("nothing to do" in r.stdout) is (not captures)


# ----------------------------------------------------------------------------- capture_routine.sh: no database

def test_prices_and_fundamentals_on_an_empty_directory_exit_1_with_one_line(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    for mode in ("prices", "fundamentals"):
        r = _routine(tmp_path, mode, empty)
        assert r.returncode == 1, (mode, r.stdout, r.stderr)
        lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
        assert len(lines) == 1 and "no database yet" in lines[0], lines


def test_an_unreadable_database_file_exits_1_with_one_line(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "quant.db").write_bytes(b"")                                    # exists, has no tables
    r = _routine(tmp_path, "prices", root)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    assert r.returncode == 1 and len(lines) == 1 and "cannot be read" in lines[0], (r.stdout, r.stderr)


# ----------------------------------------------------------------------------- capture_routine.sh: status

def _json_file(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def test_status_on_an_empty_directory_prints_the_facts_and_exits_0(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _rehearse(tmp_path, "status", empty, PAST_TUE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"date (India):            {PAST_TUE}" in r.stdout
    assert "month-end session:       2026-11-30" in r.stdout
    assert r.stdout.count("no database yet") == 2
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.parametrize("clean, tail", [(1, "clean)"), (0, "scored late or not clean)")])
def test_status_prints_the_latest_live_list_in_words(tmp_path, clean, tail):
    root = make_world(tmp_path / "w", close_date=MONTH_END, cohort_clean=clean)
    r = _rehearse(tmp_path, "status", root, MONTH_END, FAKE_TRIAGE_RC=1)
    assert r.returncode == 0, r.stdout + r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("latest live list:"))
    assert line == f"latest live list:        live:2026-10-30 (as of 2026-10-30, {tail}", line
    assert "{" not in line


# ----------------------------------------------------------------------------- capture_routine.sh: backup reminder (C4)

def _stamp(days_ago: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return when.strftime("%Y%m%dT%H%M%SZ")


def _status_with_backups(tmp_path, snapshots, **extra):
    backups = tmp_path / "bk"
    backups.mkdir()
    for name, with_inventory in snapshots:
        (backups / name).mkdir()
        if with_inventory:
            (backups / name / "INVENTORY.json").write_text("{}")
    empty = tmp_path / "empty"
    empty.mkdir()
    return _rehearse(tmp_path, "status", empty, PAST_TUE, QUANT_BACKUP_DIR=backups, **extra), backups


def test_status_says_there_is_no_backup_and_what_to_do(tmp_path):
    r, backups = _status_with_backups(tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "last backup:             none" in r.stdout
    assert "ACTION NEEDED: no backup in the last 35 days. Run scripts/backup_state.sh (it copies the price store " \
           "and raw archive, which git does not hold)." in r.stdout
    assert list(backups.iterdir()) == []


def test_status_shows_a_fresh_backup_without_a_warning(tmp_path):
    newest = f"snapshot-{_stamp(3.2)}"
    r, _ = _status_with_backups(tmp_path, [(f"snapshot-{_stamp(20)}", True), (newest, True),
                                          (f"snapshot-{_stamp(1)}", False)])       # newest folder has no inventory: not a backup
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"last backup:             {newest} (3 days ago)" in r.stdout
    assert "no backup in the last" not in r.stdout


def test_status_warns_about_an_old_backup_and_honours_the_limit_variable(tmp_path):
    old = f"snapshot-{_stamp(60)}"
    r, _ = _status_with_backups(tmp_path, [(old, True)])
    assert f"last backup:             {old} (60 days ago)" in r.stdout
    assert "ACTION NEEDED: no backup in the last 35 days." in r.stdout
    sub = tmp_path / "again"
    sub.mkdir()
    r, _ = _status_with_backups(sub, [(old, True)], MBQ_BACKUP_MAX_AGE_DAYS=90)
    assert "no backup in the last" not in r.stdout


# ----------------------------------------------------------------------------- routine.env (C2)

def _copied_checkout(tmp_path: Path, *, helper: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("capture_routine.sh", "backup_state.sh") + (("routine_env.sh",) if helper else ()):
        shutil.copy(REPO / "scripts" / name, repo / "scripts" / name)
    return repo


def _plain_status(tmp_path: Path, repo: Path, **extra):
    env = _env(tmp_path, **extra)
    return subprocess.run(["bash", str(repo / "scripts" / "capture_routine.sh"), "status"], env=env,
                          capture_output=True, text=True, timeout=60, cwd=str(tmp_path))


def test_routine_env_file_supplies_the_data_root(tmp_path):
    repo = _copied_checkout(tmp_path)
    root = tmp_path / "my data"                                  # a space: the value is literal
    root.mkdir()
    (repo / "routine.env").write_text(f"# local settings\n\nMBQ_DATA_ROOT={root}\r\nOTHER=1\n")
    r = _plain_status(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"no database yet at {root}/quant.db" in r.stdout


def test_the_environment_wins_over_routine_env(tmp_path):
    repo = _copied_checkout(tmp_path)
    (repo / "routine.env").write_text(f"MBQ_DATA_ROOT={tmp_path}/from_file\n")
    r = _plain_status(tmp_path, repo, MBQ_DATA_ROOT=tmp_path / "from_env")
    assert f"no database yet at {tmp_path}/from_env/quant.db" in r.stdout and "from_file" not in r.stdout


def test_routine_env_is_never_executed_and_unknown_keys_are_ignored(tmp_path):
    repo = _copied_checkout(tmp_path)
    (repo / "routine.env").write_text(
        "MBQ_DATA_ROOT=$(touch PWNED)\ntouch PWNED2\nMBQ_TODAY=1999-01-01\nMBQ_REHEARSAL=1\nKEY_WITH=a=b\n"
        "`touch PWNED3`\n")
    r = _plain_status(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    for d in (tmp_path, repo, repo / "scripts", Path.cwd()):
        assert not [p for p in d.glob("PWNED*")], d
    assert "no database yet at $(touch PWNED)/quant.db" in r.stdout          # literal value
    assert "1999-01-01" not in r.stdout                                        # MBQ_TODAY is not a read key


def test_scripts_continue_when_the_helper_is_missing(tmp_path):
    repo = _copied_checkout(tmp_path, helper=False)
    (repo / "routine.env").write_text(f"MBQ_DATA_ROOT={tmp_path}/ignored\n")
    r = _plain_status(tmp_path, repo, MBQ_DATA_ROOT=tmp_path / "env_root")
    assert r.returncode == 0 and f"{tmp_path}/env_root/quant.db" in r.stdout


# ----------------------------------------------------------------------------- q.sh (engine CLI against the data root)

def _q(tmp_path: Path, *args: str, routine_env: str | None = None, **extra):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("q.sh", "routine_env.sh"):
        shutil.copy(REPO / "scripts" / name, repo / "scripts" / name)
    if routine_env is not None:
        (repo / "routine.env").write_text(routine_env)
    fake = tmp_path / "fakepy.sh"                                # prints what the engine would be started with
    fake.write_text('#!/bin/bash\necho "ARGS=$*"\necho "DB=$QUANT_DB_PATH"\necho "PRICES=$QUANT_PRICES_DB_PATH"\n'
                    'echo "UI=$QUANT_UI_DIR"\necho "PP=$PYTHONPATH"\necho "CWD=$PWD"\nexit 7\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MBQ_", "QUANT_"))}
    env.update(QUANT_PYTHON=str(fake))
    env.update({k: str(v) for k, v in extra.items()})
    r = subprocess.run(["bash", str(repo / "scripts" / "q.sh"), *args], env=env, capture_output=True, text=True,
                       timeout=30, cwd=str(tmp_path))
    return repo, r, dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


def test_q_runs_the_engine_with_this_checkouts_code_against_the_data_root_from_routine_env(tmp_path):
    root = tmp_path / "the data"
    repo, r, seen = _q(tmp_path, "triage", "list", "--as-of", "2026-11-30", routine_env=f"MBQ_DATA_ROOT={root}\n")
    assert r.returncode == 7                                     # the engine's own exit code is passed through
    assert seen["ARGS"] == "-m quant triage list --as-of 2026-11-30"
    assert seen["DB"] == f"{root}/quant.db" and seen["PRICES"] == f"{root}/data/prices_daily.sqlite"
    assert seen["UI"] == f"{root}/ui"
    assert Path(seen["PP"]).resolve() == repo.resolve() and Path(seen["CWD"]).resolve() == repo.resolve()


def test_q_defaults_to_its_own_checkout_and_an_explicit_path_wins(tmp_path):
    repo, r, seen = _q(tmp_path, "status", QUANT_DB_PATH=tmp_path / "elsewhere.db")
    assert seen["DB"] == f"{tmp_path}/elsewhere.db"              # an explicit QUANT_* path wins
    assert Path(seen["PRICES"]).resolve() == (repo / "data" / "prices_daily.sqlite").resolve()


def test_q_without_arguments_prints_usage(tmp_path):
    _, r, _ = _q(tmp_path)
    assert r.returncode == 64 and "usage" in r.stderr.lower()


def test_routine_env_is_git_ignored():
    assert "routine.env" in (REPO / ".gitignore").read_text().split()


def test_backup_dry_run_takes_its_sources_from_the_data_root_in_routine_env(tmp_path, backup_templates):
    repo = _copied_checkout(tmp_path)
    data = tmp_path / "data root"
    (data / "data").mkdir(parents=True)
    shutil.copy(backup_templates / "plain" / "state.db", data / "quant.db")
    shutil.copy(backup_templates / "plain" / "prices.sqlite", data / "data" / "prices_daily.sqlite")
    shutil.copytree(backup_templates / "plain" / "archive", data / "data" / "archive")
    (repo / "routine.env").write_text(f"MBQ_DATA_ROOT={data}\n")
    env = _backup_env(tmp_path, backup_templates / "plain")
    for k in ("QUANT_DB_PATH", "QUANT_PRICES_DB_PATH", "QUANT_ARCHIVE_DIR"):
        del env[k]
    script = repo / "scripts" / "backup_state.sh"
    r = _run_backup(tmp_path, script, env, str(tmp_path / "out"), "--dry-run")
    real = os.path.realpath(data)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{real}/quant.db" in r.stdout or f"{data}/quant.db" in r.stdout
    assert "prices_daily.sqlite" in r.stdout and "data/archive" in r.stdout
    # an explicit path still wins over the data root
    other = tmp_path / "elsewhere.db"
    shutil.copy(data / "quant.db", other)
    r = _run_backup(tmp_path, script, {**env, "QUANT_DB_PATH": str(other)}, str(tmp_path / "out"), "--dry-run")
    assert str(other) in r.stdout
    # the default destination is still beside the checkout
    r = _run_backup(tmp_path, script, env, "--dry-run")
    assert f"{os.path.realpath(tmp_path)}/mb-backups/snapshot-" in r.stdout
    # a destination inside the data root is refused
    r = _run_backup(tmp_path, script, env, str(data / "backups"), "--dry-run")
    assert r.returncode == 1 and "inside the" in r.stderr and "data root" in r.stderr


# ----------------------------------------------------------------------------- capture_routine.sh: rehearsal override (B7)

def test_mbq_today_and_hour_are_ignored_without_the_rehearsal_flag(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _routine(tmp_path, "status", empty, MBQ_TODAY=PAST_TUE, MBQ_HOUR=10)
    assert r.returncode == 0
    assert f"date (India):            {_india_today()}" in r.stdout
    warn = [ln for ln in r.stderr.splitlines() if "warning" in ln]
    assert len(warn) == 1 and "MBQ_REHEARSAL=1" in warn[0]
    # and with the flag the same variables are honoured, with no warning
    r = _rehearse(tmp_path, "status", empty, PAST_TUE)
    assert f"date (India):            {PAST_TUE}" in r.stdout and "warning" not in r.stderr


def test_the_guard_cannot_be_switched_off_by_mbq_today_alone(tmp_path, world_partial):
    today = _india_today()
    if not _is_weekday(today):
        pytest.skip("weekend")
    hour = int(datetime.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H"))
    if hour >= 16:
        pytest.skip("after 16:00 IST the guard is open anyway")
    r = _routine(tmp_path, "prices", world_partial, MBQ_TODAY=PAST_TUE)     # no MBQ_REHEARSAL: ignored, real date, before 16:00
    assert r.returncode == 0 and "before 16:00 IST" in r.stdout and "warning" in r.stderr
    assert not [c for c in _calls(tmp_path) if "prices update" in c]


# ----------------------------------------------------------------------------- backup_state.sh

def _build_backup_sources(src: Path, *, wal: bool) -> None:
    (src / "archive").mkdir(parents=True)
    (src / "archive" / "raw.txt").write_text("vendor bytes")
    conn = connect(src / "state.db")
    apply_schema(conn, kind="state")
    conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                 "VALUES (1, 'INE000000001', 'A', '2025-01-01', '2026-01-01', 'listed')")
    if not wal:
        conn.execute("PRAGMA journal_mode = DELETE")       # the engine opens in WAL mode; a WAL file is a separate case
    conn.close()
    pc = sqlite3.connect(src / "prices.sqlite")
    pc.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    pc.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"v{i}") for i in range(50)])
    pc.commit()
    pc.close()


@pytest.fixture(scope="module")
def backup_templates(tmp_path_factory):
    base = tmp_path_factory.mktemp("backup_templates")
    _build_backup_sources(base / "plain", wal=False)
    _build_backup_sources(base / "wal", wal=True)
    return base


def _backup_world(tmp_path: Path, templates: Path, *, wal: bool = False):
    """Sources under tmp_path/src and a copy of the script at tmp_path/repo/scripts (so ROOT is tmp_path/repo)."""
    src = tmp_path / "src"
    shutil.copytree(templates / ("wal" if wal else "plain"), src)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "backup_state.sh"
    shutil.copy(BACKUP, script)
    return src, src / "state.db", src / "prices.sqlite", script


def _backup_env(tmp_path: Path, src: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MBQ_", "QUANT_", "KEEP"))}
    env.update(QUANT_DB_PATH=str(src / "state.db"), QUANT_PRICES_DB_PATH=str(src / "prices.sqlite"),
               QUANT_ARCHIVE_DIR=str(src / "archive"), QUANT_PYTHON=sys.executable, PYTHONPATH=str(REPO),
               PYTHONDONTWRITEBYTECODE="1")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_backup(tmp_path, script, env, *args, cwd=None):
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60,
                          cwd=str(cwd or tmp_path))


def _rows(path: Path, table: str) -> int:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def _gunzip(src: Path, dest: Path) -> Path:
    with gzip.open(src, "rb") as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return dest


def test_backup_with_no_argument_writes_beside_the_repository_leaves_sources_untouched_and_restores(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (state, prices)}
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src))        # no argument, no QUANT_BACKUP_DIR
    assert r.returncode == 0, r.stdout + r.stderr
    dest = Path(os.path.realpath(tmp_path)) / "mb-backups"             # ROOT is tmp_path/repo, so the default is its sibling
    snaps = sorted(dest.glob("snapshot-*"))
    assert len(snaps) == 1 and "/../" not in r.stdout and str(dest) in r.stdout
    snap = snaps[0]
    assert sorted(p.name for p in snap.iterdir()) == ["INVENTORY.json", "archive.tar.gz", "prices.sqlite.gz", "state.db.gz"]
    # B2: sources untouched, no WAL side files, copies restore with the same rows
    assert {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in (state, prices)} == before
    for p in (state, prices):
        assert not Path(f"{p}-wal").exists() and not Path(f"{p}-shm").exists(), p
    state_copy = _gunzip(snap / "state.db.gz", tmp_path / "restored_state.db")
    prices_copy = _gunzip(snap / "prices.sqlite.gz", tmp_path / "restored_prices.db")
    assert _rows(state_copy, "securities") == _rows(state, "securities") == 1
    assert _rows(prices_copy, "t") == _rows(prices, "t") == 50
    inv = json.loads((snap / "INVENTORY.json").read_text())
    assert {f["name"] for f in inv["files"]} == {"state.db.gz", "prices.sqlite.gz", "archive.tar.gz"}
    for f in inv["files"]:
        assert hashlib.sha256((snap / f["name"]).read_bytes()).hexdigest() == f["sha256"]
    assert not list(dest.glob(".snapshot-*"))


def test_backup_of_a_wal_mode_state_database_leaves_the_database_file_untouched(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates, wal=True)
    before = (state.stat().st_mtime_ns, state.stat().st_size)
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src), str(tmp_path / "out"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (state.stat().st_mtime_ns, state.stat().st_size) == before
    snap = next((tmp_path / "out").glob("snapshot-*"))
    assert _rows(_gunzip(snap / "state.db.gz", tmp_path / "r.db"), "securities") == 1
    # Known: SQLite may leave empty state.db-wal / state.db-shm beside a WAL source after a read-only open.


def test_backup_fails_loudly_when_the_quant_package_cannot_be_imported(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    env = _backup_env(tmp_path, src)
    del env["PYTHONPATH"]
    r = _run_backup(tmp_path, script, env, str(tmp_path / "out"), cwd=tmp_path)    # cwd has no quant package
    assert r.returncode == 1 and "cannot import the quant package" in r.stderr
    assert not list((tmp_path / "out").glob("snapshot-*"))


def test_backup_with_the_state_lock_held_fails_with_a_clear_message(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    handle = dblock.acquire(state, {"actor": "human:holder", "kind": "human"}, 0)
    try:
        t0 = time.monotonic()
        r = _run_backup(tmp_path, script, _backup_env(tmp_path, src, QUANT_LOCK_WAIT_S=0.5), str(tmp_path / "out"))
        elapsed = time.monotonic() - t0
    finally:
        dblock.release(handle)
    assert r.returncode != 0 and elapsed >= 0.5
    assert "state write lock" in r.stderr and "human:holder" in r.stderr
    assert not list((tmp_path / "out").glob("snapshot-*")) and not list((tmp_path / "out").glob(".snapshot-*"))


def test_backup_waits_for_the_lock_and_then_succeeds(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    handle = dblock.acquire(state, {"actor": "human:holder", "kind": "human"}, 0)
    threading.Timer(0.5, dblock.release, args=(handle,)).start()
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src, QUANT_LOCK_WAIT_S=20), str(tmp_path / "out"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(list((tmp_path / "out").glob("snapshot-*"))) == 1


def _fake_snapshots(dest: Path, stamps):
    for s in stamps:
        d = dest / f"snapshot-{s}"
        d.mkdir(parents=True)
        (d / "INVENTORY.json").write_text("{}")


def test_backup_keep_1_keeps_the_new_snapshot_and_only_our_own_old_ones_go(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    out = tmp_path / "out"
    _fake_snapshots(out, ["20200101T000000Z", "20200102T000000Z"])
    (out / "notes").mkdir()
    (out / "snapshot-20200103T000000Z").mkdir()          # no INVENTORY.json: not ours, never deleted
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src, KEEP=1), str(out), "--dry-run")
    assert r.returncode == 0 and r.stdout.count("would delete older snapshot") == 2      # reports, deletes nothing
    assert len(list(out.glob("snapshot-2020*"))) == 3
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src, KEEP=1), str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    names = sorted(p.name for p in out.iterdir())
    new = [n for n in names if n.startswith("snapshot-") and not n.startswith("snapshot-2020")]
    assert len(new) == 1 and (out / new[0] / "INVENTORY.json").is_file()
    assert "notes" in names and "snapshot-20200103T000000Z" in names
    assert "snapshot-20200101T000000Z" not in names and "snapshot-20200102T000000Z" not in names


@pytest.mark.parametrize("bad", ["it's", "two\nlines"])
def test_backup_refuses_a_destination_with_a_quote_or_a_newline(tmp_path, backup_templates, bad):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src), str(tmp_path / bad))
    assert r.returncode == 1 and ("single quote" in r.stderr or "newline" in r.stderr)
    assert not (tmp_path / bad).exists()
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src, QUANT_BACKUP_DIR=str(tmp_path / bad)))
    assert r.returncode == 1 and ("single quote" in r.stderr or "newline" in r.stderr)


def test_backup_refuses_a_destination_inside_the_repository(tmp_path, backup_templates):
    src, state, prices, script = _backup_world(tmp_path, backup_templates)
    r = _run_backup(tmp_path, script, _backup_env(tmp_path, src), str(tmp_path / "repo" / "backups"))
    assert r.returncode == 1 and "inside the repository" in r.stderr
