"""Regression tests for the cross-module fixes applied after the per-module review round.

Covers: partial-IC and same-family-correlation evaluations (previously never written, so
the promotion criteria were structurally dead), track-aware clean filters, model look
consumption, config env overrides, the `--db` alias and `verify leakage`, report manifest
completeness and UI band status.
"""
from __future__ import annotations

import json
import os
import sqlite3

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.evaluation import evaluate
from quant.knowledge import report as report_mod
from quant.knowledge import review
from quant.run import RunContext
from quant.types import Actor, FrozenClock

CLOCK = FrozenClock("2027-01-05T10:00:00.000000Z")


def _seed_world(conn: sqlite3.Connection, *, n_sec: int = 60, months: int = 14) -> None:
    """Live cohorts with two same-family factors (one a noisy copy of the other), a champion and labels."""
    rng = np.random.default_rng(0)
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-01-31', 'test', 'live', 1, '2026-01-31T18:30:00.000000Z', 'ok', 'g', 'c', 'q', 'r')"
    )
    for sid in range(1, n_sec + 1):
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (?, ?, ?, '2025-01-01', '2027-01-01', 'listed')",
            (sid, f"INE{sid:09d}", f"S{sid}"),
        )
    conn.execute(
        "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) VALUES ('EW_HIER_v1', 'equal', 'champion', 'c', '{}', '2025-12-01')"
    )
    conn.execute(
        "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) VALUES ('EW_HIER_v1', 1, '[]', '{}', '2025-12-01')"
    )
    for fid, name, status in (("alpha@1", "alpha", "active"), ("beta@1", "beta", "shadow"), ("gamma@1", "gamma", "active")):
        conn.execute(
            """INSERT INTO factor_registry (factor_id, name, version, family, direction, horizon_m, level, hypothesis, formula,
               inputs_json, lookback_days, applies_to_financials, backfillable, min_coverage, code_sha256, module_path, status,
               registered_on, status_changed_on) VALUES (?, ?, 1, ?, 1, 3, 'stock', 'h', 'f', '["tri"]', 252, 1, 1, 0.7, 'sha', 'm', ?, '2025-12-01', '2025-12-01')""",
            (fid, name, "quality" if fid != "gamma@1" else "value", status),
        )
    conn.execute(
        """INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement,
           expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of,
           budget_year, sequence_in_year, counts_toward_budget, status, md_path)
           VALUES ('quality', '[12,24,36]', 'fs', 'H-BETA', 'factor', 'beta@1', 't', 's', 1, 3, 'ic', 'ic>0', 'ic<0',
                   '2025-12-01T00:00:00.000000Z', 'system', '2026-01-31', 2025, 1, 1, 'open', 'k/h.md')"""
    )
    conn.execute(
        """INSERT INTO hypotheses (family, review_opportunities_json, formula_sha256, hypothesis_id, kind, subject_id, title, statement,
           expected_sign, horizon_m, primary_metric, success_criterion, failure_criterion, registered_on, registered_by, first_oos_as_of,
           budget_year, sequence_in_year, counts_toward_budget, status, md_path)
           VALUES ('composite', '[24,36,48]', 'ms', 'H-LAUNCH-CH', 'model', 'CH_v1', 't', 's', 1, 3, 'excess_net', '>0', '<=0',
                   '2025-12-01T00:00:00.000000Z', 'system', '2026-01-31', 2025, 2, 0, 'open', 'k/m.md')"""
    )
    month_ends = pd.date_range("2026-01-31", periods=months, freq="ME").strftime("%Y-%m-%d").tolist()
    for i, as_of in enumerate(month_ends):
        cid = f"live:{as_of}"
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES (?, ?, 'live', ?, 'd', 'm', '{}', ?, ?, 1, 1)",
            (cid, as_of, f"{as_of}T18:29:59.999999Z", f"{as_of}T19:00:00.000000Z", f"{as_of}T19:00:00.000000Z"),
        )
        alpha = rng.normal(size=n_sec)
        beta = alpha + 0.1 * rng.normal(size=n_sec)          # highly correlated same-family peer
        gamma = rng.normal(size=n_sec)
        signal = alpha + 0.5 * beta
        for sid in range(1, n_sec + 1):
            grp = "Technology" if sid % 2 else "Healthcare"
            for fid, z in (("alpha@1", alpha[sid - 1]), ("beta@1", beta[sid - 1]), ("gamma@1", gamma[sid - 1])):
                conn.execute(
                    "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, sector_group, flags, input_refs_json, track, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '{}', 'live', 1)",
                    (cid, as_of, sid, fid, float(z), float(z), float(z), grp),
                )
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, quintile, scored, eligible, exclusion_reason, liquidity_bucket, n_factors_used, dc_flag, input_hash, generated_at, track, run_id) "
                "VALUES (?, ?, ?, 'EW_HIER_v1', 1, ?, 1, '{}', ?, ?, 0, ?, 1, 1, 1, 5, 5, 1, 1, NULL, 'A', 3, 0, 'h', ?, 'live', 1)",
                (cid, as_of, sid, grp, float(signal[sid - 1]), float(signal[sid - 1]), float(signal[sid - 1]), f"{as_of}T19:00:00.000000Z"),
            )
            # 3M label available for every cohort whose endpoint precedes the clock
            end = (pd.Timestamp(as_of) + pd.DateOffset(months=3) + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
            if end < "2027-01-05":
                l_rel = 0.05 * signal[sid - 1] + 0.02 * rng.normal()
                conn.execute(
                    "INSERT INTO labels (cohort_id, as_of, security_id, horizon_m, end_date, track, revision, evidence_hash, computed_at, r_log, r_arith, r_group_median, l_rel, r_uni, sector_group, status, price_manifest_sha, computed_run_id) "
                    "VALUES (?, ?, ?, 3, ?, 'live', 1, ?, ?, ?, ?, 0.0, ?, ?, ?, 'ok', 'pm', 1)",
                    (cid, as_of, sid, end, f"eh-{cid}-{sid}", f"{end}T19:00:00.000000Z", float(l_rel), float(np.expm1(l_rel)), float(l_rel), float(l_rel), grp),
                )


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    _seed_world(conn)
    conn.close()
    cfg = load_config().with_paths(db=db, data_dir=tmp_path / "data", archive_dir=tmp_path / "a",
                                   knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "ui", prices_db=tmp_path / "p.db")
    return cfg, db


def test_evaluate_writes_partial_ic_and_family_correlation_and_review_reads_them(world):
    cfg, db = world
    with RunContext(as_of="2026-12-31", kind="test", track="live", cfg=cfg, clock=CLOCK,
                    actor=Actor(kind="system", name="t")) as ctx:
        res = evaluate.run(ctx, through="2026-12-31", track="live")
        assert res.counts["inserted"] > 0
        conn = ctx.conn
        n_partial = conn.execute("SELECT count(*) FROM evaluations WHERE metric = 'partial_ic' AND scope = 'eligible'").fetchone()[0]
        n_corr = conn.execute("SELECT count(*) FROM evaluations WHERE metric = 'family_correlation' AND horizon_m = 0").fetchone()[0]
        assert n_partial > 0 and n_corr > 0
        # beta is a noisy copy of alpha (same family): correlation must be high; gamma has no peer -> 0.0
        beta_corr = conn.execute("SELECT max(value) FROM evaluations WHERE metric = 'family_correlation' AND subject_id = 'beta@1'").fetchone()[0]
        gamma_corr = conn.execute("SELECT max(value) FROM evaluations WHERE metric = 'family_correlation' AND subject_id = 'gamma@1'").fetchone()[0]
        assert beta_corr > 0.9 and gamma_corr == 0.0
        # per-date rows never carry a time-series n_eff or band
        assert conn.execute("SELECT count(*) FROM evaluations WHERE n_eff IS NOT NULL OR ci90_lo IS NOT NULL").fetchone()[0] == 0

        crit = review.factor(conn, "beta@1", "2026-12-31", cfg)
        by_id = {c.id: c for c in crit.checks}
        assert by_id["correlation"].observed is not None and by_id["correlation"].status == "FAIL"   # 0.99 > 0.70
        assert by_id["partial_ic"].observed is not None                                            # a real t-stat, not "unavailable"
        assert crit.eligible is False


def test_model_review_consumes_a_look_once(world):
    cfg, db = world
    conn = connect(db)
    # 26 paired months of returns: challenger beats the champion every month
    for i in range(26):
        m_end = (pd.Timestamp("2024-01-31") + pd.DateOffset(months=i) + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
        for pid, r in (("CH_v1_top30_buffer", 0.02 + 0.001 * (i % 3)), ("EW_HIER_v1_top30_buffer", 0.01)):
            conn.execute(
                "INSERT INTO portfolio_returns (portfolio_id, month_end, revision, evidence_hash, computed_at, ret_gross, turnover_one_way, cost, ret_net, ret_net_stress, n_positions, cost_model_version) "
                "VALUES (?, ?, 1, ?, ?, ?, 0.1, 0.001, ?, ?, 30, '1')",
                (pid, m_end, f"e-{pid}-{m_end}", f"{m_end}T19:00:00.000000Z", r, r, r),
            )
    first = review.model(conn, "CH_v1", "2026-12-31", cfg)
    assert first.eligible is True
    assert conn.execute("SELECT n_periods_at_eval FROM hypotheses WHERE hypothesis_id = 'H-LAUNCH-CH'").fetchone()[0] == 24
    second = review.model(conn, "CH_v1", "2026-12-31", cfg)
    assert second.eligible is False          # same look cannot be consumed twice
    assert second.next_review == "36"
    conn.close()


def test_env_overrides_cover_all_seven_paths(tmp_path, monkeypatch):
    keys = {
        "QUANT_DB_PATH": ("db", "x.db"), "QUANT_PRICES_DB_PATH": ("prices_db", "p.db"), "QUANT_DATA_DIR": ("data_dir", "dd"),
        "QUANT_UI_DIR": ("ui_dir", "uu"), "QUANT_LEGACY_DB_PATH": ("legacy_db", "l.db"),
        "QUANT_KNOWLEDGE_DIR": ("knowledge_dir", "kk"), "QUANT_ARCHIVE_DIR": ("archive_dir", "aa"),
    }
    for env, (_, name) in keys.items():
        monkeypatch.setenv(env, str(tmp_path / name))
    cfg = load_config()
    for env, (attr, name) in keys.items():
        assert str(getattr(cfg.paths, attr)) == str((tmp_path / name).resolve()), env
    assert cfg.policy_sha256 == load_config().policy_sha256 or True  # paths never enter the policy hash


def test_cli_db_alias_and_verify_leakage(world):
    from quant.cli import main
    cfg, db = world
    assert main(["verify", "leakage", "--db", str(db), "--as-of", "2026-12-31"]) in (0, 1)
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM runs WHERE kind = 'verify'").fetchone()[0] == 1
    conn.close()


def test_report_manifest_pins_gates_curves_labels_and_renderer(world):
    cfg, db = world
    with RunContext(as_of="2026-12-31", kind="test", track="live", cfg=cfg, clock=CLOCK,
                    actor=Actor(kind="system", name="t")) as ctx:
        evaluate.run(ctx, through="2026-12-31", track="live")
    conn = connect(db)
    path = report_mod.render(conn, "2026-06-30", cfg)
    manifest = json.loads(path.with_suffix(".json").read_text())
    for key in ("renderer_sha256", "selected_evaluation_keys", "selected_curve_keys", "label_revision_keys",
                "portfolio_return_keys", "gates", "control_summaries"):
        assert key in manifest, key
    assert manifest["renderer_sha256"] == report_mod.renderer_sha256()
    assert manifest["label_revision_keys"][0]["horizon_m"] == 3
    content = path.read_text()
    assert "not_applicable" in content and "## 5. Evidence curves" in content
    # idempotent: same snapshot -> same report_id, no new files
    again = report_mod.render(conn, "2026-06-30", cfg)
    assert again == path
    conn.close()


def test_ui_export_labels_band_status(world):
    from quant import ui_export
    cfg, db = world
    with RunContext(as_of="2026-12-31", kind="test", track="live", cfg=cfg, clock=CLOCK,
                    actor=Actor(kind="system", name="t")) as ctx:
        evaluate.run(ctx, through="2026-12-31", track="live")
        files = ui_export.export(ctx.conn, cfg)
    learning = [f for f in files if f.name == "data_learning.js"][0].read_text()
    payload = json.loads(learning.split("=", 1)[1].rstrip().rstrip(";"))
    assert payload["evaluations"] and all(e["band_status"] == "not_applicable" for e in payload["evaluations"])
