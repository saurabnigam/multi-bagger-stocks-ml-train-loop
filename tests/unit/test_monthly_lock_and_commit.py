"""Fix round A: monthly() holds the state lock for the whole run, re-checks under the lock, commits no
database file and keeps the committed journal equal to the database (MASTER_SPEC 4.1, 4.2, R14, R15).

Offline: every database, ledger and git repository lives under tmp_path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import quant.run as run_mod
from quant.config import load as load_config
from quant.db import ledger
from quant.db import lock as dblock
from quant.db.core import apply_schema, connect
from quant.run import _commit_and_push, monthly
from quant.types import Actor, FrozenClock

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
CLOCK = FrozenClock("2026-10-01T18:30:00.000000Z")
ACTOR = Actor(kind="system", name="t")

PROBE = """
import sys
from quant.db import lock
try:
    h = lock.acquire(sys.argv[1], {"actor": "probe", "kind": "probe"}, wait_s=0)
except Exception as exc:
    print("blocked:" + type(exc).__name__)
else:
    lock.release(h)
    print("free")
"""


def _other_process_sees(db: Path) -> str:
    """'free' or 'blocked:<error>' as seen by a second process trying to take the lock."""
    res = subprocess.run([PY, "-c", PROBE, str(db)], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _world(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, data_dir=tmp_path / "data", archive_dir=tmp_path / "archive",
                                   knowledge_dir=tmp_path / "knowledge", ui_dir=tmp_path / "ui",
                                   prices_db=tmp_path / "prices.db")
    return cfg, db


def _journal_bytes(cfg) -> dict[str, bytes]:
    root = Path(cfg.paths.data_dir) / "ledger"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.glob("**/*")) if p.is_file()}


# ----------------------------------------------------------------------------------------------- A1
def test_a1_lock_is_held_through_the_post_publication_tail(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)
    seen: dict[str, str] = {}
    real_export = ledger.export

    def export_spy(conn, ledger_dir):
        seen["export"] = _other_process_sees(db)
        return real_export(conn, ledger_dir)

    def commit_spy(cfg_, as_of, **kw):
        seen["commit"] = _other_process_sees(db)

    monkeypatch.setattr(ledger, "export", export_spy)
    monkeypatch.setattr(run_mod, "_commit_and_push", commit_spy)
    code = monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True, commit=True)
    assert code == 2                                        # cold start: blocked, but the tail still runs
    assert seen == {"export": "blocked:StateLocked", "commit": "blocked:StateLocked"}
    assert _other_process_sees(db) == "free"                # and it is released at the very end
    assert not dblock._registry


def test_a1_lock_released_when_the_tail_raises(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)

    def boom(conn, ledger_dir):
        raise RuntimeError("export exploded")

    monkeypatch.setattr(ledger, "export", boom)
    with pytest.raises(RuntimeError):
        monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True)
    assert not dblock._registry and _other_process_sees(db) == "free"


# ----------------------------------------------------------------------------------------------- A2
def _publish_cohort(db: Path, as_of: str) -> None:
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
            "config_sha256, registry_sha256) VALUES (900, ?, 'monthly', 'live', 1, '2026-10-01T00:00:00.000000Z', "
            "'ok', 'git', 'code', 'cfg', 'reg')", (as_of,))
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
            "source_refs_json, published_at, generated_at, is_clean, run_id) VALUES (?, ?, 'live', "
            "'2026-09-30T18:29:59.999999Z', 'd', 'm', '{}', '2026-10-01T00:00:00.000000Z', "
            "'2026-10-01T00:00:00.000000Z', 1, 900)", (f"live:{as_of}", as_of))
    finally:
        conn.close()


def _monthly_runs(db: Path) -> int:
    conn = connect(db, readonly=True)
    try:
        return conn.execute("SELECT count(*) FROM runs WHERE kind = 'monthly' AND run_id <> 900").fetchone()[0]
    finally:
        conn.close()


def test_a2_cohort_published_while_waiting_for_the_lock_is_not_run_again(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    real_acquire = dblock.acquire

    def acquire_after_other_publishes(db_path, holder, *a, **kw):
        _publish_cohort(db, "2026-09-30")                   # another run published between pre-check and lock
        return real_acquire(db_path, holder, *a, **kw)

    monkeypatch.setattr(run_mod.dblock, "acquire", acquire_after_other_publishes)
    exported = []
    monkeypatch.setattr(ledger, "export", lambda *a, **k: exported.append(1))
    code = monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True)
    assert code == 0
    assert "already published" in capsys.readouterr().out
    assert _monthly_runs(db) == 0 and not exported          # nothing ran: no run row, no export
    assert not dblock._registry and _other_process_sees(db) == "free"


def test_a2_two_sequential_calls_second_does_nothing(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)
    _publish_cohort(db, "2026-09-30")
    assert monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True) == 0
    assert monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True) == 0
    assert _monthly_runs(db) == 0


# ----------------------------------------------------------------------------------------------- A3
def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    res = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    return res.stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    files = ["quant.db", "data/ledger/2026-10/events.jsonl.gz", "data/manifests/prices_2026-09-30.json",
             "knowledge/reports/2026-09.md", "ui/data.js", "unrelated.txt"]
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    cfg = load_config().with_paths(db=repo / "quant.db", data_dir=repo / "data", knowledge_dir=repo / "knowledge",
                                   ui_dir=repo / "ui", archive_dir=tmp_path / "archive",
                                   prices_db=tmp_path / "prices.db")
    return repo, cfg


@pytest.mark.parametrize("tiny_budget", [False, True])
def test_a3_commit_stages_journal_knowledge_ui_but_not_the_database(tmp_path, monkeypatch, tiny_budget):
    repo, cfg = _repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)     # no global hooks in a scratch repository
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    if tiny_budget:                                          # an "over budget" database must not change the set
        real = run_mod._cfg
        monkeypatch.setattr(run_mod, "_cfg", lambda c, s, k, d: 1 if k == "state_warn_bytes" else real(c, s, k, d))
    _commit_and_push(cfg, "2026-09-30", commit=True, push=False, clock=CLOCK, actor=ACTOR)
    committed = set(_git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split())
    assert committed == {"data/ledger/2026-10/events.jsonl.gz", "data/manifests/prices_2026-09-30.json",
                         "knowledge/reports/2026-09.md", "ui/data.js"}
    assert "quant.db" not in committed and "unrelated.txt" not in committed


def test_a3_commit_leaves_files_staged_by_someone_else_alone(tmp_path, monkeypatch):
    """Several agents share the data folder: a file another agent staged must not ride along in the state commit."""
    repo, cfg = _repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _git(repo, "add", "unrelated.txt")                       # staged by another agent, not by the run
    _commit_and_push(cfg, "2026-09-30", commit=True, push=False, clock=CLOCK, actor=ACTOR)
    committed = set(_git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split())
    assert "unrelated.txt" not in committed and "ui/data.js" in committed
    assert "unrelated.txt" in _git(repo, "diff", "--cached", "--name-only").split()      # still staged, untouched


def test_a3_no_commit_without_the_flag(tmp_path):
    repo, cfg = _repo(tmp_path)
    _commit_and_push(cfg, "2026-09-30", commit=False, push=False, clock=CLOCK, actor=ACTOR)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True)
    assert res.returncode != 0                               # still no commit


# ----------------------------------------------------------------------------------------------- A4
def test_a4_journal_equals_database_after_monthly_even_over_budget(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    real = run_mod._cfg
    monkeypatch.setattr(run_mod, "_cfg", lambda c, s, k, d: 1 if k == "state_warn_bytes" else real(c, s, k, d))
    assert monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True) == 2
    before = _journal_bytes(cfg)
    assert before
    conn = connect(db, readonly=True)
    try:
        ledger.export(conn, Path(cfg.paths.data_dir) / "ledger")
        assert _journal_bytes(cfg) == before                 # exporting again changes no journal byte
        assert ledger.verify(conn, Path(cfg.paths.data_dir) / "ledger").passed
        assert conn.execute("SELECT count(*) FROM data_quality_events WHERE code = 'STATE_BUDGET_EXCEEDED'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM runs WHERE kind = 'maintenance'").fetchone()[0] == 0
    finally:
        conn.close()


def test_a4_prints_a_warning_when_one_journal_file_is_too_big(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    monkeypatch.setattr(ledger, "JOURNAL_WARN_BYTES", 10)
    monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True)
    out = capsys.readouterr().out
    warns = [x for x in out.splitlines() if "WARN" in x and "events.jsonl.gz" in x]
    n_files = len(list((Path(cfg.paths.data_dir) / "ledger").glob("*/events.jsonl.gz")))
    assert n_files >= 1 and len(warns) == n_files                       # one line per oversized file
    assert all("above 10 bytes" in w for w in warns)
    conn = connect(db, readonly=True)
    try:                                                     # print-only: no new run, no event
        assert conn.execute("SELECT count(*) FROM runs WHERE kind = 'maintenance'").fetchone()[0] == 0
    finally:
        conn.close()


def test_a4_no_warning_when_small(tmp_path, capsys):
    cfg, _ = _world(tmp_path)
    monthly(cfg, CLOCK, ACTOR, as_of="2026-09-30", skip_capture=True)
    assert "WARN journal" not in capsys.readouterr().out
