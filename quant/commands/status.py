"""`quant status`: operational status from persisted state (C11)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quant.cli import register
from quant.config import load as load_config
from quant.db.core import connect
from quant.status import read as read_status


def cmd_status_show(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None) or None)
    db_path = getattr(args, "db_path", None)
    if db_path:
        cfg = cfg.with_paths(db=Path(db_path))
    if not Path(cfg.paths.db).exists():
        print(json.dumps({"status": "no_database", "db": str(cfg.paths.db)}, indent=2))
        return 0
    conn = connect(cfg.paths.db, readonly=True)
    try:
        status = read_status(conn, cfg)
    finally:
        conn.close()
    print(json.dumps(status, indent=2, default=str))
    return 0


register("status", "show", cmd_status_show, "Show quant engine status")
