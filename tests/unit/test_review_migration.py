"""Regression tests for the WS10 legacy-migration review fixes (C10, MASTER_SPEC 10.6).

Covers: real-ISIN identity resolution (vs. a blanket LEGACY_ placeholder), universe_membership
and sector-group rows per legacy cohort, sector-neutral standardisation of factor_values.z
(raw/winsor kept at the original 0-100 legacy scale), corrected defect dates for the two known
harness unit bugs plus the new hard-kill/split-quote defect codes, and a reconcile() that never
reports PASS on a sub-480-row sample.
"""

from __future__ import annotations

from argparse import Namespace
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from quant.commands.migrate import cmd_migrate_legacy
from quant.db.core import apply_schema, connect
from quant.migrate.legacy import (
    ROE_NONE_COERCION_BUG_DATES,
    YIELD_PCT_BUG_DATES,
    build_identity_map,
    build_sample,
    reconcile,
    run as run_migration,
)
from quant.run import RunContext
from quant.types import Actor, FrozenClock


KNOWN_20_TICKERS = [
    "360ONE.NS", "3MINDIA.NS", "AADHARHFC.NS", "AARTIIND.NS", "AAVAS.NS",
    "ABB.NS", "ABBOTINDIA.NS", "ABCAPITAL.NS", "ABDL.NS", "ABFRL.NS",
    "ABLBL.NS", "ABREL.NS", "ABSLAMC.NS", "ACC.NS", "ACE.NS",
    "ACMESOLAR.NS", "ACUTAAS.NS", "ADANIENSOL.NS", "ADANIENT.NS", "ADANIGREEN.NS",
]
# JBCHEPHARM.NS is present in quant_engine.db's daily_predictions but absent from the
# current NSE constituent list, giving a real UNRESOLVED_ISIN case alongside 20 resolved ones.
SAMPLE_TICKERS = KNOWN_20_TICKERS + ["JBCHEPHARM.NS"]


@pytest.fixture
def sample_legacy_db(tmp_path):
    source_db = Path("quant_engine.db")
    sample_path = tmp_path / "sample_legacy.db"
    build_sample(source=source_db, output=sample_path, tickers=SAMPLE_TICKERS)
    return sample_path


@pytest.fixture
def migrated_ctx(tmp_path, cfg, sample_legacy_db):
    db_path = tmp_path / "v2_migrated.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    test_cfg = cfg.with_paths(db=db_path, knowledge_dir=tmp_path / "knowledge")

    clock = FrozenClock("2026-09-03T18:30:00.000000Z")
    actor = Actor(kind="system", name="migration")

    ctx = RunContext(
        as_of="2026-09-03",
        kind="migration",
        track="legacy",
        cfg=test_cfg,
        clock=clock,
        actor=actor,
    )
    ctx.conn = conn

    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-09-03', 'migration', 'legacy', 1, '2026-09-03T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )

    res = run_migration(ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert res.status == "ok"
    return ctx


# ---------------------------------------------------------------------------- identity map


def test_build_identity_map_is_deterministic_with_required_columns(tmp_path):
    out1 = tmp_path / "map1.csv"
    out2 = tmp_path / "map2.csv"
    res1 = build_identity_map(Path("config/nifty500_constituents_2026-09-09.csv"), out1)
    res2 = build_identity_map(Path("config/nifty500_constituents_2026-09-09.csv"), out2)

    assert res1.status == "ok"
    assert res1.counts["rows"] == res2.counts["rows"] > 0
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")

    df = pd.read_csv(out1)
    assert list(df.columns) == [
        "yahoo_ticker", "nse_symbol", "isin", "company_name", "nse_sector", "source", "resolved",
    ]
    assert (df["yahoo_ticker"] == df["nse_symbol"] + ".NS").all()
    assert df["resolved"].all()
    assert df["isin"].str.len().gt(0).all()


def test_identity_map_resolves_at_least_95_percent_of_sample(migrated_ctx):
    resolved = migrated_ctx.conn.execute(
        "SELECT count(*) FROM securities WHERE isin NOT LIKE 'LEGACY_%'"
    ).fetchone()[0]
    unresolved = migrated_ctx.conn.execute(
        "SELECT count(*) FROM securities WHERE isin LIKE 'LEGACY_%'"
    ).fetchone()[0]
    total = resolved + unresolved
    assert total == len(SAMPLE_TICKERS)
    assert resolved / total >= 0.95


def test_unresolved_ticker_gets_legacy_placeholder_and_defect(migrated_ctx):
    row = migrated_ctx.conn.execute(
        "SELECT isin FROM securities WHERE name = 'JBCHEPHARM.NS'"
    ).fetchone()
    assert row is not None
    assert row[0] == "LEGACY_JBCHEPHARM.NS"

    defect = migrated_ctx.conn.execute(
        "SELECT ticker FROM legacy_defects WHERE defect_code = 'UNRESOLVED_ISIN'"
    ).fetchone()
    assert defect is not None
    assert defect[0] == "JBCHEPHARM.NS"

    # every other sample ticker resolved to a real (non-placeholder) ISIN
    for ticker in KNOWN_20_TICKERS:
        row = migrated_ctx.conn.execute(
            "SELECT s.isin FROM securities s JOIN symbol_history h ON h.security_id = s.security_id "
            "WHERE h.yahoo_ticker = ?",
            (ticker,),
        ).fetchone()
        assert row is not None
        assert not row[0].startswith("LEGACY_"), f"{ticker} unexpectedly left unresolved"


# ---------------------------------------------------------------------------- membership / groups


def test_membership_rows_exist_per_legacy_cohort_with_real_sector_groups(migrated_ctx):
    rows = migrated_ctx.conn.execute(
        "SELECT DISTINCT as_of FROM universe_membership WHERE source = 'legacy_snapshot' ORDER BY as_of"
    ).fetchall()
    as_ofs = [r[0] for r in rows]
    assert as_ofs == ["2026-06-12", "2026-07-10", "2026-08-14", "2026-09-03"]

    for as_of in as_ofs:
        n = migrated_ctx.conn.execute(
            "SELECT count(*) FROM universe_membership WHERE as_of = ? AND index_name = 'NIFTY500'",
            (as_of,),
        ).fetchone()[0]
        assert n > 0

    fv_groups = {r[0] for r in migrated_ctx.conn.execute("SELECT DISTINCT sector_group FROM factor_values")}
    score_groups = {r[0] for r in migrated_ctx.conn.execute("SELECT DISTINCT sector_group FROM scores")}
    assert fv_groups and "Broad" not in fv_groups
    assert score_groups and "Broad" not in score_groups

    sector_map_rows = migrated_ctx.conn.execute(
        "SELECT DISTINCT source, confidence FROM sector_map"
    ).fetchall()
    assert sector_map_rows
    assert all(source == "legacy_backfill" and confidence == 0.5 for source, confidence in sector_map_rows)


# ---------------------------------------------------------------------------- standardisation


def test_z_is_sector_neutral_centered_and_bounded_while_raw_stays_0_to_100(migrated_ctx):
    df = pd.read_sql_query(
        "SELECT cohort_id, factor_id, sector_group, raw, winsor, z FROM factor_values",
        migrated_ctx.conn,
    )
    assert not df.empty

    # raw/winsor keep the original legacy scale (momentum_multiplier in [0,1], others in [0,100])
    non_null_raw = df["raw"].dropna()
    assert non_null_raw.between(-0.0001, 100.0001).all()
    assert (df["raw"] == df["winsor"]).all()

    finite_z = df.dropna(subset=["z"])
    assert not finite_z.empty
    assert (finite_z["z"].abs() <= 3.0001).all()

    for (_, _, _), sub in finite_z.groupby(["cohort_id", "factor_id", "sector_group"]):
        if len(sub) >= 5:
            assert abs(sub["z"].mean()) < 1e-6


def test_scores_final_is_untouched_original_legacy_value_not_a_zscore(migrated_ctx):
    df = pd.read_sql_query("SELECT model_id, final, composite FROM scores", migrated_ctx.conn)
    assert not df.empty
    # a real legacy composite/final score is not confined to a bounded [-3, 3] z-scale
    assert df["final"].abs().max() > 5
    assert (df["final"] == df["composite"]).all()


# ---------------------------------------------------------------------------- defects


def test_known_bugs_flagged_on_the_dates_the_review_actually_shows_them(migrated_ctx):
    yield_dates = {
        r[0] for r in migrated_ctx.conn.execute(
            "SELECT DISTINCT snapshot_date FROM legacy_defects WHERE defect_code = 'YIELD_PCT_BUG'"
        )
    }
    roe_dates = {
        r[0] for r in migrated_ctx.conn.execute(
            "SELECT DISTINCT snapshot_date FROM legacy_defects WHERE defect_code = 'ROE_NONE_COERCION_BUG'"
        )
    }
    assert yield_dates <= YIELD_PCT_BUG_DATES
    assert roe_dates <= ROE_NONE_COERCION_BUG_DATES

    # Never flagged on the two pre-06-14 snapshots (partial + duplicate; not full cohorts)
    early = migrated_ctx.conn.execute(
        "SELECT count(*) FROM legacy_defects WHERE defect_code IN ('YIELD_PCT_BUG', 'ROE_NONE_COERCION_BUG') "
        "AND snapshot_date IN ('2026-06-04', '2026-06-12')"
    ).fetchone()[0]
    assert early == 0

    # 2026-09-03 must not be the ONLY flagged date (the pre-fix behaviour this replaces)
    assert not (yield_dates and yield_dates == {"2026-09-03"})


def test_yield_pct_bug_present_on_every_full_cohort_not_only_september():
    """Direct evidence from quant_engine.db's raw_json: Div_Yield_% > 25% affects a majority
    of the universe on every full cohort from 2026-06-14 onward, contradicting a defect flag
    scoped to 2026-09-03 alone (docs/analysis/red_team_review.md section 5)."""
    conn = sqlite3.connect("file:quant_engine.db?mode=ro", uri=True)
    try:
        seen = set()
        for date in sorted(YIELD_PCT_BUG_DATES):
            rows = conn.execute("SELECT raw_json FROM daily_predictions WHERE date=?", (date,)).fetchall()
            assert rows, f"expected rows for {date}"
            n_high = sum(1 for (raw,) in rows if (json.loads(raw).get("Div_Yield_%") or 0) > 25)
            rate = n_high / len(rows)
            # Measured rate is a tight 64.5%-65.4% on every full cohort. Bound it well below
            # the review's unrelated 77% ("capital-allocation score >= 90") figure, which a
            # prior version of the code comment incorrectly folded into this same range.
            assert 0.6 <= rate <= 0.7, f"{date} yield-bug rate {rate:.3f} outside the measured 64.5%-65.4% band"
            seen.add(date)
        assert seen == YIELD_PCT_BUG_DATES

        # the two pre-06-14 snapshots predate the Div_Yield_% field entirely
        for date in ("2026-06-04", "2026-06-12"):
            rows = conn.execute("SELECT raw_json FROM daily_predictions WHERE date=?", (date,)).fetchall()
            assert all("Div_Yield_%" not in json.loads(raw) for (raw,) in rows)
    finally:
        conn.close()


def test_hard_kill_and_split_quote_defects_on_full_source(tmp_path):
    """LEGACY_HARD_KILL counts must match the red-team review's death-cross table exactly;
    SUSPECT_SPLIT_QUOTE must catch the ZFCVINDIA.NS 6-for-1 split example."""
    from quant.config import load as load_config

    db_path = tmp_path / "v2_full.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    cfg = load_config().with_paths(
        db=db_path, data_dir=tmp_path / "data", knowledge_dir=tmp_path / "knowledge",
    )
    clock = FrozenClock("2026-09-03T18:30:00.000000Z")
    actor = Actor(kind="system", name="migration")
    ctx = RunContext(as_of="2026-09-03", kind="migration", track="legacy", cfg=cfg, clock=clock, actor=actor)
    ctx.conn = conn
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, "
            "code_sha256, config_sha256, registry_sha256) VALUES "
            "(1, '2026-09-03', 'migration', 'legacy', 1, '2026-09-03T18:30:00.000000Z', 'ok', "
            "'sha1', 'c_sha', 'cfg_sha', 'reg_sha')"
        )

    res = run_migration(ctx, legacy_db_path=Path("quant_engine.db"), dry_run=False)
    assert res.status == "ok"

    hard_kill_by_date = dict(conn.execute(
        "SELECT snapshot_date, count(*) FROM legacy_defects WHERE defect_code = 'LEGACY_HARD_KILL' "
        "GROUP BY snapshot_date"
    ).fetchall())
    # exact death-cross counts from docs/analysis/red_team_review.md section 1
    assert hard_kill_by_date["2026-06-14"] == 175
    assert hard_kill_by_date["2026-07-11"] == 97
    assert hard_kill_by_date["2026-08-14"] == 103
    assert hard_kill_by_date["2026-09-03"] == 148

    split_quote_tickers = {
        r[0] for r in conn.execute(
            "SELECT ticker FROM legacy_defects WHERE defect_code = 'SUSPECT_SPLIT_QUOTE'"
        )
    }
    assert "ZFCVINDIA.NS" in split_quote_tickers
    conn.close()


# ---------------------------------------------------------------------------- idempotency


def test_second_migration_run_inserts_nothing_new(migrated_ctx, sample_legacy_db):
    before = {
        tbl: migrated_ctx.conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        for tbl in ("universe_membership", "sector_map", "factor_values", "scores", "legacy_defects")
    }
    res2 = run_migration(migrated_ctx, legacy_db_path=sample_legacy_db, dry_run=False)
    assert res2.status == "ok"
    assert res2.details.get("status") == "unchanged"
    assert res2.counts.get("scores", 0) == 0
    after = {
        tbl: migrated_ctx.conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        for tbl in before
    }
    assert before == after


# ---------------------------------------------------------------------------- reconcile()


def test_reconcile_never_passes_on_a_sub_480_row_sample(migrated_ctx, sample_legacy_db):
    df = reconcile(migrated_ctx.conn, legacy_db_path=sample_legacy_db)
    assert not df.empty
    assert (df["status"] != "PASS").all()
    assert set(df["status"].unique()) <= {"INSUFFICIENT", "INFO"}


def test_reconcile_passes_within_tolerance_on_the_full_480plus_source():
    conn = sqlite3.connect(":memory:")
    try:
        df = reconcile(conn, legacy_db_path=Path("quant_engine.db"))
    finally:
        conn.close()
    core = df[df["metric"].isin(
        ["final_score", "momentum_multiplier", "fundamental_composite", "equal_weight_composite"]
    )]
    assert not core.empty
    assert (core["status"] == "PASS").all()
    assert (core["difference"].abs() <= 0.01).all()


# ---------------------------------------------------------------------------- CLI dry-run (C10)


def test_cli_dry_run_never_creates_or_writes_the_state_db(tmp_path):
    """INTERFACES.md C10: 'dry_run never writes to either DB, filesystem or Git.'

    Regression test for the adversarial-review finding: `cmd_migrate_legacy` used to wrap
    BOTH the dry-run and real-run branches in one `with RunContext(...) as ctx:` block, and
    RunContext.__enter__ unconditionally opens the target state DB and INSERTs a `runs` row
    (then __exit__ UPDATEs/COMMITs it) before `legacy.run()` is even called - so a "dry" run
    silently wrote a run record to a DB that started empty. A true dry run must not even
    create the state-DB file.
    """
    db_path = tmp_path / "would_be_state.db"
    assert not db_path.exists()

    args = Namespace(config=None, db_path=str(db_path), legacy_db="quant_engine.db", dry_run=True)
    rc = cmd_migrate_legacy(args)

    assert rc == 0
    assert not db_path.exists(), "dry_run must not create the state database file at all"


def test_cli_dry_run_leaves_a_preexisting_state_db_untouched(tmp_path):
    """Same contract, but against a state DB that already exists (e.g. from a prior real run):
    dry_run must leave its `runs` table row count and schema completely unchanged."""
    db_path = tmp_path / "existing_state.db"
    conn = connect(db_path)
    apply_schema(conn, kind="state")
    before_runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    conn.close()  # WAL checkpoint happens on close, so snapshot mtime only after this
    before_mtime = db_path.stat().st_mtime_ns

    args = Namespace(config=None, db_path=str(db_path), legacy_db="quant_engine.db", dry_run=True)
    rc = cmd_migrate_legacy(args)
    assert rc == 0

    conn = connect(db_path)
    after_runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    conn.close()
    assert after_runs == before_runs == 0
    assert db_path.stat().st_mtime_ns == before_mtime


def test_cli_real_run_does_write_a_runs_row(tmp_path):
    """Sanity counterpart: the non-dry-run path must still journal a real run (guards against
    the dry_run fix accidentally making every run a no-op).

    Uses a throwaway config file that points every mutable path at tmp_path (the real run
    writes an ADR file under knowledge_dir) so this test cannot write into the actual repo
    tree, which other agents are editing concurrently.
    """
    db_path = tmp_path / "state.db"
    sample_path = tmp_path / "sample_legacy.db"
    build_sample(source=Path("quant_engine.db"), output=sample_path, tickers=SAMPLE_TICKERS)

    config_path = tmp_path / "quant.toml"
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                f'db = "{tmp_path / "state.db"}"',
                f'legacy_db = "{sample_path}"',
                f'prices_db = "{tmp_path / "prices.db"}"',
                f'data_dir = "{tmp_path / "data"}"',
                f'knowledge_dir = "{tmp_path / "knowledge"}"',
                f'ui_dir = "{tmp_path / "ui"}"',
                f'archive_dir = "{tmp_path / "archive"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    args = Namespace(config=str(config_path), db_path=None, legacy_db=None, dry_run=False)
    rc = cmd_migrate_legacy(args)
    assert rc == 0
    assert db_path.exists()

    conn = connect(db_path)
    n_runs = conn.execute(
        "SELECT count(*) FROM runs WHERE kind = 'migration' AND track = 'legacy' AND status = 'ok'"
    ).fetchone()[0]
    conn.close()
    assert n_runs == 1
