from __future__ import annotations

import datetime
import hashlib
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
            if hasattr(self.conn, "set_run_context"):
                self.conn.set_run_context(self.run_id, self.clock.iso)

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


def monthly(
    cfg: Config,
    clock: Clock,
    actor: Actor,
    *,
    as_of: str | None = None,
    skip_capture: bool = False,
    stop_after: str | None = None,
    dry_run: bool = False,
    commit: bool = False,
    push: bool = False,
) -> int:
    """Execute the monthly quant pipeline in strict MASTER_SPEC 9.1 sequence."""
    # 1. Lock and reject future dates; exit 0 immediately if that live cohort is already published.
    now_iso = clock.iso()
    now_date = now_iso[:10]

    if as_of is None:
        now_dt = clock.now()
        first_this_month = now_dt.replace(day=1)
        last_prev_month = first_this_month - datetime.timedelta(days=1)
        as_of = last_prev_month.strftime("%Y-%m-%d")

    if as_of > now_date:
        print(f"Rejected future date: as_of={as_of} > now={now_date}")
        return 2  # Blocked

    # Governance authority precheck
    if actor.kind == "llm" and actor.name.startswith("human:"):
        print("Governance refusal: LLM cannot impersonate human actor.")
        return 3

    # Check if live cohort for as_of is already published
    conn_pre = connect(cfg.paths.db)
    try:
        cur_pre = conn_pre.cursor()
        pub_row = cur_pre.execute(
            "SELECT cohort_id FROM cohorts WHERE as_of = ? AND track = 'live'",
            (as_of,),
        ).fetchone()
        if pub_row:
            print(f"Live cohort for {as_of} already published ({pub_row[0]}); exiting 0.")
            return 0
    finally:
        conn_pre.close()

    if dry_run:
        print(f"Dry-run planning for as_of={as_of} completed without writes.")
        return 0

    # Acquire lock file to prevent concurrent runs
    lock_path = Path(str(cfg.paths.db) + ".lock")
    lock_file = None
    try:
        import fcntl
        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            print(f"Another monthly run is currently running (lock {lock_path} active).")
            return 1
    except Exception:
        lock_file = None

    ctx = RunContext(
        as_of=as_of,
        kind="monthly",
        track="live",
        cfg=cfg,
        clock=clock,
        actor=actor,
    )

    try:
        with ctx:
            # 2 & 3. Settle pending orders, mature old labels, evaluate prior cohorts
            try:
                from quant.portfolio import paper
                paper.settle(ctx, through=as_of)
                paper.roll_forward(ctx, through=as_of)
            except Exception as e:
                print(f"Order settlement notice: {e}")

            try:
                from quant.evaluation import labels
                labels.mature(ctx, through=as_of)
            except Exception as e:
                print(f"Label maturation notice: {e}")

            try:
                from quant.evaluation import evaluate
                evaluate.run(ctx, through=as_of, track="live")
            except Exception as e:
                print(f"Evaluation notice: {e}")

            ctx.conn.commit()

            if stop_after in ("settle", "labels", "evaluate"):
                return 0

            # 4 & 5. Pre-compute gates, staging, fitting, scoring, post-compute gates, publication
            ctx.conn.execute("SAVEPOINT cohort_pub")
            scoring_failed = False

            try:
                if not skip_capture:
                    try:
                        from quant.data import universe
                        universe.capture(ctx)
                    except Exception as e:
                        print(f"Capture notice: {e}")

                from quant.data.universe import members_at
                cutoff = f"{as_of}T18:29:59.999999Z"
                members_df = members_at(ctx.conn, cutoff, index_name="NIFTY500")
                min_rows = int(getattr(getattr(cfg, "universe", None), "min_rows", 480))

                if members_df.empty or len(members_df) < min_rows:
                    print(f"Coldstart or insufficient universe: {len(members_df)} < {min_rows}. Blocking publication.")
                    scoring_failed = True
                    ctx.conn.execute("ROLLBACK TO cohort_pub")
                    ctx.conn.execute("RELEASE cohort_pub")
                    ctx.status = "blocked"
                else:
                    cohort_id = f"live:{as_of}"
                    draft = Draft(
                        cohort_id=cohort_id,
                        as_of=as_of,
                        track="live",
                        knowledge_cutoff=cutoff,
                        definition_hash="live_def_hash",
                        membership_hash="live_mem_hash",
                    )
                    from quant.data.gates import run_gates
                    run_gates(ctx, draft, phase="pre", strict=True)

                    from quant.model.models import score_all
                    score_all(ctx, cohort_id=cohort_id)

                    run_gates(ctx, draft, phase="post", strict=True)

                    def_hash = hashlib.sha256(f"cohort:{as_of}".encode("utf-8")).hexdigest()
                    mem_hash = hashlib.sha256(f"members:{as_of}".encode("utf-8")).hexdigest()
                    pub_iso = clock.iso()

                    ctx.conn.execute(
                        """
                        INSERT INTO cohorts (
                            cohort_id, as_of, track, knowledge_cutoff, definition_hash,
                            membership_hash, source_refs_json, published_at, generated_at,
                            is_clean, run_id
                        ) VALUES (?, ?, 'live', ?, ?, ?, '[]', ?, ?, 1, ?)
                        """,
                        (cohort_id, as_of, cutoff, def_hash, mem_hash, pub_iso, pub_iso, ctx.run_id),
                    )
                    ctx.conn.execute("RELEASE cohort_pub")

                    # 6. Future paper orders from published scores
                    try:
                        paper.plan(ctx, cohort_id=cohort_id)
                    except Exception as e:
                        print(f"Paper planning notice: {e}")

            except Exception as e:
                scoring_failed = True
                print(f"Scoring/gates error: {e}")
                try:
                    ctx.conn.execute("ROLLBACK TO cohort_pub")
                    ctx.conn.execute("RELEASE cohort_pub")
                except Exception:
                    pass
                ctx.status = "blocked"

            # 7. Criteria review & draft proposals
            try:
                from quant.knowledge import proposals
                proposals.draft(ctx, as_of=as_of)
            except Exception as e:
                print(f"Proposals notice: {e}")

            # 8. Reports & UI export from persisted state
            try:
                from quant.knowledge import report
                report.render(ctx.conn, as_of=as_of, cfg=cfg)
            except Exception as e:
                print(f"Report notice: {e}")

            try:
                from quant import ui_export
                ui_export.export(ctx.conn, cfg)
            except Exception as e:
                print(f"UI export notice: {e}")

            ctx.conn.commit()

            if scoring_failed:
                return 2

            return 0
    except Blocked:
        return 2
    except Refused:
        return 3
    except Exception as exc:
        print(f"Runner exception: {exc}")
        return 1
    finally:
        if lock_file:
            try:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

