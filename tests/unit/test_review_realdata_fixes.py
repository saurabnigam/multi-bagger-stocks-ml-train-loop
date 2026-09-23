"""Regression tests for defects found by running the engine on real data (2026-09-23 review).

1. composite output depended on PYTHONHASHSEED (set iteration order of family names);
2. universe capture left two open symbol_history rows per security;
3. the monthly runner accepted a mid-month as_of for the live track;
4. every capture re-inserted unchanged fundamental facts (unbounded state growth);
5. a vendor hole on an earlier session passed silently (only the as_of bar was checked).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.run import RunContext, month_end_session, monthly
from quant.types import Actor, FrozenClock

REPO = Path(__file__).resolve().parents[2]

COMPOSE_SNIPPET = r"""
import json, numpy as np, pandas as pd
from quant.config import load
from quant.model.composite import compose
rng = np.random.default_rng(3)
idx = list(range(1, 41))
fams = ["momentum", "low_risk", "quality", "value", "growth", "flows"]
defs = pd.DataFrame([{"factor_id": f"{f}_{k}@1", "family": f, "status_weight": 1.0, "nonfinancial": False}
                     for f in fams for k in (1, 2)])
z = pd.DataFrame(rng.normal(size=(40, len(defs))), index=idx, columns=defs.factor_id)
groups = pd.Series(["A" if i % 2 else "B" for i in idx], index=idx)
units = {f: 10000 // len(fams) for f in fams}
units[fams[0]] += 10000 - sum(units.values())
out = compose(z, defs, units, groups, load())
print(json.dumps({"fs": out["family_scores_json"].tolist(), "c": [repr(x) for x in out["composite"].tolist()]}))
"""


def _compose_with_seed(seed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(REPO))
    res = subprocess.run([sys.executable, "-c", COMPOSE_SNIPPET], env=env, capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_composite_is_identical_across_hash_seeds():
    a, b = _compose_with_seed("1"), _compose_with_seed("12345")
    assert a["fs"] == b["fs"], "family_scores_json must not depend on PYTHONHASHSEED"
    assert a["c"] == b["c"], "composite floats must be bit-identical across processes"
    keys = list(json.loads(a["fs"][0]).keys())
    assert keys == sorted(keys)


def _fresh(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d",
                                   archive_dir=tmp_path / "a", knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    return cfg, db


def test_universe_symbol_upsert_keeps_one_open_row(tmp_path):
    from quant.data.universe import _upsert_symbol

    cfg, db = _fresh(tmp_path)
    with RunContext(as_of="2026-09-11", kind="capture", track="live", cfg=cfg,
                    clock=FrozenClock("2026-09-11T12:00:00.000000Z"), actor=Actor(kind="system", name="t")) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'X', '2026-06-12', '2026-09-11', 'listed')")
        # state left by earlier releases: migration row + capture row, both open, same symbol
        ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                         "VALUES (1, 'XYZ', 'XYZ.NS', '2026-06-12', 'legacy_snapshot')")
        ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                         "VALUES (1, 'XYZ', 'XYZ.NS', '2026-09-11', 'nifty500_csv')")
        _upsert_symbol(ctx, 1, "XYZ", "2026-09-30")
        open_rows = ctx.conn.execute("SELECT valid_from FROM symbol_history WHERE security_id = 1 AND valid_to IS NULL").fetchall()
        assert [r[0] for r in open_rows] == ["2026-06-12"]
        # a symbol change closes the open row and opens a new one
        _upsert_symbol(ctx, 1, "XYZNEW", "2026-10-31")
        rows = ctx.conn.execute("SELECT nse_symbol, valid_from, valid_to FROM symbol_history WHERE security_id = 1 "
                                "ORDER BY valid_from").fetchall()
        assert [tuple(r) for r in rows] == [("XYZ", "2026-06-12", "2026-10-31"), ("XYZ", "2026-09-11", "2026-09-11"),
                                            ("XYZNEW", "2026-10-31", None)]
        ctx.status = "ok"


def test_month_end_session_and_mid_month_refusal(tmp_path):
    assert month_end_session("2026-09-11", None) == "2026-09-30"
    assert month_end_session("2026-05-15", None) == "2026-05-29"      # 31 May 2026 is a Sunday
    cfg, db = _fresh(tmp_path)
    code = monthly(cfg, FrozenClock("2026-09-14T13:58:30.000000Z"), Actor(kind="system", name="t"),
                   as_of="2026-09-11", skip_capture=True)
    assert code == 1
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM runs WHERE kind = 'monthly'").fetchone()[0] == 0
    conn.close()


def _bundle(fetched_at: str, revenue_latest: float):
    from quant.data.yahoo import RawBundle

    stmt = pd.DataFrame({"2026-03-31": [revenue_latest, 20.0], "2025-03-31": [90.0, 18.0]},
                        index=["Total Revenue", "Net Income"])
    return RawBundle(ticker="X.NS", fetched_at=fetched_at, info={}, statements={"income_stmt": stmt},
                     earnings_dates=pd.DataFrame())


def test_unchanged_fundamental_facts_are_not_reinserted(tmp_path):
    from quant.data.fundamentals import ingest, pit_frame

    cfg, db = _fresh(tmp_path)
    with RunContext(as_of="2026-09-30", kind="capture", track="live", cfg=cfg,
                    clock=FrozenClock("2026-10-01T12:00:00.000000Z"), actor=Actor(kind="system", name="t")) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, 'INE000000001', 'X', '2026-06-12', '2026-09-30', 'listed')")
        r1 = ingest(ctx, {1: _bundle("2026-09-11T09:00:00.000000Z", 100.0)})
        r2 = ingest(ctx, {1: _bundle("2026-09-25T09:00:00.000000Z", 100.0)})       # nothing changed
        r3 = ingest(ctx, {1: _bundle("2026-09-28T09:00:00.000000Z", 101.0)})       # one restatement
        assert (r1.counts["rows"], r2.counts["rows"], r3.counts["rows"]) == (4, 0, 1)
        assert r2.counts["unchanged"] == 4 and r3.counts["unchanged"] == 3
        n = ctx.conn.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
        assert n == 5
        # PIT: before the restatement's fetch the original value is visible; after, the new one
        early = pit_frame(ctx.conn, "2026-09-27T00:00:00.000000Z", "income", "Total Revenue", "A", 1, [1])
        late = pit_frame(ctx.conn, "2026-09-30T00:00:00.000000Z", "income", "Total Revenue", "A", 1, [1])
        assert early.loc[1, 0] == 100.0 and late.loc[1, 0] == 101.0
        ctx.status = "ok"


def test_price_gap_warning_flags_vendor_holes(tmp_path):
    from quant.data import gates
    from quant.data.prices import PriceStore
    from quant.types import Draft

    cfg, db = _fresh(tmp_path)
    dates = pd.bdate_range("2026-06-01", "2026-09-11").strftime("%Y-%m-%d")
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg,
                    clock=FrozenClock("2026-09-11T19:00:00.000000Z"), actor=Actor(kind="system", name="t")) as ctx:
        ctx.store = PriceStore(tmp_path / "p.db", state_conn=ctx.conn)
        for sid in range(1, 11):
            frame = pd.DataFrame({"date": dates, "close": 100.0 + np.arange(len(dates)), "volume": 1000.0})
            if sid <= 4:                       # 4 of 10 members miss one session (like 2026-09-07)
                frame = frame[frame["date"] != "2026-09-07"]
            ctx.store.ingest(ctx, frame, {"security_id": sid, "close_basis": "raw",
                                          "observed_at": "2026-09-11T11:00:00.000000Z"})
        draft = Draft(cohort_id="live:2026-09-11", as_of="2026-09-11", track="live",
                      knowledge_cutoff="2026-09-11T18:29:59.999999Z", definition_hash="d",
                      members=pd.DataFrame({"security_id": range(1, 11)}), groups=pd.Series({i: "A" for i in range(1, 11)}))
        chk = gates._price_gap_check(ctx, draft, list(range(1, 11)))
        assert chk.status == "FAIL" and chk.blocking is False
        assert chk.observed["members_with_gaps"] == 4 and chk.observed["worst_dates"] == {"2026-09-07": 4}
        assert ctx.conn.execute("SELECT count(*) FROM data_quality_events WHERE code = 'PRICE_GAPS'").fetchone()[0] == 1
        ctx.status = "ok"
