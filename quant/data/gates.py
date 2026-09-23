"""Data quality gates G1-G10 (MASTER_SPEC 4.6), dq_runs persistence and DQ events.

Every gate computes its evidence from the draft, the state database and the
price store. A gate whose evidence cannot be computed FAILS; DEFERRED is used
only for history checks whose prerequisite (a prior cohort, evaluation
evidence) does not yet exist. Gate rows are persisted to ``dq_runs`` and, when
the run context supports it, are registered to survive a staging rollback so a
blocked run still leaves its diagnostics behind.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Literal, Optional
import numpy as np
import pandas as pd

from quant.db.core import append_rows
from quant.errors import Blocked, Refused
from quant.run import RunContext
from quant.types import Check, CheckReport, Draft

UNKNOWN_SECTORS = {"UNCLASSIFIED", "UNKNOWN", "", "None", "nan"}
PRICE_INPUTS = {"tri", "close_split", "close_raw", "volume", "adv_inr", "benchmark_tri"}
# Assumption (not in the spec table): a fresh install is BOOTSTRAP_REQUIRED when
# fewer than this share of members has any admissible fundamental statement.
MIN_FUNDAMENTALS_SHARE = 0.50
BOOTSTRAP_PREFIX = "BOOTSTRAP_REQUIRED"


def _cfg(ctx: RunContext, section: str, key: str, default: Any) -> Any:
    sec = getattr(getattr(ctx, "cfg", None), section, None)
    return getattr(sec, key, default) if sec is not None else default


def _member_ids(draft: Draft) -> List[int]:
    if draft.members is None or len(draft.members) == 0:
        return []
    if "security_id" in draft.members.columns:
        return sorted({int(x) for x in draft.members["security_id"].unique()})
    return sorted({int(x) for x in draft.members.index})


def _keep(ctx: RunContext, table: str, rows: pd.DataFrame, keys: List[str]) -> None:
    keeper = getattr(ctx, "keep_after_rollback", None)
    if callable(keeper):
        keeper(table, rows, keys)


def record_event(
    ctx: RunContext,
    code: str,
    severity: str,
    detail: Dict[str, Any],
    security_id: Optional[int] = None,
    field: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> int:
    """Record a data quality event (journaled by the database triggers)."""
    if severity not in ("INFO", "WARN", "BLOCK"):
        raise Refused("invalid_severity", f"Severity must be INFO, WARN or BLOCK, got {severity}")
    row = {
        "run_id": ctx.run_id,
        "as_of": ctx.as_of,
        "created_at": ctx.clock.iso(),
        "severity": severity,
        "code": code,
        "security_id": security_id,
        "field": field,
        "detail_json": json.dumps(detail, sort_keys=True, default=str),
        "resolved_by": resolved_by,
    }
    cur = ctx.conn.execute(
        """
        INSERT INTO data_quality_events (
            run_id, as_of, created_at, severity, code, security_id, field, detail_json, resolved_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(row.values()),
    )
    _keep(ctx, "data_quality_events", pd.DataFrame([row]),
          ["run_id", "code", "created_at", "security_id", "field", "detail_json"])
    return int(cur.lastrowid)


def _check(id_: str, ok: bool, observed: Any, expected: Any, reason: str, *, deferred: bool = False,
           blocking: bool = True) -> Check:
    if deferred:
        return Check(id=id_, status="DEFERRED", observed=observed, expected=expected, reason=reason, blocking=False)
    return Check(id=id_, status="PASS" if ok else "FAIL", observed=observed, expected=expected,
                 reason=reason, blocking=blocking and not ok)


# --------------------------------------------------------------------------- evidence helpers

def _closes_at(ctx: RunContext, draft: Draft) -> Optional[pd.Series]:
    """Close per member on as_of at the cohort vintage (cached on the draft)."""
    cache = draft.source_refs.setdefault("_cache", {})
    if "closes" in cache:
        return cache["closes"]
    store = getattr(ctx, "store", None)
    sids = _member_ids(draft)
    if store is None or not sids:
        cache["closes"] = None
        return None
    df = store.close_raw(sids, start=draft.as_of, end=draft.as_of, vintage_at=draft.knowledge_cutoff)
    ser = df.iloc[-1].reindex(sids) if not df.empty else pd.Series(np.nan, index=sids)
    cache["closes"] = ser
    return ser


def _latest_attributes(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    sids = _member_ids(draft)
    if ctx.conn is None or not sids:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in sids)
    rows = ctx.conn.execute(
        f"""
        WITH ranked AS (
            SELECT security_id, mcap_inr, trailing_pe, dividend_rate_inr, ev_inr,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY captured_at DESC) AS rn
            FROM security_attributes WHERE captured_at <= ? AND security_id IN ({placeholders})
        ) SELECT security_id, mcap_inr, trailing_pe, dividend_rate_inr, ev_inr FROM ranked WHERE rn = 1
        """,
        [draft.knowledge_cutoff, *sids],
    ).fetchall()
    return pd.DataFrame([dict(r) for r in rows]).set_index("security_id") if rows else pd.DataFrame()


def _latest_holdings(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    sids = _member_ids(draft)
    if ctx.conn is None or not sids:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in sids)
    rows = ctx.conn.execute(
        f"""
        WITH ranked AS (
            SELECT security_id, inst_pct, insider_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY captured_at DESC) AS rn
            FROM holdings WHERE captured_at <= ? AND security_id IN ({placeholders})
        ) SELECT security_id, inst_pct, insider_pct FROM ranked WHERE rn = 1
        """,
        [draft.knowledge_cutoff, *sids],
    ).fetchall()
    return pd.DataFrame([dict(r) for r in rows]).set_index("security_id") if rows else pd.DataFrame()


def _fundamentals_share(ctx: RunContext, draft: Draft) -> float:
    sids = _member_ids(draft)
    if ctx.conn is None or not sids:
        return 0.0
    placeholders = ",".join("?" for _ in sids)
    n = ctx.conn.execute(
        f"SELECT count(DISTINCT security_id) FROM fundamentals "
        f"WHERE available_from <= ? AND fetched_at <= ? AND security_id IN ({placeholders})",
        [draft.knowledge_cutoff, draft.knowledge_cutoff, *sids],
    ).fetchone()[0]
    return float(n) / float(len(sids))


def _prior_cohort(ctx: RunContext, draft: Draft) -> Optional[Dict[str, Any]]:
    if ctx.conn is None:
        return None
    row = ctx.conn.execute(
        "SELECT cohort_id, as_of, knowledge_cutoff FROM cohorts WHERE track = ? AND as_of < ? "
        "ORDER BY as_of DESC, published_at DESC LIMIT 1",
        (draft.track, draft.as_of),
    ).fetchone()
    return dict(row) if row else None


def _price_gap_check(ctx: RunContext, draft: Draft, sids: List[int]) -> Check:
    """Share of members missing at least one bar among the trailing 63 market sessions.

    A market session is a date on which at least half of the members have a close at the
    cohort vintage. Bars before a member's first bar (new listings) are not counted as gaps.
    Non-blocking: the finding is a warning with a DQ event naming the affected dates.
    """
    warn_share = float(_cfg(ctx, "gates", "missing_warn_share", 0.20))
    store = getattr(ctx, "store", None)
    if store is None or not sids:
        return _check("W_PRICE_GAPS", True, None, warn_share, "Price store unavailable; gap scan skipped",
                      deferred=True)
    start = (pd.Timestamp(draft.as_of) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    closes = store.close_raw(sids, start=start, end=draft.as_of, vintage_at=draft.knowledge_cutoff)
    if closes.empty:
        return _check("W_PRICE_GAPS", True, None, warn_share, "No bars in the window; gap scan skipped",
                      deferred=True)
    coverage = closes.notna().mean(axis=1)
    sessions = list(coverage[coverage >= 0.5].index)[-63:]
    window = closes.loc[sessions]
    started = window.notna().cummax()
    gaps = window.isna() & started
    members_with_gaps = int(gaps.any(axis=0).sum())
    share = members_with_gaps / len(sids)
    bad_dates = {d: int(n) for d, n in gaps.sum(axis=1).items() if n > 0}
    worst = dict(sorted(bad_dates.items(), key=lambda kv: -kv[1])[:5])
    draft.source_refs["price_gaps"] = {"members_with_gaps": members_with_gaps, "worst_dates": worst}
    ok = share <= warn_share
    if not ok:
        record_event(ctx, code="PRICE_GAPS", severity="WARN",
                     detail={"members_with_gaps": members_with_gaps, "share": round(share, 4), "worst_dates": worst})
    return _check("W_PRICE_GAPS", ok, {"members_with_gaps": members_with_gaps, "worst_dates": worst}, warn_share,
                  f"{members_with_gaps} members ({share:.1%}) miss at least one of the last {len(sessions)} "
                  f"sessions (warning above {warn_share:.0%}); non-blocking",
                  blocking=False)


# --------------------------------------------------------------------------- pre-compute gates

def _run_pre_gates(ctx: RunContext, draft: Draft) -> List[Check]:
    checks: List[Check] = []
    sids = _member_ids(draft)
    n_members = len(sids)

    # G1 universe size
    min_rows = int(_cfg(ctx, "universe", "min_rows", 480))
    g1_reason = f"Universe has {n_members} unique members (threshold {min_rows})"
    if n_members == 0 and draft.source_refs.get("universe_error"):
        g1_reason = f"{BOOTSTRAP_PREFIX}: {draft.source_refs['universe_error']} (no admissible members at the cutoff)"
    checks.append(_check("G1", n_members >= min_rows, n_members, min_rows, g1_reason))

    # G2 universe capture age
    stale_limit = int(_cfg(ctx, "universe", "stale_block_days", 62))
    cap_at = draft.source_refs.get("universe_captured_at") or draft.source_refs.get("universe_capture_date")
    if cap_at:
        age_days = (pd.Timestamp(draft.as_of) - pd.Timestamp(str(cap_at)[:10])).days
        checks.append(_check("G2", age_days <= stale_limit, age_days, stale_limit,
                             f"Universe capture age {age_days}d (limit {stale_limit}d)"))
    else:
        checks.append(_check("G2", False, None, stale_limit,
                             "No admissible universe capture timestamp recorded for this draft"))

    # G3 completed as_of close coverage (computed from the store; explicit hint only without a store)
    min_price_cov = float(_cfg(ctx, "gates", "price_coverage", 0.98))
    closes = _closes_at(ctx, draft)
    price_hint = draft.source_refs.get("has_prices")
    if n_members == 0:
        closes = None
        cov = 0.0
        checks.append(_check("G3", False, 0.0, min_price_cov,
                             f"{BOOTSTRAP_PREFIX}: no members at the cutoff, so no closes can be checked"))
    elif closes is not None:
        cov = float(closes.notna().sum()) / n_members if n_members else 0.0
        if cov == 0.0:
            checks.append(_check("G3", False, 0.0, min_price_cov,
                                 f"{BOOTSTRAP_PREFIX}: no completed as_of closes in the price store at the cohort vintage"))
        else:
            checks.append(_check("G3", cov >= min_price_cov, round(cov, 4), min_price_cov,
                                 f"Completed as_of close coverage {cov:.2%} (threshold {min_price_cov:.0%})"))
    elif price_hint is True:
        cov = 1.0
        checks.append(_check("G3", True, None, min_price_cov, "Price coverage asserted by caller (no price store attached)"))
    elif price_hint is False:
        cov = 0.0
        checks.append(_check("G3", False, 0.0, min_price_cov, f"{BOOTSTRAP_PREFIX}: prices missing for cohort date {draft.as_of}"))
    else:
        cov = 0.0
        checks.append(_check("G3", False, None, min_price_cov, "Price store unavailable; close coverage cannot be computed"))
    draft.source_refs["price_coverage"] = cov

    # W_PRICE_GAPS (non-blocking warning): missing bars inside the trailing 63 sessions.
    # G3 checks only the as_of bar; a vendor hole on an earlier session (observed: 202 of 502
    # tickers missing 2026-09-07 in the first download) silently shortens every window factor.
    checks.append(_price_gap_check(ctx, draft, sids))

    # G4 duplicate closes versus prior month
    dup_limit = float(_cfg(ctx, "gates", "duplicate_price_share", 0.05))
    prior = _prior_cohort(ctx, draft)
    draft.source_refs["has_prior_cohort"] = prior is not None
    if prior is None:
        checks.append(_check("G4", True, None, dup_limit, "First month: no prior cohort prices; same-close check deferred", deferred=True))
    elif closes is None:
        hint = draft.source_refs.get("duplicate_price_share")
        if hint is None:
            checks.append(_check("G4", False, None, dup_limit, "Price store unavailable; duplicate-close check cannot run"))
        else:
            checks.append(_check("G4", float(hint) < dup_limit, float(hint), dup_limit,
                                 f"Duplicate-close share asserted by caller: {float(hint):.2%}"))
    else:
        rows = ctx.conn.execute(
            "SELECT security_id, close_raw FROM prices_monthly WHERE cohort_id = ?", (prior["cohort_id"],)
        ).fetchall()
        prev = pd.Series({int(r[0]): r[1] for r in rows}, dtype=float)
        common = [s for s in sids if s in prev.index and pd.notna(prev[s]) and pd.notna(closes.get(s))]
        if not common:
            checks.append(_check("G4", False, 0, dup_limit, "No common priced names with the prior cohort"))
        else:
            same = sum(1 for s in common if abs(float(prev[s]) - float(closes[s])) < 1e-9)
            share = same / len(common)
            checks.append(_check("G4", share < dup_limit, round(share, 4), dup_limit,
                                 f"{same}/{len(common)} common names have an unchanged close ({share:.2%})"))

    # G5 unexplained price revisions
    rev_limit = float(_cfg(ctx, "gates", "revision_share", 0.02))
    store = getattr(ctx, "store", None)
    if store is None:
        hint = draft.source_refs.get("quarantined_price_share")
        if hint is None:
            checks.append(_check("G5", False, None, rev_limit, "Price store unavailable; quarantine share cannot be computed"))
        else:
            checks.append(_check("G5", float(hint) <= rev_limit, float(hint), rev_limit,
                                 f"Quarantined revision share asserted by caller: {float(hint):.2%}"))
    else:
        since = prior["as_of"] if prior else (pd.Timestamp(draft.as_of) - pd.Timedelta(days=31)).strftime("%Y-%m-%d")
        quarantined = [s for s in store.quarantined_securities(since, draft.as_of) if s in sids]
        share = len(quarantined) / n_members if n_members else 0.0
        draft.source_refs["quarantined_price_share"] = share
        checks.append(_check("G5", share <= rev_limit, round(share, 4), rev_limit,
                             f"{len(quarantined)} members with quarantined revisions ({share:.2%})"))

    # G6 unit bounds; missing pre-cutoff fundamentals on a fresh install is BOOTSTRAP_REQUIRED
    max_violators = int(_cfg(ctx, "gates", "max_unit_violators", 5))
    fund_hint = draft.source_refs.get("has_fundamentals")
    share = _fundamentals_share(ctx, draft)
    draft.source_refs["fundamentals_share"] = share
    fundamentals_missing = (fund_hint is False) or (fund_hint is None and share < MIN_FUNDAMENTALS_SHARE)
    violators: Dict[str, int] = {}
    attrs = _latest_attributes(ctx, draft)
    if not attrs.empty:
        pe = attrs["trailing_pe"].astype(float)
        violators["trailing_pe"] = int((pe.abs() >= 1000).sum())
        mcap = attrs["mcap_inr"].astype(float)
        violators["mcap_inr"] = int(((mcap <= 0) & mcap.notna()).sum())
        if closes is not None:
            dy = attrs["dividend_rate_inr"].astype(float) / closes.reindex(attrs.index).astype(float)
            violators["dividend_yield"] = int((dy > 0.25).sum())
    hold = _latest_holdings(ctx, draft)
    if not hold.empty:
        for col in ("inst_pct", "insider_pct"):
            v = hold[col].astype(float)
            violators[col] = int(((v < 0) | (v > 1)).sum())
    worst = max(violators.values()) if violators else 0
    draft.source_refs["unit_violators"] = violators
    if fundamentals_missing:
        checks.append(_check("G6", False, {"fundamentals_share": share}, "admissible pre-cutoff fundamentals",
                             f"{BOOTSTRAP_PREFIX}: missing pre-cutoff fundamentals "
                             f"(admissible statements cover {share:.0%} of members); bootstrap capture required"))
    else:
        checks.append(_check("G6", worst <= max_violators, violators, max_violators,
                             f"Unit-bound violators per field (max {worst}, limit {max_violators}); invalid inputs masked"))

    # G7 known sector coverage
    min_sector_cov = float(_cfg(ctx, "gates", "sector_coverage", 0.99))
    if draft.groups is not None and n_members > 0:
        g = pd.Series(draft.groups).reindex(sids)
        known = g.dropna().astype(str)
        known = known[~known.isin(UNKNOWN_SECTORS)]
        sector_cov = float(len(known)) / n_members
    else:
        sector_cov = 0.0
    checks.append(_check("G7", sector_cov >= min_sector_cov, round(sector_cov, 4), min_sector_cov,
                         f"Known sector coverage {sector_cov:.2%} (threshold {min_sector_cov:.0%})"))
    return checks


# --------------------------------------------------------------------------- factor coverage / G8

def factor_coverage(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    """Actual computed coverage per factor over applicable members.

    Columns: factor_id, status, family, is_price, applicable, finite, coverage,
    threshold, excluded. Structural non-applicability (financials for
    nonfinancial factors) is removed from the denominator.
    """
    cols = ["factor_id", "status", "family", "is_price", "applicable", "finite", "coverage", "threshold", "excluded",
            "prerequisite", "prerequisite_met"]
    fv = draft.factor_values
    if fv is None or len(fv) == 0:
        return pd.DataFrame(columns=cols)
    price_thr = float(_cfg(ctx, "factors", "price_min_coverage", 0.95))
    other_thr = float(_cfg(ctx, "factors", "other_min_coverage", 0.70))
    reg: Dict[str, Dict[str, Any]] = {}
    if ctx.conn is not None:
        for r in ctx.conn.execute(
            "SELECT factor_id, status, family, applies_to_financials, inputs_json FROM factor_registry"
        ).fetchall():
            try:
                inputs = set(json.loads(r["inputs_json"] or "[]"))
            except Exception:
                inputs = set()
            reg[r["factor_id"]] = {
                "status": r["status"],
                "family": r["family"],
                "applies_fin": bool(r["applies_to_financials"]),
                "is_price": bool(inputs) and inputs <= PRICE_INPUTS,
            }
    records = []
    for fid, group in fv.groupby("factor_id"):
        meta = reg.get(fid, {})
        family = meta.get("family", "")
        is_price = meta.get("is_price", family in ("momentum", "low_risk") or str(fid).split("@")[0] in
                            ("mom_12_1", "mom_6_1", "trend_200", "vol_252", "dist_52w_high", "rev_1m", "max_ret_21"))
        if "applies_to_financials" in group.columns and group["applies_to_financials"].notna().any():
            applies_fin = bool(group["applies_to_financials"].dropna().iloc[0])
        else:
            applies_fin = meta.get("applies_fin", True)
        applicable = group if applies_fin else group[group["sector_group"] != "Financial Services"]
        n_app = int(len(applicable))
        n_fin = int(applicable["z"].notna().sum()) if n_app else 0
        cov = n_fin / n_app if n_app else 0.0
        thr = price_thr if is_price else other_thr
        prereq, met = _prerequisite_status(ctx, draft, str(fid))
        records.append({
            "factor_id": fid, "status": meta.get("status", "active"), "family": family,
            "is_price": is_price, "applicable": n_app, "finite": n_fin, "coverage": cov,
            "threshold": thr, "excluded": cov < thr, "prerequisite": prereq, "prerequisite_met": met,
        })
    return pd.DataFrame(records, columns=cols)


def _prerequisite_status(ctx: RunContext, draft: Draft, factor_id: str) -> tuple[Optional[str], bool]:
    """History prerequisite a factor class declares (``prerequisite`` attribute) and whether it is met.

    MASTER_SPEC 4.6: DEFERRED is for evidence whose prerequisite history does not yet exist.
    A factor whose declared history (distinct holdings capture months, consecutive fiscal
    quarters) has not accumulated is excluded from composites but does not count against the
    G8 exclusion allowance; once the prerequisite is met, low coverage is a real failure.
    """
    try:
        from quant.factors.registry import LAUNCH_FACTOR_CLASSES
    except Exception:
        return None, True
    cls = LAUNCH_FACTOR_CLASSES.get(factor_id.split("@")[0])
    req = getattr(cls, "prerequisite", None) if cls is not None else None
    if not req or ctx.conn is None:
        return None, True
    cutoff = draft.knowledge_cutoff
    sids = _member_ids(draft)
    if "holdings_months" in req:
        need = int(req["holdings_months"])
        rows = ctx.conn.execute("SELECT DISTINCT captured_at FROM holdings WHERE captured_at <= ?", (cutoff,)).fetchall()
        months = {str(pd.Timestamp(r[0]).tz_convert("Asia/Kolkata").strftime("%Y-%m")) if pd.Timestamp(r[0]).tzinfo
                  else str(r[0])[:7] for r in rows}
        return f"holdings months {len(months)}/{need}", len(months) >= need
    if "consecutive_quarters" in req and sids:
        need = int(req["consecutive_quarters"])
        placeholders = ",".join("?" for _ in sids)
        rows = ctx.conn.execute(
            f"SELECT security_id, count(DISTINCT period_end) FROM fundamentals WHERE freq = 'Q' AND statement = 'income' "
            f"AND field IN ('Net Income', 'Net Income Common Stockholders') AND available_from <= ? AND fetched_at <= ? "
            f"AND security_id IN ({placeholders}) GROUP BY security_id",
            [cutoff, cutoff, *sids],
        ).fetchall()
        share = sum(1 for r in rows if int(r[1]) >= need) / len(sids)
        thr = float(_cfg(ctx, "factors", "other_min_coverage", 0.70))
        return f"members with {need} quarters {share:.0%} (need {thr:.0%})", share >= thr
    return None, True


def excluded_factors(ctx: RunContext, draft: Draft) -> List[str]:
    cov = factor_coverage(ctx, draft)
    if cov.empty:
        return []
    return sorted(cov.loc[cov["excluded"], "factor_id"].tolist())


def _run_post_gates(
    ctx: RunContext,
    draft: Draft,
    g9_replay_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
    g10_eval_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
) -> List[Check]:
    checks: List[Check] = []
    max_excluded = int(_cfg(ctx, "gates", "max_excluded_factors", 2))

    cov = factor_coverage(ctx, draft)
    if cov.empty:
        checks.append(_check("G8", False, 0, max_excluded, "No computed factor values in the draft"))
    else:
        active_excl = cov[cov["excluded"] & (cov["status"] == "active")]
        excluded_active = sorted(active_excl.loc[active_excl["prerequisite_met"].astype(bool), "factor_id"].tolist())
        awaiting = {r["factor_id"]: r["prerequisite"] for _, r in active_excl.iterrows() if not bool(r["prerequisite_met"])}
        draft.source_refs["excluded_factors"] = sorted(cov.loc[cov["excluded"], "factor_id"].tolist())
        draft.source_refs["factor_coverage"] = {
            r["factor_id"]: round(float(r["coverage"]), 4) for _, r in cov.iterrows()
        }
        draft.source_refs["excluded_active_factors"] = excluded_active
        draft.source_refs["awaiting_prerequisite"] = awaiting
        reason = f"{len(excluded_active)} active factors excluded on coverage (limit {max_excluded}): {excluded_active}"
        if awaiting:
            reason += f"; awaiting history prerequisites (not counted, excluded from composites): {awaiting}"
        checks.append(_check(
            "G8", len(excluded_active) <= max_excluded, len(excluded_active), max_excluded, reason,
        ))

    has_prior = bool(draft.source_refs.get("has_prior_cohort")) or _prior_cohort(ctx, draft) is not None
    if not has_prior:
        checks.append(_check("G9", True, None, "exact_match",
                             "First cohort: no prior published cohort; replay deferred", deferred=True))
    else:
        if g9_replay_cb is None:
            raise Refused("missing_callback", "G9 historical replay callback must be provided when a prior cohort exists")
        checks.append(g9_replay_cb(ctx, draft))

    if g10_eval_cb is None:
        checks.append(_check("G10", True, None, "no_leakage",
                             "Leakage evidence callback absent; deferred", deferred=True))
    else:
        checks.append(g10_eval_cb(ctx, draft))
    return checks


# --------------------------------------------------------------------------- persistence + entry point

def persist_checks(ctx: RunContext, checks: List[Check], phase: str) -> None:
    """Persist every check to dq_runs (one row per run/gate/phase; a re-evaluation updates it)."""
    rows = []
    for c in checks:
        rows.append({
            "run_id": ctx.run_id,
            "gate": c.id,
            "phase": phase,
            "status": c.status,
            "observed_json": json.dumps(c.observed, sort_keys=True, default=str),
            "expected_json": json.dumps(c.expected, sort_keys=True, default=str),
            "reason": c.reason,
            "blocking": 1 if c.blocking else 0,
        })
    if not rows:
        return
    for row in rows:
        existing = ctx.conn.execute(
            "SELECT status, observed_json, expected_json, reason, blocking FROM dq_runs "
            "WHERE run_id = ? AND gate = ? AND phase = ?",
            (row["run_id"], row["gate"], row["phase"]),
        ).fetchone()
        if existing is None:
            ctx.conn.execute(
                "INSERT INTO dq_runs (run_id, gate, phase, status, observed_json, expected_json, reason, blocking) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row.values()),
            )
        elif tuple(existing) != (row["status"], row["observed_json"], row["expected_json"], row["reason"], row["blocking"]):
            ctx.conn.execute(
                "UPDATE dq_runs SET status = ?, observed_json = ?, expected_json = ?, reason = ?, blocking = ? "
                "WHERE run_id = ? AND gate = ? AND phase = ?",
                (row["status"], row["observed_json"], row["expected_json"], row["reason"], row["blocking"],
                 row["run_id"], row["gate"], row["phase"]),
            )
    _keep(ctx, "dq_runs", pd.DataFrame(rows), ["run_id", "gate", "phase"])


def run(
    ctx: RunContext,
    draft: Draft,
    phase: Literal["pre", "post"],
    *,
    strict: bool = True,
    g9_replay_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
    g10_eval_cb: Optional[Callable[[RunContext, Draft], Check]] = None,
) -> CheckReport:
    """Run the gates of one phase, persist every check, and block when strict.

    The Blocked code is BOOTSTRAP_REQUIRED when every failing gate failed only
    because pre-cutoff captures are absent (fresh install); otherwise GATE_FAILURE.
    """
    if phase == "pre":
        checks = _run_pre_gates(ctx, draft)
    elif phase == "post":
        checks = _run_post_gates(ctx, draft, g9_replay_cb=g9_replay_cb, g10_eval_cb=g10_eval_cb)
    else:
        raise Refused("invalid_phase", f"Phase must be 'pre' or 'post', got {phase}")

    failed = [c for c in checks if c.status == "FAIL" and c.blocking]
    for c in failed:
        is_boot = c.reason.startswith(BOOTSTRAP_PREFIX)
        record_event(ctx, code=BOOTSTRAP_PREFIX if is_boot else f"GATE_{c.id}_FAILED", severity="BLOCK",
                     detail={"gate": c.id, "observed": c.observed, "expected": c.expected, "reason": c.reason})
    persist_checks(ctx, checks, phase)

    report = CheckReport(checks=checks)
    if strict and failed:
        ids = [c.id for c in failed]
        cold_start = phase == "pre" and not (
            draft.source_refs.get("universe_captured_at") or draft.source_refs.get("universe_capture_date")
        )
        if cold_start or all(c.reason.startswith(BOOTSTRAP_PREFIX) for c in failed):
            detail = "; ".join(c.reason for c in failed)
            if cold_start:
                detail = "no admissible universe capture before the cutoff; capture now and target a later month. " + detail
            raise Blocked(BOOTSTRAP_PREFIX, detail)
        raise Blocked("GATE_FAILURE", f"Phase '{phase}' blocking gates failed: {ids}")
    return report
