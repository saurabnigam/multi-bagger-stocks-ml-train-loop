"""Run lifecycle (RunContext) and the monthly orchestration (MASTER_SPEC 9.1, C11).

Transaction model
-----------------
``RunContext.__enter__`` inserts the ``runs`` row (committed immediately so a
crash leaves a visible ``running`` attempt), registers the run on the
connection so the journal triggers stamp every write with this run_id, and
opens the ``staging`` savepoint. Library code never commits. The runner calls
``ctx.checkpoint()`` at the spec's preservation points (after settlement /
maturation / evaluation, after publication); ``ctx.rollback_staging()``
discards the current staging work but re-inserts diagnostics registered with
``ctx.keep_after_rollback`` (gate rows, DQ events). ``__exit__`` releases or
rolls back the open staging work, finalizes the run row and commits.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import platform
from pathlib import Path
import subprocess
from typing import Any, Callable, Optional
import sqlite3

import pandas as pd

from quant.config import Config
from quant.db import core as dbcore
from quant.db.core import append_rows, connect, update_control
from quant.errors import Blocked, Refused
from quant.types import Actor, Check, Clock, Draft, Result

STOP_POINTS = ("capture", "mature", "gates", "stage", "publish", "record")


class RunContext:
    """Context manager for run lifecycle, transactional staging and execution tracking."""

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
        self.calendar = None
        self.checks: dict[str, Callable[[RunContext, Draft], Check]] = {}
        self.conn: sqlite3.Connection | None = None
        self.run_id: int | None = None
        self.git_sha: str = self._get_git_sha()
        self.notes: dict[str, Any] = {}
        self._audit: list[tuple[str, pd.DataFrame, list[str]]] = []
        self._staging_open = False

    @staticmethod
    def _get_git_sha() -> str:
        try:
            res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
            return res.stdout.strip()
        except Exception:
            return "dev"

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> RunContext:
        self.conn = connect(self.cfg.paths.db)
        max_att = self.conn.execute(
            "SELECT max(attempt) FROM runs WHERE as_of = ? AND kind = ? AND track = ?",
            (self.as_of, self.kind, self.track),
        ).fetchone()[0]
        attempt = (max_att or 0) + 1

        registry_hash = (
            dbcore.table_hash(self.conn, "factor_registry")
            if dbcore._has_table(self.conn, "factor_registry")
            else ""
        )
        try:
            import importlib.metadata as md
            yf_version = md.version("yfinance")
        except Exception:
            yf_version = None

        cur = self.conn.execute(
            "INSERT INTO runs (as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
            "config_sha256, registry_sha256, yfinance_version, python_version) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
            (
                self.as_of, self.kind, self.track, attempt, self.clock.iso(), self.git_sha,
                dbcore.code_sha256(), self.cfg.policy_sha256, registry_hash, yf_version,
                platform.python_version(),
            ),
        )
        self.run_id = int(cur.lastrowid)
        if hasattr(self.conn, "set_run_context"):
            self.conn.set_run_context(self.run_id, self.clock.iso)
        self.conn.execute("SAVEPOINT staging")
        self._staging_open = True
        return self

    def keep_after_rollback(self, table: str, rows: pd.DataFrame, keys: list[str]) -> None:
        """Register diagnostic rows that must survive a staging rollback."""
        self._audit.append((table, rows.copy(), list(keys)))

    def _replay_audit(self) -> None:
        for table, rows, keys in self._audit:
            append_rows(self, table, rows, keys)
        self._audit.clear()

    def rollback_staging(self) -> None:
        """Discard the open staging work but keep registered diagnostics."""
        if not self._staging_open:
            return
        self.conn.execute("ROLLBACK TO staging")
        self._replay_audit()

    def checkpoint(self) -> None:
        """Persist the staging work done so far and open a fresh staging savepoint."""
        if self._staging_open:
            self.conn.execute("RELEASE staging")
            self._staging_open = False
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")
        self._audit.clear()
        self.conn.execute("SAVEPOINT staging")
        self._staging_open = True

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        finished_at = self.clock.iso()
        if exc_type is not None:
            if self._staging_open:
                try:
                    self.conn.execute("ROLLBACK TO staging")
                    self.conn.execute("RELEASE staging")
                except Exception:
                    pass
                self._staging_open = False
            try:
                self._replay_audit()
            except Exception:
                pass
            if issubclass(exc_type, Blocked):
                final_status = "blocked"
            elif issubclass(exc_type, Refused):
                final_status = "refused"
            else:
                final_status = "failed"
            self.notes["error"] = str(exc_val)
        else:
            if self._staging_open:
                try:
                    self.conn.execute("RELEASE staging")
                except Exception:
                    pass
                self._staging_open = False
            final_status = self.status

        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

        changes: dict[str, Any] = {"finished_at": finished_at, "status": final_status}
        if self.notes:
            changes["notes_json"] = json.dumps(self.notes, sort_keys=True, default=str)
        update_control(self, "runs", {"run_id": self.run_id}, changes)
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")
        if hasattr(self.conn, "set_run_context"):
            self.conn.set_run_context(None, None)
        self.conn.close()
        return False


# ============================================================================ monthly runner

def _cfg(cfg: Config, section: str, key: str, default: Any) -> Any:
    sec = getattr(cfg, section, None)
    return getattr(sec, key, default) if sec is not None else default


def _print(msg: str) -> None:
    print(msg, flush=True)


def _load_calendar(cfg: Config, store: Any):
    from quant.data.calendar import Calendar
    try:
        return Calendar.load(cfg, store=store)
    except Exception:
        return None


def default_as_of(clock: Clock, cal: Any) -> str:
    """Last completed session of the previous calendar month (IST)."""
    from zoneinfo import ZoneInfo

    now_ist = clock.now().astimezone(ZoneInfo("Asia/Kolkata"))
    first_this_month = now_ist.replace(day=1).date()
    last_prev_month = first_this_month - datetime.timedelta(days=1)
    if cal is not None:
        try:
            return cal.last_session_on_or_before(last_prev_month.strftime("%Y-%m-%d"))
        except Exception:
            pass
    return last_prev_month.strftime("%Y-%m-%d")


def month_end_session(as_of: str, cal: Any) -> str:
    """Last session on or before the last calendar day of as_of's month."""
    import calendar as _calendar

    y, m = int(as_of[:4]), int(as_of[5:7])
    last_day = f"{y:04d}-{m:02d}-{_calendar.monthrange(y, m)[1]:02d}"
    if cal is not None:
        try:
            return cal.last_session_on_or_before(last_day)
        except Exception:
            pass
    d = datetime.date.fromisoformat(last_day)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def knowledge_cutoff(as_of: str) -> str:
    """as_of 23:59:59.999999 Asia/Kolkata expressed in UTC."""
    from zoneinfo import ZoneInfo

    dt_local = datetime.datetime.fromisoformat(f"{as_of}T23:59:59.999999").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return dt_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _definition_hash(conn: sqlite3.Connection, as_of: str, cfg: Config) -> str:
    """Hash of the definitions that produce a cohort: factors, model versions, policy, taxonomy."""
    factors = conn.execute(
        "SELECT factor_id, version, code_sha256, status, direction FROM factor_registry "
        "WHERE status IN ('active','shadow','probation') ORDER BY factor_id"
    ).fetchall()
    versions = conn.execute(
        "SELECT model_id, version, factor_set_json, weights_json FROM model_versions "
        "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) ORDER BY model_id, version",
        (as_of, as_of),
    ).fetchall()
    payload = {
        "factors": [tuple(r) for r in factors],
        "model_versions": [tuple(r) for r in versions],
        "policy_sha256": cfg.policy_sha256,
        "group_def_version": int(_cfg(cfg, "sectors", "group_def_version", 1)),
        "code_sha256": dbcore.code_sha256(),
    }
    return dbcore.sha256_text(dbcore.canonical_json(payload))


def _membership_hash(members: pd.DataFrame) -> str:
    cols = [c for c in ("security_id", "isin", "symbol", "series", "nse_sector") if c in members.columns]
    m = members.reset_index(drop=True)
    rows = m.sort_values("security_id")[cols].astype(str).values.tolist()
    return dbcore.sha256_text(dbcore.canonical_json(rows))


def build_draft(ctx: RunContext, as_of: str, cutoff: str) -> Draft:
    """Assemble the target cohort draft strictly from pre-cutoff observations."""
    from quant.data.universe import members_at
    from quant.sectors import taxonomy

    universe_error: Optional[str] = None
    try:
        members = members_at(ctx.conn, cutoff, index_name="NIFTY500")
    except Blocked as exc:
        # No admissible or a stale universe capture: let the gates record it (G1/G2) rather
        # than exiting before any diagnostic exists (spec 3.2 cold start, 4.6 G2).
        universe_error = exc.code
        members = pd.DataFrame(columns=["security_id", "isin", "symbol", "company_name", "nse_sector", "series"])
    if "security_id" in members.columns:
        members = members.set_index("security_id", drop=False)
    elif members.index.name == "security_id":
        members["security_id"] = members.index
    sids = sorted(int(x) for x in members["security_id"].unique())

    if sids:
        taxonomy.capture(ctx, members)
        groups = taxonomy.groups_at(ctx.conn, as_of, sids)
    else:
        groups = pd.Series(dtype=object)

    cap = ctx.conn.execute(
        "SELECT capture_id, captured_at, sha256 FROM captures WHERE kind = 'nifty500' AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    source_refs: dict[str, Any] = {
        "universe_capture_id": cap["capture_id"] if cap else None,
        "universe_captured_at": cap["captured_at"] if cap else None,
        "universe_sha256": cap["sha256"] if cap else None,
        "universe_error": universe_error,
        "knowledge_cutoff": cutoff,
    }
    if ctx.store is not None:
        manifest_dir = Path(ctx.cfg.paths.data_dir) / "manifests"
        source_refs["price_manifest_sha"] = ctx.store.manifest_write(
            manifest_dir / f"prices_{as_of}.json", vintage_at=cutoff
        )
    return Draft(
        cohort_id=f"live:{as_of}",
        as_of=as_of,
        track="live",
        knowledge_cutoff=cutoff,
        definition_hash=_definition_hash(ctx.conn, as_of, ctx.cfg),
        members=members,
        groups=groups,
        source_refs=source_refs,
        membership_hash=_membership_hash(members) if sids else "",
    )


def _g9_replay(ctx: RunContext, draft: Draft) -> Check:
    """G9: recompute the prior live cohort's factor values from pinned inputs and compare."""
    from quant.factors import registry as factor_registry

    prior = ctx.conn.execute(
        "SELECT cohort_id, as_of, knowledge_cutoff, definition_hash FROM cohorts "
        "WHERE track = 'live' AND as_of < ? ORDER BY as_of DESC, published_at DESC LIMIT 1",
        (draft.as_of,),
    ).fetchone()
    if prior is None:
        return Check(id="G9", status="DEFERRED", observed=None, expected="exact_match",
                     reason="No prior live cohort", blocking=False)
    stored = pd.read_sql_query(
        "SELECT security_id, factor_id, z, sector_group FROM factor_values WHERE cohort_id = ?",
        ctx.conn, params=(prior["cohort_id"],),
    )
    if stored.empty:
        return Check(id="G9", status="FAIL", observed=0, expected="stored factor rows",
                     reason=f"Prior cohort {prior['cohort_id']} has no stored factor values", blocking=True)
    sids = sorted(stored["security_id"].unique().tolist())
    members = pd.DataFrame({"security_id": sids}).set_index("security_id", drop=False)
    groups = stored.drop_duplicates("security_id").set_index("security_id")["sector_group"].reindex(sids)
    replay_draft = Draft(
        cohort_id=prior["cohort_id"], as_of=prior["as_of"], track="live",
        knowledge_cutoff=prior["knowledge_cutoff"], definition_hash=prior["definition_hash"],
        members=members, groups=groups, source_refs={"replay": True},
    )
    recomputed = factor_registry.compute_all(ctx, replay_draft)
    # T3/D5: compare only factor ids the current registry actually recomputes. A stored
    # factor id that the current registry no longer recomputes -- retired, or superseded
    # by a version bump (a new factor_id under MASTER_SPEC 5.1/5.2) -- cannot be replayed
    # and must not fail the gate; it is reported for visibility instead.
    recomputed_ids = set(recomputed["factor_id"].unique())
    stored_ids = set(stored["factor_id"].unique())
    not_replayed = sorted(stored_ids - recomputed_ids)
    stored_live = stored[stored["factor_id"].isin(recomputed_ids)]
    merged = stored_live.merge(recomputed[["security_id", "factor_id", "z"]], on=["security_id", "factor_id"],
                         how="left", suffixes=("_stored", "_replay"))
    both_nan = merged["z_stored"].isna() & merged["z_replay"].isna()
    diff = (merged["z_stored"] - merged["z_replay"]).abs()
    mismatch = merged[~both_nan & ~(diff <= 1e-9)]
    ok = mismatch.empty
    return Check(
        id="G9", status="PASS" if ok else "FAIL",
        observed={"rows": int(len(merged)), "mismatches": int(len(mismatch)), "not_replayed": not_replayed},
        expected="exact_match",
        reason=("Prior cohort reproduced from pinned inputs" if ok
                else f"{len(mismatch)} factor values differ on replay of {prior['cohort_id']}"),
        blocking=not ok,
    )


def _g10_leakage(ctx: RunContext, draft: Draft) -> Check:
    from quant.evaluation import leakage

    report = leakage.run(ctx, draft)
    failed = [c.id for c in report.checks if c.status == "FAIL" and c.blocking]
    deferred = [c.id for c in report.checks if c.status == "DEFERRED"]
    if failed:
        return Check(id="G10", status="FAIL", observed={"failed": failed, "deferred": deferred},
                     expected="no_leakage", reason=f"Leakage checks failed: {failed}", blocking=True)
    if len(deferred) == len(report.checks):
        return Check(id="G10", status="DEFERRED", observed={"deferred": deferred}, expected="no_leakage",
                     reason="No leakage evidence yet; all checks deferred", blocking=False)
    return Check(id="G10", status="PASS", observed={"deferred": deferred}, expected="no_leakage",
                 reason=f"Leakage checks passed ({len(report.checks) - len(deferred)} evaluated)", blocking=False)


def _stage_and_publish(ctx: RunContext, draft: Draft, stop_after: Optional[str]) -> bool:
    """Spec 9.1 steps 4-5. Returns True when a cohort was published."""
    from quant.data import gates
    from quant.data.prices import monthly_panel
    from quant.evaluation.walkforward import family_ic_history
    from quant.factors import registry as factor_registry
    from quant.model import models

    gates.run(ctx, draft, phase="pre", strict=True)
    if stop_after == "gates":
        return False

    draft.factor_values = factor_registry.compute_all(ctx, draft)
    if draft.factor_values is None or len(draft.factor_values) == 0:
        raise Blocked("no_factor_values", "Factor computation produced no rows")
    draft.source_refs["excluded_factors"] = gates.excluded_factors(ctx, draft)

    ic_hist = pd.DataFrame()
    try:
        defn = models.definition_at(ctx.conn, "IC_SHRUNK_v1", draft.as_of)
        ic_hist = family_ic_history(
            ctx.conn,
            {"weights_json": defn["weights"], "factor_set_json": defn["factor_set"], "horizon_m": 3},
            draft.as_of,
            known_at=ctx.clock.iso(),
        )
    except KeyError:
        pass
    models.score_all(ctx, draft, ic_hist)
    if stop_after == "stage":
        return False

    gates.run(ctx, draft, phase="post", strict=True, g9_replay_cb=_g9_replay, g10_eval_cb=_g10_leakage)
    inv = models.check_draft(draft, ctx.cfg)
    if not inv.passed:
        failed = [c.id for c in inv.checks if c.status == "FAIL" and c.blocking]
        raise Blocked("SCORE_INVARIANTS", f"Draft score invariants failed: {failed}")

    now_iso = ctx.clock.iso()
    cohort_row = pd.DataFrame([{
        "cohort_id": draft.cohort_id, "as_of": draft.as_of, "track": draft.track,
        "knowledge_cutoff": draft.knowledge_cutoff, "definition_hash": draft.definition_hash,
        "membership_hash": draft.membership_hash,
        "source_refs_json": dbcore.canonical_json({k: v for k, v in draft.source_refs.items() if not k.startswith("_")}),
        "published_at": now_iso, "generated_at": now_iso, "is_clean": 1, "run_id": ctx.run_id,
    }])
    append_rows(ctx, "cohorts", cohort_row, ["cohort_id"])
    panel = monthly_panel(ctx, draft)
    if not panel.empty:
        append_rows(ctx, "prices_monthly", panel, ["cohort_id", "security_id"])
    append_rows(ctx, "factor_values", draft.factor_values, ["cohort_id", "security_id", "factor_id"])
    if draft.model_weights is not None and len(draft.model_weights):
        append_rows(ctx, "model_weights", draft.model_weights, ["cohort_id", "model_id", "family"])
    append_rows(ctx, "scores", draft.scores, ["cohort_id", "security_id", "model_id"])

    n_scored = int(draft.scores.loc[draft.scores["model_id"] == "EW_HIER_v1", "scored"].sum()) if "scored" in draft.scores else 0
    n_elig = int(draft.scores.loc[draft.scores["model_id"] == "EW_HIER_v1", "eligible"].sum()) if "eligible" in draft.scores else 0
    ctx.notes.update({"n_universe": int(len(draft.members)), "n_scored": n_scored, "n_eligible": n_elig})
    return True


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()


def _commit_and_push(cfg: Config, as_of: str, *, commit: bool, push: bool, clock: Clock, actor: Actor) -> None:
    """Commit only intended data/report files; push only with the explicit flag."""
    if not commit:
        return
    root = Path(cfg.paths.db).resolve().parent
    intended = [cfg.paths.db, Path(cfg.paths.data_dir) / "ledger", Path(cfg.paths.data_dir) / "manifests",
                Path(cfg.paths.knowledge_dir), Path(cfg.paths.ui_dir)]
    rel = []
    for p in intended:
        p = Path(p)
        if p.exists():
            try:
                rel.append(str(p.resolve().relative_to(root)))
            except ValueError:
                continue
    if not rel:
        return
    # MASTER_SPEC 10.5: measure growth; an oversized state file is a recorded capacity issue,
    # never silently committed.
    warn_bytes = int(_cfg(cfg, "budgets", "state_warn_bytes", 50_000_000))
    db_path = Path(cfg.paths.db)
    if db_path.exists() and db_path.stat().st_size > warn_bytes:
        actual_bytes = db_path.stat().st_size
        _print(f"State database {db_path} is {actual_bytes} bytes (> state_warn_bytes {warn_bytes}); "
               "not staged. Record a capacity decision before committing it.")
        # Non-blocking: a recorded warning, never a publication block (MASTER_SPEC 10.5,
        # decision D8). Journaled through its own short RunContext, since the monthly
        # run's own RunContext has already committed and closed by this point.
        try:
            from quant.data.gates import record_event
            with RunContext(as_of=as_of, kind="maintenance", track="live", cfg=cfg, clock=clock, actor=actor) as budget_ctx:
                record_event(
                    budget_ctx,
                    code="STATE_BUDGET_EXCEEDED",
                    severity="WARN",
                    detail={"bytes": actual_bytes, "budget": warn_bytes, "db_path": str(db_path)},
                )
        except Exception as exc:
            _print(f"Failed to record STATE_BUDGET_EXCEEDED event: {exc!r}")
        rel = [r for r in rel if Path(root / r).resolve() != db_path.resolve()]
        if not rel:
            return
    _git(["add", "--", *rel], root)
    code, out = _git(["commit", "-m", f"monthly run {as_of}: state, ledger, reports and UI payloads"], root)
    _print(f"git commit: {out.splitlines()[-1] if out else code}")
    if push and code == 0:
        code, out = _git(["push"], root)
        _print(f"git push (explicit --push): {out.splitlines()[-1] if out else code}")


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
    client: Any = None,
    allow_mid_month: bool = False,
) -> int:
    """Execute the monthly pipeline in the MASTER_SPEC 9.1 sequence.

    Returns 0 success/previously done, 1 implementation/source error,
    2 blocked publication, 3 governance refusal.

    ``as_of`` must be the last completed session of its calendar month (MASTER_SPEC 2.2):
    label endpoints are month-ends, so a mid-month live cohort shares endpoints with the
    month-end cohort and double-counts overlapping evidence in HAC/n_eff statistics.
    ``allow_mid_month`` exists only for sandbox simulations and tests against a disposable
    state database; the CLI never sets it.
    """
    if stop_after is not None and stop_after not in STOP_POINTS:
        _print(f"Unknown --stop-after {stop_after!r}; expected one of {STOP_POINTS}")
        return 1
    if actor.kind == "llm" and actor.name.startswith("human:"):
        _print("Governance refusal: an LLM actor cannot claim a human identity.")
        return 3

    # 1. Resolve the target, reject future/incomplete cutoffs, exit 0 if already published.
    pre_conn = connect(cfg.paths.db)
    try:
        dbcore.apply_schema(pre_conn, kind="state")
        from quant.data.prices import PriceStore
        pre_store = PriceStore(cfg.paths.prices_db, state_conn=pre_conn) if not dry_run else None
        cal = _load_calendar(cfg, pre_store)
        if as_of is None:
            as_of = default_as_of(clock, cal)
        cutoff = knowledge_cutoff(as_of)
        now_iso = clock.iso()
        if as_of > now_iso[:10] or now_iso <= cutoff:
            _print(f"Rejected: as_of={as_of} cutoff {cutoff} has not completed at {now_iso}")
            return 2
        expected = month_end_session(as_of, cal)
        if as_of != expected and not allow_mid_month:
            _print(f"Refused: as_of={as_of} is not the last session of its month (expected {expected}); "
                   "live cohorts are month-end only (MASTER_SPEC 2.2)")
            return 1
        published = pre_conn.execute(
            "SELECT cohort_id FROM cohorts WHERE as_of = ? AND track = 'live'", (as_of,)
        ).fetchone()
        if published:
            _print(f"Live cohort for {as_of} already published ({published[0]}); nothing to do.")
            return 0
    finally:
        pre_conn.close()

    if dry_run:
        _print(f"Dry run: would run monthly for as_of={as_of} (cutoff {cutoff}); no writes, network or git.")
        return 0

    # Lock against concurrent runs
    lock_path = Path(str(cfg.paths.db) + ".lock")
    lock_file = None
    try:
        import fcntl
        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            _print(f"Another monthly run holds {lock_path}.")
            lock_file.close()
            return 1
    except ImportError:
        lock_file = None

    blocked_reason: Optional[str] = None
    published = False
    try:
        with RunContext(as_of=as_of, kind="monthly", track="live", cfg=cfg, clock=clock, actor=actor) as ctx:
            from quant.data.prices import PriceStore
            ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
            ctx.calendar = _load_calendar(cfg, ctx.store)

            # 2. Authorized expiry/reversion effects, then captures for the next cutoff.
            from quant.knowledge import proposals
            proposals.apply(ctx, as_of=as_of)
            ctx.checkpoint()

            if not skip_capture:
                from quant.data import capture as data_capture, universe
                if client is None:
                    import time
                    from quant.data.yahoo import YahooClient
                    client = YahooClient(cfg=cfg, clock=clock, sleep=time.sleep)
                try:
                    universe.capture(ctx)
                    data_capture.run(ctx, client)
                except Exception as exc:  # a failed capture must not stop settlement/labels
                    ctx.rollback_staging()
                    from quant.data.gates import record_event
                    record_event(ctx, code="CAPTURE_FAILED", severity="WARN", detail={"error": str(exc)})
                    ctx.notes["capture_error"] = str(exc)
                    _print(f"Capture failed and was recorded: {exc}")
                ctx.checkpoint()
            # Unexplained total-return jumps become 'suspected' corporate actions; the price
            # store truncates history at them until a decision resolves them (MASTER_SPEC 4.3).
            from quant.data import actions as ca_actions
            from quant.data.identity import tracked_securities
            horizons = list(getattr(getattr(cfg, "horizons", None), "tracked_m", [1, 3, 6, 12, 24, 36]))
            ca_res = ca_actions.detect(ctx, tracked_securities(ctx.conn, cutoff=as_of, horizons=horizons),
                                       since=str(getattr(cfg.yahoo, "history_start", "2015-01-01")))
            ctx.notes["suspected_actions_new"] = ca_res.counts.get("suspected", 0)
            ctx.checkpoint()
            if stop_after == "capture":
                return 0

            # 3. Settle orders, mature labels, evaluate prior cohorts; preserve regardless of scoring.
            from quant.evaluation import curves, evaluate, labels
            from quant.portfolio import paper
            paper.settle(ctx, through=as_of)
            paper.roll_forward(ctx, through=as_of)
            labels.mature(ctx, through=as_of)
            for trk in ("live", "legacy"):
                evaluate.run(ctx, through=as_of, track=trk)
            curves.update(ctx, through=as_of)
            ctx.checkpoint()
            if stop_after == "mature":
                return 0

            # 4-5. Gates, staging, scoring, publication (atomic).
            try:
                draft = build_draft(ctx, as_of, cutoff)
                published = _stage_and_publish(ctx, draft, stop_after)
                if published:
                    ctx.checkpoint()
                elif stop_after in ("gates", "stage"):
                    ctx.rollback_staging()
                    ctx.checkpoint()
                    return 0
            except Blocked as exc:
                ctx.rollback_staging()
                ctx.checkpoint()
                blocked_reason = f"{exc.code}: {exc.detail}"
                ctx.status = "blocked"
                ctx.notes["blocked"] = blocked_reason
                _print(f"Publication blocked: {blocked_reason}")
            if stop_after == "publish":
                return 0 if published else 2

            # 6. Future paper orders, portfolio returns.
            if published:
                paper.plan(ctx, cohort_id=draft.cohort_id)
                paper.roll_forward(ctx, through=as_of)
                ctx.checkpoint()

            # 7. Criteria review; draft proposals only.
            proposals.draft(ctx, as_of=as_of)

            # 8. Report and UI from persisted state.
            from quant import ui_export
            from quant.knowledge import report
            try:
                report.render(ctx.conn, as_of=as_of, cfg=cfg)
            except Exception as exc:
                ctx.notes["report_error"] = str(exc)
                _print(f"Report rendering failed: {exc}")
            ui_export.export(ctx.conn, cfg)
            ctx.checkpoint()
    except Blocked as exc:
        _print(f"Blocked: {exc}")
        return 2
    except Refused as exc:
        _print(f"Refused: {exc}")
        return 3
    except Exception as exc:
        _print(f"Runner error: {exc!r}")
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

    # 8 (cont.). Export ledger, verify rebuild, checkpoint/VACUUM outside any transaction.
    from quant.db import ledger
    post_conn = connect(cfg.paths.db)
    try:
        ledger_dir = Path(cfg.paths.data_dir) / "ledger"
        ledger.export(post_conn, ledger_dir)
        verify_report = ledger.verify(post_conn, ledger_dir)
        if not verify_report.passed:
            failed = [c.id for c in verify_report.checks if c.status == "FAIL"]
            _print(f"Ledger verification FAILED for: {failed}")
            return 1
        post_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        post_conn.execute("VACUUM")
    finally:
        post_conn.close()

    _commit_and_push(cfg, as_of, commit=commit, push=push, clock=clock, actor=actor)
    return 2 if blocked_reason else 0
