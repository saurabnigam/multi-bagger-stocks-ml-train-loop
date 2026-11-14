"""WP-S: the committed record is the compressed, deterministic ledger (MASTER_SPEC 4.2, R15).

Offline: every database, ledger and backup lives under tmp_path.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pandas as pd
import pytest

from quant.cli import main as cli_main
from quant.config import load as load_config
from quant.db import ledger
from quant.db.core import apply_schema, append_rows, connect, table_hash
from quant.run import RunContext
from quant.types import Actor, FrozenClock

REPO = Path(__file__).resolve().parents[2]
BACKUP = REPO / "scripts" / "backup_state.sh"


def _run_once(db_path: Path, *, as_of: str, now: str, security_id: int) -> None:
    cfg = load_config().with_paths(db=db_path)
    with RunContext(as_of=as_of, kind="monthly", track="live", cfg=cfg, clock=FrozenClock(now),
                    actor=Actor(kind="system", name="runner")) as ctx:
        df = pd.DataFrame([{
            "security_id": security_id, "isin": f"SYN{security_id}", "name": f"Synthetic {security_id}",
            "first_seen": "2026-01-01", "last_seen": as_of, "status": "listed",
        }])
        append_rows(ctx, "securities", df, ["security_id"])
        ctx.status = "ok"


@pytest.fixture()
def world(tmp_path):
    """A state database with journal events in two calendar months (2026-10 and 2026-11)."""
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    _run_once(db_path, as_of="2026-09-30", now="2026-10-01T00:00:00.000000Z", security_id=1)
    _run_once(db_path, as_of="2026-10-30", now="2026-11-02T00:00:00.000000Z", security_id=2)
    return db_path, tmp_path / "ledger"


def _export(db_path: Path, ledger_dir: Path) -> list[Path]:
    conn = connect(db_path, readonly=True)
    try:
        return ledger.export(conn, ledger_dir)
    finally:
        conn.close()


def _plain_from_gz(gz_path: Path) -> bytes:
    with gzip.open(gz_path, "rb") as f:
        return f.read()


def test_export_writes_gz_and_no_plain_file(world):
    db_path, ledger_dir = world
    paths = _export(db_path, ledger_dir)
    assert sorted(p.parent.name for p in paths) == ["2026-10", "2026-11"]
    for p in paths:
        assert p.name == "events.jsonl.gz" and p.is_file()
        assert not (p.parent / "events.jsonl").exists()
        assert not list(p.parent.glob("*.tmp"))
        lines = _plain_from_gz(p).decode("utf-8").splitlines()
        assert lines and all(json.loads(x)["recorded_at"].startswith(p.parent.name) for x in lines)
        seqs = [json.loads(x)["seq"] for x in lines]
        assert seqs == sorted(seqs)


def test_two_exports_of_unchanged_state_are_byte_identical(world):
    db_path, ledger_dir = world
    first = {p.parent.name: p.read_bytes() for p in _export(db_path, ledger_dir)}
    second = {p.parent.name: p.read_bytes() for p in _export(db_path, ledger_dir)}
    assert first == second


def test_gzip_header_carries_no_name_and_no_mtime(world):
    db_path, ledger_dir = world
    for p in _export(db_path, ledger_dir):
        head = p.read_bytes()[:10]
        assert head[:2] == b"\x1f\x8b"
        assert head[3] & 0x08 == 0               # FLG.FNAME unset: no file name in the header
        assert head[4:8] == b"\x00\x00\x00\x00"  # MTIME 0


def test_export_rebuild_verify_round_trip_over_two_months(world, tmp_path):
    db_path, ledger_dir = world
    _export(db_path, ledger_dir)
    rebuilt = tmp_path / "rebuilt.db"
    ledger.rebuild(ledger_dir, rebuilt)
    src, reb = connect(db_path, readonly=True), connect(rebuilt, readonly=True)
    try:
        for t in ("securities", "runs", "ledger_events"):
            assert table_hash(src, t) == table_hash(reb, t)
        assert reb.execute("SELECT count(*) FROM securities").fetchone()[0] == 2
        assert ledger.verify(src, ledger_dir).passed is True
    finally:
        src.close()
        reb.close()


def test_old_plain_ledger_still_rebuilds_and_next_export_replaces_it(world, tmp_path):
    db_path, ledger_dir = world
    gz_paths = _export(db_path, ledger_dir)
    for p in gz_paths:                               # recreate what existing repositories hold
        (p.parent / "events.jsonl").write_bytes(_plain_from_gz(p))
        p.unlink()
    rebuilt = tmp_path / "from_plain.db"
    ledger.rebuild(ledger_dir, rebuilt)
    src, reb = connect(db_path, readonly=True), connect(rebuilt, readonly=True)
    try:
        assert table_hash(src, "ledger_events") == table_hash(reb, "ledger_events")
        assert ledger.verify(src, ledger_dir).passed is True
    finally:
        src.close()
        reb.close()
    again = _export(db_path, ledger_dir)
    for p in again:
        assert p.name == "events.jsonl.gz"
        assert not (p.parent / "events.jsonl").exists()


def test_reader_prefers_gz_when_both_names_exist(world, tmp_path):
    db_path, ledger_dir = world
    _export(db_path, ledger_dir)
    (ledger_dir / "2026-10" / "events.jsonl").write_text("this is not json\n")   # stale plain twin
    rebuilt = tmp_path / "prefers_gz.db"
    ledger.rebuild(ledger_dir, rebuilt)                                         # must not touch the plain file
    assert rebuilt.exists()
    assert len(ledger.load_events(ledger_dir)) == len(
        [1 for p in ledger_dir.glob("*/events.jsonl.gz") for _ in _plain_from_gz(p).splitlines()])


@pytest.mark.parametrize("damage", ["truncated", "garbage"])
def test_damaged_gz_fails_loudly(world, tmp_path, damage):
    db_path, ledger_dir = world
    paths = _export(db_path, ledger_dir)
    target = paths[-1]
    raw = target.read_bytes()
    target.write_bytes(raw[: len(raw) // 2] if damage == "truncated" else b"not a gzip file at all")
    with pytest.raises(ValueError, match="events.jsonl.gz"):
        ledger.rebuild(ledger_dir, tmp_path / "bad.db")
    conn = connect(db_path, readonly=True)
    try:
        report = ledger.verify(conn, ledger_dir)
    finally:
        conn.close()
    assert report.passed is False
    assert [c.id for c in report.checks] == ["ledger_verify_replay"]


def test_export_temp_file_name_is_unique_per_process(world, monkeypatch):
    db_path, ledger_dir = world
    seen: list[Path] = []
    real = ledger._write_gz

    def spy(path, events):
        seen.append(Path(path))
        return real(path, events)

    monkeypatch.setattr(ledger, "_write_gz", spy)
    _export(db_path, ledger_dir)
    assert seen and all(str(os.getpid()) in p.name and p.name.endswith(".tmp") for p in seen)
    assert not list(ledger_dir.glob("*/*.tmp"))
    # another exporter's temp file is never touched by this one
    other = ledger_dir / "2026-10" / "events.jsonl.gz.99999999.tmp"
    other.write_bytes(b"belongs to another process")
    _export(db_path, ledger_dir)
    assert other.read_bytes() == b"belongs to another process"


def test_differing_plain_twin_gives_one_warning_naming_the_directory(world, capsys):
    db_path, ledger_dir = world
    _export(db_path, ledger_dir)
    month = ledger_dir / "2026-10"
    capsys.readouterr()
    ledger.load_events(ledger_dir)
    assert capsys.readouterr().err == ""                      # no twin: silent
    gz_lines = _plain_from_gz(month / "events.jsonl.gz").decode().splitlines()
    (month / "events.jsonl").write_text("\n".join(gz_lines) + "\n")        # same decoded content: silent
    ledger.load_events(ledger_dir)
    assert capsys.readouterr().err == ""
    (month / "events.jsonl").write_text("\n".join(gz_lines[:-1]) + "\n")   # valid but different
    events = ledger.load_events(ledger_dir)
    err = capsys.readouterr()
    warnings = [x for x in (err.out + err.err).splitlines() if x.strip()]
    assert len(warnings) == 1 and str(month) in warnings[0] and "events.jsonl" in warnings[0]
    assert len(events) == len([1 for p in ledger_dir.glob("*/events.jsonl.gz") for _ in _plain_from_gz(p).splitlines()])


def _size_output(monkeypatch, capsys, db_path: Path, data_dir: Path) -> str:
    monkeypatch.setenv("QUANT_DB_PATH", str(db_path))
    monkeypatch.setenv("QUANT_DATA_DIR", str(data_dir))
    assert cli_main(["db", "size"]) == 0
    return capsys.readouterr().out


def test_db_size_prints_compressed_journal_bytes_per_month_and_total(world, tmp_path, monkeypatch, capsys):
    db_path, _ = world
    data_dir = tmp_path / "data"
    paths = _export(db_path, data_dir / "ledger")
    out = _size_output(monkeypatch, capsys, db_path, data_dir)
    total = 0
    for p in paths:
        n = p.stat().st_size
        total += n
        assert f"journal {p.parent.name} events.jsonl.gz {n} bytes" in out
    assert f"journal total {total} bytes" in out
    assert "WARN" not in out


def test_db_size_warns_when_a_journal_file_exceeds_45_mib(world, tmp_path, monkeypatch, capsys):
    assert ledger.JOURNAL_WARN_BYTES == 45 * 1024 * 1024
    db_path, _ = world
    data_dir = tmp_path / "data"
    _export(db_path, data_dir / "ledger")
    monkeypatch.setattr(ledger, "JOURNAL_WARN_BYTES", 10)           # the real limit is checked above
    out = _size_output(monkeypatch, capsys, db_path, data_dir)
    warns = [x for x in out.splitlines() if x.startswith("WARN")]
    assert len(warns) == 2 and all("45 MiB" in w for w in warns)
    # a legacy plain file counts too, and is labelled as not compressed
    month = data_dir / "ledger" / "2026-10"
    (month / "events.jsonl").write_bytes(_plain_from_gz(month / "events.jsonl.gz"))
    (month / "events.jsonl.gz").unlink()
    out = _size_output(monkeypatch, capsys, db_path, data_dir)
    assert "journal 2026-10 events.jsonl " in out and "(plain, not compressed)" in out


# ---------------------------------------------------------------------------------------------------------
# scripts/backup_state.sh
# ---------------------------------------------------------------------------------------------------------

needs_sqlite3 = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def _sources(tmp_path: Path) -> dict[str, str]:
    src = tmp_path / "src"
    (src / "archive" / "sub").mkdir(parents=True)
    (src / "archive" / "a.json").write_text('{"a": 1}')
    (src / "archive" / "sub" / "b.csv").write_text("x,y\n1,2\n")
    for name in ("state.db", "prices.sqlite"):
        c = sqlite3.connect(src / name)
        c.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t (v) VALUES (?)", [(f"{name}-{i}",) for i in range(50)])
        c.commit()
        c.close()
    return {"QUANT_DB_PATH": str(src / "state.db"), "QUANT_PRICES_DB_PATH": str(src / "prices.sqlite"),
            "QUANT_ARCHIVE_DIR": str(src / "archive")}


def _backup(args: list[str], env_extra: dict[str, str], tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUANT_")}
    env.update(env_extra)
    return subprocess.run(["bash", str(BACKUP), *args], cwd=tmp_path, env=env, capture_output=True, text=True)


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


@needs_sqlite3
def test_backup_dry_run_writes_nothing(tmp_path):
    env = _sources(tmp_path)
    dest = tmp_path / "dest"
    before = _tree(tmp_path / "src")
    res = _backup(["--dry-run", str(dest)], env, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "dry run" in res.stdout.lower() and "snapshot-" in res.stdout
    assert not dest.exists()
    assert _tree(tmp_path / "src") == before


@needs_sqlite3
def test_backup_real_run_writes_files_and_a_matching_inventory(tmp_path):
    env = _sources(tmp_path)
    dest = tmp_path / "dest"
    res = _backup([str(dest)], env, tmp_path)
    assert res.returncode == 0, res.stderr
    snaps = [p for p in dest.iterdir() if p.name.startswith("snapshot-")]
    assert len(snaps) == 1
    snap = snaps[0]
    assert sorted(p.name for p in snap.iterdir()) == [
        "INVENTORY.json", "archive.tar.gz", "prices.sqlite.gz", "state.db.gz"]
    inv = json.loads((snap / "INVENTORY.json").read_text())
    assert {f["name"] for f in inv["files"]} == {"archive.tar.gz", "prices.sqlite.gz", "state.db.gz"}
    for f in inv["files"]:
        data = (snap / f["name"]).read_bytes()
        assert f["bytes"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert inv["sources"]["state_db"] == env["QUANT_DB_PATH"]
    assert inv["created_utc"].endswith("Z") and "git_commit" in inv
    # the copy is a usable SQLite database holding the source rows
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress((snap / "state.db.gz").read_bytes()))
    c = sqlite3.connect(restored)
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    listing = subprocess.run(["tar", "-tzf", str(snap / "archive.tar.gz")], capture_output=True, text=True)
    assert "archive/a.json" in listing.stdout and "archive/sub/b.csv" in listing.stdout


@needs_sqlite3
def test_backup_keeps_only_keep_newest_snapshots_and_only_its_own(tmp_path):
    env = _sources(tmp_path)
    dest = tmp_path / "dest"
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        d = dest / f"snapshot-{stamp}"
        d.mkdir(parents=True)
        (d / "INVENTORY.json").write_text("{}")
    (dest / "snapshot-20190101T000000Z").mkdir()                  # no inventory: not ours
    (dest / "photos").mkdir()
    res = _backup([str(dest)], {**env, "KEEP": "2"}, tmp_path)
    assert res.returncode == 0, res.stderr
    names = sorted(p.name for p in dest.iterdir())
    assert "snapshot-20200101T000000Z" not in names and "snapshot-20200102T000000Z" not in names
    assert "snapshot-20200103T000000Z" in names and "snapshot-20190101T000000Z" in names and "photos" in names
    assert len([n for n in names if n.startswith("snapshot-") and (dest / n / "INVENTORY.json").exists()]) == 2


@needs_sqlite3
def test_backup_fails_loudly_on_a_missing_source(tmp_path):
    env = _sources(tmp_path)
    env["QUANT_PRICES_DB_PATH"] = str(tmp_path / "src" / "nope.sqlite")
    dest = tmp_path / "dest"
    res = _backup([str(dest)], env, tmp_path)
    assert res.returncode != 0 and "nope.sqlite" in res.stderr
    assert not dest.exists() or not [p for p in dest.iterdir()]


@needs_sqlite3
def test_backup_refuses_a_destination_inside_the_repository(tmp_path):
    env = _sources(tmp_path)
    inside = REPO / "data" / "never_written_backup_dir"
    res = _backup([str(inside)], env, tmp_path)
    assert res.returncode != 0 and "repository" in res.stderr
    assert not inside.exists()
