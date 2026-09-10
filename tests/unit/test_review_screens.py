"""Regression tests for the scoring_inputs review fixes (screens, composite,
FactorInputs G6 masking, portfolio construction).

Each test targets one production behaviour that a red-team review found was faked or
missing: a test-accommodation fallback that made bad/missing data look like it passed.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from quant.config import load
from quant.factors.inputs import build as build_inputs
from quant.model.composite import compose
from quant.model.screens import apply as apply_screens
from quant.portfolio.construct import rebalance
from quant.run import RunContext
from quant.types import Actor, Draft, FrozenClock


# --------------------------------------------------------------------------- screens


def _base_scores(sids, finals):
    return pd.DataFrame(
        {
            "security_id": sids,
            "composite": finals,
            "composite_neutral": finals,
            "sector_tilt": [0.0] * len(sids),
            "final": finals,
            "scored": [1] * len(sids),
            "n_factors_used": [5] * len(sids),
            "exclusion_reason": [None] * len(sids),
        },
        index=sids,
    )


def test_missing_adv_is_ineligible_no_silent_pass(cfg):
    """A security with no ADV evidence anywhere (no members column, no store) must be
    ineligible with reason adv_missing -- not silently defaulted to a passing ADV."""
    sids = [1, 2]
    groups = pd.Series(["Sector A"] * 2, index=sids)
    members = pd.DataFrame(
        {"security_id": sids, "series": ["EQ"] * 2},
        index=sids,
    )
    scores_initial = _base_scores(sids, [1.0, 0.9])

    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=scores_initial,
    )
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    # ctx.store stays None: there is genuinely no other source for ADV.

    out = apply_screens(ctx, draft, scores_initial, "EW_HIER_v1")

    for sid in sids:
        assert out.loc[sid, "eligible"] == 0
        assert out.loc[sid, "exclusion_reason"] == "adv_missing"
        assert out.loc[sid, "liquidity_bucket"] == "D"
        assert pd.isna(out.loc[sid, "rank"])


class _FakeAdvStore:
    """Duck-typed stand-in for PriceStore.adv_inr, returning canned rows."""

    def __init__(self, rows: dict[int, tuple[float, int, int]]):
        self._rows = rows  # security_id -> (adv_inr, n_days, pos_sessions)

    def adv_inr(self, security_ids, as_of, vintage_at, window: int = 63) -> pd.DataFrame:
        cols = [f"adv_{window}_inr", f"n_days_{window}", f"pos_sessions_{window}"]
        data = {}
        for sid in security_ids:
            if sid in self._rows:
                adv, n_days, pos = self._rows[sid]
                data[sid] = {cols[0]: adv, cols[1]: n_days, cols[2]: pos}
        df = pd.DataFrame.from_dict(data, orient="index")
        df.index.name = "security_id"
        return df


def test_thin_trading_via_store_pos_sessions(cfg):
    """positive-volume session counts absent from `members` are fetched from the price
    store's adv_inr(...); a count below cfg.screens.min_traded_sessions is thin_trading,
    not a silent pass, and a sufficient count is eligible."""
    sids = [1, 2]
    groups = pd.Series(["Sector A"] * 2, index=sids)
    members = pd.DataFrame(
        {
            "security_id": sids,
            "series": ["EQ"] * 2,
            "adv_63_inr": [50_000_000.0, 60_000_000.0],  # ADV present and sufficient
            # pos_sessions_63 is deliberately absent -- must come from the store.
        },
        index=sids,
    )
    scores_initial = _base_scores(sids, [1.0, 0.9])

    draft = Draft(
        cohort_id="live:2026-09-30",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T10:00:00.000000Z",
        definition_hash="def123",
        members=members,
        groups=groups,
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=scores_initial,
    )
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.store = _FakeAdvStore({1: (50_000_000.0, 63, 40), 2: (60_000_000.0, 63, 60)})

    out = apply_screens(ctx, draft, scores_initial, "EW_HIER_v1")

    # sid1: store reports 40 positive-volume sessions, below min_traded_sessions (54).
    assert out.loc[1, "eligible"] == 0
    assert out.loc[1, "exclusion_reason"] == "thin_trading"

    # sid2: store reports 60, at or above threshold -- eligible.
    assert out.loc[2, "eligible"] == 1
    assert out.loc[2, "exclusion_reason"] is None or pd.isna(out.loc[2, "exclusion_reason"])


# -------------------------------------------------------------------------- composite


def test_small_constant_group_unscored_with_neutralisation_reason(cfg):
    """A sector group too small/constant to neutralize (MASTER_SPEC 5.2) must not fall
    back to the un-neutralized composite. Such names become scored=0 with reason
    'neutralisation'; the raw composite is kept for diagnostics but composite_neutral
    and final are NaN. A separate, genuinely varying group scores normally."""
    definitions = pd.DataFrame(
        [{"factor_id": "mom_12_1", "family": "momentum", "status_weight": 1.0, "nonfinancial": False}]
    )
    units = {"momentum": 10000}

    # Group A: 5 names, all with the IDENTICAL raw factor value -> constant group.
    # Group B: 5 names with distinct values -> standardises normally.
    sids = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    groups = pd.Series(["A"] * 5 + ["B"] * 5, index=sids)
    z_df = pd.DataFrame(
        {"mom_12_1": [0.4, 0.4, 0.4, 0.4, 0.4, 0.1, 0.2, 0.3, 0.4, 0.5]},
        index=sids,
    )

    res = compose(z_df, definitions, units, groups, cfg, mode="mom_only")

    for sid in [1, 2, 3, 4, 5]:
        assert res.loc[sid, "scored"] == 0
        assert res.loc[sid, "exclusion_reason"] == "neutralisation"
        assert res.loc[sid, "composite"] == pytest.approx(0.4)  # kept for diagnostics
        assert pd.isna(res.loc[sid, "composite_neutral"])
        assert pd.isna(res.loc[sid, "final"])

    for sid in [6, 7, 8, 9, 10]:
        assert res.loc[sid, "scored"] == 1
        assert pd.isna(res.loc[sid, "exclusion_reason"]) or res.loc[sid, "exclusion_reason"] is None
        assert pd.notna(res.loc[sid, "composite_neutral"])
        assert pd.notna(res.loc[sid, "final"])


# ------------------------------------------------------------------------ FactorInputs


@pytest.fixture
def entered_ctx(tmp_path):
    cfg = load()
    db_path = tmp_path / "state.sqlite"
    prices_path = tmp_path / "prices.sqlite"

    from quant.db.core import apply_schema, connect

    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()

    cfg = cfg.with_paths(db=db_path, prices_db=prices_path)
    clock = FrozenClock("2026-09-30T18:30:00.000000Z")
    actor = Actor(kind="system", name="test")
    run_ctx = RunContext(as_of="2026-09-30", kind="production", track="live", cfg=cfg, clock=clock, actor=actor)
    with run_ctx as c:
        yield c


def test_attribute_masking_pe_and_mcap(entered_ctx):
    """FactorInputs.attribute masks |PE| >= 1000 and non-positive mcap_inr to NaN at
    read time (MASTER_SPEC 4.6 G6), instead of letting an obviously invalid raw value
    reach a factor formula."""
    ctx = entered_ctx
    for sid in (1, 2, 3):
        ctx.conn.execute(
            "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2025-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )

    # sid1: PE = -1500 (|PE| >= 1000 -> masked); mcap = 5000 (valid, positive).
    # sid2: PE = 500 (valid, |PE| < 1000); mcap = -200 (<= 0 -> masked).
    # sid3: PE = 999.9 (valid, just under the 1000 bound); mcap = 1.0 (valid, just above 0).
    rows = [
        (1, -1500.0, 5000.0),
        (2, 500.0, -200.0),
        (3, 999.9, 1.0),
    ]
    for sid, pe, mcap in rows:
        ctx.conn.execute(
            "INSERT INTO security_attributes (captured_at, security_id, mcap_inr, trailing_pe, source_sha256) "
            "VALUES ('2026-09-30T18:29:59.999999Z', ?, ?, ?, 'sha')",
            (sid, mcap, pe),
        )

    draft = Draft(
        cohort_id="C-2026-09-30-live",
        as_of="2026-09-30",
        track="live",
        knowledge_cutoff="2026-09-30T18:29:59.999999Z",
        definition_hash="def",
        members=pd.DataFrame([{"security_id": s} for s in (1, 2, 3)]),
        groups=pd.Series({1: "Technology", 2: "Technology", 3: "Technology"}),
        source_refs={},
        factor_values=pd.DataFrame(),
        model_weights=pd.DataFrame(),
        scores=pd.DataFrame(),
    )
    inputs = build_inputs(ctx, draft)

    pe = inputs.attribute("trailing_pe")
    mcap = inputs.attribute("market_cap_inr")

    assert pd.isna(pe.loc[1])          # |-1500| >= 1000 -> masked
    assert pe.loc[2] == pytest.approx(500.0)
    assert pe.loc[3] == pytest.approx(999.9)  # just under the bound -- kept

    assert pd.isna(mcap.loc[2])        # -200 <= 0 -> masked
    assert mcap.loc[1] == pytest.approx(5000.0)
    assert mcap.loc[3] == pytest.approx(1.0)  # just above 0 -- kept


# ------------------------------------------------------------------------- portfolio


def test_construct_reads_n_holdings_and_holds_cash_on_infeasibility(cfg):
    """rebalance() reads target_n from cfg.portfolio.n_holdings (not the literal 30).
    When fewer feasible names exist than target_n, weights stay at 1/target_n -- they
    are never scaled up to consume the whole book -- and the shortfall is reported as
    cash_weight, repeated on every row."""
    cfg_test = copy.deepcopy(cfg)
    cfg_test.portfolio.n_holdings = 10

    sids = [1, 2, 3, 4, 5]
    ranks = pd.Series({s: s for s in sids})
    eligible = pd.Series({1: True, 2: True, 3: True, 4: False, 5: False})
    groups = pd.Series({s: f"G{s}" for s in sids})
    buckets = pd.Series({s: "A" for s in sids})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg_test,
        rule="top30_buffer",
    )

    # Only 3 of the 5 names are eligible -- fewer than n_holdings (10).
    assert len(positions) == 3
    for w in positions["target_weight"]:
        assert abs(w - 0.10) < 1e-9  # 1 / n_holdings(10), NOT scaled up to 1/3 (~0.333)

    assert "cash_weight" in positions.columns
    assert np.allclose(positions["cash_weight"].to_numpy(), 0.70)  # 1 - 3*0.10, repeated per row


def test_construct_sector_cap_names_from_cfg(cfg):
    """rebalance() reads the per-sector cap from cfg.portfolio.sector_cap_names, not
    the literal 6."""
    cfg_test = copy.deepcopy(cfg)
    cfg_test.portfolio.sector_cap_names = 2
    cfg_test.portfolio.n_holdings = 10

    sids = list(range(1, 11))
    ranks = pd.Series({s: s for s in sids})
    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({s: "SAMESECTOR" for s in sids})  # all 10 share one sector
    buckets = pd.Series({s: "A" for s in sids})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, _ = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg_test,
        rule="top30_buffer",
    )

    # sector_cap_names=2 (override) admits only 2 names even with 10 eligible and
    # n_holdings=10; the literal default (6) would have admitted 6.
    assert len(positions) == 2


def test_construct_buffer_rank_from_cfg(cfg):
    """rebalance() reads the retention threshold from cfg.portfolio.buffer_rank, not
    the literal 60."""
    cfg_test = copy.deepcopy(cfg)
    cfg_test.portfolio.n_holdings = 3
    cfg_test.portfolio.buffer_rank = 3  # far below the literal default of 60

    previous = pd.DataFrame(
        [{"security_id": 10, "weight": 0.30, "entry_as_of": "2026-06-30", "group": "IND"}]
    )
    sids = [1, 2, 3, 10]
    ranks = pd.Series({1: 1, 2: 2, 3: 3, 10: 5})  # sid10's rank (5) exceeds buffer_rank (3)
    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({1: "G1", 2: "G2", 3: "G3", 10: "IND"})
    buckets = pd.Series({s: "A" for s in sids})

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg_test,
        rule="top30_buffer",
    )

    # Under the literal default (60) sid10 (rank 5) would have been retained. With
    # buffer_rank=3 from cfg it is not retained, and n_holdings=3 is filled by ranks
    # 1-3 before rank 5 is ever reached as a fresh entrant.
    assert 10 not in set(positions["security_id"])
    sell_10 = deltas[deltas["security_id"] == 10]
    assert len(sell_10) == 1
    assert sell_10["side"].iloc[0] == "SELL"


def test_construct_bucket_c_redistributes_to_ab_even_when_infeasible(cfg):
    """MASTER_SPEC section 8: 'Bucket C has a 2% portfolio target cap; redistribute
    remaining target across eligible A/B names under the sector limit, else cash.'
    That redistribution rule is unconditional on the book being fully populated --
    only the 'no eligible A/B names' fallback is conditional. A book that is BOTH
    infeasible (fewer than n_holdings feasible names) AND holds a bucket-C name must
    still push the C name's capped excess into the A/B names actually selected,
    rather than dumping it into generic cash_weight alongside the unfilled slots."""
    cfg_test = copy.deepcopy(cfg)
    cfg_test.portfolio.n_holdings = 10
    cfg_test.portfolio.sector_cap_names = 10  # sector caps wide open
    cfg_test.portfolio.bucket_c_max_weight = 0.02

    sids = [1, 2, 3]
    ranks = pd.Series({1: 1, 2: 2, 3: 3})
    eligible = pd.Series({s: True for s in sids})
    groups = pd.Series({s: f"G{s}" for s in sids})
    buckets = pd.Series({1: "C", 2: "A", 3: "A"})
    previous = pd.DataFrame(columns=["security_id", "weight", "entry_as_of", "group"])

    positions, deltas = rebalance(
        previous=previous,
        ranks=ranks,
        eligible=eligible,
        groups=groups,
        buckets=buckets,
        cfg=cfg_test,
        rule="top30_buffer",
    )

    by_sid = positions.set_index("security_id")["target_weight"]
    # C name capped at bucket_c_max_weight (0.02).
    assert by_sid.loc[1] == pytest.approx(0.02)
    # C's freed capacity (0.10 - 0.02 = 0.08) redistributes across the 2 A names:
    # 0.10 + 0.08/2 = 0.14 each -- NOT left stranded in cash_weight.
    assert by_sid.loc[2] == pytest.approx(0.14)
    assert by_sid.loc[3] == pytest.approx(0.14)
    # target_weight sums to 0.30 (0.02 + 0.14 + 0.14); the remaining 0.70 is cash,
    # attributable only to the 7 unfilled n_holdings slots (7 * 0.10 = 0.70).
    assert positions["target_weight"].sum() == pytest.approx(0.30)
    assert np.allclose(positions["cash_weight"].to_numpy(), 0.70)
