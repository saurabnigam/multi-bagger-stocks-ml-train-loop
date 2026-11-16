"""Corporate actions: detection, history truncation, approved value transfers, labels.

Real-data motivation (2026-09 review): TMPV (Tata Motors demerger, 2025-10-14, -40.2%),
VEDL (2026-04-30, -64.9%), HEG (2026-09-07, -62.6%) and TRENT (2026-01-01, -33%) carried
unexplained one-day drops in Yahoo's Close and Adj Close. The engine read them as real
losses, which pushed those names to the bottom of the ranking. MASTER_SPEC 2.3/4.3: an
unresolved action is never read as a real (or zero) return; an approved value-transfer
factor adjusts that day's gross return.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data import actions
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock

EVIDENCE_AT = "2026-09-11T10:06:00.000000Z"


def _world(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d",
                                   archive_dir=tmp_path / "a", knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    return cfg, db


def _series():
    dates = pd.bdate_range("2025-06-02", "2026-09-11").strftime("%Y-%m-%d")
    close = np.full(len(dates), 600.0)
    ex = list(dates).index("2025-10-14")
    close[ex:] = 360.0                      # demerger: 40% of value moves to the spun-off company
    return dates, close, ex


def _ctx(cfg):
    return RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg,
                      clock=FrozenClock("2026-09-14T13:58:30.000000Z"), actor=Actor(kind="human", name="owner"))


def test_detect_flags_jump_with_evidence_time_and_truncates_history(tmp_path):
    cfg, db = _world(tmp_path)
    dates, close, ex = _series()
    with _ctx(cfg) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'P', '2025-01-01', '2026-09-11', 'listed')")
        ctx.store = PriceStore(tmp_path / "p.db", state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                         {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        before = ctx.store.tri([1], "2025-06-01", "2026-09-11", "2026-09-11T18:29:59.999999Z")[1]
        assert before.iloc[-1] / before.iloc[0] == pytest.approx(0.6)         # jump read as a loss

        res = actions.detect(ctx, [1], since="2025-01-01")
        assert res.counts["suspected"] == 1
        row = ctx.conn.execute("SELECT ex_date, kind, source, observed_at FROM corporate_actions").fetchone()
        assert tuple(row) == ("2025-10-14", "suspected", "inferred", EVIDENCE_AT)
        assert actions.detect(ctx, [1], since="2025-01-01").counts["suspected"] == 0     # idempotent

        tri = ctx.store.tri([1], "2025-06-01", "2026-09-11", "2026-09-11T18:29:59.999999Z")[1]
        assert tri.loc[:"2025-10-13"].isna().all() and tri.loc["2025-10-14"] == pytest.approx(100.0)
        cs = ctx.store.close_split([1], "2025-06-01", "2026-09-11", "2026-09-11T18:29:59.999999Z")[1]
        assert cs.loc[:"2025-10-13"].isna().all()
        # the suspect carries the evidence's observation time: a vintage that predates the price
        # bars sees neither the bars nor the suspect, so earlier replays are unchanged
        assert ctx.store.corporate_actions([1], "2026-09-11T10:05:59.000000Z") == {}
        assert ctx.store.corporate_actions([1], EVIDENCE_AT) == {1: [{"ex_date": "2025-10-14", "mode": "truncate"}]}
        assert ctx.conn.execute("SELECT count(*) FROM data_quality_events WHERE code = 'SUSPECTED_CORPORATE_ACTIONS'").fetchone()[0] == 1
        ctx.status = "ok"


def test_approved_demerger_factor_restores_economic_return(tmp_path):
    cfg, db = _world(tmp_path)
    dates, close, ex = _series()
    with _ctx(cfg) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'P', '2025-01-01', '2026-09-11', 'listed')")
        ctx.conn.execute("INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
                         "evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) VALUES "
                         "('D-CA-1', 'data_fix', 1, 'INE000000001', 'demerger factor', 'c', '[]', 'approve', '[]', "
                         "'2026-09-14T00:00:00.000000Z', 'human:owner', 'human', 'approved', 'k/a.md', 'g')")
        ctx.store = PriceStore(tmp_path / "p.db", state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                         {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        actions.detect(ctx, [1], since="2025-01-01")
        actions.add(ctx, isin="INE000000001", ex_date="2025-10-14", kind="demerger", factor=600.0 / 360.0,
                    decision_id="D-CA-1")
        tri = ctx.store.tri([1], "2025-06-01", "2026-09-11", "2026-09-14T18:00:00.000000Z")[1]
        assert tri.notna().all() and tri.iloc[-1] / tri.iloc[0] == pytest.approx(1.0)
        cs = ctx.store.close_split([1], "2025-06-01", "2026-09-11", "2026-09-14T18:00:00.000000Z")[1]
        assert cs.iloc[0] == pytest.approx(360.0) and cs.iloc[-1] == pytest.approx(360.0)
        ctx.status = "ok"


def test_vendor_missed_split_applied_once(tmp_path):
    cfg, db = _world(tmp_path)
    dates = pd.bdate_range("2026-01-01", "2026-03-31").strftime("%Y-%m-%d")
    close = np.where(dates < "2026-02-02", 1000.0, 200.0)            # 1:5 split the vendor did not record
    with _ctx(cfg) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'P', '2025-01-01', '2026-09-11', 'listed')")
        ctx.conn.execute("INSERT INTO decisions (decision_id, kind, tier, subject_id, title, context, options_json, decision, "
                         "evidence_refs_json, decided_on, decided_by, approver_kind, status, adr_path, git_sha) VALUES "
                         "('D-CA-2', 'data_fix', 1, 'INE000000001', 'split', 'c', '[]', 'approve', '[]', "
                         "'2026-09-14T00:00:00.000000Z', 'human:owner', 'human', 'approved', 'k/b.md', 'g')")
        ctx.store = PriceStore(tmp_path / "p.db", state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                         {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        actions.add(ctx, isin="INE000000001", ex_date="2026-02-02", kind="split", factor=5.0, decision_id="D-CA-2")
        tri = ctx.store.tri([1], "2026-01-01", "2026-03-31", "2026-09-14T18:00:00.000000Z")[1]
        assert tri.iloc[-1] / tri.iloc[0] == pytest.approx(1.0)
        ctx.status = "ok"


def test_label_spanning_unresolved_action_is_excluded_ca(tmp_path):
    from quant.evaluation.labels import mature

    cfg, db = _world(tmp_path)
    dates = pd.bdate_range("2025-06-02", "2026-02-27").strftime("%Y-%m-%d")
    with _ctx(cfg) as ctx:
        ctx.store = PriceStore(tmp_path / "p.db", state_conn=ctx.conn)
        ctx.conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, "
            "published_at, generated_at, is_clean, run_id) VALUES ('live:2025-09-30', '2025-09-30', 'live', "
            "'2025-09-30T18:29:59.999999Z', 'd', 'm', '{}', '2025-10-01T00:00:00.000000Z', '2025-10-01T00:00:00.000000Z', 1, ?)",
            (ctx.run_id,))
        ctx.conn.execute("INSERT INTO models (model_id, kind, role, description, params_json, registered_on) "
                         "VALUES ('EW_HIER_v1', 'equal', 'champion', 'c', '{}', '2025-01-01')")
        ctx.conn.execute("INSERT INTO model_versions (model_id, version, factor_set_json, weights_json, valid_from) "
                         "VALUES ('EW_HIER_v1', 1, '[]', '{}', '2025-01-01')")
        for sid in range(1, 7):
            ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                             "VALUES (?, ?, 'S', '2025-01-01', '2026-09-11', 'listed')", (sid, f"INE00000000{sid}"))
            ctx.conn.execute(
                "INSERT INTO scores (cohort_id, as_of, security_id, model_id, model_version, sector_group, group_def_version, "
                "family_scores_json, composite, composite_neutral, sector_tilt, final, rank_all, rank, rank_group, decile, "
                "quintile, scored, eligible, exclusion_reason, liquidity_bucket, n_factors_used, dc_flag, input_hash, "
                "generated_at, track, run_id) VALUES ('live:2025-09-30', '2025-09-30', ?, 'EW_HIER_v1', 1, 'A', 1, '{}', "
                "0, 0, 0, 0, 1, 1, 1, 5, 5, 1, 1, NULL, 'A', 9, 0, 'h', '2025-10-01T00:00:00.000000Z', 'live', ?)",
                (sid, ctx.run_id))
            close = np.linspace(100.0, 110.0, len(dates))
            if sid == 1:
                close = np.where(dates >= "2025-10-14", close * 0.6, close)
            ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                             {"security_id": sid, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        actions.detect(ctx, list(range(1, 7)), since="2025-01-01")
        mature(ctx, through="2026-02-27")
        rows = dict(ctx.conn.execute("SELECT security_id, status FROM labels WHERE horizon_m = 1").fetchall())
        assert rows[1] == "excluded_ca" and all(rows[s] == "ok" for s in range(2, 7))
        ctx.status = "ok"
