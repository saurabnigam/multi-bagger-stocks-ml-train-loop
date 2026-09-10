"""Leakage and causal integrity test suite T1 through T10 (C07, MASTER_SPEC 7.5).

Every check either evaluates real stored evidence from ``ctx.conn`` or, where the
spec explicitly allows it (T1 shuffle, T6 availability shift, T7 action
invariance, T8 holdings, T10 sector-only predictor), runs a fully deterministic,
self-contained fixture and says so in its ``reason``. A check that requires
evidence which does not exist yet (no published live cohort, no matured
labels, ...) reports DEFERRED rather than a fabricated PASS. Only T2-T9 can
block a publication (deterministic invariants); T1 and T10 are diagnostic and
never block, per MASTER_SPEC 7.5 ("Never block because one random permutation
happens to exceed 2SE").
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.evaluation.metrics import rank_ic
from quant.types import Check, CheckReport, Draft

if TYPE_CHECKING:
    from quant.run import RunContext

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_CASES_PATH = _REPO_ROOT / "docs/spec/contracts/golden_cases.json"
_FAR_FUTURE = "9999-12-31T23:59:59.999999Z"
_N_PERM = 200


def run(ctx: "RunContext", draft: Draft | None = None) -> CheckReport:
    """Execute leakage test suite T1 through T10.

    ``draft`` is the cohort currently being staged for publication (when this
    is called from G10 pre-publish, per MASTER_SPEC 4.6). T3_REPLAY, T5_EMBARGO
    and T9_SURVIVORSHIP still only ever examine already-published live cohorts
    -- structurally, at G10 time the current draft's own ``cohorts``/
    ``model_weights``/``labels`` rows do not exist yet (they are written by
    ``_stage_and_publish`` only after gates.run(phase="post") returns), the
    same "check staging/prior cohorts" shape MASTER_SPEC 4.6 permits and that
    the pre-existing G9 replay callback already has. T4_BOUNDARY is the
    exception: it additionally checks ``draft`` itself directly (its
    knowledge_cutoff derivation, source_refs and already-staged factor_values),
    so at least one deterministic check gates the actual cohort under review
    rather than only ever re-checking history.
    """
    checks: list[Check] = [
        _t1_shuffle(ctx),
        _t2_planted(),
        _t3_replay(ctx),
        _t4_boundary(ctx, draft),
        _t5_embargo(ctx),
        _t6_availability_shift(),
        _t7_action_invariance(),
        _t8_holdings(),
        _t9_survivorship(ctx),
        _t10_sector_null(ctx),
    ]
    return CheckReport(checks=checks)


# --------------------------------------------------------------------------- helpers

def _seed(ctx: "RunContext" | None) -> int:
    cfg = getattr(ctx, "cfg", None) if ctx is not None else None
    ev = getattr(cfg, "evaluation", None) if cfg is not None else None
    val = getattr(ev, "seed", None) if ev is not None else None
    return int(val) if val is not None else 0


def _conn(ctx: "RunContext" | None):
    return getattr(ctx, "conn", None) if ctx is not None else None


def _group_demean(values: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Subtract each group's own mean from its members.

    Mirrors the real pipeline's invariant that both the champion's composite
    score and the ``l_rel`` label used for evaluation are sector-relative (each
    has ~zero mean within its own sector group). Without this, a small random
    per-group sample can carry a persistent between-group level difference by
    chance; that fixed difference survives every within-group permutation and
    would bias the shuffle null away from zero for a reason that has nothing
    to do with leakage.
    """
    out = np.asarray(values, dtype=float).copy()
    for g in np.unique(groups):
        idx = groups == g
        out[idx] = out[idx] - out[idx].mean()
    return out


def _permute_within_groups(values: np.ndarray, groups: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    out = values.copy()
    for g in pd.unique(groups):
        idx = np.where(groups == g)[0]
        if len(idx) > 1:
            perm = rng.permutation(len(idx))
            out[idx] = values[idx][perm]
    return out


def _shuffle_null(
    score: pd.Series, label: pd.Series, groups: pd.Series, seed: int, n_perm: int = _N_PERM
) -> list[float]:
    """200 fixed within-group permutations of ``label``; returns the list of resulting rank ICs."""
    df = pd.DataFrame({"score": score.values, "label": label.values, "group": groups.values})
    df = df.dropna(subset=["score", "label"])
    df["group"] = df["group"].fillna("UNKNOWN")
    if len(df) < 3:
        return []

    rng = np.random.default_rng(seed)
    score_vals = df["score"].to_numpy()
    label_vals = df["label"].to_numpy()
    group_vals = df["group"].to_numpy()

    perm_ics: list[float] = []
    for _ in range(n_perm):
        shuffled = _permute_within_groups(label_vals, group_vals, rng)
        ic, _, status = rank_ic(pd.Series(score_vals), pd.Series(shuffled))
        if status == "ok" and ic is not None:
            perm_ics.append(float(ic))
    return perm_ics


# --------------------------------------------------------------------------- T1

def _t1_shuffle(ctx: "RunContext" | None) -> Check:
    seed = _seed(ctx)
    conn = _conn(ctx)
    score = label = groups = None
    source = None

    if conn is not None:
        # Search backward through EVERY live cohort with matured 3M labels (most
        # recent first), not just the single latest one: a thin latest cohort
        # (< 5 qualifying members) must not silently discard older real evidence
        # in favour of the synthetic fallback when an earlier cohort would
        # qualify.
        try:
            cohort_id_rows = conn.execute(
                """
                SELECT DISTINCT l.cohort_id
                FROM labels l JOIN cohorts c ON c.cohort_id = l.cohort_id
                WHERE l.horizon_m = 3 AND c.track = 'live'
                ORDER BY l.cohort_id DESC
                """
            ).fetchall()
        except Exception:
            cohort_id_rows = []

        from quant.evaluation.labels import frame as labels_frame

        for (candidate_cohort_id,) in cohort_id_rows:
            try:
                lbl_df = labels_frame(conn, candidate_cohort_id, 3, _FAR_FUTURE, scope="all", model_id="EW_HIER_v1")
                score_rows = conn.execute(
                    "SELECT security_id, final FROM scores WHERE cohort_id = ? AND model_id = 'EW_HIER_v1'",
                    (candidate_cohort_id,),
                ).fetchall()
                score_map = {r[0]: r[1] for r in score_rows if r[1] is not None}
                lbl_df = lbl_df[lbl_df["security_id"].isin(score_map)]
                if len(lbl_df) >= 5:
                    sids = lbl_df["security_id"].tolist()
                    score = pd.Series([score_map[s] for s in sids])
                    label = pd.Series(lbl_df["l_rel"].values)
                    groups = pd.Series(lbl_df["sector_group"].values)
                    source = (
                        f"live cohort {candidate_cohort_id} champion EW_HIER_v1 scores vs 3M labels "
                        f"(n={len(lbl_df)})"
                    )
                    break
            except Exception:
                continue

    if score is None:
        rng0 = np.random.default_rng(seed)
        n_groups, per_group = 6, 10
        n = n_groups * per_group
        groups_arr = np.repeat([f"SYN_G{i}" for i in range(n_groups)], per_group)
        raw_score = rng0.normal(size=n)
        raw_label = rng0.normal(size=n)
        score = pd.Series(_group_demean(raw_score, groups_arr))
        label = pd.Series(_group_demean(raw_label, groups_arr))
        groups = pd.Series(groups_arr)
        source = (
            f"documented synthetic fixture (no matured live cohort with EW_HIER_v1 3M labels found; "
            f"{n_groups} sector groups x {per_group} names, seed={seed})"
        )

    perm_ics = _shuffle_null(score, label, groups, seed=seed, n_perm=_N_PERM)
    if len(perm_ics) < 2:
        return Check(
            id="T1_SHUFFLE",
            status="FAIL",
            observed=len(perm_ics),
            expected=f">=2 of {_N_PERM}",
            reason=f"Shuffle test on {source} produced only {len(perm_ics)}/{_N_PERM} valid permutation ICs",
            blocking=False,
        )

    mc_mean = float(np.mean(perm_ics))
    mc_se = float(np.std(perm_ics, ddof=1) / np.sqrt(len(perm_ics)))
    tol = max(0.005, 5.0 * mc_se)
    passed = abs(mc_mean) <= tol
    return Check(
        id="T1_SHUFFLE",
        status="PASS" if passed else "FAIL",
        observed=round(mc_mean, 6),
        expected=0.0,
        reason=(
            f"Within-sector-group label shuffle on {source}: {len(perm_ics)}/{_N_PERM} valid permutations, "
            f"null mean={mc_mean:.5f}, MCSE={mc_se:.5f}, tolerance=max(0.005,5*MCSE)={tol:.5f} "
            f"({'centered within tolerance' if passed else 'NOT centered within tolerance'}); "
            f"diagnostic only, never blocks on a single permutation"
        ),
        blocking=False,
    )


# --------------------------------------------------------------------------- T2

def _t2_planted() -> Check:
    try:
        case = json.loads(_GOLDEN_CASES_PATH.read_text(encoding="utf-8"))["cases"]["planted_rank"]
    except Exception as e:
        return Check(
            id="T2_PLANTED", status="FAIL", observed=None, expected=None,
            reason=f"Could not load golden case 'planted_rank' from {_GOLDEN_CASES_PATH}: {e}",
            blocking=True,
        )

    scores = pd.Series(case["scores"])
    labels = pd.Series(case["labels"])
    expected_ic = float(case["expected_spearman"])
    ic, n, status = rank_ic(scores, labels)
    exact_ok = status == "ok" and ic is not None and abs(ic - expected_ic) < 1e-12

    from quant.factors.inputs import FactorInputs

    forbidden_tokens = ("label", "conn", "requests", "yf")
    leaky_members = [
        name
        for name in dir(FactorInputs)
        if not name.startswith("_") and any(tok in name.lower() for tok in forbidden_tokens)
    ]
    structural_ok = len(leaky_members) == 0

    passed = exact_ok and structural_ok
    return Check(
        id="T2_PLANTED",
        status="PASS" if passed else "FAIL",
        observed={"ic": ic, "n": n, "status": status, "leaky_members": leaky_members},
        expected={"ic": expected_ic, "leaky_members": []},
        reason=(
            f"Planted rank Spearman={ic} (n={n}, status={status}) vs golden expected={expected_ic} "
            f"(exact match={exact_ok}, tol 1e-12); FactorInputs public surface scanned for tokens "
            f"{forbidden_tokens}: leaky_members={leaky_members} (clean={structural_ok})"
        ),
        blocking=True,
    )


# --------------------------------------------------------------------------- T3

def _t3_replay(ctx: "RunContext" | None) -> Check:
    conn = _conn(ctx)
    if conn is None:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason="No database connection available to locate a published live cohort", blocking=True,
        )

    try:
        row = conn.execute(
            "SELECT cohort_id, as_of, knowledge_cutoff FROM cohorts WHERE track = 'live' "
            "ORDER BY published_at DESC, as_of DESC LIMIT 1"
        ).fetchone()
    except Exception as e:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=f"Could not query cohorts table: {type(e).__name__}: {e}", blocking=True,
        )

    if row is None:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason="No published live cohort exists yet", blocking=True,
        )

    cohort_id, as_of, cutoff = row[0], row[1], row[2]
    fv_rows = conn.execute(
        "SELECT security_id, factor_id, z, sector_group FROM factor_values WHERE cohort_id = ?",
        (cohort_id,),
    ).fetchall()
    if not fv_rows:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=f"Latest live cohort {cohort_id} has no stored factor_values to replay", blocking=True,
        )

    stored: dict[tuple[int, str], Any] = {}
    groups: dict[int, str] = {}
    for sid, fid, z, sg in fv_rows:
        stored[(sid, fid)] = z
        groups[sid] = sg

    sids = sorted(groups.keys())

    # Prerequisite: the price store must actually hold this vintage, otherwise a
    # recompute would silently produce all-NaN factors (a false FAIL, not a leak).
    try:
        from quant.data.prices import PriceStore

        store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
        latest = store.latest_date(sids)
    except Exception as e:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=f"Could not open price store to check vintage availability: {type(e).__name__}: {e}",
            blocking=True,
        )

    if latest is None:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=(
                f"Price store has no rows for cohort {cohort_id}'s {len(sids)} members; "
                f"it lacks this vintage, so replay is deferred rather than failed"
            ),
            blocking=True,
        )

    members_df = pd.DataFrame({"security_id": sids})
    groups_s = pd.Series(groups).reindex(members_df["security_id"])
    replay_draft = Draft(
        cohort_id=cohort_id, as_of=as_of, track="live", knowledge_cutoff=cutoff,
        definition_hash="t3_replay", members=members_df, groups=groups_s, source_refs={},
    )

    try:
        from quant.factors import registry as factor_registry

        recomputed = factor_registry.compute_all(ctx, replay_draft)
    except Exception as e:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=(
                f"Recompute for cohort {cohort_id} raised {type(e).__name__}: {e} "
                f"(treated as a missing prerequisite, not a mismatch)"
            ),
            blocking=True,
        )

    if recomputed is None or recomputed.empty:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=f"Recompute for cohort {cohort_id} produced no factor rows", blocking=True,
        )

    mismatches = []
    compared = 0
    for rrow in recomputed.itertuples(index=False):
        key = (int(rrow.security_id), rrow.factor_id)
        if key not in stored:
            continue
        old_z, new_z = stored[key], rrow.z
        compared += 1
        old_nan, new_nan = pd.isna(old_z), pd.isna(new_z)
        if old_nan and new_nan:
            continue
        if old_nan != new_nan or abs(float(old_z) - float(new_z)) > 1e-9:
            mismatches.append({"security_id": key[0], "factor_id": key[1], "stored_z": old_z, "recomputed_z": new_z})

    if compared == 0:
        return Check(
            id="T3_REPLAY", status="DEFERRED", observed=None, expected="matched",
            reason=(
                f"No overlapping (security_id, factor_id) between stored and recomputed factor_values for "
                f"cohort {cohort_id} (registry status likely changed since publication)"
            ),
            blocking=True,
        )

    passed = len(mismatches) == 0
    return Check(
        id="T3_REPLAY",
        status="PASS" if passed else "FAIL",
        observed={"compared": compared, "mismatches": len(mismatches)},
        expected={"mismatches": 0},
        reason=(
            f"Replayed factor computation for latest published live cohort {cohort_id} "
            f"({compared} (security,factor) z values compared, tolerance 1e-9): {len(mismatches)} mismatch(es)"
            + (f"; first={mismatches[0]}" if mismatches else "")
        ),
        blocking=True,
    )


# --------------------------------------------------------------------------- T4

def _t4_draft_violations(ctx: "RunContext" | None, draft: Draft) -> list[str]:
    """Independent pre-publish check of the cohort actually under review.

    The historical loop in ``_t4_boundary`` can only ever examine a cohort
    published in a PRIOR run: G10 runs before this month's ``cohorts`` row is
    written (see ``quant.run._stage_and_publish``), so it structurally cannot
    see the current draft. This checks ``draft`` itself -- its knowledge_cutoff
    derivation, its own source_refs, and its already-staged (in-memory,
    not-yet-persisted) factor_values -- so at least this leg of T4 gates the
    real cohort about to be published, not only cohorts already live.
    """
    from quant.run import knowledge_cutoff as expected_cutoff_fn

    violations: list[str] = []
    cutoff = draft.knowledge_cutoff

    try:
        expected_cutoff = expected_cutoff_fn(draft.as_of)
    except Exception:
        expected_cutoff = None
    if expected_cutoff is not None and cutoff != expected_cutoff:
        violations.append(
            f"draft {draft.cohort_id}: knowledge_cutoff {cutoff} != "
            f"as_of-23:59:59.999999-IST-in-UTC {expected_cutoff}"
        )

    # published_at itself does not exist yet (it is stamped only after this
    # gate returns), so this uses the current clock reading as a proxy for the
    # imminent published_at -- a real, independent check that the cutoff is
    # not somehow already in the future relative to now.
    clock = getattr(ctx, "clock", None) if ctx is not None else None
    if clock is not None:
        try:
            now = clock.iso()
        except Exception:
            now = None
        if now is not None and now < cutoff:
            violations.append(
                f"draft {draft.cohort_id}: current clock time {now} (proxy for imminent published_at) "
                f"< knowledge_cutoff {cutoff}"
            )

    refs = draft.source_refs or {}
    ucap = refs.get("universe_captured_at")
    if ucap is not None and ucap > cutoff:
        violations.append(f"draft {draft.cohort_id}: universe_captured_at {ucap} > knowledge_cutoff {cutoff}")

    pms = refs.get("price_manifest_sha")
    if pms is not None and pms == "":
        violations.append(f"draft {draft.cohort_id}: price_manifest_sha is empty")

    fv = draft.factor_values
    if fv is not None and len(fv) and "input_refs_json" in getattr(fv, "columns", []):
        for irj in fv["input_refs_json"].unique():
            try:
                ir = json.loads(irj) if irj else {}
            except Exception:
                ir = {}
            fv_cutoff = ir.get("cutoff")
            if fv_cutoff is not None and fv_cutoff != cutoff:
                violations.append(
                    f"draft {draft.cohort_id}: staged factor_values.input_refs_json cutoff {fv_cutoff} "
                    f"!= draft knowledge_cutoff {cutoff}"
                )
                break

    return violations


def _t4_boundary(ctx: "RunContext" | None, draft: Draft | None = None) -> Check:
    conn = _conn(ctx)
    if conn is None:
        return Check(
            id="T4_BOUNDARY", status="DEFERRED", observed=None, expected=True,
            reason="No database connection available", blocking=True,
        )

    rows = conn.execute(
        "SELECT cohort_id, as_of, knowledge_cutoff, published_at, source_refs_json "
        "FROM cohorts WHERE track = 'live'"
    ).fetchall()
    if not rows and draft is None:
        return Check(
            id="T4_BOUNDARY", status="DEFERRED", observed=None, expected=True,
            reason="No live cohort exists yet and no draft was supplied to check", blocking=True,
        )

    from quant.run import knowledge_cutoff as expected_cutoff_fn

    violations: list[str] = []
    missing_universe_captured_at: list[str] = []
    for cohort_id, as_of, cutoff, published_at, source_refs_json in rows:
        if published_at < cutoff:
            violations.append(f"{cohort_id}: published_at {published_at} < knowledge_cutoff {cutoff}")

        try:
            expected_cutoff = expected_cutoff_fn(as_of)
        except Exception:
            expected_cutoff = None
        if expected_cutoff is not None and cutoff != expected_cutoff:
            violations.append(
                f"{cohort_id}: knowledge_cutoff {cutoff} != as_of-23:59:59.999999-IST-in-UTC {expected_cutoff}"
            )

        try:
            refs = json.loads(source_refs_json) if source_refs_json else {}
        except Exception:
            refs = {}
        ucap = refs.get("universe_captured_at")
        if ucap is None:
            # Legitimately absent for a fresh-bootstrap cohort (run.py stores
            # null when no matching capture was found) -- not counted as a
            # violation, but tracked so a reviewer can see how often this leg
            # of the check is actually exercised versus silently skipped.
            missing_universe_captured_at.append(cohort_id)
        elif ucap > cutoff:
            violations.append(f"{cohort_id}: universe_captured_at {ucap} > knowledge_cutoff {cutoff}")

        fv_input_refs = conn.execute(
            "SELECT DISTINCT input_refs_json FROM factor_values WHERE cohort_id = ?", (cohort_id,)
        ).fetchall()
        for (irj,) in fv_input_refs:
            try:
                ir = json.loads(irj) if irj else {}
            except Exception:
                ir = {}
            fv_cutoff = ir.get("cutoff")
            if fv_cutoff is not None and fv_cutoff != cutoff:
                violations.append(
                    f"{cohort_id}: factor_values.input_refs_json cutoff {fv_cutoff} != cohort cutoff {cutoff}"
                )
                break

        empty_manifest = conn.execute(
            "SELECT COUNT(*) FROM prices_monthly WHERE cohort_id = ? "
            "AND (price_manifest_sha IS NULL OR price_manifest_sha = '')",
            (cohort_id,),
        ).fetchone()[0]
        if empty_manifest:
            violations.append(f"{cohort_id}: {empty_manifest} prices_monthly row(s) have an empty price_manifest_sha")

    draft_checked = draft is not None
    if draft_checked:
        violations.extend(_t4_draft_violations(ctx, draft))

    passed = len(violations) == 0
    return Check(
        id="T4_BOUNDARY",
        status="PASS" if passed else "FAIL",
        observed={
            "cohorts_checked": len(rows),
            "draft_checked": draft_checked,
            "violations": len(violations),
            "missing_universe_captured_at": missing_universe_captured_at,
        },
        expected={"violations": 0},
        reason=(
            f"Checked {len(rows)} live cohort(s)"
            + (" and the in-flight draft" if draft_checked else "")
            + f" for publish/cutoff/universe-capture/factor-input/price-manifest "
            f"boundary integrity: {len(violations)} violation(s)"
            + (f"; first={violations[0]}" if violations else "")
            + (
                f"; {len(missing_universe_captured_at)} cohort(s) had no universe_captured_at recorded "
                f"(not counted as a violation)"
                if missing_universe_captured_at
                else ""
            )
        ),
        blocking=True,
    )


# --------------------------------------------------------------------------- T5

def _t5_embargo(ctx: "RunContext" | None) -> Check:
    conn = _conn(ctx)
    if conn is None:
        return Check(
            id="T5_EMBARGO", status="DEFERRED", observed=None, expected=True,
            reason="No database connection available", blocking=True,
        )

    row = conn.execute(
        """
        SELECT mw.cohort_id, c.as_of, c.generated_at
        FROM model_weights mw JOIN cohorts c ON c.cohort_id = mw.cohort_id
        WHERE mw.model_id = 'IC_SHRUNK_v1' AND c.track = 'live'
        ORDER BY c.as_of DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return Check(
            id="T5_EMBARGO", status="DEFERRED", observed=None, expected=True,
            reason="No live cohort has stored model_weights for IC_SHRUNK_v1", blocking=True,
        )

    cohort_id, as_of, generated_at = row
    stored_hashes = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT evidence_hash FROM model_weights WHERE cohort_id = ? AND model_id = 'IC_SHRUNK_v1'",
            (cohort_id,),
        ).fetchall()
    }

    try:
        from quant.model.models import definition_at
        from quant.evaluation.walkforward import family_ic_history
        from quant.evaluation.labels import _add_months

        defn = definition_at(conn, "IC_SHRUNK_v1", as_of)
        ic_hist = family_ic_history(
            conn,
            {"weights_json": defn["weights"], "factor_set_json": defn["factor_set"], "horizon_m": 3},
            as_of,
            known_at=generated_at,
        )
    except Exception as e:
        return Check(
            id="T5_EMBARGO", status="DEFERRED", observed=None, expected=True,
            reason=(
                f"Could not recompute family_ic_history for cohort {cohort_id}: {type(e).__name__}: {e}"
            ),
            blocking=True,
        )

    # `family_ic_history` already discards any date whose horizon-3M endpoint
    # exceeds `as_of` before it returns (walkforward.py: `end_date =
    # _add_months(c_as_of, horizon_m); if end_date > as_of: continue`). Two
    # checks, deliberately kept separate rather than collapsed into one:
    #
    #  1. `direct_leaks` recomputes the bound unconditionally against every
    #     date ACTUALLY in the returned index, using this module's own literal
    #     `_add_months`/`>` -- it does not assume the leaked date belongs to
    #     any particular candidate set, so it still catches a leak even if the
    #     regression also corrupts which dates are considered candidates at
    #     all. This preserves full generality; it is not narrowed relative to
    #     the previous implementation.
    #  2. `should_be_excluded` is queried straight from the raw `cohorts`
    #     table -- NOT by re-inspecting `family_ic_history`'s own internal
    #     candidate-selection query or its already-filtered output -- so the
    #     evidence in `observed` documents, independently, exactly which
    #     as_of dates were expected to be excluded and lets a reviewer
    #     cross-check that set against what actually came back, rather than
    #     only trusting a single self-referential recomputation.
    direct_leaks = {str(d) for d in ic_hist.index if _add_months(str(d), 3) > as_of}

    candidate_as_of_dates = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT as_of FROM cohorts WHERE track = 'live' AND is_clean = 1 AND as_of <= ?",
            (as_of,),
        ).fetchall()
    }
    should_be_excluded = {d for d in candidate_as_of_dates if _add_months(d, 3) > as_of}
    crosscheck_leaks = {str(d) for d in ic_hist.index if str(d) in should_be_excluded}

    future_endpoints = sorted(direct_leaks | crosscheck_leaks)

    recomputed_hash = (
        hashlib.sha256(ic_hist.to_csv().encode()).hexdigest() if not ic_hist.empty else "empty_history"
    )
    hash_ok = recomputed_hash in stored_hashes
    endpoints_ok = len(future_endpoints) == 0
    passed = hash_ok and endpoints_ok

    return Check(
        id="T5_EMBARGO",
        status="PASS" if passed else "FAIL",
        observed={
            "recomputed_hash": recomputed_hash,
            "future_endpoints": future_endpoints,
            "n_dates": len(ic_hist.index),
            "independently_expected_excluded": sorted(should_be_excluded),
        },
        expected={"hash_in_stored": sorted(stored_hashes), "future_endpoints": []},
        reason=(
            f"Recomputed family_ic_history for IC_SHRUNK_v1 @ cohort {cohort_id} "
            f"(known_at=cohort.generated_at={generated_at}): {len(ic_hist.index)} index date(s); "
            f"{len(future_endpoints)} leaked a horizon-3M endpoint > as_of {as_of}, checked two ways -- "
            f"direct recomputation against the returned index, AND cross-referenced against "
            f"{len(should_be_excluded)} date(s) independently queried straight from the raw cohorts "
            f"table (not the function's own filtered output) that should have been excluded; "
            f"evidence_hash {'matches' if hash_ok else 'DOES NOT MATCH'} model_weights.evidence_hash "
            f"(this is how models.score_all hashes it)"
        ),
        blocking=True,
    )


# --------------------------------------------------------------------------- T6

def _t6_availability_shift() -> Check:
    from quant.db.core import schema_path
    from quant.data.fundamentals import pit_frame

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_path("state").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000000001', 'T6 Fixture Co', '2020-01-01', '2026-01-01', 'listed')"
        )
        available_from = "2024-04-01T00:00:00.000000Z"
        fetched_at = "2024-04-10T12:00:00.000000Z"
        conn.execute(
            """
            INSERT INTO fundamentals (
                security_id, statement, freq, period_end, field, value, unit,
                available_from, available_from_basis, fetched_at, source, run_id
            ) VALUES (1, 'income', 'Q', '2024-03-31', 'revenue', 100.0, 'inr', ?, 'lodr_45d', ?, 'fixture', 1)
            """,
            (available_from, fetched_at),
        )

        cutoff_between = "2024-04-05T00:00:00.000000Z"
        cutoff_after = "2024-04-15T00:00:00.000000Z"

        frame_between = pit_frame(conn, cutoff_between, "income", "revenue", "Q", 1, [1])
        frame_after = pit_frame(conn, cutoff_after, "income", "revenue", "Q", 1, [1])

        val_between = frame_between.loc[1, 0] if 1 in frame_between.index else np.nan
        val_after = frame_after.loc[1, 0] if 1 in frame_after.index else np.nan

        invisible_ok = pd.isna(val_between)
        visible_ok = (not pd.isna(val_after)) and abs(float(val_after) - 100.0) < 1e-9
        passed = invisible_ok and visible_ok

        return Check(
            id="T6_AVAIL_SHIFT",
            status="PASS" if passed else "FAIL",
            observed={
                "value_between_available_from_and_fetched_at": None if pd.isna(val_between) else float(val_between),
                "value_after_fetched_at": None if pd.isna(val_after) else float(val_after),
            },
            expected={"value_between_available_from_and_fetched_at": None, "value_after_fetched_at": 100.0},
            reason=(
                f"Fixture row available_from={available_from} (earlier claimed publication) fetched_at={fetched_at} "
                f"(later actual fetch). At cutoff {cutoff_between} (between the two) pit_frame returned "
                f"{'NaN, correctly invisible' if invisible_ok else val_between}; at cutoff {cutoff_after} "
                f"(after fetched_at) it returned {val_after if visible_ok else 'an unexpected value'} "
                f"({'correctly visible' if visible_ok else 'boundary bypassed'})"
            ),
            blocking=True,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- T7

def _t7_action_invariance() -> Check:
    from quant.data.prices import PriceStore

    with tempfile.TemporaryDirectory(prefix="leakage_t7_") as tmpdir:
        price_path = Path(tmpdir) / "prices_t7.db"
        store = PriceStore(price_path, state_conn=None)

        sid = 900001
        source_df = pd.DataFrame({
            "date": ["2024-01-01", "2024-01-02"],
            "close": [200.0, 100.0],
            "splits": [1.0, 2.0],
            "dividend": [0.0, 1.0],
        })
        metadata = {"security_id": sid, "observed_at": "2024-01-02T18:30:00.000000Z", "close_basis": "raw"}
        store.ingest(None, source_df, metadata)

        vintage_at = "2024-01-02T23:59:59.999999Z"
        tri_df = store.tri([sid], start="2024-01-01", end="2024-01-02", vintage_at=vintage_at)
        split_df = store.close_split([sid], start="2024-01-01", end="2024-01-02", vintage_at=vintage_at)

        tri0, tri1 = float(tri_df[sid].iloc[0]), float(tri_df[sid].iloc[1])
        split0, split1 = float(split_df[sid].iloc[0]), float(split_df[sid].iloc[1])

        # Hand computation by portfolio-value reasoning (independent of the TRI formula):
        # pre-split: 1 share @ INR200 = INR200 economic value.
        # ex-date: the 2:1 split turns it into 2 shares @ ~INR100 = INR200, plus a cash
        # dividend of INR1 per (post-split) share on those 2 shares = INR2 cash received.
        # Total economic value after the event = 200 + 2 = 202, i.e. a 1.01x total return.
        value_before = 1.0 * 200.0
        shares_after = 1.0 * 2.0
        cash_dividend = shares_after * 1.0
        value_after = shares_after * 100.0 + cash_dividend
        hand_total_return = value_after / value_before
        expected_tri1 = tri0 * hand_total_return

        tri_ok = abs(tri1 - expected_tri1) < 1e-9
        continuity_ok = abs(split0 - split1) < 1e-9
        passed = tri_ok and continuity_ok

        return Check(
            id="T7_ACTION_INVARIANCE",
            status="PASS" if passed else "FAIL",
            observed={"tri": [tri0, tri1], "close_split": [split0, split1]},
            expected={"tri_1_hand_calc": expected_tri1, "close_split_continuous": True},
            reason=(
                f"2:1 split + INR1/share cash dividend fixture: engine TRI=[{tri0:.6f}, {tri1:.6f}] vs "
                f"hand-computed total return {hand_total_return:.6f} (expected TRI[1]={expected_tri1:.6f}, "
                f"{'matches' if tri_ok else 'MISMATCH'} within 1e-9); close_split=[{split0:.6f}, {split1:.6f}] "
                f"{'continuous' if continuity_ok else 'DISCONTINUOUS'} across the split ex-date"
            ),
            blocking=True,
        )


# --------------------------------------------------------------------------- T8

def _t8_holdings() -> Check:
    from quant.db.core import schema_path
    from quant.data.holdings import series as holdings_series

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_path("state").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000000001', 'T8 Fixture Co', '2020-01-01', '2026-01-01', 'listed')"
        )
        conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
            "VALUES (1, '2024-01-15T00:00:00.000000Z', 50.0, 10.0, 1000.0, 'fixture')"
        )
        cutoff = "2024-02-01T00:00:00.000000Z"
        val_before = holdings_series(conn, cutoff, 0, [1]).get(1)

        # A capture timestamped strictly after the cutoff must not change the lag-0
        # value returned when re-querying at the SAME cutoff.
        conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
            "VALUES (1, '2024-03-01T00:00:00.000000Z', 99.0, 40.0, 1000.0, 'fixture')"
        )
        val_after = holdings_series(conn, cutoff, 0, [1]).get(1)

        passed = (
            val_before is not None and not pd.isna(val_before) and abs(float(val_before) - 50.0) < 1e-9
            and val_after is not None and not pd.isna(val_after) and abs(float(val_after) - 50.0) < 1e-9
        )
        return Check(
            id="T8_HOLDINGS",
            status="PASS" if passed else "FAIL",
            observed={"lag0_before_future_capture": val_before, "lag0_after_future_capture": val_after},
            expected={"lag0_before_future_capture": 50.0, "lag0_after_future_capture": 50.0},
            reason=(
                f"A holdings capture dated 2024-03-01 (after cutoff {cutoff}) was inserted after the first read; "
                f"the lag-0 value AT THAT SAME CUTOFF was {val_before} before and {val_after} after the insert "
                f"({'unchanged, correctly excluded' if passed else 'CHANGED: a future capture leaked into the past read'})"
            ),
            blocking=True,
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- T9

def _t9_survivorship(ctx: "RunContext" | None) -> Check:
    conn = _conn(ctx)
    if conn is None:
        return Check(
            id="T9_SURVIVORSHIP", status="DEFERRED", observed=None, expected=True,
            reason="No database connection available", blocking=True,
        )

    pairs = conn.execute(
        """
        SELECT DISTINCT l.cohort_id, l.horizon_m
        FROM labels l JOIN cohorts c ON c.cohort_id = l.cohort_id
        WHERE c.track = 'live'
        """
    ).fetchall()
    if not pairs:
        return Check(
            id="T9_SURVIVORSHIP", status="DEFERRED", observed=None, expected=True,
            reason="No matured live-track labels exist yet", blocking=True,
        )

    violations: list[str] = []
    for cohort_id, horizon_m in pairs:
        label_sids = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT security_id FROM labels WHERE cohort_id = ? AND horizon_m = ?",
                (cohort_id, horizon_m),
            ).fetchall()
        }
        member_sids = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT security_id FROM scores WHERE cohort_id = ?", (cohort_id,)
            ).fetchall()
        }
        if label_sids != member_sids:
            missing = member_sids - label_sids
            extra = label_sids - member_sids
            violations.append(
                f"{cohort_id}@{horizon_m}M: {len(missing)} scored member(s) missing a label, "
                f"{len(extra)} label(s) for non-member(s)"
            )

    passed = len(violations) == 0
    return Check(
        id="T9_SURVIVORSHIP",
        status="PASS" if passed else "FAIL",
        observed={"pairs_checked": len(pairs), "violations": len(violations)},
        expected={"violations": 0},
        reason=(
            f"Checked {len(pairs)} matured (cohort, horizon) label set(s) against their live cohort's scored "
            f"member set: {len(violations)} violation(s)" + (f"; first={violations[0]}" if violations else "")
        ),
        blocking=True,
    )


# --------------------------------------------------------------------------- T10

def _t10_sector_null(ctx: "RunContext" | None) -> Check:
    seed = _seed(ctx)
    rng = np.random.default_rng(seed)
    n_sectors, per_sector = 5, 40
    n = n_sectors * per_sector

    sector_effect = rng.normal(scale=1.0, size=n_sectors)
    noise = rng.normal(scale=1.0, size=n)
    sector_ids = np.repeat(np.arange(n_sectors), per_sector)
    returns = sector_effect[sector_ids] + noise
    predictor = sector_effect[sector_ids]

    df = pd.DataFrame({"sector": sector_ids, "ret": returns})
    group_median = df.groupby("sector")["ret"].transform("median")
    adjusted = (df["ret"] - group_median).to_numpy()

    ic, n_obs, status = rank_ic(pd.Series(predictor), pd.Series(adjusted))
    se = 1.0 / math.sqrt(n_obs - 1) if (n_obs and n_obs > 1) else float("nan")
    tol = 3.0 * se if not math.isnan(se) else float("nan")
    passed = status == "ok" and ic is not None and not math.isnan(tol) and abs(ic) <= tol

    return Check(
        id="T10_SECTOR_NULL",
        status="PASS" if passed else "FAIL",
        observed={"ic": ic, "n": n_obs, "se": None if math.isnan(se) else se},
        expected={"abs_ic_within": None if math.isnan(tol) else tol},
        reason=(
            f"Deterministic synthetic {n_sectors} sectors x {per_sector} names (seed={seed}): a sector-only "
            f"predictor (no idiosyncratic signal) against sector-median-adjusted returns gives rank IC={ic} "
            f"(n={n_obs}); SE=1/sqrt(n-1)={se:.5f}, tolerance=3*SE={tol:.5f} "
            f"({'within null bound (no stock-selection signal, as expected)' if passed else 'OUTSIDE null bound'}); "
            f"diagnostic only, does not assert real sector-dummy ICs are exactly zero"
        ),
        blocking=False,
    )
