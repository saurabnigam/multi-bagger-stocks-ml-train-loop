"""Factor registry, version-tracked hypotheses, and staging compute."""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import textwrap
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from quant.data import fundamentals as fundamentals_mod
from quant.data import holdings as holdings_mod
from quant.data.prices import PriceStore
from quant.errors import Blocked, Refused
from quant.factors.base import Factor, FactorSpec
from quant.factors.controls import Beta252, Liq, Size
from quant.factors.flows import InstHoldChg3m
from quant.factors.growth import EarnMom, EpsGrowth3y, RevGrowth3y
from quant.factors.inputs import FactorInputs, build as build_inputs
from quant.factors.legacy import DcFlag
from quant.factors.low_risk import MaxRet21, Vol252
from quant.factors.momentum import Dist52wHigh, Mom12_1, Mom6_1, Rev1m, Trend200
from quant.factors.quality import Accruals, CashConversion3y, Leverage, Roce, RoeStability3y
from quant.factors.standardise import transform
from quant.factors.value import BookToPrice, DivYield, EarningsYield, FcfYield
from quant.run import RunContext
from quant.types import Draft, Result

LAUNCH_FACTOR_CLASSES: Dict[str, type[Factor]] = {
    "mom_12_1": Mom12_1,
    "trend_200": Trend200,
    "vol_252": Vol252,
    "roce": Roce,
    "accruals": Accruals,
    "cash_conversion_3y": CashConversion3y,
    "earnings_yield": EarningsYield,
    "book_to_price": BookToPrice,
    "eps_growth_3y": EpsGrowth3y,
    "earn_mom": EarnMom,
    "inst_hold_chg_3m": InstHoldChg3m,
    "mom_6_1": Mom6_1,
    "dist_52w_high": Dist52wHigh,
    "rev_1m": Rev1m,
    "max_ret_21": MaxRet21,
    "leverage": Leverage,
    "roe_stability_3y": RoeStability3y,
    "fcf_yield": FcfYield,
    "div_yield": DivYield,
    "rev_growth_3y": RevGrowth3y,
    "size": Size,
    "liq": Liq,
    "beta_252": Beta252,
    "dc_flag": DcFlag,
}

ACTIVE_LAUNCH_FACTORS = {
    "mom_12_1", "trend_200", "vol_252", "roce", "accruals",
    "cash_conversion_3y", "earnings_yield", "book_to_price",
    "eps_growth_3y", "earn_mom", "inst_hold_chg_3m"
}


# --------------------------------------------------------------- code identity v2 (D5, T3)
#
# _calc_code_sha (v2) pins a factor version to its *code*, not just its spec text: the
# factor class source, the module-level helpers it calls by name, the FactorInputs
# accessors its compute() reads (scan for "inputs.<method>(") and whatever those
# accessors themselves depend on (below), plus the shared standardise.transform every
# factor's raw output is put through. A shared-helper change (e.g. a fix in
# quant.data.prices.PriceStore._apply_actions_to_factors) now changes the hash of every
# factor that reads through it, even though no factor's own formula text moved -- MASTER_SPEC
# 5.1 requires that to force a version bump, not pass silently under the old version.
#
# ACCESSOR_HELPER_MAP is the explicit, maintained map from a FactorInputs accessor name to
# the objects whose content backs it: the accessor method itself plus the helper
# callables/data it reads. Entries are real objects (not lambdas) so a test can monkeypatch
# a single entry to a temporary function or module and observe the hash move.
ACCESSOR_HELPER_MAP: Dict[str, Tuple[Any, ...]] = {
    "attribute": (FactorInputs.attribute,),
    "fundamental": (
        FactorInputs.fundamental,
        FactorInputs._normalise_request,
        FactorInputs._query_frames,
        fundamentals_mod.pit_frame,
        fundamentals_mod._expand_fields,
        fundamentals_mod._alias_rank,
        fundamentals_mod.FIELD_ALIASES,
    ),
    "fundamental_dated": (
        FactorInputs.fundamental_dated,
        FactorInputs._normalise_request,
        FactorInputs._query_frames,
        fundamentals_mod.pit_frame,
        fundamentals_mod._expand_fields,
        fundamentals_mod._alias_rank,
        fundamentals_mod.FIELD_ALIASES,
    ),
    "ttm": (FactorInputs.ttm, fundamentals_mod.ttm),
    "holdings": (FactorInputs.holdings, holdings_mod.series),
    "tri": (
        FactorInputs.tri,
        PriceStore._versioned,
        PriceStore.corporate_actions,
        PriceStore._apply_actions_to_factors,
    ),
    "close_split": (
        FactorInputs.close_split,
        PriceStore._versioned,
        PriceStore.corporate_actions,
        PriceStore._apply_actions_to_factors,
    ),
    "close_raw": (FactorInputs.close_raw, PriceStore._versioned),
    "volume": (FactorInputs.volume, PriceStore._versioned),
    "adv_inr": (FactorInputs.adv_inr,),
    "splits": (FactorInputs.splits, PriceStore.split_events, PriceStore.corporate_actions),
    "benchmark_tri": (FactorInputs.benchmark_tri,),
}


def _normalise_source(src: str) -> str:
    """Dedent and strip trailing per-line whitespace so formatting-only edits do not move the hash."""
    return "\n".join(line.rstrip() for line in textwrap.dedent(src).splitlines())


def _content_of(obj: Any) -> str:
    """Deterministic text content of a hash input: a dict's sorted items, else an object's source.

    No absolute paths and no object ids reach the hash -- ``inspect.getsource`` returns the
    source text itself, and dict content is captured via ``sorted(...items())``, so this is
    stable across processes and machines for a given checkout.
    """
    if isinstance(obj, dict):
        return repr(sorted(obj.items()))
    return _normalise_source(inspect.getsource(obj))


def _referenced_module_functions(cls: type) -> List[Any]:
    """Module-level functions defined in ``cls``'s own module that its source references by name.

    E.g. ``growth.EpsGrowth3y.compute`` calls the bare name ``share_basis_multiplier``, a
    module-level helper in the same file. Sorted by qualified name for determinism.
    """
    import ast

    module = inspect.getmodule(cls)
    if module is None:
        return []
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError):
        return []
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    found = [
        obj for name, obj in vars(module).items()
        if name in referenced and inspect.isfunction(obj) and getattr(obj, "__module__", None) == module.__name__
    ]
    return sorted(found, key=lambda f: f.__qualname__)


def _inputs_accessor_calls(cls: type) -> List[str]:
    """FactorInputs accessor names ``cls``'s source calls: scan for ``inputs.<method>(``."""
    import re

    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):
        return []
    return sorted(set(re.findall(r"\binputs\.(\w+)\(", src)))


def _calc_code_sha(spec: FactorSpec) -> str:
    """Code identity v2 (MASTER_SPEC 5.1, D5): pins spec text, source and helper dependencies.

    Hashes, in order: the spec text fields (name, version, formula, inputs, direction,
    hypothesis -- the v1 content); the normalised source of the factor class; the
    module-level helpers in the factor's own module it references by name; for every
    FactorInputs accessor its compute() calls, that accessor's own source plus the
    dependencies pinned for it in ``ACCESSOR_HELPER_MAP``; and the shared
    ``quant.factors.standardise.transform`` every factor's raw output passes through.
    Deterministic across processes and machines: no absolute paths, no object ids.
    """
    parts = [
        f"{spec.name}:{spec.version}:{spec.formula}:{','.join(sorted(spec.inputs))}:{spec.direction}:{spec.hypothesis}"
    ]
    cls = LAUNCH_FACTOR_CLASSES.get(spec.name)
    if cls is not None:
        parts.append(_normalise_source(inspect.getsource(cls)))
        for fn in _referenced_module_functions(cls):
            parts.append(_content_of(fn))
        for accessor in _inputs_accessor_calls(cls):
            for dep in ACCESSOR_HELPER_MAP.get(accessor, ()):
                parts.append(_content_of(dep))
    parts.append(_content_of(transform))
    content = "\x1f".join(parts)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _calc_code_sha_v1(spec: FactorSpec) -> str:
    """Legacy (pre-D5) hash: spec text only. Used only to detect a not-yet-migrated row."""
    content = f"{spec.name}:{spec.version}:{spec.formula}:{','.join(sorted(spec.inputs))}:{spec.direction}:{spec.hypothesis}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_bootstrap_decision(conn: sqlite3.Connection, timestamp: str, git_sha: str) -> None:
    """Ensure Tier-0 bootstrap decision exists for initial factor registration."""
    conn.execute(
        """
        INSERT OR IGNORE INTO decisions (
            decision_id, kind, tier, subject_id, title, context, options_json,
            decision, evidence_refs_json, decided_on, decided_by, approver_kind,
            status, adr_path, git_sha
        ) VALUES (
            'DEC_BOOTSTRAP', 'factor_registration', 0, 'factors', 'Bootstrap launch factor set',
            'Initial factor registration', '[]', 'approve', '[]', ?, 'system', 'system',
            'approved', 'docs/adr/0001-bootstrap.md', ?
        )
        """,
        (timestamp, git_sha),
    )


def _ensure_code_identity_v2_decision(conn: sqlite3.Connection, timestamp: str, git_sha: str) -> None:
    """Ensure the one-time Tier-0 decision covering the v1 -> v2 code identity re-pin exists."""
    context = (
        "MASTER_SPEC 5.1 (D5): code_sha256 now pins source and shared-helper dependencies, "
        "not only spec text; unchanged rows still on the v1 (spec-text-only) hash are "
        "re-pinned once so a future genuine drift is not silently accepted."
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO decisions (
            decision_id, kind, tier, subject_id, title, context, options_json,
            decision, evidence_refs_json, decided_on, decided_by, approver_kind,
            status, adr_path, git_sha
        ) VALUES (
            'DEC_CODE_IDENTITY_V2', 'factor_registration', 0, 'factors',
            'Re-pin factor code identity to v2 (source + helper dependency hashes)',
            ?, '[]', 'approve', '[]', ?, 'system', 'system',
            'approved', 'docs/adr/0002-code-identity-v2.md', ?
        )
        """,
        (context, timestamp, git_sha),
    )


def sync(ctx: RunContext, definitions: List[FactorSpec]) -> Result:
    """Sync registered factor definitions to factor_registry and factor_status_history.

    Rejects changed code/helper hashes without affected version bumps. A row still carrying
    the v1 (spec-text-only) hash for an otherwise-unchanged spec is re-pinned to v2 once
    (MASTER_SPEC 5.1, D5); any other mismatch still refuses. Assigns valid lifecycle shadow
    to controls and legacy diagnostics.
    """
    from quant.data.gates import record_event

    cur = ctx.conn.cursor()
    _ensure_bootstrap_decision(ctx.conn, ctx.clock.iso(), ctx.git_sha)

    synced_count = 0
    now_iso = ctx.clock.iso()
    repinned: List[str] = []

    for spec in definitions:
        code_sha = _calc_code_sha(spec)

        cur.execute("SELECT code_sha256, formula FROM factor_registry WHERE factor_id = ?", (spec.factor_id,))
        existing = cur.fetchone()

        if existing is not None:
            existing_sha, existing_formula = existing
            if existing_sha == code_sha and existing_formula == spec.formula:
                continue
            if existing_formula == spec.formula and existing_sha == _calc_code_sha_v1(spec):
                cur.execute(
                    "UPDATE factor_registry SET code_sha256 = ? WHERE factor_id = ?",
                    (code_sha, spec.factor_id),
                )
                repinned.append(spec.factor_id)
                continue
            raise Refused(
                "CODE_CHANGED_WITHOUT_VERSION_BUMP",
                f"Factor {spec.factor_id} code or formula changed without version bump (sha {existing_sha} != {code_sha})",
            )
        if spec.hypothesis_id:
            cur.execute(
                """
                INSERT OR IGNORE INTO hypotheses (
                    hypothesis_id, family, review_opportunities_json, formula_sha256,
                    kind, subject_id, title, statement, expected_sign, horizon_m,
                    primary_metric, success_criterion, failure_criterion,
                    registered_on, registered_by, first_oos_as_of, budget_year,
                    sequence_in_year, counts_toward_budget, status, md_path
                ) VALUES (
                    ?, ?, '[]', ?, 'factor', ?, ?, ?, ?, ?,
                    'spearman_ic', 'ic>0', 'ic<=0',
                    ?, 'system', ?, 2026, 0, 0, 'open', ''
                )
                """,
                (
                    spec.hypothesis_id,
                    spec.family,
                    code_sha,
                    spec.factor_id,
                    spec.name,
                    spec.hypothesis,
                    spec.direction,
                    spec.horizon_m,
                    now_iso,
                    ctx.as_of,
                ),
            )

        # Determine launch lifecycle status
        if spec.family in ("control", "legacy"):
            status = "shadow"
        elif spec.name in ACTIVE_LAUNCH_FACTORS:
            status = "active"
        else:
            status = "shadow"

        cls = LAUNCH_FACTOR_CLASSES.get(spec.name)
        module_path = f"{cls.__module__}.{cls.__name__}" if cls else f"quant.factors.{spec.family}.{spec.name}"

        cur.execute(
            """
            INSERT INTO factor_registry (
                factor_id, name, version, family, direction, horizon_m, level,
                hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
                backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
                module_path, status, registered_on, first_live_as_of,
                status_changed_on, status_decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'DEC_BOOTSTRAP')
            """,
            (
                spec.factor_id,
                spec.name,
                spec.version,
                spec.family,
                spec.direction,
                spec.horizon_m,
                spec.level,
                spec.hypothesis,
                spec.formula,
                json.dumps(list(spec.inputs)),
                spec.lookback_days,
                1 if spec.applies_to_financials else 0,
                1 if spec.backfillable else 0,
                spec.min_coverage,
                spec.evidence,
                spec.hypothesis_id,
                code_sha,
                module_path,
                status,
                now_iso,
                now_iso,
            ),
        )

        cur.execute(
            """
            INSERT OR IGNORE INTO factor_status_history (
                factor_id, effective_from, status, decision_id
            ) VALUES (?, ?, ?, 'DEC_BOOTSTRAP')
            """,
            (spec.factor_id, now_iso, status),
        )
        synced_count += 1

    if repinned:
        _ensure_code_identity_v2_decision(ctx.conn, now_iso, ctx.git_sha)
        record_event(
            ctx, "CODE_IDENTITY_REPIN", "INFO",
            {"decision_id": "DEC_CODE_IDENTITY_V2", "factor_ids": sorted(repinned)},
        )

    return Result(
        status="ok",
        counts={"synced": synced_count, "repinned": len(repinned)},
        details={"message": f"Synced {synced_count} factors", "repinned": sorted(repinned)},
    )


def compute_all(ctx: RunContext, draft: Draft) -> pd.DataFrame:
    """Compute all active, shadow, and probation factors for draft cohort.
    
    Returns staging rows only (does NOT persist into factor_values).
    Excludes future hypotheses whose registration date is after knowledge_cutoff.
    """
    cur = ctx.conn.cursor()
    cur.execute(
        """
        SELECT factor_id, name, version, family, direction, module_path
        FROM factor_registry
        WHERE registered_on <= ?
          AND status IN ('active', 'shadow', 'probation')
        ORDER BY factor_id
        """,
        (draft.knowledge_cutoff,),
    )
    factor_meta = cur.fetchall()

    empty_cols = [
        "cohort_id", "as_of", "security_id", "factor_id",
        "raw", "winsor", "z", "sector_group", "flags",
        "input_refs_json", "track", "run_id"
    ]
    if not factor_meta:
        return pd.DataFrame(columns=empty_cols)

    inputs = build_inputs(ctx, draft)
    staging_frames = []

    for fid, name, ver, family, direction, mod_path in factor_meta:
        # Resolve class
        cls = LAUNCH_FACTOR_CLASSES.get(name)
        if cls is None:
            continue

        factor_obj = cls()
        raw = factor_obj.compute(inputs)

        # Task T9 / decision D10: a nonfinancial-only factor is structurally not
        # applicable to Financial Services names (raw is already NaN for them, per
        # each factor's own _is_financial() mask) -- distinct from a security that
        # is merely missing data. Tell transform() which securities are applicable
        # so it flags not_applicable rather than folding them into small_group.
        # Same "group != Financial Services" idiom as quant.model.composite and
        # quant.data.gates use for the same denominator exclusion (MASTER_SPEC §5.2).
        if factor_obj.spec.applies_to_financials:
            applicable = None
        else:
            applicable = inputs.sector_group != "Financial Services"

        # Standardise per sector group
        std_df = transform(raw, draft.groups, direction, ctx.cfg, applicable=applicable)

        frame = pd.DataFrame({
            "cohort_id": draft.cohort_id,
            "as_of": draft.as_of,
            "security_id": inputs.members,
            "factor_id": fid,
            "raw": std_df["raw"].values,
            "winsor": std_df["winsor"].values,
            "z": std_df["z"].values,
            "sector_group": draft.groups.reindex(inputs.members).values,
            "flags": std_df["flags"].values,
            "input_refs_json": json.dumps(inputs.provenance()),
            "track": draft.track,
            "run_id": ctx.run_id,
        })
        staging_frames.append(frame)

    if not staging_frames:
        return pd.DataFrame(columns=empty_cols)

    return pd.concat(staging_frames, ignore_index=True)


def values_frame(
    conn: sqlite3.Connection,
    cohort_id: str,
    factor_ids: List[str],
) -> pd.DataFrame:
    """Retrieve factor values matrix for a given cohort and list of factor IDs.
    
    Rows are security_id, columns are factor_id with values = z.
    """
    if not factor_ids:
        return pd.DataFrame()

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in factor_ids)
    query = f"""
    SELECT security_id, factor_id, z
    FROM factor_values
    WHERE cohort_id = ?
      AND factor_id IN ({placeholders})
    ORDER BY security_id, factor_id
    """
    cur.execute(query, (cohort_id, *factor_ids))
    rows = cur.fetchall()

    if not rows:
        return pd.DataFrame(columns=factor_ids)

    df = pd.DataFrame(rows, columns=["security_id", "factor_id", "z"])
    pivoted = df.pivot(index="security_id", columns="factor_id", values="z")
    pivoted.index.name = "security_id"
    return pivoted.reindex(columns=factor_ids)
