"""Evaluation metrics: Rank IC, within-group quintiles, and partial IC (C07)."""

from __future__ import annotations

import math
import numpy as np
import pandas as pd
import scipy.stats


def rank_ic(score: pd.Series, label: pd.Series) -> tuple[float | None, int, str]:
    """Calculate Spearman rank correlation between score and label.

    Returns (ic, n, status) where status is 'ok', 'insufficient', or 'constant'.
    """
    df = pd.DataFrame({"score": score, "label": label}).dropna()
    n = len(df)
    if n < 2:
        return (None, n, "insufficient")
    if (df["score"] == df["score"].iloc[0]).all() or (df["label"] == df["label"].iloc[0]).all():
        return (None, n, "constant")

    res = scipy.stats.spearmanr(df["score"], df["label"])
    stat = float(res.statistic)
    if math.isnan(stat):
        return (None, n, "constant")
    return (stat, n, "ok")


def quintiles(score: pd.Series, returns: pd.Series, groups: pd.Series) -> pd.DataFrame:
    """Compute within-group quintiles and pool statistics across all groups.

    Returns DataFrame with columns ['q', 'n', 'mean', 'median', 'trimmed_mean'].
    """
    df = pd.DataFrame({"score": score, "returns": returns, "group": groups}).dropna()
    df["q"] = 0

    for _, grp_df in df.groupby("group"):
        n_grp = len(grp_df)
        if n_grp == 0:
            continue
        ranks_asc = grp_df["score"].rank(method="first", ascending=True)
        q_vals = np.minimum(5, 1 + np.floor((ranks_asc - 1) * 5.0 / n_grp)).astype(int)
        df.loc[grp_df.index, "q"] = q_vals

    rows = []
    for q in range(1, 6):
        q_rets = df.loc[df["q"] == q, "returns"]
        n_q = len(q_rets)
        if n_q > 0:
            mean_val = float(q_rets.mean())
            median_val = float(q_rets.median())
            trimmed_mean_val = float(scipy.stats.trim_mean(q_rets.values, 0.05))
        else:
            mean_val = None
            median_val = None
            trimmed_mean_val = None
        rows.append({
            "q": q,
            "n": n_q,
            "mean": mean_val,
            "median": median_val,
            "trimmed_mean": trimmed_mean_val,
        })

    return pd.DataFrame(rows)


def partial_ic(
    candidate: pd.Series,
    active: pd.DataFrame,
    label: pd.Series,
) -> tuple[float | None, int, str]:
    """Calculate partial IC of candidate after regressing on active factors.

    Regresses candidate on active factors (+ intercept) and computes Rank IC of residuals vs label.
    """
    if active is None or active.empty or len(active.columns) == 0:
        return rank_ic(candidate, label)

    combined = pd.concat([candidate.rename("cand"), active, label.rename("label")], axis=1).dropna()
    n = len(combined)
    k = len(active.columns)
    if n <= k + 1:
        return (None, n, "insufficient")

    y = combined["cand"].values
    if np.all(y == y[0]):
        return (None, n, "constant")

    X = combined[active.columns].values
    X_with_intercept = np.column_stack([np.ones(n), X])

    try:
        beta, _, _, _ = np.linalg.lstsq(X_with_intercept, y, rcond=None)
        residuals = y - X_with_intercept @ beta
    except Exception:
        return (None, n, "insufficient")

    return rank_ic(pd.Series(residuals, index=combined.index), combined["label"])
