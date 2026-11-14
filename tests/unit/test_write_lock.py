"""WP-L: one writer at a time for the state database (MASTER_SPEC R14). Offline, tmp databases."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quant.config import load as load_config
from quant.db import lock as dblock
from quant.db.core import apply_schema, connect
from quant.db.lock import StateLocked, acquire, holder_of, release
from quant.run import RunContext, monthly
from quant.types import Actor, FrozenClock

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

LOCK_CHILD = """
import sys, time
from quant.db import lock
h = lock.acquire(sys.argv[1], {"actor": "system:child", "kind": "holder", "as_of": "2026-09-30"})
print("ready", flush=True)
hold = float(sys.argv[2]) if len(sys.argv) > 2 else 0
if hold:
    time.sleep(hold)
else:
    sys.stdin.readline()
"""

CTX_CHILD = """
import sys
from quant.config import load
from quant.run import RunContext
from quant.types import Actor, FrozenClock
cfg = load().with_paths(db=sys.argv[1])
with RunContext(as_of="2026-09-30", kind="capture", track="live", cfg=cfg,
                clock=FrozenClock("2026-10-01T00:00:00.000000Z"), actor=Actor(kind="llm", name="agent-a")):
    print("ready", flush=True)
    sys.stdin.readline()
"""


def _spawn(code: str, *args: str) -> subprocess.Popen:
    proc = subprocess.Popen([PY, "-c", code, *args], cwd=str(ROOT), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    assert line.strip() == "ready", f"child failed: {proc.stderr.read()}"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
        except OSError:
            pass
        proc.wait(timeout=10)


def _cfg(tmp_path, name="state.db"):
    db = tmp_path / name
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", ui_dir=tmp_path / "ui",
                                   prices_db=tmp_path / "prices.db")
    return cfg, db


CLOCK = FrozenClock("2026-10-01T00:00:00.000000Z")
ACTOR = Actor(kind="system", name="t")


def _ctx(cfg, kind="test"):
    return RunContext(as_of="2026-09-30", kind=kind, track="live", cfg=cfg, clock=CLOCK, actor=ACTOR)


# a. fail fast while another process holds a RunContext
def test_second_process_fails_fast_naming_holder(tmp_path):
    cfg, db = _cfg(tmp_path)
    child = _spawn(CTX_CHILD, str(db))
    try:
        t0 = time.monotonic()
        with pytest.raises(StateLocked) as err:
            with _ctx(cfg):
                pass
        assert time.monotonic() - t0 < 3.0            # wait 0 still retries for about 1 second (A6)
        msg = str(err.value)
        assert "llm:agent-a" in msg and "capture" in msg and f"pid {child.pid}" in msg
        assert "since 20" in msg and f"{db}.lock" in msg and "QUANT_LOCK_WAIT_S" in msg
    finally:
        _stop(child)
    with _ctx(cfg):  # free again after the child ended
        pass


# b. with a wait, the second acquires after the first releases
def test_wait_acquires_after_release_subprocess(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    child = _spawn(LOCK_CHILD, str(db), "0.7")
    try:
        t0 = time.monotonic()
        h = acquire(db, {"actor": "system:me", "kind": "x"}, wait_s=10)
        waited = time.monotonic() - t0
        release(h)
        assert 0.3 < waited < 5
    finally:
        _stop(child)
    child = _spawn(LOCK_CHILD, str(db), "0.7")
    try:
        monkeypatch.setenv("QUANT_LOCK_WAIT_S", "10")
        h = acquire(db, {"actor": "system:me", "kind": "x"})
        release(h)
    finally:
        _stop(child)


def test_wait_polls_with_injected_sleeper_and_clock(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    raw = os.open(str(db) + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(raw, fcntl.LOCK_EX)
    now = [0.0]
    sleeps: list[float] = []
    unlock_at = [3]

    def sleep(s):
        sleeps.append(s)
        now[0] += s
        if len(sleeps) == unlock_at[0]:
            fcntl.flock(raw, fcntl.LOCK_UN)

    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=5, sleep=sleep, clock=lambda: now[0])
    release(h)
    assert sleeps == [0.25, 0.25, 0.25]

    fcntl.flock(raw, fcntl.LOCK_EX)
    unlock_at[0] = -1
    now[0] = 0.0
    sleeps.clear()
    with pytest.raises(StateLocked):
        acquire(db, {"actor": "a", "kind": "k"}, wait_s=1.0, sleep=sleep, clock=lambda: now[0])
    assert len(sleeps) == 4

    monkeypatch.setenv("QUANT_LOCK_WAIT_S", "0.5")
    now[0] = 0.0
    sleeps.clear()
    with pytest.raises(StateLocked):
        acquire(db, {"actor": "a", "kind": "k"}, sleep=sleep, clock=lambda: now[0])
    assert len(sleeps) == 2
    os.close(raw)


# c. re-entrant, released only at the outermost exit
def test_reentrant_in_one_process(tmp_path):
    cfg, db = _cfg(tmp_path)
    with _ctx(cfg) as outer:
        assert outer.conn is not None
        with _ctx(cfg, kind="inner"):
            assert holder_of(db)["kind"] == "test"  # the outermost holder is recorded
        assert holder_of(db) is not None  # inner exit did not release
        h1 = acquire(db, {"actor": "x", "kind": "y"})
        release(h1)
        release(h1)  # double release of one handle is harmless
        assert holder_of(db) is not None
    assert holder_of(db) is None
    assert not dblock._registry


# d. released after an exception, and after a kill
def test_released_after_exception(tmp_path):
    cfg, db = _cfg(tmp_path)
    with pytest.raises(ValueError):
        with _ctx(cfg):
            raise ValueError("boom")
    assert holder_of(db) is None
    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    release(h)


def test_released_when_enter_fails(tmp_path, monkeypatch):
    cfg, db = _cfg(tmp_path)
    import quant.run as run_mod

    def bad_connect(*a, **k):
        raise RuntimeError("no connection")

    monkeypatch.setattr(run_mod, "connect", bad_connect)
    with pytest.raises(RuntimeError):
        with _ctx(cfg):
            pass
    assert holder_of(db) is None


def test_released_after_process_killed(tmp_path):
    db = tmp_path / "k.db"
    child = _spawn(LOCK_CHILD, str(db))
    with pytest.raises(StateLocked):
        acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    t0 = time.monotonic()
    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    release(h)
    assert time.monotonic() - t0 < 0.5
    assert Path(str(db) + ".lock").exists()  # never unlinked


# e. holder_of
def test_holder_of_reports_holder_and_never_blocks(tmp_path):
    db = tmp_path / "h.db"
    assert holder_of(db) is None                      # no file
    Path(str(db) + ".lock").write_text("")
    assert holder_of(db) is None                      # empty file
    assert holder_of("") is None and holder_of(":memory:") is None
    child = _spawn(LOCK_CHILD, str(db))
    try:
        t0 = time.monotonic()
        h = holder_of(db)
        assert time.monotonic() - t0 < 0.5
        assert h["pid"] == child.pid and h["actor"] == "system:child" and h["kind"] == "holder"
        assert h["as_of"] == "2026-09-30" and h["host"] and h["started_at"].endswith("Z")
        assert "command" in h
    finally:
        _stop(child)
    assert holder_of(db) is None                      # stale content, no live holder
    assert json.loads(Path(str(db) + ".lock").read_text().splitlines()[0])["pid"] == child.pid


def test_no_lock_for_memory_or_falsy_path():
    for p in (":memory:", "", None):
        h = acquire(p, {"actor": "a"})
        release(h)
    assert not dblock._registry


# f. monthly() with another process holding the lock
def test_monthly_returns_1_and_names_holder(tmp_path, capsys):
    cfg, db = _cfg(tmp_path)
    child = _spawn(LOCK_CHILD, str(db))
    try:
        code = monthly(cfg, FrozenClock("2026-10-01T18:30:00.000000Z"), ACTOR, as_of="2026-09-30",
                       skip_capture=True)
    finally:
        _stop(child)
    out = capsys.readouterr().out
    assert code == 1
    assert "system:child" in out and f"pid {child.pid}" in out and "holder" in out
    assert out.count("\n") == 1


# g. different databases do not block each other
def test_two_databases_do_not_block(tmp_path):
    cfg_a, _ = _cfg(tmp_path, "a.db")
    cfg_b, _ = _cfg(tmp_path, "b.db")
    with _ctx(cfg_a):
        with _ctx(cfg_b):
            pass
    child = _spawn(LOCK_CHILD, str(tmp_path / "a.db"))
    try:
        with _ctx(cfg_b):
            pass
    finally:
        _stop(child)


def test_thread_safe_counting(tmp_path):
    db = tmp_path / "t.db"
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(50):
                h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=5)
                release(h)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and holder_of(db) is None and not dblock._registry


# h. status is one JSON document with a write_lock key; read-only commands do not take the lock
def test_status_is_one_json_document_with_write_lock_key(tmp_path, capsys, monkeypatch):
    from quant import cli
    from quant.status import write_lock

    cfg, db = _cfg(tmp_path)
    monkeypatch.setenv("QUANT_DB_PATH", str(db))
    assert write_lock(db) == "free"
    child = _spawn(LOCK_CHILD, str(db))
    try:
        held = write_lock(db)
        assert set(held) == {"actor", "kind", "pid", "started_at"}
        assert held["pid"] == child.pid and held["actor"] == "system:child" and held["kind"] == "holder"
        assert held["started_at"].endswith("Z")
        capsys.readouterr()
        t0 = time.monotonic()
        assert cli.main(["status"]) == 0
        assert time.monotonic() - t0 < 3
        doc = json.loads(capsys.readouterr().out)           # the whole stdout is one JSON document
        assert doc["write_lock"] == held
    finally:
        _stop(child)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["write_lock"] == "free" and "write lock:" not in out


def test_status_without_database_is_json_with_write_lock(tmp_path, capsys, monkeypatch):
    from quant import cli

    monkeypatch.setenv("QUANT_DB_PATH", str(tmp_path / "missing.db"))
    assert cli.main(["status"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "no_database" and doc["write_lock"] == "free"


def test_cli_maps_state_locked_to_exit_1_without_traceback(monkeypatch, capsys):
    from quant import cli

    cli._init_default_commands()

    def handler(args):
        raise StateLocked("State is locked by system:x (m, pid 1) since t; retry later or set QUANT_LOCK_WAIT_S")

    monkeypatch.setitem(cli._COMMAND_REGISTRY["status"], "show", (handler, "x"))
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "State is locked by system:x (m, pid 1) since t" in err
    assert "Traceback" not in err and err.count("\n") == 1


# ---------------------------------------------------------------------------------------------- fix round A
# A6. holder_of takes no flock; acquire(wait 0) retries for about a second
def test_holder_of_never_takes_a_flock(tmp_path, monkeypatch):
    db = tmp_path / "nf.db"
    child = _spawn(LOCK_CHILD, str(db))

    def forbidden(*a, **k):
        raise AssertionError("holder_of must not call flock")

    monkeypatch.setattr(dblock.fcntl, "flock", forbidden)
    try:
        assert holder_of(db)["pid"] == child.pid
    finally:
        monkeypatch.undo()
        _stop(child)
    assert holder_of(db) is None


def test_holder_of_probe_cannot_make_a_writer_fail(tmp_path):
    db = tmp_path / "race.db"
    stop = threading.Event()

    def probe():
        while not stop.is_set():
            holder_of(db)

    t = threading.Thread(target=probe)
    t.start()
    try:
        for _ in range(200):
            h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0, sleep=lambda s: (_ for _ in ()).throw(
                AssertionError("a writer never had to wait")))
            release(h)
    finally:
        stop.set()
        t.join()


def test_holder_of_liveness_rules(tmp_path):
    import socket

    db = tmp_path / "live.db"
    lock = Path(str(db) + ".lock")
    me = {"pid": os.getpid(), "actor": "a", "kind": "k", "started_at": "2026-10-01T00:00:00Z",
          "host": socket.gethostname()}
    lock.write_text(json.dumps(me) + "\n")
    assert holder_of(db)["pid"] == os.getpid()              # live pid on this host: held
    dead = subprocess.Popen([PY, "-c", "pass"])
    dead.wait()
    lock.write_text(json.dumps({**me, "pid": dead.pid}) + "\n")
    assert holder_of(db) is None                            # dead pid: free
    lock.write_text(json.dumps({**me, "pid": dead.pid, "host": "some-other-host.invalid"}) + "\n")
    assert holder_of(db)["host"] == "some-other-host.invalid"   # other host: held, liveness unknown
    lock.write_text("not json\n")
    assert holder_of(db) is None
    # a clean release leaves the lock free even though this process is still alive
    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    assert holder_of(db)["pid"] == os.getpid()
    release(h)
    assert holder_of(db) is None


def test_wait_zero_retries_for_one_second_before_failing(tmp_path):
    db = tmp_path / "w0.db"
    raw = os.open(str(db) + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(raw, fcntl.LOCK_EX)
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    with pytest.raises(StateLocked):
        acquire(db, {"actor": "a", "kind": "k"}, wait_s=0, sleep=sleep, clock=lambda: now[0])
    assert sleeps == [0.25] * 4
    # a momentary overlap that clears within the second is not refused
    sleeps.clear()
    now[0] = 0.0

    def sleep_then_free(s):
        sleep(s)
        if len(sleeps) == 2:
            fcntl.flock(raw, fcntl.LOCK_UN)

    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0, sleep=sleep_then_free, clock=lambda: now[0])
    release(h)
    assert len(sleeps) == 2
    os.close(raw)


# A7. the registry is per process
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_forked_child_does_not_believe_it_holds_the_parents_lock(tmp_path):
    db = tmp_path / "fork.db"
    h = acquire(db, {"actor": "parent", "kind": "k"}, wait_s=0)
    assert dblock._registry
    pid = os.fork()
    if pid == 0:                                            # child
        code = 3
        try:
            try:
                c = acquire(db, {"actor": "child", "kind": "k"}, wait_s=0, sleep=lambda s: None,
                            clock=iter([0.0, 5.0, 10.0, 15.0]).__next__)
                release(c)
                code = 1                                    # wrongly believed it held the lock
            except StateLocked:
                code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    release(h)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert holder_of(db) is None and not dblock._registry


def test_registry_entries_record_their_pid(tmp_path):
    db = tmp_path / "pid.db"
    h = acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    try:
        assert [e.pid for e in dblock._registry.values()] == [os.getpid()]
    finally:
        release(h)


# A8. a mistyped path is a Refused, not an OSError, and creates nothing
def test_missing_directory_is_refused_and_not_created(tmp_path):
    from quant.errors import Refused

    db = tmp_path / "no_such_dir" / "deeper" / "state.db"
    with pytest.raises(Refused) as err:
        acquire(db, {"actor": "a", "kind": "k"}, wait_s=0)
    assert "no_such_dir" in str(err.value)
    assert not (tmp_path / "no_such_dir").exists()
    assert not dblock._registry


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores directory modes")
def test_read_only_directory_is_refused(tmp_path):
    from quant.errors import Refused

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        with pytest.raises(Refused) as err:
            acquire(ro / "state.db", {"actor": "a", "kind": "k"}, wait_s=0)
        assert "ro" in str(err.value)
    finally:
        ro.chmod(0o755)
    assert not dblock._registry


def test_cli_maps_refused_lock_to_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    from quant import cli

    monkeypatch.setenv("QUANT_DB_PATH", str(tmp_path / "nope" / "state.db"))
    code = cli.main(["db", "init"])
    err = capsys.readouterr().err
    assert code != 0 and "Traceback" not in err and "nope" in err


# A5. writers that bypassed the lock
def test_db_init_holds_the_lock_while_writing(tmp_path, monkeypatch):
    from quant.commands import db as dbcmd

    db = tmp_path / "init.db"
    seen = {}
    real = dbcmd.apply_schema

    def spy(conn, **kw):
        seen["other"] = _probe(db)
        return real(conn, **kw)

    monkeypatch.setattr(dbcmd, "apply_schema", spy)
    monkeypatch.setenv("QUANT_DB_PATH", str(db))
    from quant import cli
    assert cli.main(["db", "init"]) == 0
    assert seen["other"] == "blocked:StateLocked"
    assert holder_of(db) is None and not dblock._registry


def test_migrate_bootstrap_holds_the_lock_while_writing(tmp_path, monkeypatch):
    import argparse

    from quant.commands import migrate as mig

    db = tmp_path / "mig.db"
    seen = {}

    def spy(conn, **kw):
        seen["other"] = _probe(db)
        raise RuntimeError("stop after the probe")

    monkeypatch.setattr(mig, "apply_schema", spy)
    ns = argparse.Namespace(config=None, db_path=str(db), legacy_db=str(tmp_path / "legacy.db"), dry_run=False)
    with pytest.raises(RuntimeError):
        mig.cmd_migrate_legacy(ns)
    assert seen["other"] == "blocked:StateLocked"
    assert holder_of(db) is None and not dblock._registry


def _probe(db) -> str:
    code = ("import sys\nfrom quant.db import lock\ntry:\n"
            " h = lock.acquire(sys.argv[1], {'actor': 'p', 'kind': 'p'}, wait_s=0)\n"
            "except Exception as e:\n print('blocked:' + type(e).__name__)\n"
            "else:\n lock.release(h); print('free')\n")
    res = subprocess.run([PY, "-c", code, str(db)], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    return res.stdout.strip()


def test_rebuild_refuses_an_existing_output(tmp_path, monkeypatch, capsys):
    from quant import cli

    out = tmp_path / "precious.db"
    out.write_bytes(b"do not delete me")
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    code = cli.main(["db", "rebuild", "--output", str(out)])
    text = capsys.readouterr()
    assert code == 1
    assert "precious.db" in text.out + text.err and "exists" in (text.out + text.err)
    assert out.read_bytes() == b"do not delete me"


# ---------------------------------------------------------------------------------------------- fix round C
# C1. db export and db verify write the journal files, so they hold the state write lock
def _journal_world(tmp_path, monkeypatch):
    from quant import cli

    db = tmp_path / "state.db"
    monkeypatch.setenv("QUANT_DB_PATH", str(db))
    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("QUANT_LOCK_WAIT_S", raising=False)
    assert cli.main(["db", "init"]) == 0
    cfg = load_config().with_paths(db=db, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", ui_dir=tmp_path / "ui",
                                   prices_db=tmp_path / "prices.db")
    with _ctx(cfg) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'A', '2025-01-01', '2026-01-01', 'listed')")
    assert cli.main(["db", "export"]) == 0
    files = sorted((tmp_path / "data" / "ledger").rglob("*"))
    files = [f for f in files if f.is_file()]
    assert files
    return db, {f: f.read_bytes() for f in files}


@pytest.mark.parametrize("sub, kind", [("export", "db-export"), ("verify", "db-verify")])
def test_db_export_and_verify_are_refused_while_the_lock_is_held(tmp_path, monkeypatch, capsys, sub, kind):
    from quant import cli

    db, before = _journal_world(tmp_path, monkeypatch)
    capsys.readouterr()
    child = _spawn(LOCK_CHILD, str(db))
    try:
        assert cli.main(["db", sub]) == 1
        err = capsys.readouterr().err
        assert "State is locked by system:child" in err and "Traceback" not in err and err.count("\n") == 1
    finally:
        _stop(child)
    assert {f: f.read_bytes() for f in before} == before


def test_db_export_waits_for_the_lock_and_then_succeeds(tmp_path, monkeypatch, capsys):
    from quant import cli

    db, before = _journal_world(tmp_path, monkeypatch)
    child = _spawn(LOCK_CHILD, str(db), "1.0")
    monkeypatch.setenv("QUANT_LOCK_WAIT_S", "30")
    t0 = time.monotonic()
    assert cli.main(["db", "export"]) == 0
    assert time.monotonic() - t0 >= 0.3
    child.wait(timeout=10)
    assert holder_of(db) is None and not dblock._registry


def test_db_export_and_verify_hold_the_lock_while_working(tmp_path, monkeypatch):
    from quant import cli
    from quant.commands import db as dbcmd

    db, _ = _journal_world(tmp_path, monkeypatch)
    seen = {}
    real = dbcmd.ledger.export

    def spy(conn, directory):
        seen.setdefault("others", []).append(_probe(db))
        return real(conn, directory)

    monkeypatch.setattr(dbcmd.ledger, "export", spy)
    assert cli.main(["db", "export"]) == 0
    assert cli.main(["db", "verify"]) == 0
    assert seen["others"] == ["blocked:StateLocked", "blocked:StateLocked"]
    assert holder_of(db) is None and not dblock._registry


def test_db_size_still_works_while_the_lock_is_held(tmp_path, monkeypatch, capsys):
    from quant import cli

    db, _ = _journal_world(tmp_path, monkeypatch)
    capsys.readouterr()
    child = _spawn(LOCK_CHILD, str(db))
    try:
        assert cli.main(["db", "size"]) == 0
        assert "state db" in capsys.readouterr().out
    finally:
        _stop(child)
