"""Integration tests for WS11.02: Offline UI and evidence export (C11)."""

import json
from pathlib import Path
import re
import sqlite3
import pytest

from quant.config import Config
from quant.db.core import apply_schema, connect
from quant.ui_export import export


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test_ui.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    return conn, db_path


def test_offline_no_font_or_cdn_requests():
    """Verify HTML and CSS have NO external CDN or web font requests; chart is locally vendored."""
    index_html = Path("ui/index.html").read_text(encoding="utf-8")
    style_css = Path("ui/style.css").read_text(encoding="utf-8")

    # No external http or https references in index.html (no CDNs, no Google Fonts)
    urls = re.findall(r'(?:href|src)=["\'](https?://[^"\']+)["\']', index_html)
    assert not urls, f"Found external URL requests in ui/index.html: {urls}"

    # No @import url(https://...) in CSS
    css_urls = re.findall(r'@import\s+url\(["\']?(https?://[^"\')]+)["\']?\)', style_css)
    assert not css_urls, f"Found external CSS imports in ui/style.css: {css_urls}"

    # Vendored Chart.js and VERSION exist
    chart_js = Path("ui/vendor/chart.umd.js")
    version_file = Path("ui/vendor/VERSION")
    assert chart_js.exists() and chart_js.stat().st_size > 10000
    assert version_file.exists() and version_file.read_text(encoding="utf-8").strip()


def test_eight_tabs_declared_in_html_and_js():
    """Verify all 8 tabs (Ranking, Learning, Scoreboard, Factors, Sectors, Data, Knowledge, Legacy) exist."""
    index_html = Path("ui/index.html").read_text(encoding="utf-8")
    app_js = Path("ui/app.js").read_text(encoding="utf-8")

    expected_tabs = [
        "ranking", "learning", "scoreboard", "factors",
        "sectors", "data", "knowledge", "legacy"
    ]

    for tab in expected_tabs:
        # Check tab ID or data-tab in HTML
        assert f'id="tab-{tab}"' in index_html or f'data-tab="{tab}"' in index_html, f"Missing tab: {tab} in ui/index.html"
        # Check tab handler in JS
        assert tab in app_js.lower(), f"Missing tab handler for: {tab} in ui/app.js"


def test_ui_export_produces_all_five_payloads(tmp_path, cfg, test_db):
    """ui_export.export generates all 5 payload files with valid window globals."""
    conn, db_path = test_db
    ui_out = tmp_path / "ui_out"
    ui_out.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)

    exported_files = export(conn, test_cfg)
    expected_filenames = [
        "data.js",
        "data_learning.js",
        "data_scoreboard.js",
        "data_factors.js",
        "data_kb.js",
    ]
    exported_names = [p.name for p in exported_files]
    for name in expected_filenames:
        assert name in exported_names, f"Expected {name} in exported files: {exported_names}"
        content = (ui_out / name).read_text(encoding="utf-8")
        assert "window.QUANT_" in content


def test_export_rejects_missing_band_labeled_estimable(tmp_path, cfg, test_db):
    """Export raises ValueError on a claimed estimable interval whose endpoints (ci_lo, ci_hi) are absent."""
    conn, db_path = test_db
    ui_out = tmp_path / "ui_out"
    ui_out.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)

    # Insert an evaluation row claiming status='estimable' but ci90_lo is NULL
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'monthly', 'live', 1, '2026-09-30T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            """
            INSERT INTO evaluations (
                eval_id, computed_run_id, computed_at, subject_kind, subject_id, subject_version,
                as_of, horizon_m, scope, track, metric, value, n, n_eff, se, ci90_lo, ci90_hi,
                status, method, evidence_hash, revision
            ) VALUES (
                1, 1, '2026-09-30T18:30:00.000000Z', 'model', 'CHAMPION', '1',
                '2026-09-30', 3, 'universe', 'live', 'rank_ic', 0.05, 500, 480, 0.02, NULL, NULL,
                'estimable', 'spearman', 'ev_hash', 1
            )
            """
        )

    with pytest.raises(ValueError, match="Claimed estimable interval whose endpoints are absent"):
        export(conn, test_cfg)


def test_empty_blocked_live_legacy_states_supported():
    """Verify empty/blocked/live/legacy indicators and state containers exist in UI markup and JS."""
    index_html = Path("ui/index.html").read_text(encoding="utf-8")
    app_js = Path("ui/app.js").read_text(encoding="utf-8")

    for state in ("empty", "blocked", "legacy", "live"):
        assert state in index_html.lower() or state in app_js.lower(), f"State {state} missing from UI"


def test_shows_generation_exec_dates_and_pending_orders(tmp_path, cfg, test_db):
    """Exported payloads include generation/exec dates, track, and pending orders."""
    conn, db_path = test_db
    ui_out = tmp_path / "ui_out"
    ui_out.mkdir(parents=True, exist_ok=True)
    test_cfg = cfg.with_paths(db=db_path, ui_dir=ui_out)

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
            "VALUES (1, '2026-09-30', 'monthly', 'live', 1, '2026-09-30T18:30:00.000000Z', 'ok', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES ('live:2026-09-30', '2026-09-30', 'live', '2026-09-30T18:29:59.999999Z', 'def', 'mem', '[]', '2026-09-30T18:30:00.000000Z', '2026-09-30T18:30:00.000000Z', 1, 1)"
        )
        conn.execute("INSERT OR IGNORE INTO portfolios (portfolio_id, subject_kind, subject_id, subject_version, rule, cadence, rule_version) VALUES ('P_MAIN', 'portfolio', 'P_MAIN', '1', 'top_decile', 'monthly', '1')")
        conn.execute("INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) VALUES (1, 'INE001', 'Stock 1', '2020-01-01', '2026-10-01', 'listed')")
        conn.execute(
            """
            INSERT INTO portfolio_orders (
                order_id, portfolio_id, cohort_id, security_id, created_at, earliest_exec_at,
                purpose, side, target_weight, status, liquidity_bucket
            ) VALUES (
                'ORD1', 'P_MAIN', 'live:2026-09-30', 1, '2026-09-30T18:30:00.000000Z',
                '2026-10-01T03:45:00.000000Z', 'rebalance', 'buy', 0.05, 'pending', 'top_adv'
            )
            """
        )

    export(conn, test_cfg)

    sb_content = (ui_out / "data_scoreboard.js").read_text(encoding="utf-8")
    assert "pending_orders" in sb_content
    assert "earliest_exec_at" in sb_content
    assert "ORD1" in sb_content
