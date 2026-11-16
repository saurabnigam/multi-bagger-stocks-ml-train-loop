"""Late price admission: the dashboard header carries scored_late / price_vintage.

The header is read from the stored cohort row (source_refs_json) or, for a cohort
published before the rule existed, from a COHORT_SCORED_LATE data_quality_events row.
"""

from __future__ import annotations

import json
from pathlib import Path

from quant.db.core import apply_schema, connect
from quant.ui_export import export

AS_OF = "2026-10-30"
COHORT_ID = f"live:{AS_OF}"
CUTOFF = "2026-10-30T18:29:59.999999Z"
VINTAGE = "2026-11-02T04:10:11.000000Z"
EVENT_VINTAGE = "2026-11-01T09:00:00.000000Z"
UI_DIR = Path(__file__).resolve().parents[2] / "ui"


def _export_header(tmp_path, cfg, source_refs, *, is_clean=1, event=None):
    db_path = tmp_path / "late.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, "
            "config_sha256, registry_sha256, is_clean) VALUES (1, ?, 'monthly', 'live', 1, ?, 'ok', 's', 'c', 'g', 'r', 1)",
            (AS_OF, CUTOFF))
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, "
            "source_refs_json, published_at, generated_at, is_clean, run_id) VALUES (?, ?, 'live', ?, 'd', 'm', ?, ?, ?, ?, 1)",
            (COHORT_ID, AS_OF, CUTOFF, source_refs, CUTOFF, CUTOFF, is_clean))
        if event is not None:
            conn.execute(
                "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, detail_json) "
                "VALUES (1, ?, ?, 'WARN', 'COHORT_SCORED_LATE', ?)", (AS_OF, CUTOFF, json.dumps(event)))
    ui_out = tmp_path / "ui_out"
    export(conn, cfg.with_paths(db=db_path, ui_dir=ui_out))
    text = (ui_out / "data.js").read_text(encoding="utf-8")
    return json.loads(text.split("=", 1)[1].rstrip(";\n"))


def test_normal_cohort_is_not_late(tmp_path, cfg):
    p = _export_header(tmp_path, cfg, "[]")
    assert p["scored_late"] is False and p["price_vintage"] is None


def test_empty_and_null_source_refs_are_read_defensively(tmp_path, cfg):
    for i, refs in enumerate(("{}", None, "not json")):
        d = tmp_path / f"d{i}"
        d.mkdir()
        try:
            p = _export_header(d, cfg, refs)
        except Exception as exc:  # NOT NULL on the column is acceptable; the others must work
            assert refs is None, exc
            continue
        assert p["scored_late"] is False and p["price_vintage"] is None


def test_cohort_with_stored_pin_is_late(tmp_path, cfg):
    refs = json.dumps({"price_vintage": VINTAGE, "scored_late": True,
                       "late_price_admission": {"rule": "bar_date/1", "decision_id": "D1",
                                                "coverage_at_cutoff": 0.5, "coverage_at_vintage": 0.99}})
    p = _export_header(tmp_path, cfg, refs, is_clean=0)
    assert p["scored_late"] is True and p["price_vintage"] == VINTAGE


def test_older_cohort_named_by_event_is_late(tmp_path, cfg):
    event = {"cohort_id": COHORT_ID, "price_vintage": EVENT_VINTAGE, "rule": "bar_date/0", "basis": "derived"}
    p = _export_header(tmp_path, cfg, "[]", is_clean=0, event=event)
    assert p["scored_late"] is True and p["price_vintage"] == EVENT_VINTAGE


def test_event_for_another_cohort_does_not_flag(tmp_path, cfg):
    event = {"cohort_id": "live:2026-09-30", "price_vintage": EVENT_VINTAGE}
    p = _export_header(tmp_path, cfg, "[]", event=event)
    assert p["scored_late"] is False and p["price_vintage"] is None


def test_app_js_shows_badge_keyed_on_scored_late():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert "scored_late" in js and "SCORED LATE" in js
    assert "Closing prices were recorded after the cutoff" in js
    assert "not counted as clean live evidence" in js
    assert "sys-late-badge" in html
