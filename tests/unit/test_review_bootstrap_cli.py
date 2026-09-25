"""`quant kb bootstrap` seeds the launch set inside a journaled run; status orders publications by date."""
from __future__ import annotations

from quant.cli import main
from quant.db.core import apply_schema, connect


def _fresh(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    return db


def test_kb_bootstrap_seeds_launch_set_journaled_and_idempotent(tmp_path, monkeypatch):
    db = _fresh(tmp_path)
    monkeypatch.setenv("QUANT_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    assert main(["kb", "bootstrap", "--db", str(db), "--as-of", "2026-09-30"]) == 0
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM decisions WHERE decision_id = 'DEC_BOOTSTRAP'").fetchone()[0] == 1
    # 6 launch models: EW_HIER_v1, EW_FLAT_v1, MOM_ONLY_v1, IC_SHRUNK_v1, EW_HIER_NR_v1, EW_HIER_COV_v1 (D6/D7)
    assert conn.execute("SELECT count(*) FROM models").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM factor_registry").fetchone()[0] >= 20
    n_hyp = conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0]
    assert n_hyp > 0
    # every write happened inside a 'bootstrap' run and reached the ledger
    assert conn.execute("SELECT count(*) FROM runs WHERE kind = 'bootstrap' AND status = 'ok'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ledger_events WHERE table_name = 'factor_registry'").fetchone()[0] >= 20
    fp_ref = conn.execute("SELECT evidence_refs_json FROM decisions WHERE decision_id = 'DEC_BOOTSTRAP'").fetchone()[0]
    assert len(fp_ref) > 60 and fp_ref.count('"') == 2
    conn.close()

    # second run is a no-op on content
    assert main(["kb", "bootstrap", "--db", str(db), "--as-of", "2026-09-30"]) == 0
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM hypotheses").fetchone()[0] == n_hyp
    # 6 launch models: EW_HIER_v1, EW_FLAT_v1, MOM_ONLY_v1, IC_SHRUNK_v1, EW_HIER_NR_v1, EW_HIER_COV_v1 (D6/D7)
    assert conn.execute("SELECT count(*) FROM models").fetchone()[0] == 6
    conn.close()


def test_status_last_publication_prefers_latest_as_of(tmp_path):
    from quant.config import load
    from quant.status import read

    db = _fresh(tmp_path)
    conn = connect(db)
    conn.execute(
        "INSERT INTO runs (run_id, as_of, kind, track, attempt, started_at, status, git_sha, code_sha256, config_sha256, registry_sha256) "
        "VALUES (1, '2026-09-03', 'migration', 'legacy', 1, '2026-09-10T08:00:00.000000Z', 'ok', 'g', 'c', 'q', 'r')"
    )
    for as_of in ("2026-06-14", "2026-09-03", "2026-07-11"):
        conn.execute(
            "INSERT INTO cohorts (cohort_id, as_of, track, knowledge_cutoff, definition_hash, membership_hash, source_refs_json, published_at, generated_at, is_clean, run_id) "
            "VALUES (?, ?, 'legacy', ?, 'd', 'm', '{}', '2026-09-10T08:40:55.123446Z', '2026-09-10T08:40:55.123446Z', 0, 1)",
            (f"legacy:{as_of}", as_of, f"{as_of}T18:29:59.999999Z"),
        )
    status = read(conn, load().with_paths(db=db))
    conn.close()
    assert status["last_publication"]["as_of"] == "2026-09-03"
