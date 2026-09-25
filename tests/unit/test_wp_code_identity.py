"""Tests for WP codeid (T3/D5): factor code identity over source and helpers; G9 scoped to
live factor ids.

MASTER_SPEC 5.1 requires ``code_sha256`` to pin the factor's code and its shared-helper
dependencies, not only its spec text, so a helper change under an unbumped version is
caught (``registry.sync`` already refuses a changed hash with
``CODE_CHANGED_WITHOUT_VERSION_BUMP``). MASTER_SPEC 4.6 (G9) then must not fail a replay
solely because a prior cohort's factor was retired or superseded by a version bump.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from quant.errors import Refused
from quant.factors import registry
from quant.factors.base import Factor, FactorSpec
from quant.factors.inputs import FactorInputs
from quant.knowledge import bootstrap as bootstrap_mod
from quant.run import _g9_replay
from quant.types import Draft

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec(name="wp_test_factor", version=1, formula="raw", inputs=("x",)) -> FactorSpec:
    return FactorSpec(
        name=name, version=version, family="growth", direction=1, horizon_m=3,
        hypothesis="H_WP_TEST", formula=formula, inputs=inputs, lookback_days=10,
        applies_to_financials=True, level="stock", backfillable=False,
        min_coverage=0.5, evidence="test fixture", hypothesis_id="",
    )


class _AttrFactorV1(Factor):
    """Reads FactorInputs.attribute -- used to probe both class-source and helper-map drift."""

    def __init__(self):
        super().__init__(_spec())

    def compute(self, inputs: FactorInputs) -> pd.Series:
        return inputs.attribute("market_cap_inr")


class _AttrFactorV2(Factor):
    """Same spec text as _AttrFactorV1; compute() body differs."""

    def __init__(self):
        super().__init__(_spec())

    def compute(self, inputs: FactorInputs) -> pd.Series:
        mcap = inputs.attribute("market_cap_inr")
        return mcap * 2.0


# --------------------------------------------------------------------------- code identity v2

def test_hash_changes_when_factor_class_source_changes(monkeypatch):
    monkeypatch.setitem(registry.LAUNCH_FACTOR_CLASSES, "wp_test_factor", _AttrFactorV1)
    h1 = registry._calc_code_sha(_spec())

    monkeypatch.setitem(registry.LAUNCH_FACTOR_CLASSES, "wp_test_factor", _AttrFactorV2)
    h2 = registry._calc_code_sha(_spec())

    assert h1 != h2


def test_hash_changes_when_mapped_helper_source_changes(monkeypatch):
    """Pointing the static map at a temporary function moves the hash, without touching the class."""
    monkeypatch.setitem(registry.LAUNCH_FACTOR_CLASSES, "wp_test_factor", _AttrFactorV1)
    spec = _spec()

    def _temp_helper_a(field):
        return field

    def _temp_helper_b(field, extra=1):
        return field

    monkeypatch.setitem(registry.ACCESSOR_HELPER_MAP, "attribute", (_temp_helper_a,))
    h1 = registry._calc_code_sha(spec)

    monkeypatch.setitem(registry.ACCESSOR_HELPER_MAP, "attribute", (_temp_helper_b,))
    h2 = registry._calc_code_sha(spec)

    assert h1 != h2


def test_hash_deterministic_across_subprocess_hashseeds():
    """No object ids or dict-iteration nondeterminism reach the hash (PYTHONHASHSEED-independent)."""
    code = (
        "from quant.factors import registry\n"
        "spec = registry.LAUNCH_FACTOR_CLASSES['roce']().spec\n"
        "print(registry._calc_code_sha(spec))\n"
    )
    outputs = []
    for seed in ("1", "99999"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, check=True,
        )
        outputs.append(res.stdout.strip())
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 64


def test_accessor_helper_map_covers_every_used_public_method():
    """A new FactorInputs accessor used by a registered factor cannot silently escape hashing."""
    used: set[str] = set()
    for cls in registry.LAUNCH_FACTOR_CLASSES.values():
        used |= set(registry._inputs_accessor_calls(cls))
    assert used, "sanity: launch factors should call at least one FactorInputs accessor"
    missing = used - set(registry.ACCESSOR_HELPER_MAP.keys())
    assert not missing, f"ACCESSOR_HELPER_MAP does not cover accessors: {sorted(missing)}"


def test_bootstrap_reuses_single_registry_function_no_duplicate():
    assert bootstrap_mod._calc_code_sha is registry._calc_code_sha


def test_sync_repins_v1_row_once_and_refuses_later_drift(ctx):
    spec = registry.LAUNCH_FACTOR_CLASSES["roce"]().spec
    v1_sha = registry._calc_code_sha_v1(spec)
    v2_sha = registry._calc_code_sha(spec)
    assert v1_sha != v2_sha
    now = ctx.clock.iso()

    ctx.conn.execute(
        """
        INSERT INTO factor_registry (
            factor_id, name, version, family, direction, horizon_m, level,
            hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
            backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
            module_path, status, registered_on, status_changed_on
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'active', ?, ?)
        """,
        (
            spec.factor_id, spec.name, spec.version, spec.family, spec.direction, spec.horizon_m,
            spec.level, spec.hypothesis, spec.formula, json.dumps(list(spec.inputs)),
            spec.lookback_days, int(spec.applies_to_financials), int(spec.backfillable),
            spec.min_coverage, spec.evidence, v1_sha, "quant.factors.quality.Roce", now, now,
        ),
    )

    res = registry.sync(ctx, [spec])
    assert res.counts["repinned"] == 1
    assert res.details["repinned"] == [spec.factor_id]

    row = ctx.conn.execute(
        "SELECT code_sha256 FROM factor_registry WHERE factor_id = ?", (spec.factor_id,)
    ).fetchone()
    assert row["code_sha256"] == v2_sha

    decision = ctx.conn.execute(
        "SELECT status, tier, approver_kind FROM decisions WHERE decision_id = 'DEC_CODE_IDENTITY_V2'"
    ).fetchone()
    assert decision is not None
    assert decision["tier"] == 0
    assert decision["approver_kind"] == "system"

    dq = ctx.conn.execute(
        "SELECT detail_json FROM data_quality_events WHERE code = 'CODE_IDENTITY_REPIN'"
    ).fetchall()
    assert len(dq) == 1
    assert spec.factor_id in dq[0]["detail_json"]

    # already v2: a second sync is a no-op, no duplicate decision/event
    res2 = registry.sync(ctx, [spec])
    assert res2.counts["repinned"] == 0
    dq_after = ctx.conn.execute(
        "SELECT COUNT(*) as n FROM data_quality_events WHERE code = 'CODE_IDENTITY_REPIN'"
    ).fetchone()
    assert dq_after["n"] == 1

    # genuine drift (neither v1 nor v2) still refuses
    ctx.conn.execute(
        "UPDATE factor_registry SET code_sha256 = 'deadbeef00000000000000000000000000000000000000000000000000dead' "
        "WHERE factor_id = ?", (spec.factor_id,),
    )
    with pytest.raises(Refused) as exc_info:
        registry.sync(ctx, [spec])
    assert "CODE_CHANGED_WITHOUT_VERSION_BUMP" in str(exc_info.value)


# --------------------------------------------------------------------------- G9 scoping

def _seed_prior_cohort_with_two_factors(ctx) -> None:
    now = ctx.clock.iso()
    ctx.conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'SYN1WP', 'WP Synthetic', '2026-01-01', '2026-09-30', 'listed')"
    )
    for fid, status in [("retired_factor@1", "retired"), ("roce@1", "active")]:
        ctx.conn.execute(
            """
            INSERT INTO factor_registry (
                factor_id, name, version, family, direction, horizon_m, level,
                hypothesis, formula, inputs_json, lookback_days, applies_to_financials,
                backfillable, min_coverage, evidence, hypothesis_id, code_sha256,
                module_path, status, registered_on, status_changed_on
            ) VALUES (?, ?, 1, 'growth', 1, 3, 'stock', 'H', 'f', '[]', 10, 1, 0, 0.5, 'e', NULL, 'sha', 'mod', ?, ?, ?)
            """,
            (fid, fid.split("@")[0], status, now, now),
        )
    ctx.conn.execute(
        """
        INSERT INTO cohorts (
            cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash,
            source_refs_json, published_at, generated_at, is_clean, run_id
        ) VALUES ('C-PRIOR', '2026-08-31', 'live', '2026-08-31T18:29:59.999999Z', 'defprior',
                  'memprior', '{}', ?, ?, 1, ?)
        """,
        (now, now, ctx.run_id),
    )
    for fid, z in [("retired_factor@1", 0.5), ("roce@1", 1.23)]:
        ctx.conn.execute(
            """
            INSERT INTO factor_values (
                cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group,
                flags, input_refs_json, track, run_id
            ) VALUES ('C-PRIOR', '2026-08-31', 1, ?, NULL, NULL, ?, 'Technology', '', '{}', 'live', ?)
            """,
            (fid, z, ctx.run_id),
        )


def _new_draft() -> Draft:
    return Draft(
        cohort_id="C-NEW", as_of="2026-09-30", track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z", definition_hash="defnew",
    )


def test_g9_skips_superseded_factor_and_still_fails_on_genuine_mismatch(ctx, monkeypatch):
    """retired_factor@1 is not recomputed (retired); roce@1 is, and its value has drifted."""
    _seed_prior_cohort_with_two_factors(ctx)

    def fake_compute_all(_ctx, _draft):
        return pd.DataFrame([{"security_id": 1, "factor_id": "roce@1", "z": 9.99}])

    monkeypatch.setattr("quant.factors.registry.compute_all", fake_compute_all)

    check = _g9_replay(ctx, _new_draft())

    assert check.status == "FAIL"
    assert check.blocking is True
    assert check.observed["not_replayed"] == ["retired_factor@1"]
    assert check.observed["mismatches"] == 1


def test_g9_passes_when_only_the_superseded_factor_is_absent(ctx, monkeypatch):
    """Same setup, but the recomputed value for roce@1 matches exactly: PASS, not blocked."""
    _seed_prior_cohort_with_two_factors(ctx)

    def fake_compute_all(_ctx, _draft):
        return pd.DataFrame([{"security_id": 1, "factor_id": "roce@1", "z": 1.23}])

    monkeypatch.setattr("quant.factors.registry.compute_all", fake_compute_all)

    check = _g9_replay(ctx, _new_draft())

    assert check.status == "PASS"
    assert check.blocking is False
    assert check.observed["not_replayed"] == ["retired_factor@1"]
    assert check.observed["mismatches"] == 0


def test_g9_fails_when_a_live_factor_is_not_recomputed(ctx, monkeypatch):
    """Review finding: an id the registry still lists as active but the replay did not produce
    (e.g. a class-resolution bug) must fail the gate instead of passing as 'not replayed'."""
    _seed_prior_cohort_with_two_factors(ctx)

    def fake_compute_all(_ctx, _draft):
        return pd.DataFrame(columns=["security_id", "factor_id", "z"])

    monkeypatch.setattr("quant.factors.registry.compute_all", fake_compute_all)

    check = _g9_replay(ctx, _new_draft())

    assert check.status == "FAIL" and check.blocking is True
    assert check.observed["missing_live"] == ["roce@1"]
    assert check.observed["not_replayed"] == ["retired_factor@1"]


def test_monthly_staging_refuses_a_factor_changed_without_version_bump(ctx, monkeypatch):
    """Integration finding: registry.sync was never called outside tests, so the D5 check never
    ran in production. _stage_and_publish now syncs first and refuses on drift before gates or
    factor computation run."""
    from quant import run as run_mod

    registry.sync(ctx, registry.launch_specs())
    ctx.conn.execute("UPDATE factor_registry SET code_sha256 = 'drifted' WHERE factor_id = 'roce@1'")
    called = {"gates": False}
    monkeypatch.setattr("quant.data.gates.run", lambda *a, **k: called.__setitem__("gates", True))
    with pytest.raises(Refused) as exc_info:
        run_mod._stage_and_publish(ctx, _new_draft(), stop_after="gates")
    assert "CODE_CHANGED_WITHOUT_VERSION_BUMP" in str(exc_info.value)
    assert called["gates"] is False
