"""Regression tests for work package uiexport (decision D9 / task T7).

The exporter must publish only stored V2 values for a published cohort: no legacy
multiplier/death-cross/value-trap narrative, no unit-repair thresholds, no statistics
computed in the exporter, the knowledge cutoff respected for any fundamental shown, no
read of the frozen legacy `quant_engine.db`, and a total ui/data*.js payload under the
configured budget (MASTER_SPEC 6.1, 10.6, 10.7; AGENTS.md offline UI rules).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from quant.db.core import apply_schema, connect
from quant.ui_export import export

N_SECURITIES = 501
AS_OF = "2026-09-30"
COHORT_ID = f"live:{AS_OF}"
CUTOFF = "2026-09-30T18:29:59.999999Z"
AFTER_CUTOFF = "2026-10-05T00:00:00.000000Z"
BEFORE_CUTOFF = "2026-07-20T00:00:00.000000Z"
RNG = __import__("numpy").random.default_rng(7)
FACTOR_SET = tuple((name, 1, fam, 1) for name, fam in (
    ("mom_12_1", "momentum"), ("mom_6_1", "momentum"), ("trend_200", "momentum"), ("dist_52w_high", "momentum"),
    ("rev_1m", "momentum"), ("vol_252", "low_risk"), ("max_ret_21", "low_risk"), ("roce", "quality"),
    ("accruals", "quality"), ("cash_conversion_3y", "quality"), ("leverage", "quality"), ("roe_stability_3y", "quality"),
    ("earnings_yield", "value"), ("book_to_price", "value"), ("fcf_yield", "value"), ("div_yield", "value"),
    ("eps_growth_3y", "growth"), ("rev_growth_3y", "growth"), ("earn_mom", "growth"), ("inst_hold_chg_3m", "flows"),
    ("size", "control"), ("liq", "control"), ("beta_252", "control"), ("dc_flag", "legacy"),
))


@pytest.fixture
def published_cohort_db(tmp_path):
    """A tmp state DB with a published 501-security V2 cohort: scores, factor_values,
    a model + model_weights, and a fundamentals row on each side of the knowledge cutoff.
    """
    db_path = tmp_path / "wp_ui_export.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, "
            "git_sha, code_sha256, config_sha256, registry_sha256, is_clean) "
            "VALUES (1, ?, 'monthly', 'live', 1, ?, 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha', 1)",
            (AS_OF, CUTOFF),
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES (?, ?, 'live', ?, 'def', 'mem', '[]', ?, ?, 1, 1)",
            (COHORT_ID, AS_OF, CUTOFF, CUTOFF, CUTOFF),
        )
        conn.execute(
            "INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
            "VALUES ('EW_HIER_v1', 'equal', 'champion', 'Equal-weight hierarchical champion', '{}', ?)",
            (AS_OF,),
        )
        conn.execute(
            "INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
            "VALUES ('EW_HIER_v1', 1, '[]', '{}', ?)",
            (AS_OF,),
        )
        conn.execute(
            "INSERT INTO model_weights (cohort_id, model_id, model_version, as_of, family, "
            "weight_units, gate, evidence_hash, run_id) "
            "VALUES (?, 'EW_HIER_v1', 1, ?, 'growth', 5000, 'ew', 'ev', 1)",
            (COHORT_ID, AS_OF),
        )
        conn.execute(
            "INSERT INTO model_weights (cohort_id, model_id, model_version, as_of, family, "
            "weight_units, gate, evidence_hash, run_id) "
            "VALUES (?, 'EW_HIER_v1', 1, ?, 'quality', 5000, 'ew', 'ev', 1)",
            (COHORT_ID, AS_OF),
        )

        for name, version, family, direction in FACTOR_SET:
            conn.execute(
                "INSERT INTO factor_registry (factor_id, name, version, family, direction, "
                "horizon_m, level, hypothesis, formula, inputs_json, lookback_days, "
                "applies_to_financials, backfillable, min_coverage, code_sha256, module_path, "
                "status, registered_on, status_changed_on) "
                "VALUES (?, ?, ?, ?, ?, 12, 'stock', 'h', 'f', '[]', 365, 1, 0, 0.7, 'sha', "
                "'quant.factors', 'active', ?, ?)",
                (f"{name}@{version}", name, version, family, direction, AS_OF, AS_OF),
            )

        sec_rows = []
        symbol_rows = []
        score_rows = []
        factor_rows = []
        quarterly_rows = []
        n_scored = N_SECURITIES
        for i in range(1, N_SECURITIES + 1):
            sid = i
            sec_rows.append((sid, f"INE{i:06d}A01", f"Company {i}", "2015-01-01", "2026-10-01", "listed"))
            symbol_rows.append((sid, f"SYM{i}", f"SYM{i}.NS", "2015-01-01", "nse_csv"))

            eligible = 1 if i <= 480 else 0
            scored = 1
            final = round(1000.0 - i, 4)
            composite = round(final / 10.0, 4)
            rank_asc = n_scored - i + 1
            quintile = min(5, 1 + (rank_asc - 1) * 5 // n_scored)
            decile = min(10, 1 + (rank_asc - 1) * 10 // n_scored)
            family_scores = json.dumps({"growth": round((250 - i) / 100.0, 4), "quality": round((i - 250) / 300.0, 4)}, sort_keys=True)
            sector_group = "Financial Services" if i % 7 == 0 else "Industrials"

            score_rows.append((
                COHORT_ID, AS_OF, sid, "EW_HIER_v1", 1, sector_group, 1,
                family_scores, composite, composite, 0.0, final,
                i, i, i, decile, quintile,
                scored, eligible, None if eligible else "coverage", "A", 8, 0,
                "hash", CUTOFF, "live", 1,
            ))

            # Realistic size: every launch factor, full-precision floats and mixed flags, so the
            # payload budget is tested against what a real cohort serialises (a 7-factor fixture
            # with identical values passed while the real 501-name payload was 4.0 MB).
            for k, (fname, _v, _fam, _d) in enumerate(FACTOR_SET):
                if fname == "fcf_yield":
                    raw_val = -0.02 if i == 1 else 0.03 + i * 1e-5
                else:
                    raw_val = float(RNG.normal()) * (10 ** (k % 5))
                z_val = float(RNG.normal())
                flag = ("", "", "", "small_group", "missing", "not_applicable")[(i + k) % 6]
                factor_rows.append((
                    COHORT_ID, AS_OF, sid, f"{fname}@1", raw_val, raw_val, z_val, sector_group,
                    flag, "[]", "live", 1,
                ))
            if sid > 1:
                for q in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"):
                    for field in ("Total Revenue", "EBITDA", "Net Income"):
                        quarterly_rows.append((sid, q, field, float(RNG.uniform(1e8, 5e10))))

        conn.executemany(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            sec_rows,
        )
        conn.executemany(
            "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
            "VALUES (?, ?, ?, ?, ?)",
            symbol_rows,
        )
        conn.executemany(
            """
            INSERT INTO scores (
                cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version,
                family_scores_json, composite, composite_neutral, sector_tilt, final,
                rank_all, rank, rank_group, decile, quintile,
                scored, eligible, exclusion_reason, liquidity_bucket, n_factors_used, dc_flag,
                input_hash, generated_at, track, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            score_rows,
        )
        conn.executemany(
            "INSERT INTO factor_values (cohort_id, as_of, security_id, factor_id, raw, winsor, z, "
            "sector_group, flags, input_refs_json, track, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            factor_rows,
        )

        conn.executemany(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) "
            f"VALUES (?, 'income', 'Q', ?, ?, ?, 'inr', '{BEFORE_CUTOFF}', 'earnings_date', '{BEFORE_CUTOFF}', 'yahoo', 1)",
            quarterly_rows,
        )

        # A fundamental visible as of the cohort's knowledge cutoff.
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) "
            "VALUES (1, 'income', 'Q', '2026-06-30', 'Total Revenue', 1000000000.0, 'inr', ?, "
            "'earnings_date', ?, 'yahoo', 1)",
            (BEFORE_CUTOFF, BEFORE_CUTOFF),
        )
        # A later restatement/fetch that only became available AFTER the knowledge cutoff:
        # must never be shown for this cohort (no lookahead leakage, MASTER_SPEC 4.5/G1/G6).
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) "
            "VALUES (1, 'income', 'Q', '2026-09-30', 'Total Revenue', 9999000000.0, 'inr', ?, "
            "'earnings_date', ?, 'yahoo', 1)",
            (AFTER_CUTOFF, AFTER_CUTOFF),
        )

    return conn, db_path


def test_ui_export_publishes_v2_only_within_budget_and_respects_cutoff(tmp_path, cfg, published_cohort_db):
    conn, db_path = published_cohort_db
    ui_out = tmp_path / "ui_out"
    ui_out.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)

    # The exporter must never open the frozen legacy quant_engine.db.
    orig_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if "quant_engine.db" in str(database):
            raise AssertionError("export() must not open the frozen legacy quant_engine.db")
        return orig_connect(database, *args, **kwargs)

    import quant.ui_export as ui_export_mod

    original_module_connect = ui_export_mod.sqlite3.connect
    ui_export_mod.sqlite3.connect = guarded_connect
    try:
        exported = export(conn, test_cfg)
    finally:
        ui_export_mod.sqlite3.connect = original_module_connect

    data_files = [p for p in exported if p.name.startswith("data") and p.name.endswith(".js")]
    assert data_files, "expected ui/data*.js files to be exported"

    total_bytes = sum(p.stat().st_size for p in data_files)
    budget = test_cfg.budgets.ui_payload_bytes
    assert total_bytes < budget, f"ui/data*.js payload {total_bytes} bytes exceeds budget {budget} bytes"

    all_text = "\n".join(p.read_text(encoding="utf-8") for p in data_files)

    # No legacy prediction-based-multiplier / death-cross / value-trap narrative anywhere in V2 data.
    for forbidden in ("MULTIPLIER", "Death Cross", "Value Trap"):
        assert forbidden not in all_text, f"forbidden legacy narrative {forbidden!r} found in exported UI payload"

    # A fundamental that only became available after the cohort's knowledge cutoff must not be shown:
    # neither its raw value (in Cr) nor its period should appear anywhere in the payload.
    assert "999.9" not in all_text
    assert "2026-09-30" not in _data_js_stocks_text(ui_out)

    data_payload = json.loads((ui_out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
    stocks_by_id = {s["security_id"]: s for s in data_payload["stocks"]}
    assert len(stocks_by_id) == N_SECURITIES

    sec1 = stocks_by_id[1]
    assert sec1["quarterly"]["latest_quarter"] == "2026-06-30"
    assert sec1["quarterly"]["revenue_cr"] == pytest.approx(100.0)

    # accepted/rejected/turnaround are id arrays, not duplicated stock objects (payload budget item 3).
    assert all(isinstance(x, int) for x in data_payload["accepted"])
    assert all(isinstance(x, int) for x in data_payload["rejected"])
    assert all(isinstance(x, int) for x in data_payload["turnaround"])
    assert set(data_payload["accepted"]) <= set(stocks_by_id.keys())

    # No orphaned top-level duplicated globals.
    data_js_raw = (ui_out / "data.js").read_text(encoding="utf-8")
    for orphan in ("acceptedStocks", "rejectedStocks", "turnaroundStocks"):
        assert orphan not in data_js_raw


def _data_js_stocks_text(ui_out: Path) -> str:
    payload = json.loads((ui_out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
    return json.dumps(payload["stocks"])


def test_ui_export_still_succeeds_when_legacy_db_open_would_fail(tmp_path, cfg, published_cohort_db, monkeypatch):
    """Even if quant_engine.db is present but unreadable, export() must not depend on it."""
    conn, db_path = published_cohort_db
    ui_out = tmp_path / "ui_out2"
    ui_out.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)

    def blow_up(*args, **kwargs):
        raise AssertionError("legacy quant_engine.db must not be opened by the V2 exporter")

    monkeypatch.setattr(sqlite3, "connect", blow_up)
    exported = export(conn, test_cfg)
    assert exported


def test_ui_export_factor_rows_are_compact_and_gates_come_from_the_cohort(tmp_path, cfg, published_cohort_db):
    """Integration finding: factor evidence was 1.6 MB of verbose objects on a real cohort and the
    gates panel was a hard-coded all-PASS list. Rows now reference one catalog, and gates are the
    cohort's recorded dq_runs (none recorded here, so none shown)."""
    conn, db_path = published_cohort_db
    ui_out = tmp_path / "ui_compact"
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)
    export(conn, test_cfg)
    payload = json.loads((ui_out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
    assert len(payload["factor_catalog"]) == len(FACTOR_SET)
    row = payload["stocks"][0]["factors"][0]
    assert isinstance(row, list) and len(row) == 4
    assert payload["factor_flags"][0] == ""
    assert payload["gates_audit"] == []


def test_ui_export_unscored_names_publish_null_scores(tmp_path, cfg):
    """Verification finding: unscored names (final NULL in the store) were exported as 0.0 and
    counted in sector averages. They must publish null and stay out of the averages."""
    db_path = tmp_path / "unscored.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    with conn:
        conn.execute("INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
                     "config_sha256, registry_sha256, is_clean) VALUES (1, ?, 'monthly', 'live', 1, ?, 'ok', 's', 'c', 'g', 'r', 1)",
                     (AS_OF, CUTOFF))
        conn.execute("INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
                     "source_refs_json, published_at, generated_at, is_clean, run_id) VALUES (?, ?, 'live', ?, 'd', 'm', '[]', ?, ?, 1, 1)",
                     (COHORT_ID, AS_OF, CUTOFF, CUTOFF, CUTOFF))
        conn.execute("INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
                     "VALUES ('EW_HIER_v1', 'equal', 'champion', 'c', '{}', ?)", (AS_OF,))
        conn.execute("INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
                     "VALUES ('EW_HIER_v1', 1, '[]', '{}', ?)", (AS_OF,))
        for sid, final, scored in ((1, 1.5, 1), (2, 0.5, 1), (3, None, 0)):
            conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (?, ?, ?, '2015-01-01', '2026-10-01', 'listed')", (sid, f"INE00000{sid}A01", f"Co {sid}"))
            conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                         "VALUES (?, ?, ?, '2015-01-01', 'nse_csv')", (sid, f"S{sid}", f"S{sid}.NS"))
            conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, "
                "family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, "
                "quintile, scored, eligible, exclusion_reason, liquidity_bucket, n_factors_used, dc_flag, input_hash, "
                "generated_at, track, run_id) VALUES (?, ?, ?, 'EW_HIER_v1', 1, 'Industrials', 1, '{}', ?, ?, 0, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, 'A', 8, 0, 'h', ?, 'live', 1)",
                (COHORT_ID, AS_OF, sid, final, final, final, sid if scored else None, sid if scored else None,
                 sid if scored else None, 10 if scored else None, 5 if scored else None, scored, scored,
                 None if scored else "coverage", CUTOFF))
    ui_out = tmp_path / "ui"
    export(conn, cfg.with_paths(db=db_path, ui_dir=ui_out))
    payload = json.loads((ui_out / "data.js").read_text(encoding="utf-8").split("=", 1)[1].rstrip(";\n"))
    by_id = {s["security_id"]: s for s in payload["stocks"]}
    assert by_id[3]["final_score"] is None and by_id[3]["composite"] is None
    sector = next(d for d in payload["sector_distribution"] if d["sector"] == "Industrials")
    assert sector["count"] == 3 and sector["avg_score"] == 1.0      # mean of 1.5 and 0.5 only
