"""Regression tests for the review fixes to the CLI/verify/status module group.

MASTER_SPEC 10.3 (CLI surface) and INTERFACES C11 (quant.verify.*, quant.status.read):
- `prices backfill|update|manifest --verify` must actually dispatch (the CLI previously
  had no argparse wiring for the --isin/--ex-date/--kind/--factor/--decision-id/--event-id
  names quant.commands.prices already expected, and no backfill/update/manifest commands
  at all).
- `run backfill-track --start --end` must replay the backfill track inside a RunContext.
- `verify pit --months N` / `verify report --as-of DATE` must exist and return real exit
  codes (0 pass / 1 fail), never a fabricated PASS.
- quant.status.read must report real keys sourced from the actual `captures` table
  (not the nonexistent `source_captures`).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import quant.cli as cli
import quant.commands.prices as prices_cmd
import quant.commands.run as run_cmd
import quant.commands.verify as verify_cmd
import quant.status
import quant.verify
from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.types import CheckReport


def _fresh_cfg(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(
        db=db_path,
        prices_db=tmp_path / "prices.db",
        data_dir=tmp_path / "data",
        archive_dir=tmp_path / "archive",
        knowledge_dir=tmp_path / "knowledge",
        ui_dir=tmp_path / "ui",
    )
    return cfg, db_path


def _isolate_config_loader(monkeypatch, module, cfg):
    """Patch module.load_config/load so a real `quant.cli.main([...])` dispatch never
    touches the real repository's config/quant.toml paths (db, prices_db, data_dir, ...)."""
    for name in ("load_config", "load"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: cfg)


# --------------------------------------------------------------------------- prices backfill/update


def test_cli_prices_backfill_dispatch_empty_universe(tmp_path, monkeypatch):
    """`quant prices backfill --start --end` dispatches, runs inside a RunContext, and
    makes no network calls when the tracked universe is empty."""
    cfg, db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, prices_cmd, cfg)

    def _boom(*a, **k):
        raise AssertionError("YahooClient must not touch the network for an empty universe")

    monkeypatch.setattr(prices_cmd.YahooClient, "download_batch", _boom)

    code = cli.main(["prices", "backfill", "--start", "2020-01-01", "--end", "2020-01-31"])
    assert code == 0

    conn = connect(db_path, readonly=True)
    n_runs = conn.execute("SELECT count(*) FROM runs WHERE kind = 'prices_backfill'").fetchone()[0]
    conn.close()
    assert n_runs == 1


def test_cli_prices_update_dispatch_empty_universe(tmp_path, monkeypatch):
    """`quant prices update --through` dispatches and runs inside a RunContext."""
    cfg, db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, prices_cmd, cfg)

    def _boom(*a, **k):
        raise AssertionError("YahooClient must not touch the network for an empty universe")

    monkeypatch.setattr(prices_cmd.YahooClient, "download_batch", _boom)

    code = cli.main(["prices", "update", "--through", "2020-01-31"])
    assert code == 0

    conn = connect(db_path, readonly=True)
    n_runs = conn.execute("SELECT count(*) FROM runs WHERE kind = 'prices_update'").fetchone()[0]
    conn.close()
    assert n_runs == 1


def test_cli_prices_backfill_requires_start_and_end(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, prices_cmd, cfg)
    assert cli.main(["prices", "backfill"]) == 1


# --------------------------------------------------------------------------- prices manifest --verify


def test_cli_prices_manifest_verify_pass_then_fail_on_tamper(tmp_path, monkeypatch):
    """`quant prices manifest --verify` exits 0 on a matching manifest and 1 once the
    manifest (or the store it describes) has been tampered with -- SOURCE_CHANGED."""
    cfg, db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, prices_cmd, cfg)

    conn = connect(db_path)
    store = PriceStore(cfg.paths.prices_db, state_conn=conn)
    conn.execute(
        "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (1, 'INE000001', 'Test Co', '2015-01-01', '2026-09-30', 'listed')"
    )
    import pandas as pd

    df = pd.DataFrame({
        "date": ["2026-09-30"],
        "close": [100.0],
        "volume": [1000.0],
        "split_ratio": [1.0],
        "dividend": [0.0],
    })
    store.ingest(None, df, {
        "security_id": 1,
        "close_basis": "raw",
        "observed_at": "2026-09-30T18:29:59.999999Z",
        "capture_id": "cap_test",
        "source_sha256": "sha_test",
    })
    conn.commit()
    conn.close()

    as_of = "2026-09-30"
    manifest_path = Path(cfg.paths.data_dir) / "manifests" / f"prices_{as_of}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    store.manifest_write(manifest_path, vintage_at="2026-09-30T18:29:59.999999Z")

    code_pass = cli.main(["prices", "manifest", "--verify", "--as-of", as_of])
    assert code_pass == 0

    # MASTER_SPEC 10.3: "No fake run rows for read-only status/help/verify" -- a pure
    # verification command must never insert into `runs` (or, via the journaling
    # triggers, `ledger_events`), on either the pass or the fail path.
    check_conn = connect(db_path, readonly=True)
    n_runs_after_pass = check_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    check_conn.close()
    assert n_runs_after_pass == 0

    # Tamper with the archived manifest itself (a changed/forged evidence file).
    data = json.loads(manifest_path.read_text())
    data["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data))

    code_fail = cli.main(["prices", "manifest", "--verify", "--as-of", as_of])
    assert code_fail == 1

    check_conn = connect(db_path, readonly=True)
    n_runs_after_fail = check_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    check_conn.close()
    assert n_runs_after_fail == 0


def test_cli_prices_manifest_verify_requires_flag(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, prices_cmd, cfg)
    assert cli.main(["prices", "manifest"]) == 1


# --------------------------------------------------------------------------- run backfill-track


def test_cli_run_backfill_track_dispatch(tmp_path, monkeypatch):
    """`quant run backfill-track --start --end` dispatches and replays inside a RunContext."""
    cfg, db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, run_cmd, cfg)

    code = cli.main(["run", "backfill-track", "--start", "2020-01-01", "--end", "2020-12-31"])
    assert code == 0

    conn = connect(db_path, readonly=True)
    n_runs = conn.execute("SELECT count(*) FROM runs WHERE kind = 'backfill_track'").fetchone()[0]
    conn.close()
    assert n_runs == 1


def test_cli_run_backfill_track_requires_start_and_end(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, run_cmd, cfg)
    assert cli.main(["run", "backfill-track"]) == 1


# --------------------------------------------------------------------------- verify pit / report CLI


def test_cli_verify_pit_dispatch_empty_db(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, verify_cmd, cfg)
    assert cli.main(["verify", "pit", "--months", "6"]) == 0


def test_cli_verify_report_dispatch_fails_without_persisted_report(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, verify_cmd, cfg)
    assert cli.main(["verify", "report", "--as-of", "2026-09-30"]) == 1


def test_cli_verify_report_requires_as_of(tmp_path, monkeypatch):
    cfg, _db_path = _fresh_cfg(tmp_path)
    _isolate_config_loader(monkeypatch, verify_cmd, cfg)
    assert cli.main(["verify", "report"]) == 1


# --------------------------------------------------------------------------- verify.pit seeded violation


def test_verify_pit_fails_on_seeded_backdated_fundamentals(tmp_path):
    """A fundamentals row that claims pre-cutoff availability but was fetched after the
    cutoff is exactly the leak PD4 (MASTER_SPEC prime directive 4) prohibits; verify.pit
    must catch it, and must not flag cohorts with no such row."""
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    cutoff = "2026-08-31T18:29:59.999999Z"
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) VALUES "
            "('live:2026-08-31', '2026-08-31', 'live', ?, 'def', 'mem', '[]', "
            "'2026-08-31T18:30:00.000000Z', '2026-08-31T18:30:00.000000Z', 1, 1)",
            (cutoff,),
        )
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000001', 'Leaky Co', '2015-01-01', '2026-08-31', 'listed')"
        )
        conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
            "VALUES ('2026-08-31', '2026-08-31T10:00:00.000000Z', 1, 'NIFTY500', 'LEAKY', 'current_backfill')"
        )
        # Backdating leak: claims available before the cutoff, but was actually fetched after it.
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) VALUES "
            "(1, 'income', 'Q', '2026-06-30', 'revenue', 1000.0, 'inr', '2026-07-15', 'earnings_date', "
            "'2026-09-05T00:00:00.000000Z', 'yahoo', 1)"
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    rep = quant.verify.pit(conn, 3, cfg)
    conn.close()

    assert isinstance(rep, CheckReport)
    assert not rep.passed
    fund_checks = [c for c in rep.checks if "fundamentals_no_backdated_fetch" in c.id]
    assert len(fund_checks) == 1
    assert fund_checks[0].status == "FAIL"
    assert fund_checks[0].observed == 1
    # No other check for this cohort should be dragged down by an unrelated seed.
    other_checks = [c for c in rep.checks if c.id != fund_checks[0].id]
    assert all(c.status == "PASS" for c in other_checks)


def test_verify_pit_does_not_flag_benign_later_revision_fetch(tmp_path):
    """MASTER_SPEC line 163: 'a changed value inserts a version with its new timestamp'.

    A fact fetched once before the cutoff (used by this cohort, per quant/factors/inputs.py's
    own available_from<=cutoff AND fetched_at<=cutoff filter) that later gets an ordinary
    revision fetch -- SAME available_from, a LATER fetched_at that lands after the cutoff --
    must NOT be flagged. That later-fetched row is never selected for this cohort; only a
    fact with no pre-cutoff fetch at all is a genuine backdating leak."""
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    cutoff = "2026-08-31T18:29:59.999999Z"
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) VALUES "
            "('live:2026-08-31', '2026-08-31', 'live', ?, 'def', 'mem', '[]', "
            "'2026-08-31T18:30:00.000000Z', '2026-08-31T18:30:00.000000Z', 1, 1)",
            (cutoff,),
        )
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000001', 'Revised Co', '2015-01-01', '2026-08-31', 'listed')"
        )
        conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
            "VALUES ('2026-08-31', '2026-08-31T10:00:00.000000Z', 1, 'NIFTY500', 'REVISED', 'current_backfill')"
        )
        # Row A: the honest, pre-cutoff fetch actually usable/used by this cohort.
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) VALUES "
            "(1, 'income', 'Q', '2026-06-30', 'revenue', 1000.0, 'inr', '2026-07-15', 'earnings_date', "
            "'2026-07-16T00:00:00.000000Z', 'yahoo', 1)"
        )
        # Row B: the SAME fact, later re-fetched (a normal correction) after the cutoff.
        # inputs.py/gates.py never select this row for this cohort -- it cannot leak.
        conn.execute(
            "INSERT INTO fundamentals (security_id, statement, freq, period_end, field, value, unit, "
            "available_from, available_from_basis, fetched_at, source, run_id) VALUES "
            "(1, 'income', 'Q', '2026-06-30', 'revenue', 1000.0, 'inr', '2026-07-15', 'earnings_date', "
            "'2026-09-05T00:00:00.000000Z', 'yahoo', 1)"
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    rep = quant.verify.pit(conn, 3, cfg)
    conn.close()

    assert isinstance(rep, CheckReport)
    assert rep.passed, [c for c in rep.checks if c.status != "PASS"]


def test_verify_pit_holdings_first_capture_only_fails_the_older_cohort(tmp_path):
    """A security whose only `holdings`/`security_attributes` row lands after an OLDER
    cohort's cutoff but before a NEWER cohort's cutoff is normal accumulation, not a leak
    for the newer cohort -- the newer cohort has a pre-cutoff-captured row, the older one
    does not. Covers both tables since verify.pit runs the identical check for each."""
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    older_cutoff = "2026-07-31T18:29:59.999999Z"
    newer_cutoff = "2026-08-31T18:29:59.999999Z"
    capture_at = "2026-08-10T00:00:00.000000Z"  # after older_cutoff, before newer_cutoff
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) VALUES "
            "('live:2026-07-31', '2026-07-31', 'live', ?, 'def', 'mem', '[]', "
            "'2026-07-31T18:30:00.000000Z', '2026-07-31T18:30:00.000000Z', 1, 1)",
            (older_cutoff,),
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) VALUES "
            "('live:2026-08-31', '2026-08-31', 'live', ?, 'def', 'mem', '[]', "
            "'2026-08-31T18:30:00.000000Z', '2026-08-31T18:30:00.000000Z', 1, 1)",
            (newer_cutoff,),
        )
        conn.execute(
            "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (1, 'INE000001', 'Late Cover Co', '2015-01-01', '2026-08-31', 'listed')"
        )
        conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
            "VALUES ('2026-07-31', '2026-07-31T10:00:00.000000Z', 1, 'NIFTY500', 'LATECOV', 'current_backfill')"
        )
        conn.execute(
            "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, source) "
            "VALUES ('2026-08-31', '2026-08-31T10:00:00.000000Z', 1, 'NIFTY500', 'LATECOV', 'current_backfill')"
        )
        conn.execute(
            "INSERT INTO holdings (security_id, captured_at, inst_pct, insider_pct, shares_out, source) "
            "VALUES (1, ?, 0.42, 0.10, 1000000.0, 'yahoo')",
            (capture_at,),
        )
        conn.execute(
            "INSERT INTO security_attributes (captured_at, security_id, mcap_inr, shares_out, float_shares, "
            "ev_inr, trailing_pe, price_to_book, dividend_rate_inr, beta, yahoo_sector, yahoo_industry, "
            "source_sha256) VALUES (?, 1, 5000000.0, 1000000.0, 900000.0, 5500000.0, 22.0, 3.0, 1.5, 1.1, "
            "'Financials', 'Banks', 'sha_attr')",
            (capture_at,),
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    rep = quant.verify.pit(conn, 2, cfg)
    conn.close()

    by_cohort_check = {c.id: c for c in rep.checks}
    for table in ("holdings", "security_attributes"):
        older = by_cohort_check[f"verify:pit:live:2026-07-31:{table}_no_future_only_evidence"]
        newer = by_cohort_check[f"verify:pit:live:2026-08-31:{table}_no_future_only_evidence"]
        assert older.status == "FAIL", (table, older)
        assert newer.status == "PASS", (table, newer)


def test_verify_pit_clean_cohort_passes(tmp_path):
    """Sanity check: a cohort with no PIT evidence at all is not a false-positive failure."""
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, "
            "membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) VALUES "
            "('live:2026-08-31', '2026-08-31', 'live', '2026-08-31T18:29:59.999999Z', 'def', 'mem', '[]', "
            "'2026-08-31T18:30:00.000000Z', '2026-08-31T18:30:00.000000Z', 1, 1)"
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    rep = quant.verify.pit(conn, 3, cfg)
    conn.close()
    assert rep.passed


def test_verify_pit_empty_database_is_non_blocking(tmp_path):
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path, readonly=True)
    rep = quant.verify.pit(conn, 12, cfg)
    conn.close()
    assert isinstance(rep, CheckReport)
    assert rep.passed


# --------------------------------------------------------------------------- status.read


def test_status_read_keys_and_captures_table(tmp_path):
    """status.read must source last-capture data from the real `captures` table (the
    review found it queried a nonexistent `source_captures` table and silently no-opped)."""
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )
        conn.execute(
            "INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
            "VALUES ('cap1', '2026-08-30T10:00:00.000000Z', 'nifty500', ?, 'sha_cap', 'v1', 1)",
            (str(tmp_path / "does_not_exist.csv"),),
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    st = quant.status.read(conn, cfg)
    conn.close()

    for key in (
        "last_publication",
        "last_capture",
        "blocked_reason",
        "pending_orders",
        "pending_proposals",
        "overdue_ratifications",
        "next_possible_maturities",
        "source_archive_availability",
    ):
        assert key in st

    assert st["last_capture"]["nifty500"] is not None
    assert st["last_capture"]["nifty500"]["capture_id"] == "cap1"
    assert st["last_capture"]["yahoo_bundle"] is None
    assert st["source_archive_availability"]["total"] == 1
    assert st["source_archive_availability"]["available"] == 0
    assert len(st["source_archive_availability"]["missing"]) == 1


def test_status_read_blocked_reason_from_notes_json(tmp_path):
    cfg, db_path = _fresh_cfg(tmp_path)
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, finished_at, status, "
            "git_sha, code_sha256, config_sha256, registry_sha256, notes_json) VALUES "
            "(1, '2026-08-31', 'monthly', 'live', 1, '2026-08-31T18:30:00.000000Z', "
            "'2026-08-31T18:35:00.000000Z', 'blocked', 'sha1', 'c_sha', 'cfg_sha', 'reg_sha', ?)",
            (json.dumps({"blocked": "G1_FAILED: not enough constituents"}),),
        )
    conn.close()

    conn = connect(db_path, readonly=True)
    st = quant.status.read(conn, cfg)
    conn.close()
    assert st["blocked_reason"] is not None
    assert "G1_FAILED" in st["blocked_reason"]
