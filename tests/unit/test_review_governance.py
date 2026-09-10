"""Regression tests for the governance module group fixes (review, proposals, evaluate,
walkforward, curves): scope wiring, HAC lag, look consumption, coverage, per-date n_eff,
causal walk-forward visibility, and the provisional-decision reversion plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pandas as pd
import pytest

from quant.db.core import apply_schema, connect
from quant.evaluation.curves import update as update_curves
from quant.evaluation.evaluate import run as evaluate_run
from quant.evaluation.labels import _add_months
from quant.evaluation.stats import hac_mean_test
from quant.evaluation.walkforward import family_ic_history
from quant.knowledge.proposals import apply as apply_proposals, draft as draft_proposals
from quant.knowledge.review import factor as review_factor
from quant.run import RunContext
from quant.types import Actor, Clock


class FrozenTestClock(Clock):
    def __init__(self, now_str: str):
        self._now_str = now_str

    def now(self) -> datetime:
        return datetime.fromisoformat(self._now_str.replace("Z", "+00:00"))

    def now_iso(self) -> str:
        return self._now_str

    def advance(self, dt: str) -> None:
        self._now_str = dt


# --------------------------------------------------------------------------- raw sqlite fixture


@pytest.fixture
def raw_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    with open("quant/db/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _insert_hypothesis(conn, hypothesis_id, subject_id, kind="factor", opportunities="[12, 24, 36]"):
    conn.execute(
        "INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, "
        "subject_id, title, statement, expected_sign, horizon_m, primary_metric, success_criterion, "
        "failure_criterion, registered_on, registered_by, first_oos_as_of, code_sha, budget_year, "
        "sequence_in_year, counts_toward_budget, status, md_path) "
        "VALUES ('quality', ?, 'fsha', ?, ?, ?, 'title', 'stmt', 1, 3, 'ic', 'ic >= 0.02', 'ic < 0', "
        "'2026-01-01', 'system', '2026-01-31', 'csha', 2026, 1, 0, 'open', 'h.md')",
        (opportunities, hypothesis_id, kind, subject_id),
    )


def _insert_factor_registry(conn, factor_id, name, applies_to_financials=0, status="shadow"):
    conn.execute(
        "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
        "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, "
        "min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
        "VALUES (?, ?, 1, 'quality', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, ?, 1, 0.8, 'sha', 'mod', ?, "
        "'2026-01-01', '2026-01-01')",
        (factor_id, name, applies_to_financials, status),
    )


def _insert_eligible_eval(conn, eval_id, factor_id, as_of, value, horizon_m=3, status="ok"):
    conn.execute(
        "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, "
        "subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, "
        "window_start, window_end, evidence_hash, revision) "
        "VALUES (?, 1, '2026-01-01T00:00:00Z', 'factor', ?, '1', ?, ?, 'eligible', 'live', 'oriented_ic', "
        "?, 100, NULL, ?, 'spearman', '', '', ?, 1)",
        (eval_id, factor_id, as_of, horizon_m, value, status, f"ev_{eval_id}"),
    )


# --------------------------------------------------------------------------- defect 1: review.factor


def test_review_factor_ignores_scope_full_and_reads_eligible(raw_db):
    """scope='full' rows (the old, wrong scope) are invisible; only 'eligible' rows count."""
    fid = "mom_test@1"
    _insert_factor_registry(raw_db, fid, "mom_test")
    _insert_hypothesis(raw_db, "H-1", fid)
    # 12 months written under the WRONG legacy scope: must not be picked up.
    for i in range(1, 13):
        raw_db.execute(
            "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, "
            "subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, "
            "window_start, window_end, evidence_hash, revision) "
            "VALUES (?, 1, '2026-01-01T00:00:00Z', 'factor', ?, '1', ?, 3, 'full', 'live', 'oriented_ic', "
            "0.05, 100, NULL, 'ok', 'spearman', '', '', ?, 1)",
            (i, fid, f"2025-{i:02d}-28", f"legacy_{i}"),
        )

    cfg_stub = _cfg_stub()
    res = review_factor(raw_db, factor_id=fid, as_of="2026-01-31", cfg=cfg_stub)
    labeled = next(c for c in res.checks if c.id == "labeled_months")
    assert labeled.observed == 0
    assert res.evidence_ids == []


def test_review_factor_finds_evaluations_written_by_evaluate_run(raw_db, cfg):
    """End-to-end: evaluate.run() writes scope 'eligible'/'all' evaluations; review.factor()
    must be able to see the ones evaluate.run actually produced (the original bug queried a
    scope evaluate.run never writes, so no factor could ever be reviewed)."""
    fid = "mom_12_1@1"
    raw_db.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (1, '2024-01-31', 'test', 'live', 1, "
        "'2024-01-31T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    for sid in range(1, 11):
        raw_db.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2020-01-01', '2026-01-01', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )
    _insert_factor_registry(raw_db, fid, "mom_12_1", status="shadow")
    _insert_hypothesis(raw_db, "H-mom", fid)

    dates = pd.date_range("2024-01-31", periods=12, freq="ME")
    through = _add_months(dates[-1].strftime("%Y-%m-%d"), 3)
    for cdate in dates:
        c = cdate.strftime("%Y-%m-%d")
        cid = f"live:{c}"
        raw_db.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES (?, ?, 'live', ?, 'def', 'mem', '{}', ?, ?, 1, 1)",
            (cid, c, f"{c}T18:29:59.999999Z", f"{c}T09:00:00.000000Z", f"{c}T09:00:00.000000Z"),
        )
        end_date = _add_months(c, 3)
        for sid in range(1, 11):
            raw_db.execute(
                "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
                "sector_group, flags, input_refs_json, track, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'Industrials', '', '{}', 'live', 1)",
                (cid, c, sid, fid, float(sid), float(sid), float(sid) - 5.5),
            )
            raw_db.execute(
                "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, "
                "evidence_hash, computed_at, r_log, r_arith, r_group_median, l_rel, r_uni, sector_group, "
                "status, mb36, mb36_touch, price_manifest_sha, computed_run_id, decision_id, "
                "supersedes_revision) "
                "VALUES (?, ?, ?, 3, ?, 'live', 1, ?, ?, ?, ?, 0.0, ?, ?, 'Industrials', 'ok', 0, 0, "
                "'sha', 1, NULL, NULL)",
                (
                    cid, c, sid, end_date, f"eh_{cid}_{sid}", f"{end_date}T00:00:00.000000Z",
                    float(sid) * 0.01, float(sid) * 0.01, float(sid) * 0.01, sid,
                ),
            )

    ctx = RunContext(as_of=through, kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = raw_db
    ctx.run_id = 1

    res = evaluate_run(ctx, through=through, track="live")
    assert res.status == "ok"
    assert res.counts["inserted"] > 0

    check_res = review_factor(raw_db, factor_id=fid, as_of=through, cfg=cfg)
    labeled = next(c for c in check_res.checks if c.id == "labeled_months")
    assert labeled.observed == 12
    assert len(check_res.evidence_ids) == 12


def test_hac_lag_is_horizon_minus_one(raw_db):
    """HAC lag must be h-1 (2 for the 3M horizon), matching stats.hac_mean_test(lag=2)."""
    fid = "qual_x@1"
    _insert_factor_registry(raw_db, fid, "qual_x")
    _insert_hypothesis(raw_db, "H-2", fid)
    values = [0.04, 0.01, 0.06, -0.02, 0.03, 0.05, 0.02, 0.07, -0.01, 0.04, 0.03, 0.06]
    for i, v in enumerate(values, start=1):
        _insert_eligible_eval(raw_db, i, fid, f"2025-{i:02d}-28", v)

    cfg_stub = _cfg_stub()
    check_res = review_factor(raw_db, factor_id=fid, as_of="2026-01-31", cfg=cfg_stub)
    hac_check = next(c for c in check_res.checks if c.id == "hac_t")

    expected = hac_mean_test(values, lag=2)
    assert hac_check.observed == pytest.approx(expected.t)

    # A lag of 1 (the old, wrong value) would give a different t-statistic for this series.
    wrong = hac_mean_test(values, lag=1)
    assert wrong.t != pytest.approx(expected.t)


def test_factor_look_never_consumed_twice(raw_db):
    """Once look 12 is consumed, further calls below 24 replay it (not eligible for a new
    look, next_review stays '24'); reaching 24 consumes the next look."""
    fid = "acc_x@1"
    _insert_factor_registry(raw_db, fid, "acc_x")
    _insert_hypothesis(raw_db, "H-3", fid)
    for i in range(1, 13):
        _insert_eligible_eval(raw_db, i, fid, f"2025-{i:02d}-28", 0.03)

    cfg_stub = _cfg_stub()
    res1 = review_factor(raw_db, factor_id=fid, as_of="2026-01-31", cfg=cfg_stub)
    assert res1.next_review == "24"
    stored = raw_db.execute("SELECT n_periods_at_eval FROM hypotheses WHERE hypothesis_id = 'H-3'").fetchone()
    assert stored["n_periods_at_eval"] == 12

    # Recompute at the exact same evidence: not eligible for a new look (idempotent replay).
    res1b = review_factor(raw_db, factor_id=fid, as_of="2026-01-31", cfg=cfg_stub)
    assert res1b.eligible is False
    assert res1b.next_review == "24"

    # More months accumulate but stay below the next opportunity (24): still no new look.
    for i in range(13, 21):
        _insert_eligible_eval(raw_db, i, fid, f"2026-{i - 12:02d}-15", 0.03)

    res2 = review_factor(raw_db, factor_id=fid, as_of="2026-09-30", cfg=cfg_stub)
    assert res2.eligible is False
    assert res2.next_review == "24"
    stored2 = raw_db.execute("SELECT n_periods_at_eval FROM hypotheses WHERE hypothesis_id = 'H-3'").fetchone()
    assert stored2["n_periods_at_eval"] == 12  # unchanged: no new look consumed

    # Reach 24: a new look is due.
    for i in range(21, 25):
        _insert_eligible_eval(raw_db, i, fid, f"2026-{i-12:02d}-16", 0.03)
    res3 = review_factor(raw_db, factor_id=fid, as_of="2026-12-31", cfg=cfg_stub)
    assert res3.next_review == "36"
    stored3 = raw_db.execute("SELECT n_periods_at_eval FROM hypotheses WHERE hypothesis_id = 'H-3'").fetchone()
    assert stored3["n_periods_at_eval"] == 24


def test_coverage_excludes_financials_for_nonfinancial_factor(raw_db):
    """Coverage is computed over applicable (nonfinancial) members only, from
    factor_values.sector_group, for a factor that does not apply to financials."""
    fid = "roce_x@1"
    raw_db.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (1, '2026-01-31', 'test', 'live', 1, "
        "'2026-01-31T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    _insert_factor_registry(raw_db, fid, "roce_x", applies_to_financials=0)
    _insert_hypothesis(raw_db, "H-4", fid)
    for sid in range(1, 11):
        raw_db.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2020-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )
    raw_db.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
        "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-01-31', '2026-01-31', 'live', '2026-01-31T18:29:59Z', 'def', 'mem', '{}', "
        "'2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z', 1, 1)"
    )
    # 8 nonfinancial members, all computed (z present); 2 financial members, uncomputed (z NULL).
    for sid in range(1, 9):
        raw_db.execute(
            "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
            "sector_group, flags, input_refs_json, track, run_id) "
            "VALUES ('live:2026-01-31', '2026-01-31', ?, ?, 1.0, 1.0, 0.5, 'Industrials', '', '{}', 'live', 1)",
            (sid, fid),
        )
    for sid in range(9, 11):
        raw_db.execute(
            "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
            "sector_group, flags, input_refs_json, track, run_id) "
            "VALUES ('live:2026-01-31', '2026-01-31', ?, ?, NULL, NULL, NULL, 'Financial Services', '', "
            "'{}', 'live', 1)",
            (sid, fid),
        )

    cfg_stub = _cfg_stub()
    check_res = review_factor(raw_db, factor_id=fid, as_of="2026-01-31", cfg=cfg_stub)
    cov = next(c for c in check_res.checks if c.id == "coverage")
    # All 8 applicable (nonfinancial) members are computed -> 100% coverage, not diluted by
    # the 2 structurally inapplicable financial members.
    assert cov.observed == pytest.approx(1.0)
    assert cov.status == "PASS"


def test_coverage_query_excludes_backfill_track(raw_db):
    """Coverage evidence must only be drawn from track='live' factor_values; backfill-track
    rows (survivorship-biased, spec 7.4/6.2) must never enter promotion evidence, matching the
    track='live' filter already used by the eval_rows/eval_12m queries in this function."""
    fid = "roce_y@1"
    raw_db.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (1, '2026-03-31', 'test', 'live', 1, "
        "'2026-03-31T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    _insert_factor_registry(raw_db, fid, "roce_y", applies_to_financials=0)
    _insert_hypothesis(raw_db, "H-5", fid)
    for sid in range(1, 9):
        raw_db.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2020-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )

    # One live cohort: full coverage (all 8 members computed).
    raw_db.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
        "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-03-31', '2026-03-31', 'live', '2026-03-31T18:29:59Z', 'def_l', 'mem_l', '{}', "
        "'2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z', 1, 1)"
    )
    for sid in range(1, 9):
        raw_db.execute(
            "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
            "sector_group, flags, input_refs_json, track, run_id) "
            "VALUES ('live:2026-03-31', '2026-03-31', ?, ?, 1.0, 1.0, 0.5, 'Industrials', '', '{}', 'live', 1)",
            (sid, fid),
        )

    # Two backfill cohorts (earlier dates) with zero coverage (all z NULL). A missing
    # track filter would pull these into cohort_order[:3] alongside the live cohort and
    # drag the averaged coverage from 100% down to ~33%, a false FAIL.
    for i, c in enumerate(["2026-01-31", "2026-02-28"], start=1):
        cid = f"backfill:{c}"
        raw_db.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES (?, ?, 'backfill', ?, ?, ?, '{}', ?, ?, 1, 1)",
            (cid, c, f"{c}T18:29:59Z", f"def_b{i}", f"mem_b{i}", f"{c}T09:00:00Z", f"{c}T09:00:00Z"),
        )
        for sid in range(1, 9):
            raw_db.execute(
                "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
                "sector_group, flags, input_refs_json, track, run_id) "
                "VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'Industrials', '', '{}', 'backfill', 1)",
                (cid, c, sid, fid),
            )

    cfg_stub = _cfg_stub()
    check_res = review_factor(raw_db, factor_id=fid, as_of="2026-03-31", cfg=cfg_stub)
    cov = next(c for c in check_res.checks if c.id == "coverage")
    # Only the live cohort counts: 100% coverage, not diluted by the two zero-coverage
    # backfill cohorts a missing track filter would otherwise pull in.
    assert cov.observed == pytest.approx(1.0)
    assert cov.status == "PASS"


def test_evaluations_n_eff_is_null_per_date(raw_db, cfg):
    """Per-date cross-sectional evaluations store n_eff = NULL; n_eff is a series statistic."""
    _seed_minimal_evaluate_world(raw_db)
    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = raw_db
    ctx.run_id = 2

    res = evaluate_run(ctx, through="2026-09-30", track="live")
    assert res.status == "ok"
    rows = raw_db.execute("SELECT n, n_eff FROM evaluations").fetchall()
    assert len(rows) > 0
    for r in rows:
        assert r["n_eff"] is None
        assert r["n"] is not None


# --------------------------------------------------------------------------- defect 4: walkforward


def test_family_ic_history_revision_computed_after_known_at_is_invisible(raw_db):
    """A later revision computed after known_at must not shadow the revision that was known
    at the time; family_ic_history sees only what was knowable as of known_at."""
    _seed_minimal_cohort_and_model(raw_db)
    # Revision 1: known at fit time, value 0.08.
    raw_db.execute(
        "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, "
        "subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, "
        "window_start, window_end, evidence_hash, revision, supersedes_eval_id) "
        "VALUES (301, 1, '2026-10-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1', '2026-06-30', 3, "
        "'eligible', 'live', 'ic', 0.08, 10, NULL, 'ok', 'spearman', '', '', 'eh_r1', 1, NULL)"
    )
    # Revision 2: computed AFTER known_at, with a different value; must stay invisible.
    raw_db.execute(
        "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, "
        "subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, "
        "window_start, window_end, evidence_hash, revision, supersedes_eval_id) "
        "VALUES (302, 2, '2026-12-01T00:00:00.000000Z', 'factor', 'mom_12_1@1', '1', '2026-06-30', 3, "
        "'eligible', 'live', 'ic', 0.99, 10, NULL, 'ok', 'spearman', '', '', 'eh_r2', 2, 301)"
    )

    mv = {
        "model_id": "EW_HIER_v1",
        "version": 1,
        "factor_set_json": '[{"factor_id":"mom_12_1@1","family":"momentum"}]',
        "weights_json": '{"family":{"momentum":1.0}}',
    }
    df_hist = family_ic_history(raw_db, mv, as_of="2026-09-30", known_at="2026-10-05T00:00:00.000000Z")
    assert abs(df_hist.loc["2026-06-30", "momentum"] - 0.08) < 1e-12


# --------------------------------------------------------------------------- defect 5: curves


def test_learning_curve_k_from_model_weights_and_train_end_is_last_matured_cohort(raw_db, cfg):
    """k comes from stored model_weights.n_eff * h (not hardcoded 0); train_end is the last
    matured cohort's as_of, not the scored cohort's own as_of."""
    _seed_minimal_cohort_and_model(raw_db)
    # An earlier cohort that has matured by the time '2026-06-30' is scored.
    raw_db.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
        "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-03-31', '2026-03-31', 'live', '2026-03-31T18:29:59Z', 'def0', 'mem0', '{}', "
        "'2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z', 1, 1)"
    )
    raw_db.execute(
        "INSERT INTO model_weights (cohort_id, model_id, model_version, as_of, family, weight_units, "
        "n_eff, alpha, gate, evidence_hash, run_id) "
        "VALUES ('live:2026-06-30', 'EW_HIER_v1', 1, '2026-06-30', 'momentum', 10000, 4.0, 0.5, 'open', "
        "'ehw', 1)"
    )
    raw_db.execute(
        "INSERT INTO evaluations (eval_id, computed_run_id, computed_at, subject_kind, subject_id, "
        "subject_version, as_of, horizon_m, scope, track, metric, value, n, n_eff, status, method, "
        "window_start, window_end, evidence_hash, revision) "
        "VALUES (401, 1, '2026-09-30T10:00:00.000000Z', 'model', 'EW_HIER_v1', '1', '2026-06-30', 3, "
        "'eligible', 'live', 'ic', 0.10, 10, NULL, 'ok', 'spearman', '', '', 'eh_m1', 1)"
    )

    ctx = RunContext(as_of="2026-09-30", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = raw_db
    ctx.run_id = 2

    res1 = update_curves(ctx, through="2026-09-30")
    assert res1.status == "ok"

    lp = raw_db.execute(
        "SELECT k, train_end, test_as_of FROM learning_curve_points WHERE model_id = 'EW_HIER_v1'"
    ).fetchone()
    assert lp is not None
    assert lp["k"] == 12  # n_eff(4.0) * h(3)
    assert lp["train_end"] == "2026-03-31"  # last matured cohort, not the '2026-06-30' test_as_of
    assert lp["test_as_of"] == "2026-06-30"

    # Unchanged recomputation inserts zero new rows.
    res2 = update_curves(ctx, through="2026-09-30")
    assert res2.counts.get("learning_points", 0) == 0
    assert res2.counts.get("evidence_curves", 0) == 0


# --------------------------------------------------------------------------- defect 2: proposals.apply


@pytest.fixture
def prop_ctx(tmp_path, cfg):
    db_path = tmp_path / "test_proposals_gov.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=knowledge_dir)

    clock = FrozenTestClock("2026-09-01T09:00:00.000000Z")
    actor = Actor(kind="system", name="cli")
    ctx = RunContext(as_of="2026-09-01", kind="proposal", track="live", cfg=test_cfg, clock=clock, actor=actor)
    ctx.conn = conn

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES (1, '2026-09-01', 'proposal', 'live', 1, "
            "'2026-09-01T09:00:00.000000Z', 'ok', 'test', 'c', 'q', 'r')"
        )
        conn.execute(
            "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
            "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, "
            "min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
            "VALUES ('mom_z@1', 'mom_z', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 1, 1, "
            "0.8, 'sha', 'mod', 'active', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('EW_HIER_v1', 1, "
            "'[{\"factor_id\":\"mom_z@1\",\"family\":\"momentum\"},{\"factor_id\":\"other@1\",\"family\":\"quality\"}]', "
            "'{\"family\":{\"momentum\":0.5,\"quality\":0.5}}', '2026-01-01')"
        )

    return ctx


def test_expiry_appends_reversion_decision_and_new_model_version_leaving_old_untouched(prop_ctx):
    """On expiry, promote_factor reverts to shadow with a factor_status_history row, and every
    model whose current factor_set includes the factor gets a NEW version without it; the old
    version's content is never rewritten (only its valid_to is closed)."""
    conn = prop_ctx.conn
    past_ts = "2026-06-25T09:00:00.000000Z"
    did = "D-2026-06-01"
    with conn:
        # applied_on = past_ts: the effect was already staged by a prior apply() call before
        # this decision expires (only an applied effect has something to revert).
        conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, "
            "decision, evidence_refs_json, decided_on, decided_by, approver_kind, status, applied_on, "
            "adr_path, git_sha) VALUES (?, 'promote_factor', 1, 'mom_z@1', 'Promote mom_z', 'ctx', '[]', "
            "'approve', '[]', ?, 'llm:gemini-flash', 'llm', 'provisional', ?, 'adr.md', 'sha1')",
            (did, past_ts, past_ts),
        )
        conn.execute("UPDATE factor_registry SET status = 'active' WHERE factor_id = 'mom_z@1'")

    old_row_before = dict(
        conn.execute("SELECT * FROM model_versions WHERE model_id = 'EW_HIER_v1' AND version = 1").fetchone()
    )

    res = apply_proposals(prop_ctx, as_of="2026-09-01")
    assert res.status == "ok"
    assert res.counts.get("reverted", 0) == 1

    old_d = conn.execute("SELECT status FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert old_d["status"] == "reverted"

    rev_row = conn.execute("SELECT decision_id FROM decisions WHERE kind = 'reversion'").fetchone()
    assert rev_row is not None
    rev_did = rev_row["decision_id"]

    f_stat = conn.execute("SELECT status FROM factor_registry WHERE factor_id = 'mom_z@1'").fetchone()["status"]
    assert f_stat == "shadow"

    hist = conn.execute(
        "SELECT status, decision_id FROM factor_status_history WHERE factor_id = 'mom_z@1' ORDER BY effective_from DESC LIMIT 1"
    ).fetchone()
    assert hist["status"] == "shadow"
    assert hist["decision_id"] == rev_did

    # Old version content is exactly unchanged (only valid_to may have been closed).
    old_row_after = dict(
        conn.execute("SELECT * FROM model_versions WHERE model_id = 'EW_HIER_v1' AND version = 1").fetchone()
    )
    assert old_row_after["factor_set_json"] == old_row_before["factor_set_json"]
    assert old_row_after["weights_json"] == old_row_before["weights_json"]
    assert old_row_after["valid_to"] == "2026-09-01"

    # A NEW version was appended without the reverted factor.
    new_row = conn.execute(
        "SELECT * FROM model_versions WHERE model_id = 'EW_HIER_v1' AND version = 2"
    ).fetchone()
    assert new_row is not None
    new_factor_set = json.loads(new_row["factor_set_json"])
    assert all(item["factor_id"] != "mom_z@1" for item in new_factor_set)
    assert any(item["factor_id"] == "other@1" for item in new_factor_set)
    assert new_row["valid_from"] == "2026-09-01"
    assert new_row["valid_to"] is None
    assert new_row["decision_id"] == rev_did


def test_provisional_decision_applies_effect_before_ratification(prop_ctx):
    """A Tier-1 LLM (provisional) decision's effect is applied prospectively immediately;
    status stays 'provisional' until a human ratifies it, but applied_on is recorded."""
    conn = prop_ctx.conn
    did = "D-2026-09-05"
    with conn:
        conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, "
            "decision, evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, "
            "git_sha) VALUES (?, 'promote_factor', 1, 'mom_z@1', 'Promote mom_z', 'ctx', '[]', 'approve', "
            "'[]', '2026-09-01T09:00:00.000000Z', 'llm:gemini-flash', 'llm', 'provisional', 'adr.md', 'sha1')",
            (did,),
        )
        conn.execute("UPDATE factor_registry SET status = 'shadow' WHERE factor_id = 'mom_z@1'")

    res = apply_proposals(prop_ctx, as_of="2026-09-01")
    assert res.counts.get("applied", 0) == 1

    d_row = conn.execute("SELECT status, applied_on FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert d_row["status"] == "provisional"  # unchanged: still pending ratification
    assert d_row["applied_on"]  # but the effect was recorded as applied

    f_stat = conn.execute("SELECT status FROM factor_registry WHERE factor_id = 'mom_z@1'").fetchone()["status"]
    assert f_stat == "active"

    # A second apply() call must not re-apply (applied_on already set).
    res2 = apply_proposals(prop_ctx, as_of="2026-09-02")
    assert res2.counts.get("applied", 0) == 0


def test_expiry_of_never_applied_provisional_decision_is_rejected_without_effect(prop_ctx):
    """A provisional decision past the ratification window whose effect was never actually
    staged (applied_on still NULL -- e.g. apply() never ran in the interim) has nothing
    recorded to invert. It must be closed out as 'rejected' with no fabricated reversion
    decision and no changes to factor_registry/factor_status_history/model_versions."""
    conn = prop_ctx.conn
    past_ts = "2026-06-01T09:00:00.000000Z"  # ~92 days before the 2026-09-01 clock: expired
    did = "D-2026-06-02"
    with conn:
        conn.execute(
            "INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, "
            "decision, evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, "
            "git_sha) VALUES (?, 'promote_factor', 1, 'mom_z@1', 'Promote mom_z', 'ctx', '[]', 'approve', "
            "'[]', ?, 'llm:gemini-flash', 'llm', 'provisional', 'adr.md', 'sha1')",
            (did, past_ts),
        )
        # applied_on is left NULL: this decision's effect was never staged by any apply() call.
        conn.execute("UPDATE factor_registry SET status = 'shadow' WHERE factor_id = 'mom_z@1'")

    old_model_version = dict(
        conn.execute("SELECT * FROM model_versions WHERE model_id = 'EW_HIER_v1' AND version = 1").fetchone()
    )

    res = apply_proposals(prop_ctx, as_of="2026-09-01")
    assert res.counts.get("reverted", 0) == 0

    d_row = conn.execute("SELECT status, applied_on FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert d_row["status"] == "rejected"
    assert not d_row["applied_on"]

    # No fabricated reversion decision was created for an effect that was never applied.
    assert conn.execute("SELECT 1 FROM decisions WHERE kind = 'reversion'").fetchone() is None

    # factor_registry, factor_status_history and model_versions are all untouched.
    f_stat = conn.execute("SELECT status FROM factor_registry WHERE factor_id = 'mom_z@1'").fetchone()["status"]
    assert f_stat == "shadow"
    hist_count = conn.execute(
        "SELECT count(*) FROM factor_status_history WHERE factor_id = 'mom_z@1'"
    ).fetchone()[0]
    assert hist_count == 0
    versions = conn.execute("SELECT version FROM model_versions WHERE model_id = 'EW_HIER_v1'").fetchall()
    assert [v["version"] for v in versions] == [1]
    new_model_version = dict(
        conn.execute("SELECT * FROM model_versions WHERE model_id = 'EW_HIER_v1' AND version = 1").fetchone()
    )
    assert new_model_version == old_model_version


def test_draft_dedupes_by_kind_and_subject_and_reviews_models(prop_ctx, monkeypatch):
    """draft() dedupes by (kind, subject_id) against proposed/provisional/approved-unapplied
    state, and also drafts promote_model proposals for eligible challenger models."""
    conn = prop_ctx.conn
    with conn:
        conn.execute("UPDATE factor_registry SET status = 'shadow' WHERE factor_id = 'mom_z@1'")
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('CH_v1', 'shrink', 'challenger', 'Challenger', '{}', '2024-01-01')"
        )

    import quant.knowledge.proposals as proposals_mod

    fake_factor_check = type("C", (), {"eligible": True, "evidence_ids": [1, 2, 3]})()
    fake_model_check = type("C", (), {"eligible": True, "evidence_ids": [4, 5]})()
    monkeypatch.setattr(proposals_mod, "review_factor", lambda *a, **k: fake_factor_check)
    monkeypatch.setattr(proposals_mod, "review_model", lambda *a, **k: fake_model_check)

    drafted1 = draft_proposals(prop_ctx, as_of="2026-09-01")
    assert len(drafted1) == 2  # one promote_factor, one promote_model
    kinds = {
        r["kind"] for r in conn.execute("SELECT kind FROM proposals WHERE proposal_id IN ({})".format(
            ",".join("?" * len(drafted1))
        ), drafted1).fetchall()
    }
    assert kinds == {"promote_factor", "promote_model"}

    # Calling draft() again while those proposals are still 'proposed' must not duplicate them.
    drafted2 = draft_proposals(prop_ctx, as_of="2026-09-02")
    assert drafted2 == []


# --------------------------------------------------------------------------- shared seed helpers


def _cfg_stub():
    class _Budget:
        review_labelled_months = [12, 24, 36]

    class _Cfg:
        budget = _Budget()

    return _Cfg()


def _seed_minimal_evaluate_world(conn):
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (1, '2026-06-30', 'test', 'live', 1, "
        "'2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (2, '2026-09-30', 'test', 'live', 1, "
        "'2026-09-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    for sid in range(1, 11):
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
        "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T10:00:00.000000Z', 'def1', 'mem1', "
        "'{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)"
    )
    conn.execute(
        "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
        "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, "
        "min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
        "VALUES ('mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, "
        "0.8, 'sha', 'mod', 'active', '2026-06-01', '2026-06-01')"
    )
    for sid in range(1, 11):
        conn.execute(
            "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
            "sector_group, flags, input_refs_json, track, run_id) VALUES ('live:2026-06-30', "
            "'2026-06-30', ?, 'mom_12_1@1', ?, ?, ?, 'Industrials', '', '{}', 'live', 1)",
            (sid, float(sid), float(sid), float(sid) - 5.5),
        )
        conn.execute(
            "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, "
            "evidence_hash, computed_at, r_log, r_arith, r_group_median, l_rel, r_uni, sector_group, "
            "status, mb36, mb36_touch, price_manifest_sha, computed_run_id, decision_id, "
            "supersedes_revision) VALUES ('live:2026-06-30', '2026-06-30', ?, 3, '2026-09-30', 'live', 1, "
            "'eh_labels_1', '2026-09-30T10:00:00.000000Z', ?, ?, 0.0, ?, ?, 'Industrials', 'ok', 0, 0, "
            "'sha', 1, NULL, NULL)",
            (sid, float(sid) * 0.02, float(sid) * 0.02, float(sid) * 0.02, float(sid) * 0.02),
        )


def _seed_minimal_cohort_and_model(conn):
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
        "config_sha256, registry_sha256) VALUES (1, '2026-06-30', 'test', 'live', 1, "
        "'2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
        "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
        "VALUES ('live:2026-06-30', '2026-06-30', 'live', '2026-06-30T10:00:00.000000Z', 'def1', 'mem1', "
        "'{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)"
    )
    conn.execute(
        "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
        "VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-06-01')"
    )
    conn.execute(
        "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
        "VALUES ('EW_HIER_v1', 1, '[{\"factor_id\":\"mom_12_1@1\",\"family\":\"momentum\"}]', "
        "'{\"family\":{\"momentum\":1.0}}', '2026-06-01')"
    )
    conn.execute(
        "INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, "
        "hypothesis, formula, inputs_json, lookback_days, applies_to_financials, backfillable, "
        "min_coverage, code_sha256, module_path, status, registered_on, status_changed_on) "
        "VALUES ('mom_12_1@1', 'mom', 1, 'momentum', 1, 3, 'stock', 'hyp', 'formula', '[]', 252, 0, 1, "
        "0.8, 'sha', 'mod', 'active', '2026-06-01', '2026-06-01')"
    )
