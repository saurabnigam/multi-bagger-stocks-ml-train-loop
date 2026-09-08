"""Automated monthly report authoring, content-addressed storage and reproducibility (C09)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config


def _get_knowledge_dir(cfg: Config) -> Path:
    if hasattr(cfg, "paths") and hasattr(cfg.paths, "knowledge_dir"):
        return Path(cfg.paths.knowledge_dir)
    return Path("knowledge")


def _build_snapshot_from_db(
    conn: sqlite3.Connection, as_of: str, track: str
) -> dict[str, Any]:
    """Extract pinned evidence snapshot from SQLite database."""
    # 1. Cohort
    c_row = conn.execute(
        """
        SELECT cohort_id, as_of, track, knowledge_cutoff, definition_hash,
               membership_hash, source_refs_json, published_at, generated_at, run_id
        FROM cohorts
        WHERE as_of = ? AND track = ?
        ORDER BY rowid DESC LIMIT 1
        """,
        (as_of, track),
    ).fetchone()
    cohort = dict(c_row) if c_row else {}

    run_id = cohort.get("run_id")
    run = {}
    if run_id:
        r_row = conn.execute(
            """
            SELECT run_id, as_of, kind, track, attempt, started_at, status,
                   git_sha, code_sha256, config_sha256, registry_sha256
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if r_row:
            run = dict(r_row)

    # 2. Latest evaluations for this as_of and track
    eval_rows = conn.execute(
        """
        SELECT eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
               as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
               status, method, window_start, window_end, evidence_hash, revision, supersedes_eval_id
        FROM evaluations
        WHERE as_of = ? AND track = ?
        ORDER BY subject_id ASC, metric ASC, horizon_m ASC, revision DESC
        """,
        (as_of, track),
    ).fetchall()

    # Deduplicate by (subject_kind, subject_id, metric, horizon_m) selecting highest revision
    seen_keys = set()
    selected_evals = []
    for r in eval_rows:
        d = dict(r)
        key = (d["subject_kind"], d["subject_id"], d["metric"], d["horizon_m"])
        if key not in seen_keys:
            seen_keys.add(key)
            selected_evals.append(d)

    # 3. Decisions relevant up to as_of
    d_rows = conn.execute(
        """
        SELECT decision_id, proposal_id, kind, tier, subject_id, title, context,
               decided_on, decided_by, approver_kind, ratified_by, ratified_on,
               status, effective_from, applied_on, adr_path, git_sha
        FROM decisions
        WHERE status IN ('approved', 'provisional', 'applied', 'reverted')
        ORDER BY decided_on ASC
        """
    ).fetchall()
    decisions = [dict(r) for r in d_rows]

    # 4. Pending orders for this cohort
    cohort_id = cohort.get("cohort_id")
    ord_rows = []
    if cohort_id:
        ord_rows = conn.execute(
            """
            SELECT order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at,
                   purpose, side, target_weight, status, liquidity_bucket, decision_id
            FROM portfolio_orders
            WHERE cohort_id = ?
            ORDER BY order_id ASC
            """,
            (cohort_id,),
        ).fetchall()
    orders = [dict(r) for r in ord_rows]

    # Calculate known_at
    timestamps = []
    if cohort.get("published_at"):
        timestamps.append(cohort["published_at"])
    if cohort.get("generated_at"):
        timestamps.append(cohort["generated_at"])
    for ev in selected_evals:
        if ev.get("computed_at"):
            timestamps.append(ev["computed_at"])
    for o in orders:
        if o.get("created_at"):
            timestamps.append(o["created_at"])
    for d in decisions:
        if d.get("decided_on"):
            timestamps.append(d["decided_on"])
        if d.get("ratified_on"):
            timestamps.append(d["ratified_on"])

    known_at = max(timestamps) if timestamps else f"{as_of}T18:30:00.000000Z"

    manifest_body = {
        "as_of": as_of,
        "track": track,
        "known_at": known_at,
        "cohort": cohort,
        "run": run,
        "selected_evaluation_keys": selected_evals,
        "control_summaries": {
            "decisions": decisions,
            "orders": orders,
        },
    }
    return manifest_body


def _render_content(manifest: dict[str, Any], report_id: str) -> str:
    """Generate deterministic markdown content from pinned manifest data."""
    as_of = manifest["as_of"]
    track = manifest["track"]
    known_at = manifest.get("known_at", "N/A")
    cohort = manifest.get("cohort", {})
    run = manifest.get("run", {})
    evals = manifest.get("selected_evaluation_keys", [])
    control_summaries = manifest.get("control_summaries", {})
    decisions = control_summaries.get("decisions", [])
    orders = control_summaries.get("orders", [])

    lines = [
        f"# Monthly Quant Engine Report: {as_of} ({track.upper()})",
        "",
        f"- **Report ID:** `{report_id}`",
        f"- **As Of:** {as_of}",
        f"- **Track:** {track}",
        f"- **Known At:** {known_at}",
        f"- **Knowledge Cutoff:** {cohort.get('knowledge_cutoff', 'N/A')}",
        f"- **Cohort Published At:** {cohort.get('published_at', 'N/A')}",
        f"- **Cohort Generated At:** {cohort.get('generated_at', 'N/A')}",
        f"- **Run ID:** {run.get('run_id', 'N/A')}",
        f"- **Git SHA:** `{run.get('git_sha', 'N/A')}`",
        "",
        "## 1. Executive Summary & Required Actions",
        "",
        f"Automated empirical report generated from immutable evidence for cycle {as_of}.",
        "All claims are strictly bounded by recorded out-of-sample data.",
        "",
        "## 2. Gates & Data Provenance",
        "",
        f"- **Definition Hash:** `{cohort.get('definition_hash', 'N/A')}`",
        f"- **Membership Hash:** `{cohort.get('membership_hash', 'N/A')}`",
        f"- **Code SHA256:** `{run.get('code_sha256', 'N/A')}`",
        f"- **Config SHA256:** `{run.get('config_sha256', 'N/A')}`",
        f"- **Registry SHA256:** `{run.get('registry_sha256', 'N/A')}`",
        "",
        "## 3. Evaluated Metrics & Empirical Evidence",
        "",
        "| Subject | Metric | Horizon (m) | Method | Value | n | n_eff | 90% Confidence Band | Uncertainty Status |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for ev in evals:
        sub = f"{ev.get('subject_id', 'N/A')} (v{ev.get('subject_version', 1)})"
        metric = ev.get("metric", "N/A")
        hm = ev.get("horizon_m", "N/A")
        meth = ev.get("method", "N/A")
        val = f"{ev.get('value'):.4f}" if ev.get("value") is not None else "NULL"
        n = ev.get("n", "N/A")
        n_eff = f"{ev.get('n_eff'):.1f}" if ev.get("n_eff") is not None else "N/A"
        ustat = ev.get("status", "unknown")

        # Invariant: Finite band required ONLY when status says estimable
        if ustat == "estimable" and ev.get("ci90_lo") is not None and ev.get("ci90_hi") is not None:
            band = f"[{ev.get('ci90_lo'):.4f}, {ev.get('ci90_hi'):.4f}]"
        else:
            band = f"N/A ({ustat})"

        lines.append(f"| {sub} | {metric} | {hm} | {meth} | {val} | {n} | {n_eff} | {band} | {ustat} |")

    lines.extend([
        "",
        "## 4. Portfolios & Pending Orders",
        "",
    ])

    if orders:
        lines.append("| Order ID | Portfolio | Security ID | Side | Target Weight | Purpose | Status | Earliest Exec |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for o in orders:
            lines.append(
                f"| {o.get('order_id')} | {o.get('portfolio_id')} | {o.get('security_id')} | "
                f"{o.get('side')} | {o.get('target_weight')} | {o.get('purpose')} | "
                f"{o.get('status')} | {o.get('earliest_exec_at')} |"
            )
    else:
        lines.append("No pending orders for this cycle.")

    lines.extend([
        "",
        "## 5. Governance, Ratifications & Decisions",
        "",
    ])

    if decisions:
        lines.append("| Decision ID | Kind | Tier | Subject ID | Status | Approver | Ratified By | Decided On |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for d in decisions:
            lines.append(
                f"| {d.get('decision_id')} | {d.get('kind')} | Tier {d.get('tier')} | "
                f"{d.get('subject_id')} | {d.get('status')} | {d.get('decided_by')} | "
                f"{d.get('ratified_by') or 'N/A'} | {d.get('decided_on')} |"
            )
    else:
        lines.append("No decisions logged.")

    lines.extend([
        "",
        "---",
        "Small or dependent samples may not distinguish skill from noise.",
        "",
    ])

    return "\n".join(lines)


def _render_track(
    conn: sqlite3.Connection,
    as_of: str,
    cfg: Config,
    track: str,
    *,
    snapshot: dict[str, Any] | None = None,
) -> Path:
    """Core renderer writing content-addressed report and companion JSON manifest."""
    knowledge_dir = _get_knowledge_dir(cfg)
    reports_dir = knowledge_dir / "reports" / as_of[:7]
    reports_dir.mkdir(parents=True, exist_ok=True)

    if snapshot is not None:
        # Use provided snapshot
        manifest_body = snapshot.get("manifest_body", snapshot)
        manifest_clean = {k: v for k, v in manifest_body.items() if k not in ("report_id", "output_hashes")}
        canonical_json = json.dumps(manifest_clean, sort_keys=True, separators=(",", ":"))
        report_id = snapshot.get("report_id") or hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    else:
        manifest_clean = _build_snapshot_from_db(conn, as_of, track)
        canonical_json = json.dumps(manifest_clean, sort_keys=True, separators=(",", ":"))
        report_id = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    manifest_full = dict(manifest_clean)
    manifest_full["report_id"] = report_id

    report_md_path = reports_dir / f"{report_id}.md"
    manifest_json_path = reports_dir / f"{report_id}.json"

    # Deterministic markdown rendering
    content = _render_content(manifest_full, report_id)

    report_md_path.write_text(content, encoding="utf-8")
    manifest_json_path.write_text(
        json.dumps(manifest_full, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Navigation index YYYY-MM.md
    nav_index_path = knowledge_dir / "reports" / f"{as_of[:7]}.md"
    nav_content = (
        f"# Reports Index for {as_of[:7]}\n\n"
        f"- [{track.upper()} Report ({report_id})]({as_of[:7]}/{report_id}.md)\n"
    )
    nav_index_path.write_text(nav_content, encoding="utf-8")

    return report_md_path


def render(
    conn: sqlite3.Connection,
    as_of: str,
    cfg: Config,
    *,
    snapshot: dict[str, Any] | None = None,
) -> Path:
    """Render live-track monthly evidence report."""
    return _render_track(conn, as_of, cfg, track="live", snapshot=snapshot)


def render_backfill(
    conn: sqlite3.Connection,
    as_of: str,
    cfg: Config,
    *,
    snapshot: dict[str, Any] | None = None,
) -> Path:
    """Render backfill-track monthly evidence report."""
    return _render_track(conn, as_of, cfg, track="backfill", snapshot=snapshot)
