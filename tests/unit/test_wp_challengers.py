"""WP challengers (MASTER_SPEC 6.4 decisions D6/D7, task T6).

Registers two challenger models against the frozen champion EW_HIER_v1:
  - EW_HIER_NR_v1  (composite.py mode "hierarchical_nr"): same family/composite math as
    the champion, but ranks on the raw composite instead of composite_neutral, so a
    sector's group size no longer sets a ceiling on how high its best name can score
    (docs/analysis/verification_2026-09-24.md, decision D6).
  - EW_HIER_COV_v1 (composite.py mode "hierarchical_cov"): shrinks composite_neutral by
    sqrt(n_factors_used / n_applicable_factors), so a composite built from fewer of the
    applicable factors counts for less (decision D7).

Also covers quant.model.models.seed() registering both as challengers on a fresh
install, and register_challenger() for adding one to an existing, already-bootstrapped
database under a governed decision.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from quant.config import Config, load as load_config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.model.composite import compose
from quant.model.models import CHALLENGER_DEFINITIONS, register_challenger, seed
from quant.run import RunContext
from quant.types import Actor, FrozenClock


# ============================================================================ helpers

def _fresh_ctx(cfg: Config, actor_kind: str = "system", actor_name: str = "pytest",
               as_of: str = "2026-09-30") -> RunContext:
    """A RunContext over a freshly schema'd database, ready to enter with `with`."""
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    conn.close()
    return RunContext(
        as_of=as_of, kind="test", track="live", cfg=cfg,
        clock=FrozenClock("2026-10-01T00:00:00.000000Z"),
        actor=Actor(kind=actor_kind, name=actor_name),
    )


def _insert_decision(conn, decision_id: str, tier: int, approver_kind: str, status: str = "approved") -> None:
    conn.execute(
        "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
        "evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) VALUES "
        "(?, 'model_promotion', ?, ?, 'register challenger', 'ctx', '[]', 'approve', '[]', "
        "'2026-09-24T00:00:00.000000Z', ?, ?, ?, 'k/d.md', 'g')",
        (decision_id, tier, decision_id, f"{approver_kind}:owner", approver_kind, status),
    )


# ============================================================================ golden fixture (composite.py)
#
# Captured from `compose()` on the base code (before this work package touched
# composite.py), for the three modes that already existed: hierarchical (the champion,
# and IC_SHRUNK_v1's math), flat (EW_FLAT_v1) and mom_only (MOM_ONLY_v1). Column order
# is fixed; family_scores_json is the last entry of each row.
_GOLDEN_JSON = r"""
{"hierarchical":{"cols":["composite","composite_neutral","sector_tilt","final","scored","eligible","exclusion_reason","n_factors_used","rank_all","rank","rank_group","decile","quintile","sector_group","family_scores_json"],"rows":{"1":[0.575,1.465233793,0.0,1.465233793,"1","1",null,"7",1.0,1.0,1.0,10.0,5.0,"A","{\"growth\": 0.9, \"momentum\": 0.9, \"quality\": 0.15, \"value\": 0.35}"],"2":[-0.025,-0.366106357,0.0,-0.366106357,"1","1",null,"7",8.0,8.0,5.0,4.0,2.0,"A","{\"growth\": -0.4, \"momentum\": 0.4, \"quality\": 0.05000000000000002, \"value\": -0.15000000000000002}"],"3":[0.1125,-0.0,0.0,-0.0,"1","1",null,"7",7.0,7.0,4.0,5.0,3.0,"A","{\"growth\": 0.2, \"momentum\": -0.4, \"quality\": 0.09999999999999998, \"value\": 0.55}"],"4":[-0.266666667,-1.465233793,0.0,-1.465233793,"1","1",null,"5",12.0,12.0,7.0,1.0,1.0,"A","{\"growth\": -0.6, \"momentum\": 0.15000000000000002, \"value\": -0.35}"],"5":[0.133333333,0.366106357,0.0,0.366106357,"1","1",null,"5",5.0,5.0,3.0,6.0,3.0,"A","{\"growth\": 0.5, \"momentum\": -0.95, \"value\": 0.8500000000000001}"],"6":[-0.25,-0.791638608,0.0,-0.791638608,"1","1",null,"5",10.0,10.0,6.0,2.0,1.0,"A","{\"growth\": -0.9, \"momentum\": 0.7, \"value\": -0.55}"],"7":[0.233333333,0.791638608,0.0,0.791638608,"1","1",null,"5",3.0,3.0,2.0,8.0,4.0,"A","{\"growth\": 0.7, \"momentum\": -0.15000000000000002, \"value\": 0.15000000000000002}"],"8":[0.175,0.524400513,0.0,0.524400513,"1","1",null,"7",4.0,4.0,2.0,7.0,4.0,"B","{\"growth\": 0.4, \"momentum\": 0.4, \"quality\": 0.15000000000000002, \"value\": -0.25}"],"9":[-0.15,-1.281551566,0.0,-1.281551566,"1","1",null,"7",11.0,11.0,5.0,1.0,1.0,"B","{\"growth\": -0.3, \"momentum\": -0.6, \"quality\": -0.15, \"value\": 0.45}"],"10":[0.3125,1.281551566,0.0,1.281551566,"1","1",null,"7",2.0,2.0,1.0,9.0,5.0,"B","{\"growth\": 0.8, \"momentum\": 1.1, \"quality\": 0.10000000000000003, \"value\": -0.75}"],"11":[0.1125,0.0,0.0,0.0,"1","1",null,"7",6.0,6.0,3.0,6.0,3.0,"B","{\"growth\": -0.5, \"momentum\": 0.05, \"quality\": 0.25, \"value\": 0.6499999999999999}"],"12":[-0.0375,-0.524400513,0.0,-0.524400513,"1","1",null,"7",9.0,9.0,4.0,3.0,2.0,"B","{\"growth\": 0.1, \"momentum\": -0.30000000000000004, \"quality\": 0.1, \"value\": -0.05}"]}},"flat":{"cols":["composite","composite_neutral","sector_tilt","final","scored","eligible","exclusion_reason","n_factors_used","rank_all","rank","rank_group","decile","quintile","sector_group","family_scores_json"],"rows":{"1":[0.528571429,1.465233793,0.0,1.465233793,"1","1",null,"7",1.0,1.0,1.0,10.0,5.0,"A","{\"growth\": 0.9, \"momentum\": 0.9, \"quality\": 0.15, \"value\": 0.35}"],"2":[0.028571429,-0.366106357,0.0,-0.366106357,"1","1",null,"7",8.0,8.0,5.0,4.0,2.0,"A","{\"growth\": -0.4, \"momentum\": 0.4, \"quality\": 0.05000000000000002, \"value\": -0.15000000000000002}"],"3":[0.1,0.366106357,0.0,0.366106357,"1","1",null,"7",5.0,5.0,3.0,6.0,3.0,"A","{\"growth\": 0.2, \"momentum\": -0.4, \"quality\": 0.09999999999999998, \"value\": 0.55}"],"4":[-0.2,-1.465233793,0.0,-1.465233793,"1","1",null,"5",12.0,12.0,7.0,1.0,1.0,"A","{\"growth\": -0.6, \"momentum\": 0.15000000000000002, \"value\": -0.35}"],"5":[0.06,-0.0,0.0,-0.0,"1","1",null,"5",7.0,7.0,4.0,5.0,3.0,"A","{\"growth\": 0.5, \"momentum\": -0.95, \"value\": 0.8500000000000001}"],"6":[-0.12,-0.791638608,0.0,-0.791638608,"1","1",null,"5",10.0,10.0,6.0,2.0,1.0,"A","{\"growth\": -0.9, \"momentum\": 0.7, \"value\": -0.55}"],"7":[0.14,0.791638608,0.0,0.791638608,"1","1",null,"5",3.0,3.0,2.0,8.0,4.0,"A","{\"growth\": 0.7, \"momentum\": -0.15000000000000002, \"value\": 0.15000000000000002}"],"8":[0.142857143,0.0,0.0,0.0,"1","1",null,"7",6.0,6.0,3.0,6.0,3.0,"B","{\"growth\": 0.4, \"momentum\": 0.4, \"quality\": 0.15000000000000002, \"value\": -0.25}"],"9":[-0.128571429,-1.281551566,0.0,-1.281551566,"1","1",null,"7",11.0,11.0,5.0,1.0,1.0,"B","{\"growth\": -0.3, \"momentum\": -0.6, \"quality\": -0.15, \"value\": 0.45}"],"10":[0.242857143,1.281551566,0.0,1.281551566,"1","1",null,"7",2.0,2.0,1.0,9.0,5.0,"B","{\"growth\": 0.8, \"momentum\": 1.1, \"quality\": 0.10000000000000003, \"value\": -0.75}"],"11":[0.2,0.524400513,0.0,0.524400513,"1","1",null,"7",4.0,4.0,2.0,7.0,4.0,"B","{\"growth\": -0.5, \"momentum\": 0.05, \"quality\": 0.25, \"value\": 0.6499999999999999}"],"12":[-0.057142857,-0.524400513,0.0,-0.524400513,"1","1",null,"7",9.0,9.0,4.0,3.0,2.0,"B","{\"growth\": 0.1, \"momentum\": -0.30000000000000004, \"quality\": 0.1, \"value\": -0.05}"]}},"mom_only":{"cols":["composite","composite_neutral","sector_tilt","final","scored","eligible","exclusion_reason","n_factors_used","rank_all","rank","rank_group","decile","quintile","sector_group","family_scores_json"],"rows":{"1":[0.9,1.465233793,0.0,1.465233793,"1","1",null,"7",1.0,1.0,1.0,10.0,5.0,"A","{\"growth\": 0.9, \"momentum\": 0.9, \"quality\": 0.15, \"value\": 0.35}"],"2":[0.4,0.366106357,0.0,0.366106357,"1","1",null,"7",5.0,5.0,3.0,6.0,3.0,"A","{\"growth\": -0.4, \"momentum\": 0.4, \"quality\": 0.05000000000000002, \"value\": -0.15000000000000002}"],"3":[-0.4,-0.791638608,0.0,-0.791638608,"1","1",null,"7",10.0,10.0,6.0,2.0,1.0,"A","{\"growth\": 0.2, \"momentum\": -0.4, \"quality\": 0.09999999999999998, \"value\": 0.55}"],"4":[0.15,-0.0,0.0,-0.0,"1","1",null,"5",7.0,7.0,4.0,5.0,3.0,"A","{\"growth\": -0.6, \"momentum\": 0.15000000000000002, \"value\": -0.35}"],"5":[-0.95,-1.465233793,0.0,-1.465233793,"1","1",null,"5",12.0,12.0,7.0,1.0,1.0,"A","{\"growth\": 0.5, \"momentum\": -0.95, \"value\": 0.8500000000000001}"],"6":[0.7,0.791638608,0.0,0.791638608,"1","1",null,"5",3.0,3.0,2.0,8.0,4.0,"A","{\"growth\": -0.9, \"momentum\": 0.7, \"value\": -0.55}"],"7":[-0.15,-0.366106357,0.0,-0.366106357,"1","1",null,"5",8.0,8.0,5.0,4.0,2.0,"A","{\"growth\": 0.7, \"momentum\": -0.15000000000000002, \"value\": 0.15000000000000002}"],"8":[0.4,0.524400513,0.0,0.524400513,"1","1",null,"7",4.0,4.0,2.0,7.0,4.0,"B","{\"growth\": 0.4, \"momentum\": 0.4, \"quality\": 0.15000000000000002, \"value\": -0.25}"],"9":[-0.6,-1.281551566,0.0,-1.281551566,"1","1",null,"7",11.0,11.0,5.0,1.0,1.0,"B","{\"growth\": -0.3, \"momentum\": -0.6, \"quality\": -0.15, \"value\": 0.45}"],"10":[1.1,1.281551566,0.0,1.281551566,"1","1",null,"7",2.0,2.0,1.0,9.0,5.0,"B","{\"growth\": 0.8, \"momentum\": 1.1, \"quality\": 0.10000000000000003, \"value\": -0.75}"],"11":[0.05,0.0,0.0,0.0,"1","1",null,"7",6.0,6.0,3.0,6.0,3.0,"B","{\"growth\": -0.5, \"momentum\": 0.05, \"quality\": 0.25, \"value\": 0.6499999999999999}"],"12":[-0.3,-0.524400513,0.0,-0.524400513,"1","1",null,"7",9.0,9.0,4.0,3.0,2.0,"B","{\"growth\": 0.1, \"momentum\": -0.30000000000000004, \"quality\": 0.1, \"value\": -0.05}"]}}}
"""


def _golden_fixture():
    definitions = pd.DataFrame([
        {"factor_id": "mom_12_1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "trend_200", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "roce", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "accruals", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
        {"factor_id": "earnings_yield", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "book_to_price", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "eps_growth_3y", "family": "growth", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 2500, "quality": 2500, "value": 2500, "growth": 2500}
    sids = list(range(1, 13))
    groups = pd.Series((["A"] * 7) + (["B"] * 5), index=sids)
    z = pd.DataFrame({
        "mom_12_1":       [1.0, 0.5, -0.5, 0.2, -1.0, 0.8, -0.2, 0.3, -0.7, 1.2, 0.1, -0.4],
        "trend_200":      [0.8, 0.3, -0.3, 0.1, -0.9, 0.6, -0.1, 0.5, -0.5, 1.0, 0.0, -0.2],
        "earnings_yield": [0.4, -0.2, 0.6, -0.4, 0.9, -0.6, 0.2, -0.3, 0.5, -0.8, 0.7, -0.1],
        "book_to_price":  [0.3, -0.1, 0.5, -0.3, 0.8, -0.5, 0.1, -0.2, 0.4, -0.7, 0.6, 0.0],
        "eps_growth_3y":  [0.9, -0.4, 0.2, -0.6, 0.5, -0.9, 0.7, 0.4, -0.3, 0.8, -0.5, 0.1],
    }, index=sids)
    roce_fill = {1: 0.5, 2: -0.3, 3: 0.7, 8: 0.2, 9: -0.6, 10: 0.9, 11: -0.1, 12: 0.4}
    accr_fill = {1: -0.2, 2: 0.4, 3: -0.5, 8: 0.1, 9: 0.3, 10: -0.7, 11: 0.6, 12: -0.2}
    z["roce"] = pd.Series(roce_fill).reindex(sids)
    z["accruals"] = pd.Series(accr_fill).reindex(sids)
    return definitions, units, groups, z


@pytest.mark.parametrize("mode", ["hierarchical", "flat", "mom_only"])
def test_existing_models_produce_byte_identical_scores(cfg, mode):
    """Golden regression: EW_HIER_v1/IC_SHRUNK_v1 (hierarchical), EW_FLAT_v1 (flat) and
    MOM_ONLY_v1 (mom_only) must score exactly as before this work package touched
    composite.py to add the two new modes."""
    golden = json.loads(_GOLDEN_JSON)[mode]
    definitions, units, groups, z = _golden_fixture()

    out = compose(z, definitions, units, groups, cfg, mode=mode)

    for sid_str, expected_row in golden["rows"].items():
        sid = int(sid_str)
        for col, expected in zip(golden["cols"], expected_row):
            actual = out.loc[sid, col]
            if col == "family_scores_json":
                assert json.loads(actual) == json.loads(expected), f"{mode}/{sid}/{col}"
            elif isinstance(expected, float):
                if expected is None or (isinstance(actual, float) and pd.isna(actual)):
                    assert pd.isna(actual) == (expected is None), f"{mode}/{sid}/{col}"
                else:
                    assert actual == pytest.approx(expected, abs=1e-9), f"{mode}/{sid}/{col}"
            else:
                assert str(actual) == str(expected), f"{mode}/{sid}/{col}"


# ============================================================================ hierarchical_nr (D6)

def test_hierarchical_nr_final_equals_raw_composite():
    """final == composite everywhere a name is scored; composite_neutral is still stored."""
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 1

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    sids = list(range(1, 26))
    groups = pd.Series(["BIG"] * 20 + ["SMALL"] * 5, index=sids)
    big_z = list(np.linspace(-1.9, 1.0, 20))
    small_z = [-1.0, -0.5, 0.0, 0.5, 2.5]
    z = pd.DataFrame({"m1": big_z + small_z}, index=sids)

    out_nr = compose(z, definitions, units, groups, cfg, mode="hierarchical_nr")
    scored = out_nr[out_nr["scored"] == 1]
    assert len(scored) == 25
    for sid in scored.index:
        assert out_nr.loc[sid, "final"] == pytest.approx(out_nr.loc[sid, "composite"])
        # composite_neutral is still computed for diagnostics, not NaN-blanked by mode
        assert pd.notna(out_nr.loc[sid, "composite_neutral"])


def test_hierarchical_nr_has_no_sector_size_ceiling():
    """A small sector's genuinely higher raw composite beats a large sector's compressed
    ceiling under NR, even though the champion (composite_neutral) ranks it the other way
    round purely because of the 20-name sector's larger rank-normal quantile ceiling."""
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 1

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    sids = list(range(1, 26))
    groups = pd.Series(["BIG"] * 20 + ["SMALL"] * 5, index=sids)
    # BIG sector's top (sid=20) has raw composite 1.0; SMALL sector's top (sid=25) has a
    # genuinely higher raw composite of 2.5, but n=5 caps its rank-normal quantile lower
    # than the n=20 sector's top quantile.
    big_z = list(np.linspace(-1.9, 1.0, 20))
    small_z = [-1.0, -0.5, 0.0, 0.5, 2.5]
    z = pd.DataFrame({"m1": big_z + small_z}, index=sids)
    big_top, small_top = 20, 25
    assert z.loc[small_top, "m1"] > z.loc[big_top, "m1"]

    out_champion = compose(z, definitions, units, groups, cfg, mode="hierarchical")
    assert out_champion.loc[big_top, "composite_neutral"] > out_champion.loc[small_top, "composite_neutral"]
    assert out_champion.loc[big_top, "rank_all"] < out_champion.loc[small_top, "rank_all"]

    out_nr = compose(z, definitions, units, groups, cfg, mode="hierarchical_nr")
    assert out_nr.loc[small_top, "final"] > out_nr.loc[big_top, "final"]
    assert out_nr.loc[small_top, "rank_all"] < out_nr.loc[big_top, "rank_all"]


def test_hierarchical_nr_tiebreak_is_composite_then_security_id():
    """Ties in final (== composite) break by security_id ascending (the lower id ranks
    better), matching the existing generic (-final, security_id) sort key."""
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 1

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    sids = [10, 5, 3, 2, 1]
    groups = pd.Series(["A"] * 5, index=sids)
    z = pd.DataFrame({"m1": [1.0, 1.0, 0.5, 0.0, -0.5]}, index=sids)  # sid 10 and 5 tie

    out = compose(z, definitions, units, groups, cfg, mode="hierarchical_nr")
    assert out.loc[5, "rank_all"] == 1.0
    assert out.loc[10, "rank_all"] == 2.0


def test_hierarchical_nr_unscored_when_coverage_fails_but_not_for_failed_neutralisation():
    """Coverage/eligibility rules are unchanged; a name failing coverage is still unscored.
    A name that passes coverage but whose sector group is too small to standardise (which
    would exclude it under 'hierarchical') stays scored under NR, since final does not
    need composite_neutral."""
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 1

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
    ])
    units = {"momentum": 10000}
    # A 3-name group is below min_group_nonnull (5): composite_neutral standardisation
    # fails for this whole group, though each member individually passes coverage.
    sids = [1, 2, 3]
    groups = pd.Series(["TINY"] * 3, index=sids)
    z = pd.DataFrame({"m1": [1.0, 0.0, -1.0]}, index=sids)

    out_hier = compose(z, definitions, units, groups, cfg, mode="hierarchical")
    assert (out_hier["scored"] == 0).all()
    assert (out_hier["exclusion_reason"] == "neutralisation").all()

    out_nr = compose(z, definitions, units, groups, cfg, mode="hierarchical_nr")
    assert (out_nr["scored"] == 1).all()
    assert out_nr["final"].equals(out_nr["composite"])
    assert out_nr["composite_neutral"].isna().all()  # still NaN for diagnostics, just not exclusionary


# ============================================================================ hierarchical_cov (D7)

def test_hierarchical_cov_scales_by_sqrt_coverage_ratio():
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 2
    cfg.standardise.min_factor_share = 0.6

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "v1", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "q1", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
    ])
    units = {"momentum": 3334, "value": 3333, "quality": 3333}
    sids = [1, 2, 3, 4, 5]
    groups = pd.Series(["A"] * 5, index=sids)
    z = pd.DataFrame({
        "m1": [1.0, 0.5, -0.2, 0.1, -0.6],
        "v1": [0.8, -0.4, 0.3, -0.1, 0.5],
        "q1": [np.nan, 0.2, -0.3, 0.4, -0.5],  # id 1 misses q1: K=2, D=3
    }, index=sids)

    out = compose(z, definitions, units, groups, cfg, mode="hierarchical_cov")
    assert out.loc[1, "n_factors_used"] == 2
    full_cov = out.loc[2]  # K=3, D=3 -> scale 1.0
    partial_cov = out.loc[1]  # K=2, D=3 -> scale sqrt(2/3)

    out_hier = compose(z, definitions, units, groups, cfg, mode="hierarchical")
    assert full_cov["final"] == pytest.approx(out_hier.loc[2, "composite_neutral"] * 1.0)
    assert partial_cov["final"] == pytest.approx(out_hier.loc[1, "composite_neutral"] * math.sqrt(2.0 / 3.0))
    # the thin-evidence name is shrunk toward zero relative to its own composite_neutral
    assert abs(partial_cov["final"]) < abs(out_hier.loc[1, "composite_neutral"])


def test_hierarchical_cov_n_applicable_excludes_nonfinancial_only_for_financial_services():
    """A Financial Services name missing only a nonfinancial-only factor gets no coverage
    penalty, because that factor was never in its applicable set (n_applicable_factors)."""
    import copy
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg.standardise.min_families = 1
    cfg.standardise.min_factor_share = 0.6

    definitions = pd.DataFrame([
        {"factor_id": "m1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "v1", "family": "value", "status_weight": 1.0, "nonfinancial": False},
        {"factor_id": "q1", "family": "quality", "status_weight": 1.0, "nonfinancial": True},
    ])
    units = {"momentum": 3334, "value": 3333, "quality": 3333}
    sids = [11, 12, 13, 14, 15]
    groups = pd.Series(["Financial Services"] * 5, index=sids)
    z = pd.DataFrame({
        "m1": [1.0, 0.5, -0.2, 0.1, -0.6],
        "v1": [0.8, -0.4, 0.3, -0.1, 0.5],
        "q1": [np.nan, np.nan, np.nan, np.nan, np.nan],  # inapplicable for Financial Services
    }, index=sids)

    out_cov = compose(z, definitions, units, groups, cfg, mode="hierarchical_cov")
    out_hier = compose(z, definitions, units, groups, cfg, mode="hierarchical")
    for sid in sids:
        assert out_cov.loc[sid, "n_factors_used"] == 2  # K
        # D (n_applicable) excludes q1 for Financial Services -> D=2, ratio 2/2=1 -> no shrink
        assert out_cov.loc[sid, "final"] == pytest.approx(out_hier.loc[sid, "composite_neutral"])


# ============================================================================ models.py registration

def test_seed_registers_six_models(cfg):
    ctx = _fresh_ctx(cfg)
    with ctx:
        res = seed(ctx, bootstrap_decision_id="DEC_BOOTSTRAP_TEST")
        assert res.status == "ok"
        assert res.counts["models"] == 6
        model_ids = {r[0] for r in ctx.conn.execute("SELECT model_id FROM models").fetchall()}
        assert model_ids == {
            "EW_HIER_v1", "EW_FLAT_v1", "MOM_ONLY_v1", "IC_SHRUNK_v1",
            "EW_HIER_NR_v1", "EW_HIER_COV_v1",
        }
        roles = dict(ctx.conn.execute("SELECT model_id, role FROM models").fetchall())
        assert roles["EW_HIER_NR_v1"] == "challenger"
        assert roles["EW_HIER_COV_v1"] == "challenger"
        n_versions = ctx.conn.execute(
            "SELECT count(*) FROM model_versions WHERE model_id IN ('EW_HIER_NR_v1', 'EW_HIER_COV_v1')"
        ).fetchone()[0]
        assert n_versions == 2


def test_register_challenger_rejects_unknown_model(cfg):
    ctx = _fresh_ctx(cfg)
    with ctx:
        seed(ctx, bootstrap_decision_id="DEC_BOOTSTRAP_TEST")
        _insert_decision(ctx.conn, "D-1", tier=2, approver_kind="human")
        with pytest.raises(Refused) as exc:
            register_challenger(ctx, "NOT_A_MODEL", "D-1")
        assert exc.value.code == "unknown_model"


def test_register_challenger_requires_decision_to_exist(cfg):
    ctx = _fresh_ctx(cfg, actor_kind="human", actor_name="owner")
    with ctx:
        seed(ctx, bootstrap_decision_id="DEC_BOOTSTRAP_TEST")
        with pytest.raises(Refused) as exc:
            register_challenger(ctx, "EW_HIER_NR_v1", "D-MISSING")
        assert exc.value.code == "decision_missing"


def test_register_challenger_tier2_requires_human_approver(cfg):
    """An LLM-approved Tier >= 1 decision cannot authorize a challenger registration,
    even when the calling actor's kind matches the decision's approver_kind."""
    ctx = _fresh_ctx(cfg, actor_kind="llm", actor_name="agent")
    with ctx:
        seed(ctx, bootstrap_decision_id="DEC_BOOTSTRAP_TEST")
        _insert_decision(ctx.conn, "D-LLM-T2", tier=2, approver_kind="llm", status="provisional")
        with pytest.raises(Refused) as exc:
            register_challenger(ctx, "EW_HIER_NR_v1", "D-LLM-T2")
        assert exc.value.code == "governance"
        # nothing was written
        assert ctx.conn.execute(
            "SELECT count(*) FROM models WHERE model_id = 'EW_HIER_NR_v1'"
        ).fetchone()[0] == 1  # only the one seeded at bootstrap, not a second row


def test_register_challenger_succeeds_with_human_tier2_and_is_idempotent(cfg):
    ctx = _fresh_ctx(cfg, actor_kind="human", actor_name="owner")
    with ctx:
        # Start from a database that only has the launch set minus the two challengers,
        # i.e. an "existing database" predating this work package.
        conn = ctx.conn
        conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
            "evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) VALUES "
            "('DEC_BOOTSTRAP_TEST', 'system_bootstrap', 0, 'launch_set', 't', 'c', '[]', 'approve', '[]', "
            "'2026-09-01T00:00:00.000000Z', 'system', 'system', 'approved', 'k/a.md', 'g')"
        )
        _insert_decision(conn, "D-CH-1", tier=2, approver_kind="human")

        res1 = register_challenger(ctx, "EW_HIER_COV_v1", "D-CH-1")
        assert res1.status == "ok"
        assert res1.counts == {"models": 1, "versions": 1}

        row = conn.execute(
            "SELECT role, decision_id FROM models WHERE model_id = 'EW_HIER_COV_v1'"
        ).fetchone()
        assert row[0] == "challenger"
        assert row[1] == "D-CH-1"
        v_row = conn.execute(
            "SELECT version, decision_id FROM model_versions WHERE model_id = 'EW_HIER_COV_v1'"
        ).fetchone()
        assert tuple(v_row) == (1, "D-CH-1")

        # Idempotent: registering again is a no-op on content, not an error.
        res2 = register_challenger(ctx, "EW_HIER_COV_v1", "D-CH-1")
        assert res2.status == "ok"
        n_models = conn.execute(
            "SELECT count(*) FROM models WHERE model_id = 'EW_HIER_COV_v1'"
        ).fetchone()[0]
        n_versions = conn.execute(
            "SELECT count(*) FROM model_versions WHERE model_id = 'EW_HIER_COV_v1'"
        ).fetchone()[0]
        assert n_models == 1
        assert n_versions == 1


def test_challenger_definitions_cover_both_models():
    assert set(CHALLENGER_DEFINITIONS.keys()) == {"EW_HIER_NR_v1", "EW_HIER_COV_v1"}
    assert CHALLENGER_DEFINITIONS["EW_HIER_NR_v1"]["params"]["mode"] == "hierarchical_nr"
    assert CHALLENGER_DEFINITIONS["EW_HIER_COV_v1"]["params"]["mode"] == "hierarchical_cov"
    for defn in CHALLENGER_DEFINITIONS.values():
        assert defn["kind"] == "equal"
