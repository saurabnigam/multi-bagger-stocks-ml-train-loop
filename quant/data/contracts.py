"""Data quality contracts, bounds checking, masking and PSI drift detection."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from quant.config import Config
from quant.types import Check


def field_contracts(cfg: Config) -> Dict[str, Dict[str, Any]]:
    """Load dictionary of field contracts from configuration."""
    # Look for config/field_contracts_v1.json relative to config root or repo root
    contract_path = cfg._root / "config" / "field_contracts_v1.json"
    if not contract_path.exists():
        contract_path = Path("config/field_contracts_v1.json")

    with open(contract_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return data


def check(values: pd.Series, contract: Dict[str, Any]) -> Tuple[pd.Series, Check]:
    """Validate series against field bounds; returns masked copy and check result.
    
    The original input values series remains strictly unmodified.
    Violators of [min_value, max_value] are set to NaN in the returned copy.
    """
    field_name = contract.get("field", values.name or "field")
    min_val = contract.get("min_value")
    max_val = contract.get("max_value")
    max_null_rate = float(contract.get("max_null_rate", 1.0))

    # Mask invalid values in a copy
    masked = values.copy()

    violators = 0
    if min_val is not None:
        under = values < min_val
        violators += int(under.sum())
        masked[under] = np.nan

    if max_val is not None:
        over = values > max_val
        violators += int(over.sum())
        masked[over] = np.nan

    n_total = len(values)
    null_count = int(masked.isna().sum())
    null_rate = float(null_count / n_total) if n_total > 0 else 0.0

    # Gate G6: unit bounds: BLOCK if > 5 violators per field
    if violators > 5:
        chk = Check(
            id=f"contract_{field_name}",
            status="FAIL",
            observed=float(null_rate),
            expected=max_null_rate,
            reason=f"{violators} violators exceed unit bounds [{min_val}, {max_val}] (>5 limit)",
            blocking=True,
        )
    elif null_rate > max_null_rate:
        chk = Check(
            id=f"contract_{field_name}",
            status="FAIL",
            observed=float(null_rate),
            expected=max_null_rate,
            reason=f"Null rate {null_rate:.2%} exceeds max allowed {max_null_rate:.2%}",
            blocking=True,
        )
    else:
        chk = Check(
            id=f"contract_{field_name}",
            status="PASS",
            observed=float(null_rate),
            expected=max_null_rate,
            reason=f"Field {field_name} passed bounds check ({violators} violators masked, null rate {null_rate:.2%})",
            blocking=False,
        )

    return masked, chk


def psi(
    current: pd.Series,
    reference: pd.Series,
    edges: List[float],
    eps: float = 1e-6,
) -> Optional[float]:
    """Calculate Population Stability Index (PSI) between current and reference.
    
    Uses fixed bin edges. Empty/zero bins are handled using documented epsilon 1e-6.
    Returns None if either series has no finite/non-null observations.
    """
    if current is None or reference is None:
        return None

    cur_clean = current.dropna()
    ref_clean = reference.dropna()

    if len(cur_clean) == 0 or len(ref_clean) == 0:
        return None

    if not edges:
        return None

    clean_edges = sorted(list(set(edges)))
    bin_edges = [-np.inf] + [e for e in clean_edges if np.isfinite(e)] + [np.inf]

    cur_cut = pd.cut(cur_clean, bins=bin_edges, include_lowest=True)
    ref_cut = pd.cut(ref_clean, bins=bin_edges, include_lowest=True)

    cur_counts = cur_cut.value_counts(sort=False)
    ref_counts = ref_cut.value_counts(sort=False)

    cur_total = len(cur_clean)
    ref_total = len(ref_clean)

    psi_total = 0.0
    for b in cur_counts.index:
        p_act = cur_counts[b] / cur_total
        p_exp = ref_counts[b] / ref_total

        if p_act == 0.0:
            p_act = eps
        if p_exp == 0.0:
            p_exp = eps

        psi_total += (p_act - p_exp) * np.log(p_act / p_exp)

    return float(psi_total)


def check_drift(
    field: str,
    current: pd.Series,
    reference: Optional[pd.Series],
    edges: List[float],
    threshold: float = 0.25,
) -> Check:
    """Check distribution drift using PSI against reference distribution."""
    if reference is None or (isinstance(reference, pd.Series) and reference.dropna().empty):
        return Check(
            id=f"psi_drift_{field}",
            status="DEFERRED",
            observed=None,
            expected=threshold,
            reason=f"First baseline for {field}: prior reference distribution absent, drift deferred",
            blocking=False,
        )

    score = psi(current, reference, edges)
    if score is None:
        return Check(
            id=f"psi_drift_{field}",
            status="DEFERRED",
            observed=None,
            expected=threshold,
            reason=f"Insufficient observations for {field} PSI calculation",
            blocking=False,
        )

    if score > threshold:
        return Check(
            id=f"psi_drift_{field}",
            status="FAIL",
            observed=score,
            expected=threshold,
            reason=f"Field {field} PSI {score:.4f} > {threshold:.4f} indicates material distribution shift",
            blocking=False,
        )

    return Check(
        id=f"psi_drift_{field}",
        status="PASS",
        observed=score,
        expected=threshold,
        reason=f"Field {field} distribution stable (PSI {score:.4f} <= {threshold:.4f})",
        blocking=False,
    )
