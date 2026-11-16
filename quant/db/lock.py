"""One writer at a time for the state database (MASTER_SPEC correction R14).

An exclusive advisory ``fcntl.flock`` on ``<db>.lock``. The kernel frees it when the
process ends, however it ends. The lock file is never unlinked: unlinking races with a
waiter that has already opened the old file. The holder (who, what, since when) is
written into the file as one JSON line so a waiting writer and ``quant status`` can
name it.

Re-entrant inside one process and reference-counted: monthly() holds the lock and then
opens RunContexts on the same database.

The lock is per process, not per thread: the engine is single-threaded, and the re-entrancy
registry is keyed by database path only. Each registry entry records the pid that created it and
entries made by another pid are ignored, so a forked child never believes it holds its parent's
lock (it is refused like any other second writer). Do not share a held lock across threads.

``holder_of`` is a status probe. It takes no flock (a probe must never make a real writer's
non-blocking attempt fail). It reads the holder line and reports a holder when that line's pid is
alive on this host; a line written on another host is reported as held (liveness unknown). A
clean release empties the file, so only a crashed holder leaves a line behind, and its pid is dead.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from quant.errors import Refused

ENV_WAIT = "QUANT_LOCK_WAIT_S"
POLL_S = 0.25
RETRY_FLOOR_S = 1.0         # with no wait requested, still retry this long so a momentary overlap is not refused


class StateLocked(RuntimeError):
    """The state database is held by another writer."""


class _Entry:
    __slots__ = ("fd", "count", "pid")

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.count = 1
        self.pid = os.getpid()


_registry: dict[str, _Entry] = {}
_mutex = threading.Lock()


class Handle:
    """Returned by acquire(); pass to release(). A no-op handle has key None."""

    def __init__(self, key: str | None) -> None:
        self.key = key
        self.released = False


def _lock_path(db_path: Any) -> Path | None:
    if not db_path:
        return None
    text = str(db_path)
    if text == ":memory:" or fcntl is None:
        return None
    return Path(text + ".lock")


def _env_wait() -> float:
    try:
        return max(0.0, float(os.environ.get(ENV_WAIT, "0") or 0))
    except ValueError:
        return 0.0


def describe(holder: dict | None) -> str:
    """'<actor> (<kind>, pid N) since <started_at>' from a holder dict."""
    h = holder or {}
    return (f"{h.get('actor', 'unknown')} ({h.get('kind', 'unknown')}, pid {h.get('pid', '?')}) "
            f"since {h.get('started_at', 'unknown')}")


def _read_holder(path: Path) -> dict | None:
    try:
        text = path.read_text().strip()
        if not text:
            return None
        data = json.loads(text.splitlines()[0])
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        import errno
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return False
        raise


def _write_holder(fd: int, holder: dict) -> None:
    record = {
        "pid": os.getpid(),
        "actor": None,
        "kind": None,
        "as_of": None,
        "command": " ".join(sys.argv)[:200],
        "started_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
    }
    record.update({k: v for k, v in holder.items() if v is not None})
    record["pid"] = os.getpid()
    data = (json.dumps(record, sort_keys=True, default=str) + "\n").encode()
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)


def acquire(
    db_path: Any,
    holder: dict,
    wait_s: float | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Handle:
    path = _lock_path(db_path)
    if path is None:
        return Handle(None)
    key = str(path.resolve())
    if wait_s is None:
        wait_s = _env_wait()
    if wait_s <= 0:
        wait_s = RETRY_FLOOR_S
    deadline = clock() + wait_s
    try:                                    # never create directories: a mistyped path must fail, not appear
        fd = os.open(key, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise Refused("lock_unavailable",
                      f"cannot open the write-lock file {key}: {exc.strerror or exc}; "
                      "check that the database path and its directory exist and are writable") from exc
    try:
        while True:
            with _mutex:
                entry = _registry.get(key)
                if entry is not None and entry.pid == os.getpid():
                    entry.count += 1
                    os.close(fd)
                    return Handle(key)
                if _try_flock(fd):
                    try:
                        _write_holder(fd, holder)
                    except Exception:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        raise
                    _registry[key] = _Entry(fd)
                    return Handle(key)
            if clock() >= deadline:
                current = _read_holder(path)
                raise StateLocked(
                    f"State is locked by {describe(current)} [lock file {key}]; "
                    f"retry later or set {ENV_WAIT}"
                )
            sleep(POLL_S)
    except BaseException:
        # Not registered: we still own the descriptor we opened.
        with _mutex:
            entry = _registry.get(key)
            if entry is None or entry.fd != fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
        raise


def release(handle: Handle | None) -> None:
    if handle is None or handle.key is None or handle.released:
        return
    handle.released = True
    with _mutex:
        entry = _registry.get(handle.key)
        if entry is None:
            return
        entry.count -= 1
        if entry.count > 0:
            return
        del _registry[handle.key]
        try:
            try:
                os.ftruncate(entry.fd, 0)           # a clean release leaves no holder line behind
            except OSError:
                pass
            fcntl.flock(entry.fd, fcntl.LOCK_UN)
        finally:
            os.close(entry.fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                         # exists, owned by someone else
    except OSError:
        return False
    return True


def holder_of(db_path: Any) -> dict | None:
    """Holder record if a live holder has the lock now, else None. Never blocks, never locks, never raises.

    Reads the holder line only. A pid that is alive on this host means held; a record from another
    host is reported as held (liveness unknown); an empty or unreadable line means free.
    """
    try:
        path = _lock_path(db_path)
        if path is None or not path.exists():
            return None
        record = _read_holder(path)
        if not record:
            return None
        host = record.get("host")
        if host and host != socket.gethostname():
            return record
        pid = record.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        return record if _pid_alive(pid) else None
    except Exception:
        return None
