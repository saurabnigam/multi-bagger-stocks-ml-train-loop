from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Callable
import sqlite3

from quant.config import Config
from quant.db.core import connect, update_control
from quant.errors import Blocked, Refused
from quant.types import Actor, Check, Clock, Draft


class RunContext:
    """Context manager for run lifecycle, transactional staging, and execution tracking."""

    def __init__(
        self,
        as_of: str,
        kind: str,
        track: str,
        cfg: Config,
        clock: Clock,
        actor: Actor,
    ):
        self.as_of = as_of
        self.kind = kind
        self.track = track
        self.cfg = cfg
        self.clock = clock
        self.actor = actor
        self.status = "ok"
        self.store = None
        self.checks: dict[str, Callable[[RunContext, Draft], Check]] = {}
        self.conn: sqlite3.Connection | None = None
        self.run_id: int | None = None
        self.git_sha: str = self._get_git_sha()

    @staticmethod
    def _get_git_sha() -> str:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            return res.stdout.strip()
        except Exception:
            return "dev"

    def __enter__(self) -> RunContext:
        self.conn = connect(self.cfg.paths.db)
        
        # Determine attempt number
        cur = self.conn.execute(
            "SELECT max(attempt) FROM runs WHERE as_of = ? AND kind = ? AND track = ?",
            (self.as_of, self.kind, self.track),
        )
        max_att = cur.fetchone()[0]
        attempt = (max_att or 0) + 1

        started_at = self.clock.iso()
        config_hash = self.cfg.policy_sha256
        code_hash = config_hash  # Consistent deterministic identifier
        registry_hash = "r"

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    self.as_of,
                    self.kind,
                    self.track,
                    attempt,
                    started_at,
                    self.git_sha,
                    code_hash,
                    config_hash,
                    registry_hash,
                ),
            )
            self.run_id = cur.lastrowid

            # Journal run initiation to ledger_events
            cur_run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (self.run_id,))
            run_dict = dict(cur_run.fetchone())
            key_json = json.dumps({"run_id": self.run_id}, sort_keys=True, default=str, separators=(",", ":"))
            after_json = json.dumps(run_dict, sort_keys=True, default=str, separators=(",", ":"))
            self.conn.execute(
                "INSERT INTO ledger_events (run_id, recorded_at, table_name, operation, key_json, before_sha256, after_json) "
                "VALUES (?, ?, 'runs', 'insert', ?, '', ?)",
                (self.run_id, started_at, key_json, after_json),
            )

        # Begin staging savepoint
        self.conn.execute("SAVEPOINT staging")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        finished_at = self.clock.iso()
        if exc_type is not None:
            # Rollback any uncommitted staging work
            try:
                self.conn.execute("ROLLBACK TO staging")
                self.conn.execute("RELEASE staging")
            except Exception:
                pass

            if issubclass(exc_type, Blocked):
                final_status = "blocked"
            elif issubclass(exc_type, Refused):
                final_status = "refused"
            else:
                final_status = "failed"
            error_msg = str(exc_val)
        else:
            try:
                self.conn.execute("RELEASE staging")
            except Exception:
                pass
            final_status = self.status
            error_msg = None

        # Update run status via update_control
        changes = {
            "finished_at": finished_at,
            "status": final_status,
        }
        if error_msg:
            changes["notes_json"] = json.dumps({"error": error_msg})

        update_control(
            self,
            "runs",
            {"run_id": self.run_id},
            changes,
        )
        self.conn.commit()
        self.conn.close()

        # Propagate exceptions
        return False
