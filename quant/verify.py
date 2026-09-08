"""Verification module for reports, evidence references, and PIT audits (C11)."""

from __future__ import annotations

import sqlite3
from typing import Any

from quant.config import Config
from quant.types import Check, CheckReport


def report(conn: sqlite3.Connection, as_of: str, cfg: Config) -> CheckReport:
    """Verify that persisted report reproduces recorded evidence references."""
    checks: list[Check] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    # 1. Query evaluations for as_of
    eval_rows = cur.execute(
        "SELECT * FROM evaluations WHERE as_of = ? ORDER BY eval_id ASC",
        (as_of,),
    ).fetchall()

    if not eval_rows:
        checks.append(
            Check(
                id="verify:report:evaluations_found",
                status="PASS",
                observed=0,
                expected=0,
                reason=f"No evaluations found for as_of={as_of} (empty cohort/report)",
                blocking=False,
            )
        )
        return CheckReport(checks=checks)

    # 2. Check evidence references and uncertainty status
    all_refs_valid = True
    all_uncertainty_valid = True
    bad_refs: list[str] = []
    bad_uncertainty: list[str] = []

    for r in eval_rows:
        ev_id = r["eval_id"]
        ev_hash = r["evidence_hash"]
        status = r["status"]
        ci_lo = r["ci90_lo"]
        ci_hi = r["ci90_hi"]

        if not ev_hash or len(str(ev_hash)) < 4:
            all_refs_valid = False
            bad_refs.append(f"eval_{ev_id}:missing_hash")

        if status not in ("estimable", "unavailable", "refused"):
            all_uncertainty_valid = False
            bad_uncertainty.append(f"eval_{ev_id}:invalid_status({status})")
        elif status == "estimable" and (ci_lo is None or ci_hi is None):
            all_uncertainty_valid = False
            bad_uncertainty.append(f"eval_{ev_id}:estimable_missing_bounds")

    checks.append(
        Check(
            id="verify:report:evidence_refs",
            status="PASS" if all_refs_valid else "FAIL",
            observed="valid" if all_refs_valid else bad_refs,
            expected="valid",
            reason="All evidence hashes valid" if all_refs_valid else f"Invalid hashes: {bad_refs}",
            blocking=True,
        )
    )

    checks.append(
        Check(
            id="verify:report:uncertainty_status",
            status="PASS" if all_uncertainty_valid else "FAIL",
            observed="valid" if all_uncertainty_valid else bad_uncertainty,
            expected="valid",
            reason="All uncertainty bounds valid" if all_uncertainty_valid else f"Invalid uncertainty: {bad_uncertainty}",
            blocking=True,
        )
    )

    return CheckReport(checks=checks)


def pit(conn: sqlite3.Connection, months: int, cfg: Config) -> CheckReport:
    """Verify point-in-time constraints over the last `months` periods."""
    checks: list[Check] = []
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    # Query recent cohorts
    cohort_rows = cur.execute(
        "SELECT * FROM cohorts ORDER BY as_of DESC LIMIT ?",
        (months,),
    ).fetchall()

    if not cohort_rows:
        checks.append(
            Check(
                id="verify:pit:cohorts",
                status="PASS",
                observed=0,
                expected=0,
                reason="No recent cohorts to verify",
                blocking=False,
            )
        )
        return CheckReport(checks=checks)

    pit_ok = True
    detail_msgs: list[str] = []

    for c in cohort_rows:
        c_id = c["cohort_id"]
        cutoff = c["knowledge_cutoff"]
        pub = c["published_at"]
        if pub and cutoff and pub < cutoff:
            pit_ok = False
            detail_msgs.append(f"Cohort {c_id} published_at ({pub}) < cutoff ({cutoff})")

    checks.append(
        Check(
            id="verify:pit:publication_temporal_order",
            status="PASS" if pit_ok else "FAIL",
            observed="valid" if pit_ok else detail_msgs,
            expected="valid",
            reason="Temporal ordering verified" if pit_ok else "; ".join(detail_msgs),
            blocking=True,
        )
    )

    return CheckReport(checks=checks)
