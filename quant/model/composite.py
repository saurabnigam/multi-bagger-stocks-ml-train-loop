"""Composite scoring and family aggregation."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from quant.factors.standardise import transform

if TYPE_CHECKING:
    from quant.config import Config


NONFINANCIAL_PREFIXES = (
    "roce",
    "accruals",
    "cash_conversion",
    "cash_conv",
    "leverage",
    "fcf_yield",
)


def compose(
    z: pd.DataFrame,
    definitions: pd.DataFrame,
    units: dict[str, int],
    groups: pd.Series,
    cfg: Config,
    *,
    mode: str = "hierarchical",
    sleeve: pd.Series | None = None,
    sleeve_weight: float = 0.0,
) -> pd.DataFrame:
    """Compose standardized factor z-scores into family scores, composites, and final ranks.

    Modes:
      - 'hierarchical': Within each family, average finite factors using status weights
        (1.0 active, 0.5 probation, 0 otherwise). Across present families, weighted mean
        with weights from units renormalized over present families.
      - 'flat': Averages all finite active/probation factors directly using status weights.
      - 'mom_only': Only requires the momentum family and >= 1 finite factor.
      - 'hierarchical_nr': Family scores and composite exactly as 'hierarchical', but
        'final' is the raw composite -- no within-sector re-neutralisation. Drops the
        group-size ceiling that composite_neutral's rank-normal transform imposes
        (MASTER_SPEC 6.4 decision D6). 'composite_neutral' is still computed and stored
        for diagnostics; a group too small/constant to standardise never excludes a name
        from being scored in this mode, since 'final' does not depend on it. Ranks by
        'final' descending; since final == composite, ties are already broken by
        composite descending, then security_id ascending.
      - 'hierarchical_cov': As 'hierarchical', but 'final' = composite_neutral *
        sqrt(n_factors_used / n_applicable_factors) -- a mild coverage shrinkage so a
        composite over few applicable factors counts for less (MASTER_SPEC 6.4 decision
        D7). n_applicable_factors excludes nonfinancial-only factors for Financial
        Services names, matching the coverage-share denominator above. Coverage and
        eligibility rules are unchanged; a name unscored for neutralisation stays
        unscored here too, since 'final' derives from composite_neutral.
    """
    idx = z.index
    groups_aligned = groups.reindex(idx).fillna("UNKNOWN")

    # Determine status weights and applicability from definitions
    def_df = definitions.copy()
    if "status_weight" not in def_df.columns:
        if "status" in def_df.columns:
            def_df["status_weight"] = (
                def_df["status"]
                .map({"active": 1.0, "probation": 0.5, "shadow": 0.0, "retired": 0.0})
                .fillna(0.0)
            )
        else:
            def_df["status_weight"] = 1.0

    # Build factor metadata map for active/probation factors
    def_map: dict[str, dict[str, Any]] = {}
    for _, row in def_df.iterrows():
        fid = str(row["factor_id"])
        w = float(row["status_weight"])
        if w <= 0.0:
            continue
        fam = str(row["family"])
        is_nonfin = False
        if "nonfinancial" in row and pd.notna(row["nonfinancial"]):
            is_nonfin = bool(row["nonfinancial"])
        elif any(fid.startswith(p) for p in NONFINANCIAL_PREFIXES):
            is_nonfin = True
        def_map[fid] = {
            "family": fam,
            "status_weight": w,
            "nonfinancial": is_nonfin,
        }

    # Intersect with columns present in z
    eval_factors = [f for f in def_map if f in z.columns]

    min_families_cfg = int(getattr(getattr(cfg, "standardise", None), "min_families", 3))
    min_share_cfg = float(getattr(getattr(cfg, "standardise", None), "min_factor_share", 0.60))

    family_scores_json = {}
    composites = {}
    scored_flags = {}
    n_factors_used = {}
    n_applicable = {}
    exclusion_reasons = {}

    for sid in idx:
        grp = str(groups_aligned.loc[sid])
        is_financial = grp == "Financial Services"

        # Determine applicable factors for this security
        if is_financial:
            app_factors = [f for f in eval_factors if not def_map[f]["nonfinancial"]]
        else:
            app_factors = eval_factors

        D = len(app_factors)
        n_applicable[sid] = D

        # Finite observations
        finite_factors = [
            f
            for f in app_factors
            if pd.notna(z.loc[sid, f]) and np.isfinite(z.loc[sid, f])
        ]
        K = len(finite_factors)
        n_factors_used[sid] = K

        # Sorted, not a set: set iteration order of strings depends on PYTHONHASHSEED, which made
        # family_scores_json and the order of float additions differ between identical runs.
        present_families = sorted({def_map[f]["family"] for f in finite_factors})
        P = len(present_families)

        # Check coverage
        is_mom_only = mode.lower() in ("mom_only", "momentum_only")
        if is_mom_only:
            passed_cov = ("momentum" in present_families) and (K >= 1)
        else:
            passed_cov = (P >= min_families_cfg) and (D > 0) and ((K / D) >= min_share_cfg)

        if not passed_cov:
            scored_flags[sid] = 0
            exclusion_reasons[sid] = "coverage"
            composites[sid] = np.nan
            family_scores_json[sid] = "{}"
            continue

        scored_flags[sid] = 1
        exclusion_reasons[sid] = None

        # Compute per-family scores
        f_scores: dict[str, float] = {}
        for fam in present_families:
            fam_factors = [f for f in finite_factors if def_map[f]["family"] == fam]
            w_sum = sum(def_map[f]["status_weight"] for f in fam_factors)
            val_sum = sum(
                def_map[f]["status_weight"] * float(z.loc[sid, f]) for f in fam_factors
            )
            f_scores[fam] = float(val_sum / w_sum) if w_sum > 0 else 0.0
        family_scores_json[sid] = json.dumps(f_scores, sort_keys=True)

        # Compute composite
        if mode.lower() == "flat":
            # Direct status-weighted average of finite factors
            w_sum = sum(def_map[f]["status_weight"] for f in finite_factors)
            val_sum = sum(
                def_map[f]["status_weight"] * float(z.loc[sid, f]) for f in finite_factors
            )
            composites[sid] = float(val_sum / w_sum) if w_sum > 0 else np.nan
        elif is_mom_only:
            composites[sid] = f_scores.get("momentum", np.nan)
        else:  # hierarchical
            tot_units = sum(units.get(fam, 0) for fam in present_families)
            if tot_units > 0:
                composites[sid] = float(
                    sum(units.get(fam, 0) * f_scores[fam] for fam in present_families)
                    / tot_units
                )
            else:
                composites[sid] = float(np.mean(list(f_scores.values())))

    comp_series = pd.Series(composites, index=idx)
    scored_series = pd.Series(scored_flags, index=idx, dtype=int)

    # Neutralize composite per sector group (direction=+1)
    neutral_res = transform(comp_series, groups_aligned, direction=1, cfg=cfg)
    comp_neutral = neutral_res["z"]

    # A name that passed coverage but whose sector group is too small or constant for
    # standardisation (MASTER_SPEC 5.2: >=5 finite observations, >1 distinct value)
    # cannot be neutralized. It must NOT fall back to the un-neutralized composite --
    # doing so would silently mix a raw, non-comparable score into production ranking.
    # Such names are unscored with reason "neutralisation"; the raw composite is kept
    # for diagnostics but composite_neutral/final stay NaN.
    #
    # 'hierarchical_nr' does not use composite_neutral for 'final' at all (MASTER_SPEC
    # 6.4 D6), so a failed neutralisation must not exclude a name here -- comp_neutral
    # is retained purely as a diagnostic column in that mode.
    is_nr_mode = mode.lower() == "hierarchical_nr"
    failed_neutral = (scored_series == 1) & comp_neutral.isna() & comp_series.notna()
    if not is_nr_mode:
        for sid in failed_neutral[failed_neutral].index:
            scored_flags[sid] = 0
            exclusion_reasons[sid] = "neutralisation"
        scored_series = pd.Series(scored_flags, index=idx, dtype=int)

    comp_neutral = comp_neutral.where(scored_series == 1, np.nan)

    # Sector tilt and final score
    if sleeve is not None and sleeve_weight > 0.0:
        sleeve_aligned = sleeve.reindex(idx).fillna(0.0)
        sector_tilt = sleeve_aligned
        final = (1.0 - sleeve_weight) * comp_neutral + sleeve_weight * sector_tilt
    else:
        sector_tilt = pd.Series(0.0, index=idx, dtype=float)
        final = comp_neutral.copy()

    if is_nr_mode:
        # No within-sector re-neutralisation: rank directly on the raw composite.
        final = comp_series.copy()
    elif mode.lower() == "hierarchical_cov":
        # Mild coverage shrinkage (MASTER_SPEC 6.4 D7): a composite built from fewer of
        # the applicable factors counts for less. n_applicable_factors (D, computed per
        # security above) already excludes nonfinancial-only factors for Financial
        # Services names, matching the coverage-share denominator.
        n_used_series = pd.Series(n_factors_used, index=idx, dtype=float)
        n_app_series = pd.Series(n_applicable, index=idx, dtype=float)
        with np.errstate(invalid="ignore", divide="ignore"):
            cov_scale = np.sqrt(n_used_series / n_app_series)
        final = comp_neutral * cov_scale

    final = final.where(scored_series == 1, np.nan)

    # Initial rankings on scored names
    scored_sids = [sid for sid in idx if scored_series.loc[sid] == 1 and pd.notna(final.loc[sid])]
    scored_sids.sort(key=lambda s: (-final.loc[s], s))

    rank_all = pd.Series(np.nan, index=idx, dtype=float)
    for r, sid in enumerate(scored_sids, start=1):
        rank_all.loc[sid] = r

    # Group rank
    rank_group = pd.Series(np.nan, index=idx, dtype=float)
    for grp_name, g_sids in pd.Series(idx, index=idx).groupby(groups_aligned):
        g_scored = [sid for sid in g_sids if sid in scored_sids]
        g_scored.sort(key=lambda s: (-final.loc[s], s))
        for r, sid in enumerate(g_scored, start=1):
            rank_group.loc[sid] = r

    # Provisional quantiles
    n_scored = len(scored_sids)
    quintile = pd.Series(np.nan, index=idx, dtype=float)
    decile = pd.Series(np.nan, index=idx, dtype=float)
    if n_scored > 0:
        for r, sid in enumerate(scored_sids, start=1):
            rank_asc = n_scored - r + 1
            quintile.loc[sid] = min(5, 1 + math.floor((rank_asc - 1) * 5 / n_scored))
            decile.loc[sid] = min(10, 1 + math.floor((rank_asc - 1) * 10 / n_scored))

    return pd.DataFrame(
        {
            "family_scores_json": pd.Series(family_scores_json, index=idx),
            "composite": comp_series,
            "composite_neutral": comp_neutral,
            "sector_tilt": sector_tilt,
            "final": final,
            "scored": scored_series,
            "eligible": scored_series.copy(),
            "exclusion_reason": pd.Series(exclusion_reasons, index=idx),
            "n_factors_used": pd.Series(n_factors_used, index=idx, dtype=int),
            "rank_all": rank_all,
            "rank": rank_all.copy(),
            "rank_group": rank_group,
            "decile": decile,
            "quintile": quintile,
            "sector_group": groups_aligned,
        },
        index=idx,
    )
