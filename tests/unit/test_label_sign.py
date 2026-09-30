"""Sign of l_rel, the sector-relative forward-return label.

Real-world motivation: l_rel is the primary Rank IC evaluation target for both
the champion (EW_HIER_v1) and every challenger (MASTER_SPEC 2.3/6.3) -- it is
literally what "did this model rank the right stocks higher" is measured
against. If quant.evaluation.labels.mature ever computed l_rel with the wrong
sign, a stock that genuinely beat its sector would be recorded as the group's
worst performer and vice versa; every downstream IC, HAC t-test and promotion
decision would silently score against a negated target while still "running
green". This test builds a one-sector, 1-month cohort of 4 securities with
hand-computed simple returns (+10%, +2%, -3%, -12%) and checks, per security,
that r_log = ln(1 + simple_return) and l_rel = r_log - median(r_log of the
group), to 1e-9 -- including that the best performer's l_rel is positive and
the worst performer's is negative.

Follows the fixture pattern of tests/unit/test_ws07_01.py (raw sqlite3 schema
load, prices_monthly-only, no PriceStore) and
tests/unit/test_review_corporate_actions.py::test_label_spanning_unresolved_action_is_excluded_ca
(scores/cohorts seeding for quant.evaluation.labels.mature).
"""
from __future__ import annotations

import math
import sqlite3
import statistics

import pytest

from quant.evaluation.labels import mature
from quant.run import RunContext


@pytest.fixture
def clean_test_db():
    conn = sqlite3.connect(":memory:")
    with open("docs/spec/contracts/schema.sql") as f:
        conn.executescript(f.read())
    return conn


# security_id -> simple (arithmetic) 1-month return, hand-picked so the group
# has a clear best (sid 1) and worst (sid 4) performer straddling the median.
SIMPLE_RETURNS = {1: 0.10, 2: 0.02, 3: -0.03, 4: -0.12}
START_TRI = 100.0


def _seed(conn):
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-06-30', 'test', 'live', 1, '2026-06-30T10:00:00.000000Z', 'running', 'git', 'code', 'cfg', 'reg')"
    )
    for sid in SIMPLE_RETURNS:
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, '2026-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}", f"Stock {sid}"),
        )

    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, "
        "published_at, generated_at, is_clean, run_id) VALUES ('live:2026-06-30', '2026-06-30', 'live', "
        "'2026-06-30T10:00:00.000000Z', 'def1', 'mem1', '{}', '2026-07-01T00:00:00.000000Z', '2026-07-01T00:00:00.000000Z', 1, 1)"
    )
    # Endpoint cohort (1-month horizon: 2026-06-30 -> 2026-07-31). mature()
    # looks this up by (track, as_of) to find the endpoint's prices_monthly
    # rows; its own scores/members are irrelevant to labeling the *origin*
    # cohort's members.
    conn.execute(
        "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, "
        "published_at, generated_at, is_clean, run_id) VALUES ('live:2026-07-31', '2026-07-31', 'live', "
        "'2026-07-31T10:00:00.000000Z', 'def2', 'mem2', '{}', '2026-08-01T00:00:00.000000Z', '2026-08-01T00:00:00.000000Z', 1, 1)"
    )
    conn.execute(
        "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
        "VALUES ('EW_HIER_v1', 'equal', 'champion', 'Champion', '{}', '2026-06-01')"
    )
    conn.execute(
        "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
        "VALUES ('EW_HIER_v1', 1, '[]', '{}', '2026-06-01')"
    )

    # All 4 members in a single sector group ("A") so the group median spans
    # exactly this set of hand-computed returns.
    for sid in SIMPLE_RETURNS:
        conn.execute(
            "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, "
            "family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, "
            "quintile, scored, eligible, n_factors_used, input_hash, generated_at, track, run_id) "
            "VALUES ('live:2026-06-30', '2026-06-30', ?, 'EW_HIER_v1', 1, 'A', 1, '{}', 0.5, 0.5, 0.0, 0.5, ?, ?, ?, 5, 3, "
            "1, 1, 5, 'h1', '2026-07-01', 'live', 1)",
            (sid, sid, sid, sid),
        )

    # Start prices at the cohort as_of: every security starts at the same TRI.
    for sid in SIMPLE_RETURNS:
        conn.execute(
            "INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id) "
            "VALUES ('live:2026-06-30', '2026-06-30', ?, ?, ?, 'synthetic', 'sha1', 1)",
            (sid, START_TRI, START_TRI),
        )

    # End prices at the 1-month endpoint (2026-07-31), one per hand-picked
    # simple return, attached to the endpoint cohort inserted above.
    for sid, simple_ret in SIMPLE_RETURNS.items():
        end_tri = START_TRI * (1.0 + simple_ret)
        conn.execute(
            "INSERT INTO prices_monthly (cohort_id, as_of, security_id, close_raw, tri, source, price_manifest_sha, run_id) "
            "VALUES ('live:2026-07-31', '2026-07-31', ?, ?, ?, 'synthetic', 'sha2', 1)",
            (sid, end_tri, end_tri),
        )
    conn.commit()


def test_label_sign_and_magnitude_match_hand_computed_log_returns(clean_test_db, cfg):
    _seed(clean_test_db)

    ctx = RunContext(as_of="2026-07-31", kind="test", track="live", cfg=cfg, clock=None, actor=None)
    ctx.conn = clean_test_db

    res = mature(ctx, through="2026-07-31")
    assert res.status == "ok"

    rows = {
        sid: (r_log, l_rel)
        for sid, r_log, l_rel in clean_test_db.execute(
            "SELECT security_id, r_log, l_rel FROM labels WHERE cohort_id = 'live:2026-06-30' AND horizon_m = 1"
        ).fetchall()
    }
    assert set(rows) == set(SIMPLE_RETURNS)

    # Hand-computed r_log = ln(1 + simple_return) per security.
    expected_r_log = {sid: math.log(1.0 + ret) for sid, ret in SIMPLE_RETURNS.items()}
    expected_median = statistics.median(expected_r_log.values())

    for sid, ret in SIMPLE_RETURNS.items():
        r_log, l_rel = rows[sid]
        assert r_log == pytest.approx(expected_r_log[sid], abs=1e-9)
        expected_l_rel = expected_r_log[sid] - expected_median
        assert l_rel == pytest.approx(expected_l_rel, abs=1e-9)

    # The best performer (sid 1, +10%) must beat its own group median: positive
    # l_rel. The worst performer (sid 4, -12%) must trail it: negative l_rel.
    assert rows[1][1] > 0
    assert rows[4][1] < 0
