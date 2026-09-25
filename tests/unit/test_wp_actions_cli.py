"""Operator CLI path for suspected corporate actions (WP `actions`, decision D3 / task T2).

Before this work package, `quant/data/actions.py` could detect a suspected action and
record an approved one, but nothing let an operator see the review queue or resolve an
entry -- so real names (TMPV, VEDL, HEG, TRENT) stayed truncated with no path forward
(docs/analysis/verification_2026-09-24.md section 5, D3/T2). These tests exercise the new
`data actions-list` / `data actions-resolve` CLI commands end to end, through
`quant.cli.main`, the same way the review harness's own CLI tests do
(tests/unit/test_review_bootstrap_cli.py). Fixture shape (frozen clock, price series with
a single unexplained jump) follows tests/unit/test_review_corporate_actions.py.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
import pytest

from quant.cli import main
from quant.config import load as load_config
from quant.data import actions
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock

EVIDENCE_AT = "2026-09-11T10:06:00.000000Z"
ISIN = "INE000000001"
SYMBOL = "TESTCO"


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
    return dates, close


def _seed_suspect(cfg):
    """Detect a suspect the way a real capture run would (actions.detect inside a RunContext)."""
    dates, close = _series()
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg,
                     clock=FrozenClock("2026-09-14T13:58:30.000000Z"),
                     actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, ?, 'P', '2025-01-01', '2026-09-11', 'listed')", (ISIN,))
        ctx.conn.execute(
            "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
            "VALUES (1, ?, ?, '2025-01-01', 'test')", (SYMBOL, f"{SYMBOL}.NS"))
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                          {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        res = actions.detect(ctx, [1], since="2025-01-01")
        assert res.counts["suspected"] == 1
        ctx.status = "ok"


def _env(monkeypatch, cfg):
    monkeypatch.setenv("QUANT_PRICES_DB_PATH", str(cfg.paths.prices_db))
    monkeypatch.setenv("QUANT_KNOWLEDGE_DIR", str(cfg.paths.knowledge_dir))
    monkeypatch.delenv("QUANT_DB_PATH", raising=False)


def _tri(conn, cfg):
    # Far-future vintage: the resolution's observed_at is stamped with the real system
    # clock (SystemClock), not the frozen clock used to seed the suspect, so the read
    # must be at least that recent to see it.
    store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    return store.tri([1], "2025-06-01", "2026-09-11", "2099-01-01T00:00:00.000000Z")[1]


def test_actions_list_shows_a_detected_suspect(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)

    assert main(["data", "actions-list", "--db", str(db), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["isin"] == ISIN
    assert row["symbol"] == SYMBOL
    assert row["ex_date"] == "2025-10-14"
    assert row["gross_factor"] == pytest.approx(0.6, abs=1e-3)
    assert row["evidence_observed_at"] == EVIDENCE_AT
    assert row["in_trailing_13m"] is True          # 2025-10-14 is within 13m of the 2026-09-11 latest bar
    assert row["resolved"] is None

    # non-JSON mode renders a human table without crashing
    assert main(["data", "actions-list", "--db", str(db)]) == 0
    text = capsys.readouterr().out
    assert SYMBOL in text and ISIN in text


def test_actions_resolve_by_human_creates_decision_adr_and_restores_tri(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)

    rc = main([
        "data", "actions-resolve", "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14",
        "--kind", "demerger", "--factor", str(600.0 / 360.0),
        "--evidence", "BSE filing dated 2025-10-10: demerger scheme record date 2025-10-14",
        "--actor-kind", "human", "--by", "human:owner",
    ])
    assert rc == 0
    capsys.readouterr()

    conn = connect(db, readonly=True)
    dec = conn.execute(
        "SELECT decision_id, kind, tier, status, approver_kind, subject_id, adr_path, decided_by "
        "FROM decisions WHERE subject_id = ?", (ISIN,)
    ).fetchone()
    assert dec is not None
    decision_id, kind, tier, status, approver_kind, subject_id, adr_path, decided_by = dec
    assert (kind, tier, status, approver_kind, subject_id) == ("data_fix", 1, "approved", "human", ISIN)
    assert "owner" in decided_by
    assert pathlib.Path(adr_path).exists()
    assert "demerger" in pathlib.Path(adr_path).read_text()

    ca = conn.execute(
        "SELECT kind, adj_factor, decision_id FROM corporate_actions "
        "WHERE security_id = 1 AND ex_date = '2025-10-14' AND decision_id IS NOT NULL"
    ).fetchone()
    assert tuple(ca) == ("demerger", pytest.approx(600.0 / 360.0), decision_id)

    tri = _tri(conn, cfg)
    assert tri.notna().all()
    assert tri.iloc[-1] / tri.iloc[0] == pytest.approx(1.0)   # economic return restored
    conn.close()

    # resolved entries drop out of the default (unresolved-only) listing
    assert main(["data", "actions-list", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    # but remain visible with --all, showing the resolution
    assert main(["data", "actions-list", "--db", str(db), "--json", "--all"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["resolved"]["decision_id"] == decision_id


def test_actions_resolve_refuses_non_human_actor(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)

    rc = main([
        "data", "actions-resolve", "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14",
        "--kind", "demerger", "--factor", "1.6667", "--evidence", "filing",
        "--actor-kind", "llm", "--by", "llm:agent",
    ])
    assert rc == 3
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM corporate_actions WHERE decision_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_actions_resolve_refuses_duplicate_resolution(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)
    common = [
        "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14", "--kind", "demerger",
        "--factor", str(600.0 / 360.0), "--evidence", "filing",
        "--actor-kind", "human", "--by", "human:owner",
    ]
    assert main(["data", "actions-resolve", *common]) == 0
    rc2 = main(["data", "actions-resolve", *common])
    assert rc2 == 3

    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
    conn.close()


def test_actions_resolve_refuses_implausible_factor_without_force(tmp_path, monkeypatch):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)          # gross_factor recorded ~0.6
    _env(monkeypatch, cfg)
    common = [
        "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14", "--kind", "manual_adj",
        "--evidence", "filing", "--actor-kind", "human", "--by", "human:owner", "--factor", "5.0",
    ]
    # 0.6 * 5.0 = 3.0, far outside [1/1.4, 1.4]
    rc = main(["data", "actions-resolve", *common])
    assert rc == 3
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    conn.close()

    rc2 = main(["data", "actions-resolve", *common, "--force"])
    assert rc2 == 0
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
    conn.close()


def test_actions_resolve_genuine_sets_factor_one_and_removes_truncation(tmp_path, monkeypatch, capsys):
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)

    rc = main([
        "data", "actions-resolve", "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14",
        "--kind", "genuine", "--evidence", "Confirmed real operating loss per Q2 filing",
        "--actor-kind", "human", "--by", "human:owner",
    ])
    assert rc == 0
    capsys.readouterr()

    conn = connect(db, readonly=True)
    ca = conn.execute(
        "SELECT kind, adj_factor FROM corporate_actions "
        "WHERE security_id = 1 AND ex_date = '2025-10-14' AND decision_id IS NOT NULL"
    ).fetchone()
    assert tuple(ca) == ("manual_adj", pytest.approx(1.0))

    tri = _tri(conn, cfg)
    assert tri.notna().all()                                   # no longer truncated
    assert tri.iloc[-1] / tri.iloc[0] == pytest.approx(0.6)     # the real loss is preserved, not adjusted away
    conn.close()


def test_actions_resolve_scheme_kind_stores_its_own_factor(tmp_path, monkeypatch):
    """Regression: 'scheme' is a VALUE_TRANSFER_KINDS member (prices.py) that must carry its
    own adj_factor, the same as demerger/rights/manual_adj -- not fall through to 1.0."""
    cfg, db = _world(tmp_path)
    _seed_suspect(cfg)
    _env(monkeypatch, cfg)

    rc = main([
        "data", "actions-resolve", "--db", str(db), "--isin", ISIN, "--ex-date", "2025-10-14",
        "--kind", "scheme", "--factor", str(600.0 / 360.0),
        "--evidence", "NCLT-approved scheme of arrangement, record date 2025-10-14",
        "--actor-kind", "human", "--by", "human:owner",
    ])
    assert rc == 0

    conn = connect(db, readonly=True)
    ca = conn.execute(
        "SELECT kind, adj_factor FROM corporate_actions "
        "WHERE security_id = 1 AND ex_date = '2025-10-14' AND decision_id IS NOT NULL"
    ).fetchone()
    assert tuple(ca) == ("scheme", pytest.approx(600.0 / 360.0))

    tri = _tri(conn, cfg)
    assert tri.notna().all()
    assert tri.iloc[-1] / tri.iloc[0] == pytest.approx(1.0)   # economic return restored, same as demerger
    conn.close()


def _seed_prices_only(cfg):
    """Prices ingested but detect() never run: an operator resolves ahead of detection."""
    dates, close = _series()
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg,
                     clock=FrozenClock("2026-09-14T13:58:30.000000Z"),
                     actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, ?, 'P', '2025-01-01', '2026-09-11', 'listed')", (ISIN,))
        ctx.conn.execute(
            "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
            "VALUES (1, ?, ?, '2025-01-01', 'test')", (SYMBOL, f"{SYMBOL}.NS"))
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                          {"security_id": 1, "close_basis": "raw", "observed_at": EVIDENCE_AT})
        ctx.status = "ok"


def test_actions_resolve_without_suspect_measures_the_jump_from_prices(tmp_path, monkeypatch):
    """Integration finding: with no suspect row the plausibility check assumed a gross factor
    of 1.0 and refused every real demerger factor; it now measures the ex-date jump."""
    common = ["--isin", ISIN, "--ex-date", "2025-10-14", "--kind", "demerger", "--evidence", "filing",
              "--actor-kind", "human", "--by", "human:owner"]
    cfg, db = _world(tmp_path / "ok")
    _seed_prices_only(cfg)
    _env(monkeypatch, cfg)
    assert main(["data", "actions-resolve", "--db", str(db), *common, "--factor", str(600.0 / 360.0)]) == 0

    cfg2, db2 = _world(tmp_path / "bad")
    _seed_prices_only(cfg2)
    _env(monkeypatch, cfg2)
    assert main(["data", "actions-resolve", "--db", str(db2), *common, "--factor", "5.0"]) == 3
