"""Automated monthly report authoring, content-addressed storage and reproducibility (C09).

MASTER_SPEC 9.4: a report is rendered only from a pinned manifest of persisted
evidence (cohort, run, selected evaluation/label/portfolio revision keys, gate
results, proposals, decisions, orders, evidence curves) plus the renderer code
hash. ``report_id`` is the SHA256 of the canonical manifest body, so re-rendering
the same snapshot is a no-op and changed evidence or a changed renderer yields
a different report_id. Rendering never computes statistics.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from quant.config import Config

_INFERENTIAL_STATUSES = ("ok", "insufficient", "constant")


def renderer_sha256() -> str:
    """Hash of this renderer's source; part of the manifest so renderer drift changes report_id."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _get_knowledge_dir(cfg: Config) -> Path:
    if hasattr(cfg, "paths") and hasattr(cfg.paths, "knowledge_dir"):
        return Path(cfg.paths.knowledge_dir)
    return Path("knowledge")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _build_snapshot_from_db(
    conn: sqlite3.Connection, as_of: str, track: str
) -> dict[str, Any]:
    """Extract the pinned evidence snapshot for one as_of/track from the state database."""
    # 1. Cohort and its run
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
    cohort_id = cohort.get("cohort_id")
    run_id = cohort.get("run_id")
    run: dict[str, Any] = {}
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

    # 2. Latest revision of every per-date evaluation for this as_of/track
    eval_rows = _rows(
        conn,
        """
        SELECT eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
               as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
               status, method, window_start, window_end, evidence_hash, revision, supersedes_eval_id
        FROM evaluations
        WHERE as_of = ? AND track = ?
        ORDER BY subject_kind ASC, subject_id ASC, subject_version ASC, metric ASC, horizon_m ASC,
                 scope ASC, method ASC, revision DESC
        """,
        (as_of, track),
    )
    seen_keys: set[tuple] = set()
    selected_evals = []
    for d in eval_rows:
        key = (d["subject_kind"], d["subject_id"], d["subject_version"], d["metric"], d["horizon_m"], d["scope"], d["method"])
        if key not in seen_keys:
            seen_keys.add(key)
            selected_evals.append(d)

    # 3. Latest evidence curve per subject key on this track
    curve_rows = _rows(
        conn,
        """
        SELECT computed_at, subject_kind, subject_id, subject_version, horizon_m, track, evidence_hash,
               months_clean, n_labelled, n_eff, ic_cum_mean, ic_hac_se, ic_hac_t, ci90_lo, ci90_hi, status
        FROM evidence_curve WHERE track = ?
        ORDER BY subject_kind, subject_id, subject_version, horizon_m, computed_at DESC
        """,
        (track,),
    )
    seen_c: set[tuple] = set()
    selected_curves = []
    for d in curve_rows:
        key = (d["subject_kind"], d["subject_id"], d["subject_version"], d["horizon_m"])
        if key not in seen_c:
            seen_c.add(key)
            selected_curves.append(d)

    # 4. Gates of the publishing run, decisions, proposals for this month, pending orders
    gates = _rows(
        conn,
        "SELECT gate, phase, status, observed_json, expected_json, reason, blocking FROM dq_runs "
        "WHERE run_id = ? ORDER BY phase DESC, gate ASC",
        (run_id,),
    ) if run_id else []
    decisions = _rows(
        conn,
        """
        SELECT decision_id, proposal_id, kind, tier, subject_id, title, context,
               decided_on, decided_by, approver_kind, ratified_by, ratified_on,
               status, effective_from, applied_on, adr_path, git_sha
        FROM decisions
        WHERE status IN ('approved', 'provisional', 'applied', 'reverted')
        ORDER BY decided_on ASC, decision_id ASC
        """,
    )
    proposals = _rows(
        conn,
        "SELECT proposal_id, kind, subject_id, status, rule_id, proposed_by, decision_id FROM proposals "
        "WHERE as_of = ? ORDER BY proposal_id ASC",
        (as_of,),
    )
    orders = _rows(
        conn,
        """
        SELECT order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at,
               purpose, side, target_weight, status, liquidity_bucket, decision_id
        FROM portfolio_orders WHERE cohort_id = ? ORDER BY order_id ASC
        """,
        (cohort_id,),
    ) if cohort_id else []

    # 5. Label revision keys for this cohort and portfolio returns at this month end
    label_keys = _rows(
        conn,
        """
        SELECT horizon_m, max(revision) AS revision, count(*) AS rows, sum(status = 'ok') AS n_ok,
               max(computed_at) AS computed_at
        FROM labels WHERE cohort_id = ? GROUP BY horizon_m ORDER BY horizon_m
        """,
        (cohort_id,),
    ) if cohort_id else []
    portfolio_returns = _rows(
        conn,
        """
        SELECT portfolio_id, month_end, revision, ret_gross, ret_net, ret_net_stress, cost, turnover_one_way,
               n_positions, evidence_hash, computed_at
        FROM portfolio_returns WHERE month_end = ? ORDER BY portfolio_id, revision
        """,
        (as_of,),
    )

    # known_at: latest timestamp of any pinned evidence
    timestamps: list[str] = []
    for key in ("published_at", "generated_at"):
        if cohort.get(key):
            timestamps.append(cohort[key])
    timestamps += [ev["computed_at"] for ev in selected_evals if ev.get("computed_at")]
    timestamps += [c["computed_at"] for c in selected_curves if c.get("computed_at")]
    timestamps += [o["created_at"] for o in orders if o.get("created_at")]
    timestamps += [lk["computed_at"] for lk in label_keys if lk.get("computed_at")]
    timestamps += [pr["computed_at"] for pr in portfolio_returns if pr.get("computed_at")]
    for d in decisions:
        for key in ("decided_on", "ratified_on"):
            if d.get(key):
                timestamps.append(d[key])
    known_at = max(timestamps) if timestamps else f"{as_of}T18:30:00.000000Z"

    return {
        "as_of": as_of,
        "track": track,
        "known_at": known_at,
        "cohort": cohort,
        "run": run,
        "renderer_sha256": renderer_sha256(),
        "selected_evaluation_keys": selected_evals,
        "selected_curve_keys": selected_curves,
        "label_revision_keys": label_keys,
        "portfolio_return_keys": portfolio_returns,
        "gates": gates,
        "control_summaries": {
            "decisions": decisions,
            "proposals": proposals,
            "orders": orders,
        },
    }


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return "NULL"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _band(status: Any, lo: Any, hi: Any) -> str:
    """Finite band only when both endpoints are pinned for an estimable statistic; never invented."""
    if status in ("ok", "estimable") and lo is not None and hi is not None:
        return f"[{_fmt(lo)}, {_fmt(hi)}]"
    return f"unavailable ({status})"


def _per_date_band(ev: dict[str, Any]) -> str:
    """Per-date cross-sections carry no time-series band unless one was explicitly pinned."""
    if ev.get("ci90_lo") is not None and ev.get("ci90_hi") is not None:
        return f"[{_fmt(ev.get('ci90_lo'))}, {_fmt(ev.get('ci90_hi'))}]"
    return "not_applicable"


def _render_content(manifest: dict[str, Any], report_id: str) -> str:
    """Deterministic markdown from the pinned manifest. Renders; never calculates statistics."""
    as_of = manifest["as_of"]
    track = manifest["track"]
    known_at = manifest.get("known_at", "N/A")
    cohort = manifest.get("cohort", {}) or {}
    run = manifest.get("run", {}) or {}
    evals = manifest.get("selected_evaluation_keys", []) or []
    curves = manifest.get("selected_curve_keys", []) or []
    label_keys = manifest.get("label_revision_keys", []) or []
    port_returns = manifest.get("portfolio_return_keys", []) or []
    gates = manifest.get("gates", []) or []
    control = manifest.get("control_summaries", {}) or {}
    decisions = control.get("decisions", []) or []
    proposals = control.get("proposals", []) or []
    orders = control.get("orders", []) or []

    blocking_fail = [g for g in gates if g.get("status") == "FAIL" and g.get("blocking")]
    if not cohort:
        verdict = "No cohort was published for this cycle."
    elif blocking_fail:
        verdict = f"Cohort published with {len(blocking_fail)} blocking gate failures recorded (inspect section 2)."
    else:
        verdict = "Cohort published; all applicable blocking gates passed."

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
        f"- **Renderer SHA256:** `{manifest.get('renderer_sha256', 'N/A')}`",
        "",
        "## 1. Verdict and required actions",
        "",
        verdict,
        "",
        "Every number below is read from pinned, persisted evidence; nothing is computed at render time.",
        "",
        "## 2. Gates and data provenance",
        "",
        f"- **Definition Hash:** `{cohort.get('definition_hash', 'N/A')}`",
        f"- **Membership Hash:** `{cohort.get('membership_hash', 'N/A')}`",
        f"- **Code SHA256:** `{run.get('code_sha256', 'N/A')}`",
        f"- **Config SHA256:** `{run.get('config_sha256', 'N/A')}`",
        f"- **Registry SHA256:** `{run.get('registry_sha256', 'N/A')}`",
        "",
    ]
    if gates:
        lines.append("| Phase | Gate | Status | Blocking | Reason |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for g in gates:
            lines.append(f"| {g.get('phase')} | {g.get('gate')} | {g.get('status')} | {int(bool(g.get('blocking')))} | {g.get('reason')} |")
    else:
        lines.append("No gate results recorded for this cycle.")

    lines += [
        "",
        "## 3. Matured labels",
        "",
    ]
    if label_keys:
        lines.append("| Horizon (m) | Latest revision | Rows | ok | Computed at |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for lk in label_keys:
            lines.append(f"| {lk.get('horizon_m')} | {lk.get('revision')} | {lk.get('rows')} | {lk.get('n_ok')} | {lk.get('computed_at')} |")
    else:
        lines.append("No matured labels for this cohort yet.")

    lines += [
        "",
        "## 4. Per-date evaluations (monthly cross-sections; a band appears only when pinned)",
        "",
        "| Subject | Metric | Horizon (m) | Scope | Method | Value | n | Status | Band |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for ev in evals:
        sub = f"{ev.get('subject_id', 'N/A')} (v{ev.get('subject_version', 1)})"
        status = ev.get("status", "unknown")
        lines.append(
            f"| {sub} | {ev.get('metric', 'N/A')} | {ev.get('horizon_m', 'N/A')} | {ev.get('scope', 'N/A')} | "
            f"{ev.get('method', 'N/A')} | {_fmt(ev.get('value'))} | {ev.get('n', 'N/A')} | {status} | {_per_date_band(ev)} |"
        )
    if not evals:
        lines.append("| (none) | | | | | | | | |")

    lines += [
        "",
        "## 5. Evidence curves (time-series statistics with HAC uncertainty)",
        "",
        "| Subject | Horizon (m) | Months clean | n labelled | n_eff | Cumulative mean IC | HAC se | HAC t | 90% band | Status |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for c in curves:
        sub = f"{c.get('subject_id', 'N/A')} (v{c.get('subject_version', 1)})"
        lines.append(
            f"| {sub} | {c.get('horizon_m')} | {c.get('months_clean')} | {c.get('n_labelled')} | {_fmt(c.get('n_eff'), 2)} | "
            f"{_fmt(c.get('ic_cum_mean'))} | {_fmt(c.get('ic_hac_se'))} | {_fmt(c.get('ic_hac_t'), 2)} | "
            f"{_band(c.get('status'), c.get('ci90_lo'), c.get('ci90_hi'))} | {c.get('status')} |"
        )
    if not curves:
        lines.append("| (none) | | | | | | | | | |")

    lines += [
        "",
        "## 6. Portfolios, returns and pending orders",
        "",
    ]
    if port_returns:
        lines.append("| Portfolio | Revision | Gross | Net | Net (stress) | Cost | Turnover | Positions |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for pr in port_returns:
            lines.append(
                f"| {pr.get('portfolio_id')} | {pr.get('revision')} | {_fmt(pr.get('ret_gross'))} | {_fmt(pr.get('ret_net'))} | "
                f"{_fmt(pr.get('ret_net_stress'))} | {_fmt(pr.get('cost'))} | {_fmt(pr.get('turnover_one_way'))} | {pr.get('n_positions')} |"
            )
    else:
        lines.append("No portfolio returns recorded at this month end.")
    lines.append("")
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

    lines += [
        "",
        "## 7. Criteria, proposals and decisions",
        "",
    ]
    if proposals:
        lines.append("| Proposal | Kind | Subject | Status | Rule | Proposed by |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for p in proposals:
            lines.append(f"| {p.get('proposal_id')} | {p.get('kind')} | {p.get('subject_id')} | {p.get('status')} | {p.get('rule_id')} | {p.get('proposed_by')} |")
    else:
        lines.append("No proposals drafted this cycle.")
    lines.append("")
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

    lines += [
        "",
        "## 8. Reproduction",
        "",
        f"`python -m quant verify report --as-of {as_of}` re-renders this report from its manifest "
        f"`knowledge/reports/{as_of[:7]}/{report_id}.json` and compares it byte for byte.",
        "",
        "---",
        "Small or dependent samples may not distinguish skill from noise.",
        "",
    ]
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

    content = _render_content(manifest_full, report_id)
    if not report_md_path.exists() or report_md_path.read_text(encoding="utf-8") != content:
        report_md_path.write_text(content, encoding="utf-8")
    manifest_text = json.dumps(manifest_full, indent=2, sort_keys=True)
    if not manifest_json_path.exists() or manifest_json_path.read_text(encoding="utf-8") != manifest_text:
        manifest_json_path.write_text(manifest_text, encoding="utf-8")

    # Navigation index YYYY-MM.md (replaceable index only; lists every report of the month)
    nav_index_path = knowledge_dir / "reports" / f"{as_of[:7]}.md"
    entries = sorted(p.stem for p in reports_dir.glob("*.md"))
    nav_lines = [f"# Reports Index for {as_of[:7]}", ""]
    for rid in entries:
        marker = " (this render)" if rid == report_id else ""
        nav_lines.append(f"- [{rid}]({as_of[:7]}/{rid}.md){marker}")
    nav_index_path.write_text("\n".join(nav_lines) + "\n", encoding="utf-8")

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
