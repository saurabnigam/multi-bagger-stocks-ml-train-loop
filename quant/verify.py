"""Verification module for reports, evidence references, and PIT audits (C11).

report() re-renders a persisted monthly report from its archived JSON manifest
(knowledge/reports/YYYY-MM/<report_id>.json, written by quant.knowledge.report)
and confirms the manifest's own content hash and the archived markdown both
reproduce exactly. A missing manifest or a renderer/content mismatch is always
a FAIL -- there is no code path that returns PASS without an archived,
reproducible snapshot.

pit() re-derives point-in-time integrity for the last N live cohorts directly
from persisted evidence: publication ordering, factor input cutoffs, whether
any fundamentals/holdings/security_attributes evidence for cohort members
could have leaked from after the cutoff, and label endpoint ordering.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config
from quant.types import Check, CheckReport

# evaluations.status vocabulary (quant.evaluation.stats.hac_mean_test / metrics.rank_ic);
# NOT the "estimable/unavailable/refused" wording used elsewhere in the codebase for
# unrelated review/paper-spread status fields.
_EVAL_STATUSES = ("ok", "insufficient", "constant")


def _knowledge_dir(cfg: Config) -> Path:
    return Path(getattr(getattr(cfg, "paths", None), "knowledge_dir", None) or "knowledge")


def _check_eval_vocabulary(report_id: str, evals: list[dict[str, Any]]) -> Check:
    """MASTER_SPEC 9.5: every inferential number needs n, n_eff, method and either a
    finite band or an explicit insufficient/not_applicable uncertainty status."""
    bad: list[str] = []
    for ev in evals:
        status = ev.get("status")
        key = f"{ev.get('subject_id')}/{ev.get('metric')}/{ev.get('horizon_m')}"
        if status not in _EVAL_STATUSES:
            bad.append(f"{key}:unknown_status({status!r})")
            continue
        if status == "ok" and (ev.get("ci90_lo") is None or ev.get("ci90_hi") is None):
            bad.append(f"{key}:ok_missing_band")
        if not ev.get("evidence_hash"):
            bad.append(f"{key}:missing_evidence_hash")
    ok = not bad
    return Check(
        id=f"verify:report:{report_id}:evaluation_status_vocabulary",
        status="PASS" if ok else "FAIL",
        observed="valid" if ok else bad,
        expected=f"status in {_EVAL_STATUSES}; finite band when status == 'ok'",
        reason="All pinned evaluations use the real status vocabulary with bands where required"
        if ok
        else f"{len(bad)} pinned evaluations fail the status/band invariant: {bad[:5]}",
        blocking=True,
    )


def report(conn: sqlite3.Connection, as_of: str, cfg: Config) -> CheckReport:
    """Verify every archived report snapshot for as_of's month reproduces its own evidence.

    For each knowledge/reports/<YYYY-MM>/<report_id>.json found:
      1. The manifest's own content (excluding report_id/output_hashes) hashes back to
         both its filename and its embedded report_id (catches a tampered/corrupted manifest).
      2. The pinned evaluations obey the real status vocabulary and band invariant.
      3. Re-rendering the archived manifest with the current renderer reproduces the
         archived markdown byte-for-byte (catches renderer drift / missing inputs).

    Missing manifests, or any of the above failing, is FAIL. There is no PASS without a
    persisted, reproducible snapshot.
    """
    checks: list[Check] = []
    reports_dir = _knowledge_dir(cfg) / "reports" / as_of[:7]

    manifest_paths = sorted(reports_dir.glob("*.json")) if reports_dir.exists() else []
    if not manifest_paths:
        checks.append(
            Check(
                id="verify:report:manifest_present",
                status="FAIL",
                observed=0,
                expected=">=1",
                reason=f"No archived report manifest found for {as_of} under {reports_dir}",
                blocking=True,
            )
        )
        return CheckReport(checks=checks)

    from quant.knowledge.report import _render_content  # local import: knowledge is a higher layer

    for mpath in manifest_paths:
        report_id = mpath.stem
        try:
            manifest_full = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            checks.append(
                Check(
                    id=f"verify:report:{report_id}:manifest_readable",
                    status="FAIL",
                    observed=str(exc),
                    expected="valid JSON manifest",
                    reason=f"Could not read/parse manifest {mpath}: {exc}",
                    blocking=True,
                )
            )
            continue

        stored_report_id = manifest_full.get("report_id")
        manifest_clean = {k: v for k, v in manifest_full.items() if k not in ("report_id", "output_hashes")}
        canonical_json = json.dumps(manifest_clean, sort_keys=True, separators=(",", ":"))
        recomputed_id = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        id_ok = stored_report_id == report_id == recomputed_id
        checks.append(
            Check(
                id=f"verify:report:{report_id}:report_id_matches_evidence",
                status="PASS" if id_ok else "FAIL",
                observed={"filename": report_id, "manifest_report_id": stored_report_id, "recomputed": recomputed_id},
                expected="filename == manifest.report_id == sha256(canonical manifest body)",
                reason="report_id reproduces exactly from the pinned manifest body"
                if id_ok
                else "Manifest content hash does not match its report_id (tampered or corrupted evidence)",
                blocking=True,
            )
        )

        evals = manifest_full.get("selected_evaluation_keys", [])
        checks.append(_check_eval_vocabulary(report_id, evals))

        md_path = mpath.with_suffix(".md")
        if not md_path.exists():
            checks.append(
                Check(
                    id=f"verify:report:{report_id}:renderer_reproduces",
                    status="FAIL",
                    observed="missing_markdown",
                    expected=str(md_path),
                    reason=f"Archived report markdown missing at {md_path}",
                    blocking=True,
                )
            )
            continue

        try:
            recomputed_content = _render_content(manifest_full, stored_report_id or report_id)
        except Exception as exc:  # renderer code/inputs no longer compatible with archived manifest
            checks.append(
                Check(
                    id=f"verify:report:{report_id}:renderer_reproduces",
                    status="FAIL",
                    observed=str(exc),
                    expected="renderer succeeds on archived manifest",
                    reason=f"Renderer raised while reproducing {report_id}: {exc}",
                    blocking=True,
                )
            )
            continue

        archived_content = md_path.read_text(encoding="utf-8")
        content_ok = recomputed_content == archived_content
        checks.append(
            Check(
                id=f"verify:report:{report_id}:renderer_reproduces",
                status="PASS" if content_ok else "FAIL",
                observed="matches" if content_ok else "diverges",
                expected="renderer output byte-identical to archived markdown",
                reason="Renderer reproduces the archived report byte-for-byte"
                if content_ok
                else f"Current renderer output diverges from archived {md_path} (renderer or evidence mismatch)",
                blocking=True,
            )
        )

    return CheckReport(checks=checks)


def _end_month(as_of: str, horizon_m: int) -> str:
    y, m = int(as_of[:4]), int(as_of[5:7])
    total_m = m + horizon_m
    end_y = y + (total_m - 1) // 12
    end_m = (total_m - 1) % 12 + 1
    return f"{end_y:04d}-{end_m:02d}"


def pit(conn: sqlite3.Connection, months: int, cfg: Config) -> CheckReport:
    """Verify point-in-time integrity for the last `months` live cohorts.

    For each cohort: published_at >= knowledge_cutoff; every factor_values row's
    input_refs_json cutoff matches the cohort's knowledge_cutoff; no fundamentals fact
    (security_id, statement, freq, period_end, field) for a cohort member claims
    available_from <= cutoff while having no fetch at or before cutoff at all -- a fact
    with an honest earlier fetch may also carry a later, ordinary revision fetch without
    that being a leak, since production selection (quant/factors/inputs.py,
    quant/data/gates.py) never uses fetched_at > cutoff rows for this cohort; no
    holdings/security_attributes row for a cohort member exists only with captured_at
    after cutoff (i.e. the only evidence for that member would postdate the cutoff); and
    every label's end_date is <= its computed_at (you cannot compute a return before its
    window closes).
    """
    checks: list[Check] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    cohort_rows = cur.execute(
        "SELECT cohort_id, as_of, knowledge_cutoff, published_at FROM cohorts "
        "WHERE track = 'live' ORDER BY as_of DESC LIMIT ?",
        (int(months),),
    ).fetchall()

    if not cohort_rows:
        checks.append(
            Check(
                id="verify:pit:cohorts",
                status="PASS",
                observed=0,
                expected=0,
                reason="No live cohorts available to verify",
                blocking=False,
            )
        )
        return CheckReport(checks=checks)

    for c in cohort_rows:
        cohort_id, as_of, cutoff, published_at = c["cohort_id"], c["as_of"], c["knowledge_cutoff"], c["published_at"]

        pub_ok = bool(published_at) and bool(cutoff) and published_at >= cutoff
        checks.append(
            Check(
                id=f"verify:pit:{cohort_id}:published_after_cutoff",
                status="PASS" if pub_ok else "FAIL",
                observed=published_at,
                expected=f">= {cutoff}",
                reason="published_at >= knowledge_cutoff"
                if pub_ok
                else f"published_at {published_at!r} precedes knowledge_cutoff {cutoff!r}",
                blocking=True,
            )
        )

        fv_rows = cur.execute(
            "SELECT DISTINCT security_id, factor_id, input_refs_json FROM factor_values WHERE cohort_id = ?",
            (cohort_id,),
        ).fetchall()
        bad_fv: list[str] = []
        members: set[int] = set()
        for r in fv_rows:
            members.add(int(r["security_id"]))
            try:
                refs = json.loads(r["input_refs_json"] or "{}")
            except (TypeError, ValueError):
                bad_fv.append(f"{r['security_id']}/{r['factor_id']}:unparseable_input_refs")
                continue
            if refs.get("cutoff") != cutoff:
                bad_fv.append(f"{r['security_id']}/{r['factor_id']}:cutoff={refs.get('cutoff')!r}")
        checks.append(
            Check(
                id=f"verify:pit:{cohort_id}:factor_input_cutoff",
                status="PASS" if not bad_fv else "FAIL",
                observed=len(bad_fv) if bad_fv else "matched",
                expected=f"every factor_values.input_refs_json.cutoff == {cutoff!r}",
                reason="All factor_values input_refs match the cohort cutoff"
                if not bad_fv
                else f"{len(bad_fv)} factor_values rows disagree with the cohort cutoff: {bad_fv[:5]}",
                blocking=True,
            )
        )

        if not members:
            m_rows = cur.execute(
                "SELECT DISTINCT security_id FROM universe_membership WHERE as_of = ?", (as_of,)
            ).fetchall()
            members = {int(r["security_id"]) for r in m_rows}

        if members:
            placeholders = ",".join("?" for _ in members)
            member_list = list(members)
            # A fact is identified by (security_id, statement, freq, period_end, field) --
            # the schema's own primary key minus fetched_at (docs/spec/contracts/schema.sql).
            # MASTER_SPEC line 163: "a changed value inserts a version with its new
            # timestamp" -- once a fact has ANY row fetched at or before the cutoff, a later,
            # normal revision fetch for the SAME fact (same available_from, later fetched_at)
            # is expected and correct, and quant/factors/inputs.py:200 / quant/data/gates.py:153
            # never select it for this cohort (both filter available_from<=cutoff AND
            # fetched_at<=cutoff). So a row only leaks if its fact has NO pre-cutoff-fetched
            # row at all: the fact was genuinely unknown at cutoff, yet claims (via
            # available_from) to have been available before it -- a true backdating leak,
            # not routine accumulation of later corrections.
            leak_rows = cur.execute(
                f"""
                SELECT f.security_id, f.field, f.period_end, f.fetched_at
                FROM fundamentals f
                WHERE f.security_id IN ({placeholders})
                  AND f.available_from <= ?
                  AND f.fetched_at > ?
                  AND NOT EXISTS (
                    SELECT 1 FROM fundamentals f2
                    WHERE f2.security_id = f.security_id
                      AND f2.statement = f.statement
                      AND f2.freq = f.freq
                      AND f2.period_end = f.period_end
                      AND f2.field = f.field
                      AND f2.fetched_at <= ?
                  )
                """,
                (*member_list, cutoff, cutoff, cutoff),
            ).fetchall()
        else:
            leak_rows = []
        checks.append(
            Check(
                id=f"verify:pit:{cohort_id}:fundamentals_no_backdated_fetch",
                status="PASS" if not leak_rows else "FAIL",
                observed=len(leak_rows),
                expected=0,
                reason="No fundamentals fact is claimed pre-cutoff-available with no fetch at or before cutoff"
                if not leak_rows
                else (
                    f"{len(leak_rows)} fundamentals facts for cohort members claim available_from<=cutoff "
                    f"but have no fetch at or before {cutoff} (e.g. security {leak_rows[0]['security_id']} "
                    f"field {leak_rows[0]['field']} period_end {leak_rows[0]['period_end']} "
                    f"fetched_at {leak_rows[0]['fetched_at']})"
                ),
                blocking=True,
            )
        )

        for table in ("holdings", "security_attributes"):
            if members:
                only_future = cur.execute(
                    f"SELECT DISTINCT security_id FROM {table} WHERE security_id IN ({placeholders}) "
                    f"AND security_id NOT IN ("
                    f"  SELECT security_id FROM {table} WHERE security_id IN ({placeholders}) AND captured_at <= ?"
                    f")",
                    (*member_list, *member_list, cutoff),
                ).fetchall()
            else:
                only_future = []
            checks.append(
                Check(
                    id=f"verify:pit:{cohort_id}:{table}_no_future_only_evidence",
                    status="PASS" if not only_future else "FAIL",
                    observed=len(only_future),
                    expected=0,
                    reason=f"Every cohort member with {table} evidence has a row captured at or before cutoff"
                    if not only_future
                    else (
                        f"{len(only_future)} cohort members have {table} evidence only captured after "
                        f"cutoff {cutoff}: {[r['security_id'] for r in only_future][:5]}"
                    ),
                    blocking=True,
                )
            )

        lbl_rows = cur.execute(
            "SELECT security_id, horizon_m, end_date, computed_at FROM labels WHERE cohort_id = ?",
            (cohort_id,),
        ).fetchall()
        bad_lbl = [r for r in lbl_rows if r["end_date"] and r["computed_at"] and r["end_date"] > r["computed_at"]]
        checks.append(
            Check(
                id=f"verify:pit:{cohort_id}:labels_endpoint_before_computed",
                status="PASS" if not bad_lbl else "FAIL",
                observed=len(bad_lbl),
                expected=0,
                reason="All labels' end_date <= computed_at"
                if not bad_lbl
                else f"{len(bad_lbl)} labels rows were computed before their own end_date",
                blocking=True,
            )
        )

    return CheckReport(checks=checks)
