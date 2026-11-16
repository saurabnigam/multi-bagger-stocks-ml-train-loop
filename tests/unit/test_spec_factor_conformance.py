"""Every launch factor's code matches MASTER_SPEC 5.3's launch-set table.

Real-world motivation: MASTER_SPEC 5.3 is the normative, human-editable source for each
launch factor's family, raw direction, horizon, launch status (active vs shadow) and
formula. `quant/factors/registry.py`'s `LAUNCH_FACTOR_CLASSES` and `ACTIVE_LAUNCH_FACTORS`
are a second, independently-maintained copy of the same facts, and nothing enforces that a
hand-edit to one is mirrored in the other. A single flipped sign here is exactly the kind of
change a reviewer skims past: e.g. vol_252 (low_risk, `direction=-1` -- low historical
volatility is supposed to be the *good* side of the low-risk anomaly, MASTER_SPEC 5.3 and
Baker/Bradley/Wurgler 2011) silently becoming `direction=+1` would flip every published
cohort's low_risk hypothesis to reward high volatility as if it were safety, while every
other launch invariant (family, horizon, active/shadow status) stays intact -- nothing else
in the test suite parses the spec table and compares it field-by-field against the registry,
so this would ship unnoticed.

This test parses the actual markdown table out of docs/spec/MASTER_SPEC.md (never a
hand-copied transcription of it) and checks it against `quant.factors.registry` for every
factor class in `LAUNCH_FACTOR_CLASSES`.

Formula comparison note: the spec's "Raw formula" column and each FactorSpec.formula string
agree on the mathematical content but are not always byte-identical -- e.g. mom_12_1's spec
cell reads ">=253 bars" while the code reads ">= 253 bars", and roce's spec cell reads
"denominator >0" while the code reads "denominator > 0". Three shadow/diagnostic factors
(mom_6_1, rev_1m, max_ret_21) also carry a "; requires >= N bars" data-sufficiency clause in
code that the spec table's cell omits entirely, even though the same clause is present for
other rows (mom_12_1, vol_252). None of this is a hypothesis or formula disagreement, so the
comparison here normalizes whitespace and drops any trailing "; requires ..." clause before
comparing, which is enough to catch an actual formula-content change without tripping on
these known formatting quirks.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quant.factors.registry import ACTIVE_LAUNCH_FACTORS, LAUNCH_FACTOR_CLASSES

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "spec" / "MASTER_SPEC.md"

_DIRECTION_MAP = {"+1": 1, "-1": -1, "0": 0}


def _parse_launch_table() -> dict:
    """Parse MASTER_SPEC 5.3's launch-set table into {factor_id: {field: value}}.

    Reads the markdown table's rows directly (not a copy pasted into this file), so a spec
    edit is picked up the next time this test runs without anyone touching test code.
    """
    text = SPEC_PATH.read_text()
    start = text.index("### 5.3 Launch set and formulas")
    end = text.index("### 5.4", start)
    block = text[start:end]

    rows = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| ID"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        assert len(cols) == 4, f"expected 4 columns in launch-table row, got {len(cols)}: {line!r}"
        factor_id, fam_dir_hor, role, formula = cols
        parts = [p.strip() for p in fam_dir_hor.split(";")]
        assert len(parts) == 3, f"expected 'family; direction; horizon' in {fam_dir_hor!r} for {factor_id}"
        family, direction, horizon = parts
        rows[factor_id] = {
            "family": family,
            "direction": _DIRECTION_MAP[direction],
            "horizon_m": int(horizon),
            "role": role,
            "formula": formula,
        }
    assert rows, "parsed zero rows out of MASTER_SPEC 5.3 -- table heading or fence text moved"
    return rows


def _normalize_formula(formula: str) -> str:
    """Strip an optional trailing '; requires ...' clause and all whitespace (see module docstring)."""
    core = formula.split("; requires")[0]
    return re.sub(r"\s+", "", core)


SPEC_ROWS = _parse_launch_table()


def test_every_spec_row_has_a_registry_class_and_vice_versa():
    spec_ids = set(SPEC_ROWS)
    class_ids = set(LAUNCH_FACTOR_CLASSES)
    missing_classes = spec_ids - class_ids
    missing_spec_rows = class_ids - spec_ids
    assert not missing_classes, f"MASTER_SPEC 5.3 rows with no LAUNCH_FACTOR_CLASSES entry: {sorted(missing_classes)}"
    assert not missing_spec_rows, f"LAUNCH_FACTOR_CLASSES entries with no MASTER_SPEC 5.3 row: {sorted(missing_spec_rows)}"


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_family_matches_spec(factor_id):
    spec = LAUNCH_FACTOR_CLASSES[factor_id]().spec
    row = SPEC_ROWS[factor_id]
    assert spec.family == row["family"], (
        f"{factor_id}: registry family {spec.family!r} != MASTER_SPEC 5.3 family {row['family']!r}"
    )


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_direction_matches_spec(factor_id):
    """Must catch M11: vol_252's raw direction flipped from -1 (low vol is good) to +1."""
    spec = LAUNCH_FACTOR_CLASSES[factor_id]().spec
    row = SPEC_ROWS[factor_id]
    assert spec.direction == row["direction"], (
        f"{factor_id}: registry direction {spec.direction!r} != MASTER_SPEC 5.3 direction {row['direction']!r}"
    )


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_horizon_matches_spec(factor_id):
    spec = LAUNCH_FACTOR_CLASSES[factor_id]().spec
    row = SPEC_ROWS[factor_id]
    assert spec.horizon_m == row["horizon_m"], (
        f"{factor_id}: registry horizon_m {spec.horizon_m!r} != MASTER_SPEC 5.3 horizon {row['horizon_m']!r}"
    )


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_launch_status_matches_spec(factor_id):
    """MASTER_SPEC 5.3's role column marks a row 'active' (optionally with a suffix such as
    ', nonfinancial' or ' when covered') or something else entirely (shadow, shadow
    diagnostic, diagnostic never weighted); registry.sync treats a factor as launched active
    exactly when its id is in ACTIVE_LAUNCH_FACTORS, everything else lands shadow.
    """
    row = SPEC_ROWS[factor_id]
    spec_says_active = row["role"].strip().lower().startswith("active")
    registry_says_active = factor_id in ACTIVE_LAUNCH_FACTORS
    assert spec_says_active == registry_says_active, (
        f"{factor_id}: MASTER_SPEC 5.3 role {row['role']!r} implies "
        f"active={spec_says_active}, but ACTIVE_LAUNCH_FACTORS says active={registry_says_active}"
    )


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_financial_applicability_matches_spec(factor_id):
    """A role column containing 'nonfinancial' means Financial Services names are structurally
    excluded (applies_to_financials=False); every other role means the factor applies to
    financials too (applies_to_financials=True).
    """
    spec = LAUNCH_FACTOR_CLASSES[factor_id]().spec
    row = SPEC_ROWS[factor_id]
    spec_is_nonfinancial_only = "nonfinancial" in row["role"].lower()
    assert spec.applies_to_financials == (not spec_is_nonfinancial_only), (
        f"{factor_id}: MASTER_SPEC 5.3 role {row['role']!r} implies "
        f"applies_to_financials={not spec_is_nonfinancial_only}, but registry has "
        f"applies_to_financials={spec.applies_to_financials}"
    )


@pytest.mark.parametrize("factor_id", sorted(LAUNCH_FACTOR_CLASSES))
def test_formula_matches_spec(factor_id):
    spec = LAUNCH_FACTOR_CLASSES[factor_id]().spec
    row = SPEC_ROWS[factor_id]
    assert spec.formula, f"{factor_id}: FactorSpec.formula is empty"
    code_formula = _normalize_formula(spec.formula)
    spec_formula = _normalize_formula(row["formula"])
    assert code_formula == spec_formula, (
        f"{factor_id}: registry formula {spec.formula!r} does not match "
        f"MASTER_SPEC 5.3 formula {row['formula']!r} (normalized: {code_formula!r} != {spec_formula!r})"
    )
