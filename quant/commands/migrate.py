"""CLI command handler for legacy SQLite database migration and reconciliation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant.cli import register
from quant.config import load
from quant.db.core import apply_schema, connect
from quant.migrate.legacy import reconcile, run as run_migration
from quant.run import RunContext
from quant.types import Actor, SystemClock


def cmd_migrate_legacy(args: argparse.Namespace) -> int:
    """Migrate legacy database into V2 schema."""
    cfg = load(args.config if hasattr(args, "config") and args.config else None)
    db_path = Path(args.db_path) if hasattr(args, "db_path") and args.db_path else cfg.paths.db
    legacy_db = Path(args.legacy_db) if hasattr(args, "legacy_db") and args.legacy_db else cfg.paths.legacy_db

    conn = connect(db_path)
    apply_schema(conn, kind="state")

    clock = SystemClock()
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=getattr(args, "by", "system:migration"),
    )

    ctx = RunContext(
        as_of="2026-09-03",
        kind="migration",
        track="legacy",
        cfg=cfg.with_paths(db=db_path),
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    try:
        # Check if runs row exists for migration
        r = conn.execute("SELECT run_id FROM runs WHERE kind = 'migration' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not r:
            with conn:
                conn.execute(
                    """
                    INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256)
                    VALUES (1, '2026-09-03', 'migration', 'legacy', 1, ?, 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')
                    """,
                    (clock.iso(),),
                )
        dry_run = getattr(args, "dry_run", False)
        res = run_migration(ctx, legacy_db_path=legacy_db, dry_run=dry_run)
        print(f"Migration result: {res.status}, counts={res.counts}, details={res.details}")

        if not dry_run:
            df = reconcile(conn, legacy_db_path=legacy_db)
            print(df.to_string(index=False))
        return 0
    finally:
        conn.close()


register("db", "migrate-legacy", cmd_migrate_legacy, "Migrate legacy database into V2 schema")
