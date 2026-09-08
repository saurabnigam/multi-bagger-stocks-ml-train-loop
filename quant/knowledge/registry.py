"""Hypothesis registry, registration validation, and budget tracking (C09)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any

from quant.data.calendar import Calendar
from quant.errors import Refused

if TYPE_CHECKING:
    from quant.run import RunContext


def _get_calendar(ctx: RunContext) -> Calendar:
    if getattr(ctx, "calendar", None) is not None:
        return ctx.calendar
    import pandas as pd
    dates = pd.bdate_range("2020-01-01", "2035-12-31")
    sessions_df = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "close_at": [f"{d.strftime('%Y-%m-%d')}T10:00:00.000000Z" for d in dates],
    })
    return Calendar(sessions_df)


def new_hypothesis(ctx: RunContext, fields: dict[str, Any]) -> str:
    """Register a new hypothesis in the knowledge registry.

    Enforces annual and family budgets for non-exempt hypotheses.
    Enforces that first_oos_as_of knowledge cutoff is strictly after registration time.
    """
    conn = ctx.conn
    if conn is None:
        raise ValueError("RunContext connection required")

    family = fields["family"]
    kind = fields.get("kind", "factor")
    subject_id = fields["subject_id"]
    title = fields["title"]
    statement = fields["statement"]
    expected_sign = fields.get("expected_sign", 1)
    horizon_m = fields.get("horizon_m", 3)
    primary_metric = fields.get("primary_metric", "ic")
    success_criterion = fields.get("success_criterion", "ic >= 0.02")
    failure_criterion = fields.get("failure_criterion", "ic < 0")

    registered_on = fields.get("registered_on") or (
        ctx.clock.now_iso()
        if (ctx and getattr(ctx, "clock", None))
        else f"{ctx.as_of}T00:00:00.000000Z"
    )
    by_val = fields.get("registered_by") or (
        f"{ctx.actor.kind}:{ctx.actor.name}"
        if (ctx and getattr(ctx, "actor", None))
        else "system"
    )

    first_oos_as_of = fields["first_oos_as_of"]
    cal = _get_calendar(ctx)
    first_cutoff = cal.cutoff(first_oos_as_of)

    # Cutoff must be strictly after registration timestamp
    if first_cutoff <= registered_on:
        raise Refused(
            "timing",
            f"first_oos_as_of cutoff ({first_cutoff}) must be strictly after registration timestamp ({registered_on})",
        )

    budget_year = int(fields.get("budget_year") or registered_on[:4])
    counts_toward_budget = int(fields.get("counts_toward_budget", 1))

    # Enforce annual and family hypothesis budgets
    if counts_toward_budget == 1:
        annual_used = conn.execute(
            "SELECT count(*) FROM hypotheses WHERE budget_year = ? AND counts_toward_budget = 1",
            (budget_year,),
        ).fetchone()[0]
        if annual_used >= 6:
            raise Refused("budget", f"Annual hypothesis budget of 6 exceeded for year {budget_year}")

        family_used = conn.execute(
            "SELECT count(*) FROM hypotheses WHERE budget_year = ? AND family = ? AND counts_toward_budget = 1",
            (budget_year, family),
        ).fetchone()[0]
        if family_used >= 3:
            raise Refused("budget", f"Family hypothesis budget of 3 exceeded for family '{family}' in year {budget_year}")

    seq = conn.execute(
        "SELECT coalesce(max(sequence_in_year), 0) + 1 FROM hypotheses WHERE budget_year = ?",
        (budget_year,),
    ).fetchone()[0]

    hypothesis_id = fields.get("hypothesis_id") or f"H-{budget_year}-{seq:03d}"
    formula = fields.get("formula", "")
    formula_sha = fields.get("formula_sha256") or hashlib.sha256(formula.encode("utf-8")).hexdigest()
    code_sha = fields.get("code_sha") or hashlib.sha256(f"{formula}:{subject_id}".encode("utf-8")).hexdigest()

    default_looks = [24, 36, 48] if kind == "model" else [12, 24, 36]
    review_ops = fields.get("review_opportunities_json") or json.dumps(default_looks)
    md_path = fields.get("md_path") or f"knowledge/hypotheses/{hypothesis_id}.md"

    conn.execute(
        """
        INSERT INTO hypotheses (
            family, review_opportunities_json, formula_sha256, hypothesis_id, kind,
            subject_id, title, statement, expected_sign, horizon_m, primary_metric,
            success_criterion, failure_criterion, registered_on, registered_by,
            first_oos_as_of, code_sha, budget_year, sequence_in_year, counts_toward_budget,
            status, n_periods_at_eval, t_hac_at_eval, t_crit_at_eval, m_tests_at_eval,
            resolved_on, resolution, decision_id, md_path
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            'open', NULL, NULL, NULL, NULL,
            NULL, NULL, NULL, ?
        )
        """,
        (
            family,
            review_ops,
            formula_sha,
            hypothesis_id,
            kind,
            subject_id,
            title,
            statement,
            expected_sign,
            horizon_m,
            primary_metric,
            success_criterion,
            failure_criterion,
            registered_on,
            by_val,
            first_oos_as_of,
            code_sha,
            budget_year,
            seq,
            counts_toward_budget,
            md_path,
        ),
    )
    conn.commit()
    return hypothesis_id


def budget_status(conn: sqlite3.Connection, year: int, horizon_m: int = 3) -> dict[str, Any]:
    """Report hypothesis registration and trial budget status for the given year."""
    annual_used = conn.execute(
        "SELECT count(*) FROM hypotheses WHERE budget_year = ? AND counts_toward_budget = 1",
        (year,),
    ).fetchone()[0]
    annual_trials = conn.execute(
        "SELECT count(*) FROM hypotheses WHERE budget_year = ?",
        (year,),
    ).fetchone()[0]
    total_trials = conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]

    fam_rows = conn.execute(
        "SELECT family, sum(counts_toward_budget) as used, count(*) as trials "
        "FROM hypotheses WHERE budget_year = ? GROUP BY family",
        (year,),
    ).fetchall()

    by_family = {}
    for r in fam_rows:
        fam = r["family"]
        u = int(r["used"] or 0)
        t = int(r["trials"] or 0)
        by_family[fam] = {
            "budget_used": u,
            "budget_remaining": max(0, 3 - u),
            "trials_count": t,
        }

    challenger_count = conn.execute(
        "SELECT count(*) FROM models WHERE role = 'challenger'"
    ).fetchone()[0]

    return {
        "year": year,
        "horizon_m": horizon_m,
        "budget_used": annual_used,
        "budget_remaining": max(0, 6 - annual_used),
        "annual_trials_count": annual_trials,
        "trials_count": total_trials,
        "by_family": by_family,
        "active_challengers_count": challenger_count,
    }
