"""CLI command handler for legacy SQLite database migration and reconciliation."""

from __future__ import annotations

import argparse
from pathlib import Path

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
    cfg = cfg.with_paths(db=db_path)
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        # INTERFACES.md C10: "dry_run never writes to either DB, filesystem or Git." A
        # RunContext must NOT be opened here: RunContext.__enter__ unconditionally connects
        # to the target state DB and INSERTs a `runs` row (and __exit__ UPDATEs/COMMITs it),
        # which would itself be a write. legacy.run()'s dry_run branch only hashes the
        # legacy source file and never dereferences `ctx`, so no RunContext, no state-DB
        # connection, and no schema bootstrap are needed for a dry run at all.
        res = run_migration(None, legacy_db_path=legacy_db, dry_run=True)
        print(f"Migration result: {res.status}, counts={res.counts}, details={res.details}")
        return 0

    # RunContext.__enter__ inserts the run's `runs` row immediately, so the schema (and its
    # journal triggers) must already exist on the target file before it opens its connection.
    bootstrap_conn = connect(db_path)
    apply_schema(bootstrap_conn, kind="state")
    bootstrap_conn.close()

    clock = SystemClock()
    actor = Actor(
        kind=getattr(args, "actor_kind", "system"),
        name=getattr(args, "by", "system:migration"),
    )

    with RunContext(as_of="2026-09-03", kind="migration", track="legacy", cfg=cfg, clock=clock, actor=actor) as ctx:
        res = run_migration(ctx, legacy_db_path=legacy_db, dry_run=False)
        print(f"Migration result: {res.status}, counts={res.counts}, details={res.details}")

        df = reconcile(ctx.conn, legacy_db_path=legacy_db)
        print(df.to_string(index=False))
    return 0


register("db", "migrate-legacy", cmd_migrate_legacy, "Migrate legacy database into V2 schema")
