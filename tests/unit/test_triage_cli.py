"""WP5 of evidence triage: the ``triage`` CLI group and the human flow around it.

Every test runs on a disposable fixture world: a tmp_path state database and price store, a
FrozenClock and a fake Yahoo client (the conftest network guard stays on, so an unmocked socket
raises). The cohort is as_of 2026-10-30 (a Friday; 2026-10-31 is a Saturday), whose knowledge
cutoff is 2026-10-30T18:29:59.999999Z. The world has one security, TESTCO, with a demerger-sized
one-day fall on 2025-10-14 (gross factor 0.6) among four flat securities, so the market median is
flat and no price-only rule fires. Nothing is committed to a real database.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.cli import main
from quant.commands import triage as triage_cmd
from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.data.yahoo import RawActions, RawNews, YahooClient
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.types import Actor, FrozenClock

AS_OF = "2026-10-30"
CUTOFF = "2026-10-30T18:29:59.999999Z"
EVENING = "2026-10-30T13:00:00.000000Z"          # 18:30 IST on the cohort day, before the cutoff
LATE = "2026-10-30T18:30:00.000000Z"             # 1 microsecond past the cutoff
OBSERVED = "2026-10-30T10:06:00.000000Z"
ISIN = "INE000000001"
EX = "2025-10-14"
KEY = f"{ISIN}@{EX}"
SPOS_EXCERPT = "Previous close Rs 600.00; special pre-open price Rs 360.00"
HUMAN = ["--actor-kind", "human", "--by", "human:owner"]
LLM = ["--actor-kind", "llm", "--by", "llm:test-model"]
REPO = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here (amendment F7)."""
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """One seeded state database and price store, copied into every test's tmp_path."""
    root = tmp_path_factory.mktemp("triage_cli_template")
    db = root / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=root / "p.db", data_dir=root / "d", archive_dir=root / "a",
                                    knowledge_dir=root / "k", ui_dir=root / "u")
    dates = pd.bdate_range("2025-06-02", AS_OF).strftime("%Y-%m-%d")
    flat = np.full(len(dates), 600.0)
    drop = flat.copy()
    drop[list(dates).index(EX):] = 360.0               # demerger-sized fall: gross factor 0.6
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg, clock=FrozenClock(EVENING),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        for sid, series in ((1, drop), (2, flat), (3, flat), (4, flat), (5, flat)):
            ctx.conn.execute(
                "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                "VALUES (?, ?, ?, '2025-01-01', ?, 'listed')",
                (sid, f"INE00000000{sid}", "Testco Industries Limited" if sid == 1 else f"Flatco {sid} Limited", AS_OF))
            sym = "TESTCO" if sid == 1 else f"FLAT{sid}"
            ctx.conn.execute(
                "INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                "VALUES (?, ?, ?, '2025-01-01', 'test')", (sid, sym, f"{sym}.NS"))
            ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": series, "volume": 1e5}),
                             {"security_id": sid, "close_basis": "raw", "observed_at": OBSERVED})
    return root


class World:
    def __init__(self, root: Path, template: Path, monkeypatch):
        self.root = root
        self.db = root / "state.db"
        shutil.copy(template / "state.db", self.db)
        shutil.copy(template / "p.db", root / "p.db")
        self.cfg = load_config().with_paths(db=self.db, prices_db=root / "p.db", data_dir=root / "d",
                                             archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("QUANT_DB_PATH", str(self.db))
        monkeypatch.setenv("QUANT_PRICES_DB_PATH", str(root / "p.db"))
        monkeypatch.setenv("QUANT_DATA_DIR", str(root / "d"))
        monkeypatch.setenv("QUANT_ARCHIVE_DIR", str(root / "a"))
        monkeypatch.setenv("QUANT_KNOWLEDGE_DIR", str(root / "k"))
        self.now(EVENING)
        self.client = FakeYahoo()
        self.built_clients = 0
        monkeypatch.setattr(triage_cmd, "_yahoo_client", self._make_client)

    def _make_client(self, cfg, clock):
        self.built_clients += 1
        return self.client

    def now(self, iso: str):
        self.monkeypatch.setattr(triage_cmd, "_clock", lambda: FrozenClock(iso))
        # data actions-resolve stamps its decision with SystemClock: resolve "on the cohort evening"
        self.monkeypatch.setattr("quant.commands.data.SystemClock", lambda: FrozenClock(iso))

    def q(self, sql, params=()):
        """A plain read-only sqlite3 connection: the engine's connect() registers journal functions this reader never needs."""
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def fingerprint(self):
        """What a write would change: row counts of the journaled tables, the run count and the file's mtime."""
        counts = tuple(self.q("SELECT " + ", ".join(f"(SELECT count(*) FROM {t})" for t in
                       ("runs", "captures", "proposals", "decisions", "ledger_events")))[0])
        return counts, self.db.stat().st_mtime_ns

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if p.is_file() and p.name not in ("state.db", "p.db") and not p.name.startswith("state.db-"))


class FakeYahoo:
    """Stands in for YahooClient: records calls, answers with a headline that names the company."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.clock = FrozenClock(EVENING)

    def actions(self, ticker):
        self.calls.append(("actions", ticker))
        return RawActions(ticker=ticker, fetched_at=EVENING, status="ok", splits={"2025-03-03": 2.0}, dividends={})

    def news(self, ticker):
        self.calls.append(("news", ticker))
        return RawNews(ticker=ticker, fetched_at=EVENING, status="ok", items=[
            {"headline": "TESTCO board approves scheme of arrangement for demerger", "link": "https://news.example/1",
             "published_at": "2025-10-09T05:00:00Z"}])


@pytest.fixture
def world(tmp_path, template, monkeypatch):
    return World(tmp_path, template, monkeypatch)


def run(w, *argv):
    return main([*argv, "--db", str(w.db)])


def attach_circular(w, *, extra=(), actor=HUMAN, file=True, published="2025-10-10"):
    circular = w.root / "circular.txt"
    circular.write_text("NSE circular: TESTCO special pre-open session. " + SPOS_EXCERPT + "\n")
    where = ["--file", str(circular)] if file else ["--url", "https://www.nseindia.com/circular/1"]
    return run(w, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX,
               "--title", "TESTCO: special pre-open session for demerger", "--published-at", published,
               *where, "--source-class", "exchange", "--relevance", "entity", "--excerpt", SPOS_EXCERPT,
               "--field", "prev_close=600", "--field", "spos_price=360", "--field", "action_type=demerger",
               *actor, *extra)


def build_all(w, *extra):
    return run(w, "triage", "build", "--as-of", AS_OF, "--all", *extra)


def engine_rows(w):
    return w.q("SELECT proposal_id, status, payload_json, evidence_json FROM proposals "
               "WHERE rule_id = 'TRIAGE_RULES_V1' ORDER BY rowid")


def agent_proposal(w):
    (_pid, _status, payload_json, _ev), = [r for r in engine_rows(w) if r[1] == "proposed"]
    engine = json.loads(payload_json)
    capture_id = engine["citations"][0]["capture_id"]
    return {
        "schema": "triage.proposal/1", "key": KEY, "as_of": AS_OF,
        "producer": {"kind": "llm", "name": "llm:test-model", "pack_sha256": engine["producer"]["pack_sha256"],
                     "prompt_sha256": None, "self_confidence": "high"},
        "anomaly_kind": "suspected_jump",
        "classification": {"label": "demerger", "confidence": "high", "rule_id": None, "alternatives": []},
        "resolution": {"action": "resolve", "resolve_kind": "demerger", "factor": 1.666667,
                       "factor_basis": "exchange_spos", "factor_uncertainty_pct": None, "needs_force": False,
                       "needs_primary": False, "recheck_on": None,
                       "factor_inputs": [{"name": "prev_close", "value": 600.0, "citation": "c1"},
                                         {"name": "spos_price", "value": 360.0, "citation": "c1"}]},
        "claims": [{"id": "k1", "type": "fact", "depends_on": [], "citations": ["c1"],
                    "text": "The circular gives previous close 600.00 and special pre-open price 360.00."}],
        "citations": [{"id": "c1", "capture_id": capture_id, "quote": SPOS_EXCERPT}],
        "negative_checks": [], "missing_evidence": [], "needs_human": [],
        "usage": {"n_fetches": 0, "tokens_in": None, "tokens_out": None, "wall_s": 0.0},
    }


def write_json(w, name, obj):
    path = w.root / name
    path.write_text(json.dumps(obj))
    return path


@pytest.fixture
def no_run_context(monkeypatch):
    """Any RunContext opened while this is active fails the test: read commands must not open one."""
    def boom(self):
        raise AssertionError("a read-only command opened a RunContext")
    monkeypatch.setattr(RunContext, "__enter__", boom)


# ----------------------------------------------------------------------------- list

def test_list_shows_the_measured_jump_and_writes_nothing(world, capsys, no_run_context):
    before = world.fingerprint()
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["as_of"] == AS_OF and out["cutoff_utc"] == CUTOFF and out["window_open"] is True
    assert [a["key"] for a in out["anomalies"]] == [KEY]
    row = out["anomalies"][0]
    assert row["status"] == "new" and row["source"] == "measured" and row["proposal_id"] is None
    assert row["gross_factor"] == pytest.approx(0.6, abs=1e-3)
    assert world.fingerprint() == before and world.files() == []

    assert run(world, "triage", "list", "--as-of", AS_OF) == 0          # the human table renders
    text = capsys.readouterr().out
    assert ISIN in text and "TESTCO" in text and "new" in text


def test_list_window_is_thirteen_months_and_since_widens_it(world, capsys):
    late_cohort = ["triage", "list", "--as-of", "2027-01-29", "--json"]
    assert run(world, *late_cohort) == 0
    assert json.loads(capsys.readouterr().out)["anomalies"] == []       # 2025-10-14 is older than 13 months
    assert run(world, *late_cohort, "--since", "2025-10-01") == 0
    assert [a["key"] for a in json.loads(capsys.readouterr().out)["anomalies"]] == [KEY]


def test_list_warns_when_the_cohort_window_has_closed(world, capsys):
    world.now(LATE)
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    assert json.loads(capsys.readouterr().out)["window_open"] is False
    assert run(world, "triage", "list", "--as-of", AS_OF) == 0
    assert "cutoff" in capsys.readouterr().out.lower()


def test_list_needs_as_of(world, capsys):
    assert run(world, "triage", "list") == 1
    assert "--as-of" in capsys.readouterr().err


def _seed_old_suspect_and_placeholder(w):
    """An unresolved suspect row far outside the 13-month window, and an INDEX_PLACEHOLDER event for another security."""
    cfg = w.cfg
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg, clock=FrozenClock(EVENING),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute(
            "INSERT INTO corporate_actions (security_id, ex_date, kind, source, observed_at, note) "
            "VALUES (2, '2024-01-15', 'suspected', 'inferred', '2025-07-01T00:00:00.000000Z', ?)",
            (json.dumps({"gross_factor": 0.5}),))
        ctx.conn.execute(
            "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
            "VALUES (?, ?, '2026-10-29T12:00:00.000000Z', 'WARN', 'INDEX_PLACEHOLDER', 3, ?)",
            (ctx.run_id, AS_OF, json.dumps({"placeholder_symbol": "DUMMYFLAT3", "placeholder_isin": "INE999999999"})))
    return ["INE000000002@2024-01-15", "INE000000003@2026-10-29"]


OLD_KEY = "INE000000002@2024-01-15"
PLACEHOLDER_KEY = "INE000000003@2026-10-29"


def test_the_placeholder_reaches_list_collect_and_build_and_the_old_suspect_is_hidden(world, capsys):
    assert _seed_old_suspect_and_placeholder(world) == [OLD_KEY, PLACEHOLDER_KEY]
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    rows = {a["key"]: a for a in out["anomalies"]}
    assert set(rows) == {KEY, PLACEHOLDER_KEY} and rows[PLACEHOLDER_KEY]["source"] == "placeholder_event"
    assert out["n_hidden"] == 1 and out["hidden_since"] == "2024-01-15"       # D4: counted, not listed

    assert run(world, "triage", "list", "--as-of", AS_OF) == 0
    text = capsys.readouterr().out
    hidden_lines = [line for line in text.splitlines() if "hidden" in line]
    assert len(hidden_lines) == 1 and "1 older unresolved suspect" in hidden_lines[0] and "--since 2024-01-15" in hidden_lines[0]
    assert OLD_KEY.split("@")[0] not in text.replace(hidden_lines[0], "")

    assert run(world, "triage", "list", "--as-of", AS_OF, "--since", "2024-01-15", "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert {a["key"] for a in shown["anomalies"]} == {KEY, PLACEHOLDER_KEY, OLD_KEY} and shown["n_hidden"] == 0
    assert run(world, "triage", "list", "--as-of", AS_OF, "--since", "2024-01-15") == 0
    assert "hidden" not in capsys.readouterr().out                             # nothing hidden, no line

    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", "--dry-run", *HUMAN) == 0
    out = capsys.readouterr().out
    assert KEY in out and PLACEHOLDER_KEY in out and OLD_KEY not in out and world.client.calls == []
    assert build_all(world, "--dry-run") == 0
    out = capsys.readouterr().out
    assert KEY in out and PLACEHOLDER_KEY in out and OLD_KEY not in out

    assert build_all(world) == 0
    assert sorted(json.loads(r[2])["key"] for r in engine_rows(world)) == sorted([KEY, PLACEHOLDER_KEY])
    assert build_all(world) == 0                                               # a rebuild duplicates nothing
    assert len(engine_rows(world)) == 2


def test_an_old_suspect_can_be_picked_by_isin_and_ex_date_and_then_stays_listed_with_its_proposal(world, capsys):
    _seed_old_suspect_and_placeholder(world)
    assert run(world, "triage", "build", "--as-of", AS_OF, "--isin", "INE000000002", "--ex-date", "2024-01-15") == 0
    assert OLD_KEY in capsys.readouterr().out
    assert [json.loads(r[2])["key"] for r in engine_rows(world)] == [OLD_KEY]
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    rows = {a["key"]: a for a in out["anomalies"]}
    assert rows[OLD_KEY]["status"] == "proposed" and out["n_hidden"] == 0       # an open proposal keeps it in the window


# ----------------------------------------------------------------------------- collect

def test_collect_archives_yahoo_evidence_and_is_idempotent(world, capsys):
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *HUMAN) == 0
    out = capsys.readouterr().out
    assert "yahoo_actions" in out and "yahoo_news" in out and "feeds" in out
    assert world.client.calls == [("actions", "TESTCO.NS"), ("news", "TESTCO.NS")]
    rows = world.q("SELECT capture_id, kind, source_version, captured_at FROM captures")
    assert len(rows) == 2 and {r[1] for r in rows} == {"triage_doc"}
    assert all(r[3] == EVENING for r in rows)                            # captured_at is the run clock
    notes = world.q("SELECT notes_json FROM runs WHERE kind = 'triage'")[0][0]
    status = json.loads(notes)["collect"][KEY]
    assert status["yahoo_actions"]["status"] == "ok" and status["feeds"]["status"] == "skipped"

    assert run(world, "triage", "collect", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX) == 0
    assert len(world.q("SELECT 1 FROM captures")) == 2                   # identical envelopes reuse their rows


def test_collect_dry_run_makes_no_call_and_writes_nothing(world, capsys):
    before = world.fingerprint()
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", "--dry-run", *HUMAN) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and KEY in out
    assert world.built_clients == 0 and world.client.calls == []
    assert world.fingerprint() == before and world.files() == []


def test_collect_single_anomaly_needs_isin_and_ex_date(world, capsys):
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--isin", ISIN) == 1
    assert run(world, "triage", "collect", "--as-of", AS_OF) == 1
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", "2025-01-01") == 1
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", "--isin", ISIN, "--ex-date", EX) == 1
    assert world.client.calls == []


def test_collect_keeps_going_when_a_source_answers_badly(world, capsys):
    class Broken(FakeYahoo):
        def actions(self, ticker):
            raise RuntimeError("yahoo changed its response")
    world.client = Broken()
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *HUMAN) == 0
    notes = json.loads(world.q("SELECT notes_json FROM runs WHERE kind = 'triage'")[0][0])["collect"][KEY]
    assert notes["yahoo_actions"]["status"] == "error" and notes["yahoo_news"]["status"] == "ok"
    assert len(world.q("SELECT 1 FROM captures")) == 1                   # only the news envelope


def test_collect_refuses_an_llm_actor(world):
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *LLM) == 3
    assert world.built_clients == 0 and world.client.calls == []


# ----------------------------------------------------------------------------- the window closes at the cutoff

def test_late_clock_exits_3_with_no_transport_call_and_no_write(world, capsys):
    circular = world.root / "c.txt"
    circular.write_text("x")
    proposal = write_json(world, "p.json", {"schema": "triage.proposal/1"})
    world.now(LATE)
    before = world.fingerprint()
    attempts = {
        "collect": ["triage", "collect", "--as-of", AS_OF, "--all", *HUMAN],
        "attach": ["triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
                   "--url", "https://example.org/x", "--source-class", "news", "--relevance", "context", *HUMAN],
        "build": ["triage", "build", "--as-of", AS_OF, "--all"],
        "submit": ["triage", "submit", "--as-of", AS_OF, "--file", str(proposal), *LLM],
    }
    for name, argv in attempts.items():
        assert run(world, *argv) == 3, name
        assert "cutoff" in capsys.readouterr().err, name
    assert world.built_clients == 0 and world.client.calls == []
    assert world.fingerprint() == before and world.files() == ["c.txt", "p.json"]


def test_the_cutoff_instant_itself_is_still_open(world):
    world.now(CUTOFF)
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *HUMAN) == 0


# ----------------------------------------------------------------------------- attach

def test_attach_file_archives_envelope_and_private_body(world, capsys):
    assert attach_circular(world) == 0
    (capture_id, kind, archive_path, sha), = world.q("SELECT capture_id, kind, archive_path, sha256 FROM captures")
    assert kind == "triage_doc" and capture_id == "TD-" + sha[:16]
    env = json.loads(Path(archive_path).read_text())
    assert env["provenance"] == "human_attach" and env["source_class"] == "exchange"
    assert env["published_at"] == "2025-10-10" and env["published_at_basis"] == "human_declared"
    assert env["published_at_precision"] == "date" and env["url"] is None
    assert env["typed_fields"] == {"prev_close": 600, "spos_price": 360, "action_type": "demerger"}
    assert env["entity_match"] is True and env["relevance"] == "entity"
    body = world.root / "a" / "triage_private" / env["body_sha256"]
    assert body.is_file() and hashlib.sha256(body.read_bytes()).hexdigest() == env["body_sha256"]
    assert capture_id in capsys.readouterr().out


def test_attach_dry_run_writes_nothing(world, capsys):
    before = world.fingerprint()
    assert attach_circular(world, extra=["--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert world.fingerprint() == before and world.files() == ["circular.txt"]
    assert not (world.root / "a").exists()


def test_attach_url_keeps_link_only_and_never_fetches(world):
    assert attach_circular(world, file=False) == 0
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    env = json.loads(Path(archive_path).read_text())
    assert env["url"] == "https://www.nseindia.com/circular/1" and env["body_sha256"] is None
    assert not (world.root / "a" / "triage_private").exists()


def test_attach_entity_match_is_computed_not_declared(world):
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX,
               "--title", "Sector regulator paper on distribution", "--published-at", "2025-10-10",
               "--url", "https://example.org/paper", "--source-class", "regulator", "--relevance", "entity",
               "--excerpt", "Consultation on commissions", *HUMAN) == 0
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    env = json.loads(Path(archive_path).read_text())
    assert env["relevance"] == "entity" and env["entity_match"] is False      # the text never names the company


@pytest.mark.parametrize("title, matched", [
    ("Testco special pre-open session circular", False),       # one-word name alone: no second signal (R-10)
    ("TESTCO special pre-open session circular", True),        # the NSE symbol written as the symbol
])
def test_attach_single_word_company_name_needs_the_symbol_as_its_second_signal(world, title, matched, capsys):
    conn = connect(world.db)
    conn.execute("UPDATE securities SET name = 'Testco Limited' WHERE security_id = 1")
    conn.close()
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", title,
               "--published-at", "2025-10-10", "--url", "https://example.org/circular", "--source-class", "exchange",
               "--relevance", "entity", "--excerpt", "Special pre-open session", *HUMAN) == 0
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    env = json.loads(Path(archive_path).read_text())
    assert env["entity_match"] is matched
    out = capsys.readouterr().out
    if matched:
        assert env["relevance_basis"].endswith("symbol_token@title") and "WARNING" not in out
    else:
        assert env["relevance_basis"].endswith("name_single_token_unconfirmed")
        assert "WARNING" in out and "Write the NSE symbol (TESTCO)" in out


def test_attach_llm_is_link_only(world, capsys):
    assert attach_circular(world, actor=LLM, file=True) == 3
    assert "--file" in capsys.readouterr().err
    assert world.q("SELECT 1 FROM captures") == [] and not (world.root / "a").exists()

    assert attach_circular(world, actor=LLM, file=False) == 0
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    env = json.loads(Path(archive_path).read_text())
    assert env["provenance"] == "agent_attach" and env["published_at_basis"] == "agent_declared"


def test_attach_llm_needs_date_and_excerpt(world):
    base = ["triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
            "--url", "https://example.org/x", "--source-class", "news", "--relevance", "context", *LLM]
    assert run(world, *base, "--excerpt", "text") == 3                      # no --published-at
    assert run(world, *base, "--published-at", "2025-10-10") == 3           # no --excerpt
    assert world.q("SELECT 1 FROM captures") == []


def test_attach_refuses_a_system_actor_and_a_mislabelled_llm(world):
    assert attach_circular(world, actor=["--actor-kind", "system", "--by", "system:cron"]) == 3
    assert attach_circular(world, actor=["--actor-kind", "llm", "--by", "human:owner"], file=False) == 3
    assert attach_circular(world, actor=["--actor-kind", "human", "--by", "llm:model"]) == 3
    assert world.q("SELECT 1 FROM captures") == []


def test_attach_refuses_a_date_in_the_future_and_bad_arguments(world, capsys):
    assert attach_circular(world, published="2026-10-31") == 3               # after the moment of capture
    assert "published" in capsys.readouterr().err
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
               "--source-class", "news", "--relevance", "context", *HUMAN) == 1        # neither --file nor --url
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
               "--url", "ftp://x", "--source-class", "news", "--relevance", "context", *HUMAN) == 1
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
               "--url", "https://x.org", "--source-class", "blog", "--relevance", "context", *HUMAN) == 1
    assert attach_circular(world, published="10 Oct 2025") == 1                  # neither a date nor a timestamp
    assert attach_circular(world, published="2025-10-10T10:00:00") == 1         # a timestamp needs its timezone
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t",
               "--url", "https://x.org", "--source-class", "news", "--relevance", "context",
               "--field", "no_equals_sign", *HUMAN) == 1
    assert world.q("SELECT 1 FROM captures") == []


def test_attach_published_at_is_never_defaulted(world):
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "TESTCO note",
               "--url", "https://example.org/x", "--source-class", "company", "--relevance", "entity", *HUMAN) == 0
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    env = json.loads(Path(archive_path).read_text())
    assert env["published_at"] is None and env["published_at_basis"] is None


# ----------------------------------------------------------------------------- build, show, resolve

def test_build_without_documents_proposes_unknown_and_is_idempotent(world, capsys):
    assert build_all(world) == 0
    (pid, status, payload_json, _), = engine_rows(world)
    payload = json.loads(payload_json)
    assert status == "proposed" and payload["classification"]["label"] == "unknown"
    assert payload["classification"]["confidence"] == "low" and payload["key"] == KEY
    pack = world.root / "d" / "triage" / "packs" / f"{pid}.json"
    sheet = world.root / "d" / "triage" / "sheets" / f"{pid}.md"
    assert pack.is_file() and sheet.is_file()
    assert hashlib.sha256(pack.read_bytes()).hexdigest() == payload["producer"]["pack_sha256"]
    assert "created" in capsys.readouterr().out

    rows_before = world.q("SELECT count(*) FROM proposals")[0][0]
    pack_hash = hashlib.sha256(pack.read_bytes()).hexdigest()
    assert build_all(world) == 0                                               # F4: same inputs, same pack, no row
    assert world.q("SELECT count(*) FROM proposals")[0][0] == rows_before
    assert hashlib.sha256(pack.read_bytes()).hexdigest() == pack_hash
    assert "unchanged" in capsys.readouterr().out


def test_build_rebuilds_a_missing_sheet_without_a_new_proposal(world):
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    sheet = world.root / "d" / "triage" / "sheets" / f"{pid}.md"
    sheet.unlink()
    assert build_all(world) == 0
    assert sheet.is_file() and len(engine_rows(world)) == 1


def test_a_failed_commit_leaves_no_pack_or_sheet_behind(world, monkeypatch, capsys):
    def boom(self):
        raise RuntimeError("disk full")
    monkeypatch.setattr(RunContext, "checkpoint", boom)
    assert build_all(world) == 1
    assert "disk full" in capsys.readouterr().err
    assert engine_rows(world) == [] and world.files() == []                     # no orphan file names a missing proposal


def test_build_dry_run_writes_nothing(world, capsys):
    before = world.fingerprint()
    assert build_all(world, "--dry-run") == 0
    assert world.fingerprint() == before and world.files() == []
    out = capsys.readouterr().out
    assert KEY in out and "would" in out


def test_build_single_anomaly_and_unknown_anomaly(world):
    assert run(world, "triage", "build", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX) == 0
    assert len(engine_rows(world)) == 1
    assert run(world, "triage", "build", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", "2025-01-01") == 1
    assert run(world, "triage", "build", "--as-of", AS_OF) == 1


def test_attached_circular_supersedes_the_price_only_proposal(world):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    rows = engine_rows(world)
    assert [r[1] for r in rows] == ["superseded", "proposed"]
    payload = json.loads(rows[1][2])
    assert payload["classification"]["label"] == "demerger" and payload["classification"]["rule_id"] == "R2"
    assert payload["classification"]["confidence"] == "high"
    assert payload["resolution"]["factor"] == pytest.approx(600 / 360, abs=1e-6)
    assert payload["resolution"]["factor_basis"] == "exchange_spos"
    assert json.loads(rows[1][3])[0]["capture_id"].startswith("TD-")


def test_evidence_captured_after_the_cutoff_is_invisible_to_the_cohort(world):
    assert build_all(world) == 0
    world.now("2026-10-31T10:00:00.000000Z")                                   # next day: the window is closed
    assert attach_circular(world) == 3
    assert len(world.q("SELECT 1 FROM captures")) == 0
    assert len(engine_rows(world)) == 1


def test_show_text_sections_and_the_resolve_command(world, capsys, monkeypatch):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    (_, _, payload_json, _), = [r for r in engine_rows(world) if r[1] == "proposed"]
    pid = [r for r in engine_rows(world) if r[1] == "proposed"][0][0]
    confirm = hashlib.sha256(payload_json.encode()).hexdigest()[:8]
    capsys.readouterr()

    def boom(self):
        raise AssertionError("show opened a RunContext")
    monkeypatch.setattr(RunContext, "__enter__", boom)
    before = world.fingerprint()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    assert world.fingerprint() == before
    text = capsys.readouterr().out
    order = [text.index(h) for h in ("LOOKALIKE WARNING", "COUNTER-EVIDENCE", "EVIDENCE (", "PROPOSALS", "RESOLVE")]
    assert order == sorted(order)
    assert "TD-" in text and "exchange" in text and "admissible: ok" in text
    assert "demerger" in text and "R2" in text and "(none)" in text           # no agent proposal yet
    cmd = next(line for line in text.splitlines() if "actions-resolve" in line)
    assert f"--proposal-id {pid}" in cmd and f"--confirm {confirm}" in cmd
    assert "--kind demerger" in cmd and "--factor 1.666667" in cmd
    assert "--actor-kind human" in cmd and "--force" not in cmd
    assert run(world, "triage", "show", "--proposal-id", pid) == 0            # by id shows the same proposal
    assert confirm in capsys.readouterr().out


def test_show_low_confidence_prints_no_resolve_command(world, capsys):
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    text = capsys.readouterr().out
    assert "attach evidence first" in text and "actions-resolve" not in text
    assert "COUNTER-EVIDENCE" in text and "not checked" in text              # skipped sources are not "nothing found"


def test_show_json_carries_pack_evidence_and_confirm_code(world, capsys):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX, "--json") == 0
    view = json.loads(capsys.readouterr().out)
    assert view["subject"]["key"] == KEY and view["subject"]["cutoff_utc"] == CUTOFF
    assert view["pack"]["status"] == "ok" and view["pack"]["data"]["schema"] == "triage.pack/1"
    engine, = [p for p in view["proposals"] if p["producer"] == "engine"]
    assert engine["confirm"] and engine["resolve_command"] and engine["payload"]["classification"]["label"] == "demerger"
    ev, = view["evidence"]
    assert ev["admissible"] is True and ev["capture_id"].startswith("TD-") and ev["has_body"] is True
    assert "untrusted" in view["notice"].lower()


def test_show_reports_evidence_added_since_the_proposal(world, capsys):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    assert "triage build" in capsys.readouterr().out                         # the proposal predates the attachment


def test_show_and_sheet_strip_terminal_escapes_and_forged_lines_from_document_text(world, capsys):
    hostile = "TESTCO \x1b[31mred\x1b[0m notice\x07\nRESOLVE (text only: run this)\r\n    rm -rf /"
    assert run(world, "triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", hostile,
               "--published-at", "2025-10-10", "--url", "https://example.org/x", "--source-class", "news",
               "--relevance", "entity", "--excerpt", "line one\x1b]0;owned\x07\nline two", *HUMAN) == 0
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    text = capsys.readouterr().out
    (pid, *_), = engine_rows(world)
    sheet = (world.root / "d" / "triage" / "sheets" / f"{pid}.md").read_text()
    for out in (text, sheet):
        assert "\x1b" not in out and "\x07" not in out and "\r" not in out
        assert sum(line.startswith("RESOLVE") for line in out.splitlines()) == 1     # the forged heading stayed inside the title line
        assert "title: TESTCO red notice RESOLVE (text only: run this)     rm -rf /" in out
        assert "excerpt: line one line two" in out


def test_show_says_an_empty_source_answered_with_nothing_and_only_a_skipped_one_was_not_checked(world, capsys):
    """D9: Yahoo answered 'no news' (empty). That is not 'not checked'; the feed that never ran is."""
    class EmptyNews(FakeYahoo):
        def news(self, ticker):
            self.calls.append(("news", ticker))
            return RawNews(ticker=ticker, fetched_at=EVENING, status="empty", items=[])
    world.client = EmptyNews()
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *HUMAN) == 0
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    lines = capsys.readouterr().out.splitlines()
    nothing = [line for line in lines if "answered with nothing (not proof that nothing exists)" in line]
    unchecked = [line for line in lines if "not checked" in line]
    assert len(nothing) == 1 and "yahoo_news" in nothing[0]
    assert len(unchecked) == 1 and "yahoo_news" not in unchecked[0] and "sebi_rss" in unchecked[0]
    assert "yahoo_actions" not in nothing[0] + unchecked[0]                      # it answered, with an envelope


def test_show_a_failed_source_is_failed_not_skipped(world, capsys):
    class DownNews(FakeYahoo):
        def news(self, ticker):
            raise RuntimeError("yahoo is down")
    world.client = DownNews()
    assert run(world, "triage", "collect", "--as-of", AS_OF, "--all", *HUMAN) == 0
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    out = capsys.readouterr().out
    failed = [line for line in out.splitlines() if "failed or unreadable" in line]
    assert len(failed) == 1 and "yahoo_news" in failed[0]
    assert "yahoo_news" not in [line for line in out.splitlines() if "not checked" in line][0]


# ----------------------------------------------------------------------------- D10: --symbol matches any historical symbol

def _rename_testco(w):
    """TESTCO was OLDTEST until 2025-01-01: a second symbol_history row for the same security."""
    conn = connect(w.db)
    conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, valid_to, source) "
                 "VALUES (1, 'OLDTEST', 'OLDTEST.NS', '2024-01-01', '2025-01-01', 'test')")
    conn.close()


def test_list_accepts_a_current_or_historical_symbol_in_any_case(world, capsys):
    _rename_testco(world)
    for symbol in ("OLDTEST", "oldtest", "TESTCO"):
        assert run(world, "triage", "list", "--as-of", AS_OF, "--symbol", symbol, "--json") == 0
        assert [a["key"] for a in json.loads(capsys.readouterr().out)["anomalies"]] == [KEY], symbol
    assert run(world, "triage", "list", "--as-of", AS_OF, "--symbol", "FLAT2", "--json") == 0
    assert json.loads(capsys.readouterr().out)["anomalies"] == []               # a known security with no anomaly
    assert run(world, "triage", "list", "--as-of", AS_OF, "--symbol", "NOSUCH") == 1
    assert "NOSUCH" in capsys.readouterr().err


def test_show_finds_the_security_by_a_historical_symbol(world, capsys):
    _rename_testco(world)
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--symbol", "OLDTEST", "--ex-date", EX, "--json") == 0
    assert json.loads(capsys.readouterr().out)["subject"]["key"] == KEY
    assert run(world, "triage", "show", "--symbol", "OLDTEST") == 0               # one proposal: no ex-date needed
    assert f"TRIAGE {KEY}" in capsys.readouterr().out
    assert run(world, "triage", "show", "--symbol", "OLDTEST", "--isin", "INE000000002", "--ex-date", EX) == 1
    assert "INE000000001" in capsys.readouterr().err                              # symbol and ISIN disagree
    assert run(world, "triage", "show", "--symbol", "NOSUCH", "--ex-date", EX) == 1


def test_show_by_symbol_without_an_ex_date_lists_the_choices_when_there_are_several(world, capsys):
    conn = connect(world.db)
    conn.execute("INSERT INTO corporate_actions (security_id, ex_date, kind, source, observed_at, note) "
                 "VALUES (1, '2026-03-02', 'suspected', 'inferred', '2026-10-01T00:00:00.000000Z', ?)",
                 (json.dumps({"gross_factor": 0.5}),))
    conn.close()
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--symbol", "TESTCO") == 1
    err = capsys.readouterr().err
    assert f"{ISIN}@2026-03-02" in err and KEY in err and "--ex-date" in err


def test_attach_accepts_a_historical_symbol_instead_of_an_isin(world, capsys):
    _rename_testco(world)
    circular = world.root / "circular.txt"
    circular.write_text("NSE circular: TESTCO special pre-open session. " + SPOS_EXCERPT + "\n")
    argv = ["triage", "attach", "--as-of", AS_OF, "--ex-date", EX, "--title", "TESTCO: special pre-open session for demerger",
            "--published-at", "2025-10-10", "--file", str(circular), "--source-class", "exchange", "--relevance", "entity",
            "--excerpt", SPOS_EXCERPT, "--field", "prev_close=600", "--field", "spos_price=360", *HUMAN]
    assert run(world, *argv, "--symbol", "OLDTEST") == 0
    assert f"for {ISIN}@{EX}" in capsys.readouterr().out
    assert len(world.q("SELECT 1 FROM captures")) == 1
    assert run(world, *argv, "--isin", ISIN) == 0                                  # the same envelope: already archived
    assert "Already archived" in capsys.readouterr().out and len(world.q("SELECT 1 FROM captures")) == 1
    assert run(world, *argv, "--symbol", "NOSUCH") == 1
    assert run(world, *argv) == 1                                                  # neither --isin nor --symbol
    assert "--isin" in capsys.readouterr().err


def test_show_unknown_subject_exits_1(world, capsys):
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 1
    assert run(world, "triage", "show", "--proposal-id", "PT-none") == 1
    assert run(world, "triage", "show") == 1


def test_show_flags_a_tampered_pack_and_an_altered_envelope(world, capsys):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    (pid, *_), = [r for r in engine_rows(world) if r[1] == "proposed"]
    (archive_path,), = world.q("SELECT archive_path FROM captures")
    Path(archive_path).write_text(Path(archive_path).read_text().replace("exchange", "company"))
    pack = world.root / "d" / "triage" / "packs" / f"{pid}.json"
    pack.write_text(pack.read_text() + " ")
    capsys.readouterr()
    assert run(world, "triage", "show", "--proposal-id", pid, "--json") == 0
    view = json.loads(capsys.readouterr().out)
    assert view["pack"]["status"] == "hash_mismatch"
    assert view["evidence"][0]["admissible"] is False and view["evidence"][0]["reason"] == "envelope_hash_mismatch"


def resolve_args(w, pid, confirm, *extra):
    return ["data", "actions-resolve", "--db", str(w.db), "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--kind", "demerger",
            "--factor", "1.666667", "--evidence", "NSE circular: previous close 600.00, special pre-open 360.00",
            "--proposal-id", pid, "--confirm", confirm, *HUMAN, *extra]


def proposed_engine(w):
    (pid, _status, payload_json, _ev), = [r for r in engine_rows(w) if r[1] == "proposed"]
    return pid, hashlib.sha256(payload_json.encode()).hexdigest()[:8]


def test_actions_resolve_links_the_decision_to_the_proposal(world, capsys):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    pid, confirm = proposed_engine(world)
    assert main(resolve_args(world, pid, confirm)) == 0
    assert "Resolved" in capsys.readouterr().out
    (decision_id, proposal_id), = world.q("SELECT decision_id, proposal_id FROM decisions")
    assert proposal_id == pid
    assert world.q("SELECT status, decision_id FROM proposals WHERE proposal_id = ?", (pid,)) == [("approved", decision_id)]
    assert world.q("SELECT kind, adj_factor FROM corporate_actions WHERE decision_id IS NOT NULL")[0][0] == "demerger"
    # the resolved anomaly drops out of the list
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    assert json.loads(capsys.readouterr().out)["anomalies"] == []


def test_actions_resolve_refuses_a_wrong_confirm_code(world, capsys):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    pid, confirm = proposed_engine(world)
    wrong = "00000000" if confirm != "00000000" else "ffffffff"
    assert main(resolve_args(world, pid, wrong)) == 3
    assert "confirm" in capsys.readouterr().err
    assert world.q("SELECT 1 FROM decisions") == []
    assert world.q("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("proposed",)]
    assert main(["data", "actions-resolve", "--db", str(world.db), "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX,
                 "--kind", "demerger", "--factor", "1.666667", "--evidence", "x", "--confirm", confirm, *HUMAN]) == 3
    assert world.q("SELECT 1 FROM decisions") == []


def test_actions_resolve_without_a_proposal_is_unchanged(world):
    assert main(["data", "actions-resolve", "--db", str(world.db), "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX,
                 "--kind", "demerger", "--factor", "1.666667", "--evidence", "filing", *HUMAN]) == 0
    assert world.q("SELECT proposal_id FROM decisions") == [(None,)]


# ----------------------------------------------------------------------------- submit

def built_with_circular(world):
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0


def test_submit_stores_a_validated_llm_proposal(world, capsys):
    built_with_circular(world)
    path = write_json(world, "agent.json", agent_proposal(world))
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *LLM) == 0
    (pid, rule_id, proposed_by, status, review), = world.q(
        "SELECT proposal_id, rule_id, proposed_by, status, llm_review FROM proposals WHERE rule_id = 'TRIAGE_AGENT_V1'")
    assert (rule_id, proposed_by, status) == ("TRIAGE_AGENT_V1", "llm:test-model", "proposed")
    assert review is None                                                       # same label as the engine
    assert (world.root / "d" / "triage" / "sheets" / f"{pid}.md").is_file()
    assert pid in capsys.readouterr().out
    # show puts the two proposals side by side
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    text = capsys.readouterr().out
    assert "llm:test-model" in text and "rules:triage-rules/1" in text


def test_submit_dry_run_writes_nothing(world, capsys):
    built_with_circular(world)
    path = write_json(world, "agent.json", agent_proposal(world))
    before, files = world.fingerprint(), world.files()
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), "--dry-run", *LLM) == 0
    assert world.fingerprint() == before and world.files() == files
    assert "would" in capsys.readouterr().out


def test_submit_refuses_every_error_and_writes_nothing(world, capsys):
    built_with_circular(world)
    bad = agent_proposal(world)
    bad["citations"][0]["quote"] = "Previous close Rs 999.00"                     # not a verbatim excerpt
    bad["citations"].append({"id": "c2", "capture_id": "TD-0000000000000000", "quote": "x"})   # no such capture
    path = write_json(world, "agent.json", bad)
    before = world.fingerprint()
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *LLM) == 3
    err = capsys.readouterr().err
    assert "V4" in err and "V2" in err                                           # every error, not only the first
    assert world.fingerprint() == before


def test_submit_refuses_human_and_system_actors(world):
    built_with_circular(world)
    path = write_json(world, "agent.json", agent_proposal(world))
    before = world.fingerprint()
    for actor in (HUMAN, ["--actor-kind", "system", "--by", "system:cron"], ["--actor-kind", "llm", "--by", "human:owner"]):
        assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *actor) == 3
    assert world.fingerprint() == before


def test_submit_needs_a_built_pack_a_readable_file_and_the_right_cohort(world, capsys):
    path = write_json(world, "agent.json", {"schema": "triage.proposal/1"})
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *LLM) == 3      # schema errors
    assert "V1" in capsys.readouterr().err
    (world.root / "junk.json").write_text("{not json")
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(world.root / "junk.json"), *LLM) == 3
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(world.root / "nope.json"), *LLM) == 1
    assert run(world, "triage", "submit", "--file", str(path), *LLM) == 1

    built_with_circular(world)
    other = agent_proposal(world)
    other["as_of"] = "2026-10-29"
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(write_json(world, "o.json", other)), *LLM) == 3
    # a proposal for an anomaly nobody built has no pack to validate against
    stray = agent_proposal(world)
    stray["key"] = "INE000000009@2025-10-14"
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(write_json(world, "s.json", stray)), *LLM) == 3
    assert "build" in capsys.readouterr().err


def test_submitted_proposal_cannot_go_through_kb_approve(world):
    built_with_circular(world)
    path = write_json(world, "agent.json", agent_proposal(world))
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *LLM) == 0
    (pid,), = world.q("SELECT proposal_id FROM proposals WHERE rule_id = 'TRIAGE_AGENT_V1'")
    assert main(["kb", "approve", pid, "--db", str(world.db), *HUMAN]) == 3
    assert world.q("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("proposed",)]


# ----------------------------------------------------------------------------- reject

def test_reject_is_human_only_and_records_the_reason(world, capsys):
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "bad_evidence", *LLM) == 3
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "bad_evidence",
               "--actor-kind", "system", "--by", "system:cron") == 3
    assert world.q("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("proposed",)]
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "Bad Reason!", *HUMAN) == 1
    assert run(world, "triage", "reject", "--proposal-id", pid, *HUMAN) == 1

    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "bad_evidence", *HUMAN) == 0
    (status, decided_by, review), = world.q("SELECT status, decided_by, llm_review FROM proposals WHERE proposal_id = ?", (pid,))
    assert status == "rejected" and decided_by == "human:owner" and "REJECT_bad_evidence" in review
    notes = world.q("SELECT notes_json FROM runs WHERE kind = 'triage' ORDER BY run_id DESC")[0][0]
    assert json.loads(notes)["reject"] == {"proposal_id": pid, "reason": "bad_evidence"}
    assert world.q("SELECT 1 FROM decisions") == []                              # rejecting resolves nothing

    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "bad_evidence", *HUMAN) == 3   # already decided
    assert run(world, "triage", "reject", "--proposal-id", "PT-none", "--reason", "stale", *HUMAN) == 1


def _runs(w):
    return w.q("SELECT kind, status, notes_json FROM runs ORDER BY run_id")


def test_a_refused_actions_resolve_is_audited_as_a_refused_run_and_writes_no_data_row(world, capsys):
    """D5: the refusal happens inside the data_fix run, so the run row is the audit record and nothing else is kept."""
    assert build_all(world) == 0
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    pid, confirm = proposed_engine(world)
    wrong = "00000000" if confirm != "00000000" else "ffffffff"
    before = world.q("SELECT (SELECT count(*) FROM decisions), (SELECT count(*) FROM corporate_actions), "
                     "(SELECT count(*) FROM runs), (SELECT count(*) FROM proposals)")[0]
    assert main(resolve_args(world, pid, wrong)) == 3
    assert "confirm" in capsys.readouterr().err
    after = world.q("SELECT (SELECT count(*) FROM decisions), (SELECT count(*) FROM corporate_actions), "
                    "(SELECT count(*) FROM runs), (SELECT count(*) FROM proposals)")[0]
    assert after[:2] == before[:2] and after[3] == before[3]                       # no decision, no corporate action
    assert after[2] == before[2] + 1                                              # one audit run
    kind, status, notes = _runs(world)[-1]
    assert (kind, status) == ("data_fix", "refused") and "confirm" in json.loads(notes)["error"]
    assert world.q("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("proposed",)]


def test_a_reject_refused_before_its_run_opens_leaves_no_run_row(world, capsys):
    """D5: the first 'not open' check runs on a read-only connection, before any RunContext: nothing is audited."""
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "stale", *HUMAN) == 0
    capsys.readouterr()
    before = world.fingerprint()
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "stale", *HUMAN) == 3
    assert "not 'proposed'" in capsys.readouterr().err
    assert world.fingerprint() == before                                          # no runs row, no ledger event
    assert not [r for r in _runs(world) if r[1] == "refused"]


def test_a_reject_that_loses_a_race_inside_its_run_is_audited_as_refused(world, monkeypatch, capsys):
    """D5: only the race path is refused inside the run: the proposal is no longer open by the time the run reads it."""
    from quant.triage import store as triage_store
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "stale", *HUMAN) == 0
    capsys.readouterr()
    real = triage_store.get_proposal
    monkeypatch.setattr(triage_store, "get_proposal", lambda conn, p: {**real(conn, p), "status": "proposed"})   # a stale read
    before = len(_runs(world))
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "duplicate", *HUMAN) == 3
    assert "no longer" in capsys.readouterr().err
    assert len(_runs(world)) == before + 1
    kind, status, notes = _runs(world)[-1]
    assert (kind, status) == ("triage", "refused") and "no longer" in json.loads(notes)["error"]
    flags = world.q("SELECT llm_review FROM proposals WHERE proposal_id = ?", (pid,))[0][0]
    assert "REJECT_duplicate" not in flags                                        # the refused attempt changed no proposal


def test_rebuild_after_a_reject_keeps_it_rejected_until_the_evidence_changes(world):
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "stale", *HUMAN) == 0
    assert build_all(world) == 0
    assert [r[1] for r in engine_rows(world)] == ["rejected"]                    # identical payload: no second row
    assert attach_circular(world) == 0
    assert build_all(world) == 0
    assert [r[1] for r in engine_rows(world)] == ["rejected", "proposed"]


# ----------------------------------------------------------------------------- adversarial review fixes

NEXT_COHORT = "2026-11-02"                      # a Monday: a later cohort whose cutoff the evening clock has not passed


def test_build_refuses_an_llm_actor(world, capsys):
    """GOV-2: an engine proposal skips the validator, so an agent may not run 'triage build'."""
    before = world.fingerprint()
    assert build_all(world, *LLM) == 3
    assert "llm" in capsys.readouterr().err and world.fingerprint() == before and world.files() == []
    assert run(world, "triage", "build", "--as-of", AS_OF, "--all", "--dry-run", *LLM) == 3
    assert build_all(world, *HUMAN) == 0 and build_all(world) == 0               # human and the default system actor still build


def test_submit_refuses_a_proposal_checked_against_another_cohorts_pack(world, capsys):
    """ADV-PIT-1: cohort A's pack and engine row carry A's features. A submission for cohort B must say 'build first'."""
    built_with_circular(world)
    stale = agent_proposal(world)
    stale["as_of"] = NEXT_COHORT
    path = write_json(world, "agent_b.json", stale)
    before = world.fingerprint()
    for extra in ((), ("--dry-run",)):
        assert run(world, "triage", "submit", "--as-of", NEXT_COHORT, "--file", str(path), *extra, *LLM) == 3
        err = capsys.readouterr().err
        assert "V1:binding" in err and "build for this cohort first" in err and f"--as-of {NEXT_COHORT}" in err
    assert world.fingerprint() == before
    assert world.q("SELECT 1 FROM proposals WHERE rule_id = 'TRIAGE_AGENT_V1'") == []
    # building for the new cohort first makes the same submission pass
    assert run(world, "triage", "build", "--as-of", NEXT_COHORT, "--all") == 0
    rebuilt = json.loads(world.q("SELECT payload_json FROM proposals WHERE rule_id = 'TRIAGE_RULES_V1' "
                                 "AND status = 'proposed'")[0][0])
    assert rebuilt["as_of"] == NEXT_COHORT
    stale["producer"]["pack_sha256"] = rebuilt["producer"]["pack_sha256"]
    assert run(world, "triage", "submit", "--as-of", NEXT_COHORT, "--file", str(write_json(world, "ok.json", stale)),
               "--dry-run", *LLM) == 0


def test_show_warns_when_the_rows_belong_to_different_cohorts(world, capsys):
    built_with_circular(world)
    path = write_json(world, "agent.json", agent_proposal(world))
    assert run(world, "triage", "submit", "--as-of", AS_OF, "--file", str(path), *LLM) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX, "--json") == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []                # one cohort: nothing to warn about
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX, "--as-of", NEXT_COHORT, "--json") == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert any(NEXT_COHORT in w and AS_OF in w and "triage build" in w for w in warnings), warnings

    assert run(world, "triage", "build", "--as-of", NEXT_COHORT, "--all") == 0   # the rules row moves to the next cohort
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX, "--json") == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert any("agent proposal" in w and AS_OF in w and NEXT_COHORT in w for w in warnings), warnings
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    assert "agent proposal" in capsys.readouterr().out


def test_list_shows_a_rejected_proposal_as_rejected_and_still_open(world, capsys):
    """F4: a rejection resolves nothing. The anomaly stays truncated, so the list must not call it resolved."""
    assert build_all(world) == 0
    (pid, *_), = engine_rows(world)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "wrong_label", *HUMAN) == 0
    assert world.q("SELECT 1 FROM corporate_actions WHERE decision_id IS NOT NULL") == []
    capsys.readouterr()
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    (row,) = json.loads(capsys.readouterr().out)["anomalies"]
    assert (row["key"], row["status"], row["proposal_id"]) == (KEY, "rejected", pid)
    assert run(world, "triage", "list", "--as-of", AS_OF) == 0
    text = capsys.readouterr().out
    assert "rejected" in text and "resolved" not in text.replace("unresolved", "")


# ----------------------------------------------------------------------------- X6: a folded placeholder

PARENT_ISIN, PARENT_SYMBOL, PARENT_JUMP = "INE000000006", "PARENT6", "2026-09-07"
DUMMY_ISIN, DUMMY_SYMBOL, DUMMY_FIRST_SEEN = "DUM000000006", "DUMMYPARENT6", "2026-10-01"
STANDALONE_KEY = f"{DUMMY_ISIN}@{DUMMY_FIRST_SEEN}"
PARENT_KEY = f"{PARENT_ISIN}@{PARENT_JUMP}"


def _seed_placeholder_event(w, *, parent_symbol=None):
    """The NSE index file lists DUMMYPARENT6 (DUM000000006): security 7 and one INDEX_PLACEHOLDER event, first seen 2026-10-01."""
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=w.cfg, clock=FrozenClock(EVENING),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (7, ?, 'Dummy Parent6', '2026-10-01', ?, 'listed')", (DUMMY_ISIN, AS_OF))
        ctx.conn.execute(
            "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
            "VALUES (?, ?, '2026-10-01T10:00:00.000000Z', 'WARN', 'INDEX_PLACEHOLDER', 7, ?)",
            (ctx.run_id, AS_OF, json.dumps({"placeholder_symbol": DUMMY_SYMBOL, "placeholder_isin": DUMMY_ISIN,
                                            "parent_symbol": parent_symbol})))


def _add_parent_with_jump(w):
    """PARENT6 (INE000000006) falls to 0.3738 of its close on 2026-09-07: the jump the placeholder belongs to."""
    dates = pd.bdate_range("2025-06-02", AS_OF).strftime("%Y-%m-%d")
    series = np.where(np.array(dates) < PARENT_JUMP, 600.0, 224.28)
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=w.cfg, clock=FrozenClock(EVENING),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (6, ?, 'Parent6 Limited', '2025-01-01', ?, 'listed')", (PARENT_ISIN, AS_OF))
        ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                         "VALUES (6, ?, 'PARENT6.NS', '2025-01-01', 'test')", (PARENT_SYMBOL,))
        ctx.store = PriceStore(w.cfg.paths.prices_db, state_conn=ctx.conn)
        ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": series, "volume": 1e5}),
                         {"security_id": 6, "close_basis": "raw", "observed_at": OBSERVED})


def _statuses(w):
    return {json.loads(r[2])["key"]: r[1] for r in engine_rows(w)}


def test_show_names_the_folded_placeholder_on_the_parent_row(world, capsys):
    _add_parent_with_jump(world)
    _seed_placeholder_event(world)
    assert run(world, "triage", "build", "--as-of", AS_OF, "--isin", PARENT_ISIN, "--ex-date", PARENT_JUMP) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", PARENT_ISIN, "--ex-date", PARENT_JUMP) == 0
    text = capsys.readouterr().out
    line, = [ln for ln in text.splitlines() if "FOLDED PLACEHOLDER" in ln]
    assert DUMMY_SYMBOL in line and DUMMY_ISIN in line and f"first seen {DUMMY_FIRST_SEEN}" in line
    assert run(world, "triage", "show", "--isin", PARENT_ISIN, "--ex-date", PARENT_JUMP, "--json") == 0
    view = json.loads(capsys.readouterr().out)
    assert view["subject"]["folded_placeholders"] == [
        {"symbol": DUMMY_SYMBOL, "isin": DUMMY_ISIN, "first_seen": DUMMY_FIRST_SEEN}]


def test_show_has_no_folded_placeholder_line_for_a_jump_with_no_placeholder(world, capsys):
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX) == 0
    assert "FOLDED PLACEHOLDER" not in capsys.readouterr().out
    assert run(world, "triage", "show", "--isin", ISIN, "--ex-date", EX, "--json") == 0
    assert json.loads(capsys.readouterr().out)["subject"]["folded_placeholders"] == []


def test_show_does_not_fold_a_standalone_placeholder_into_itself(world, capsys):
    _seed_placeholder_event(world)
    assert build_all(world) == 0
    capsys.readouterr()
    assert run(world, "triage", "show", "--isin", DUMMY_ISIN, "--ex-date", DUMMY_FIRST_SEEN) == 0
    assert "FOLDED PLACEHOLDER" not in capsys.readouterr().out


def test_a_standalone_proposal_for_a_now_folded_placeholder_is_superseded_by_the_next_build(world, capsys):
    _seed_placeholder_event(world)
    assert build_all(world) == 0                                       # the placeholder is listed on its own
    capsys.readouterr()
    assert _statuses(world)[STANDALONE_KEY] == "proposed"
    _add_parent_with_jump(world)                                       # the parent's jump arrives: the event now folds into it
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    keys = {a["key"] for a in json.loads(capsys.readouterr().out)["anomalies"]}
    assert PARENT_KEY in keys and STANDALONE_KEY not in keys

    before = world.fingerprint()
    assert build_all(world, "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"would supersede" in out and STANDALONE_KEY in out and "folded" in out
    assert world.fingerprint() == before and _statuses(world)[STANDALONE_KEY] == "proposed"

    assert build_all(world) == 0
    out = capsys.readouterr().out
    assert STANDALONE_KEY in out and "superseded" in out and PARENT_ISIN in out
    statuses = _statuses(world)
    assert statuses[STANDALONE_KEY] == "superseded" and statuses[PARENT_KEY] == "proposed"
    assert build_all(world) == 0                                       # idempotent: nothing more to supersede or write
    assert _statuses(world) == statuses
    assert "supersede" not in capsys.readouterr().out


def test_a_rejected_standalone_placeholder_proposal_is_not_touched_when_it_folds(world):
    _seed_placeholder_event(world)
    assert build_all(world) == 0
    pid = next(r[0] for r in engine_rows(world) if json.loads(r[2])["key"] == STANDALONE_KEY)
    assert run(world, "triage", "reject", "--proposal-id", pid, "--reason", "other", *HUMAN) == 0
    _add_parent_with_jump(world)
    assert build_all(world) == 0
    assert _statuses(world)[STANDALONE_KEY] == "rejected"             # a human decision is never overwritten


def test_a_build_of_another_anomaly_leaves_the_standalone_proposal_alone(world):
    _seed_placeholder_event(world)
    assert build_all(world) == 0
    _add_parent_with_jump(world)
    assert run(world, "triage", "build", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX) == 0
    assert _statuses(world)[STANDALONE_KEY] == "proposed"             # only a build that covers the parent supersedes


# ----------------------------------------------------------------------------- X8: as-of bar coverage on list

def _seed_universe(w, *, extra_members=(), session_stamp="2026-10-29T10:00:00.000000Z"):
    """A NIFTY500 snapshot of securities 1-5 plus ``extra_members`` (ids with no bars in the store)."""
    with RunContext(as_of=AS_OF, kind="test", track="live", cfg=w.cfg, clock=FrozenClock(EVENING),
                    actor=Actor(kind="human", name="owner")) as ctx:
        for sid in extra_members:
            ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                             "VALUES (?, ?, ?, '2025-01-01', ?, 'listed')", (sid, f"INE0000000{sid:02d}", f"Extra {sid}", AS_OF))
        ctx.conn.execute("INSERT INTO captures (capture_id, captured_at, kind, archive_path, sha256, source_version, run_id) "
                         "VALUES ('cap_n500_test', ?, 'nifty500', 'x', ?, 'test', ?)", (session_stamp, "0" * 64, ctx.run_id))
        for sid in (1, 2, 3, 4, 5, *extra_members):
            ctx.conn.execute(
                "INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, series, "
                "source, source_sha256) VALUES ('2026-10-29', ?, ?, 'NIFTY500', ?, 'Industrials', 'EQ', 'nse_csv', 'sha')",
                (session_stamp, sid, f"SYM{sid}"))


def test_list_prints_the_as_of_bar_coverage_and_the_g3_threshold(world, capsys, request):
    _seed_universe(world, extra_members=(8, 9))                     # 7 members, 5 of them have a bar on 2026-10-30
    request.getfixturevalue("no_run_context")                       # from here on, opening a RunContext fails the test
    before = world.fingerprint()
    assert run(world, "triage", "list", "--as-of", AS_OF) == 0
    lines = capsys.readouterr().out.splitlines()
    line, = [ln for ln in lines if ln.startswith("As-of bar coverage")]
    assert "2026-10-30" in line and "5 of 7" in line and "71.4%" in line and "G3 needs 98%" in line
    assert lines.index(line) < min(i for i, ln in enumerate(lines) if ln.startswith("KIND"))     # a header: before the table
    assert world.fingerprint() == before and world.files() == []


def test_list_json_carries_the_bar_coverage(world, capsys):
    _seed_universe(world, extra_members=(8, 9))
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    cov = json.loads(capsys.readouterr().out)["bar_coverage"]
    assert cov == {"session": "2026-10-30", "members": 7, "with_close": 5, "share": pytest.approx(5 / 7, abs=1e-4),
                   "g3_min_share": 0.98, "meets_g3": False}


def test_list_full_coverage_meets_g3(world, capsys):
    _seed_universe(world)
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    cov = json.loads(capsys.readouterr().out)["bar_coverage"]
    assert (cov["members"], cov["with_close"], cov["meets_g3"]) == (5, 5, True)


def test_list_uses_the_latest_session_at_or_before_as_of(world, capsys):
    _seed_universe(world)
    assert run(world, "triage", "list", "--as-of", "2026-10-31", "--json") == 0      # a Saturday: the 30th is the session
    assert json.loads(capsys.readouterr().out)["bar_coverage"]["session"] == "2026-10-30"


def test_list_coverage_is_unknown_not_zero_without_a_universe_snapshot(world, capsys):
    assert run(world, "triage", "list", "--as-of", AS_OF, "--json") == 0
    assert json.loads(capsys.readouterr().out)["bar_coverage"] is None
    assert run(world, "triage", "list", "--as-of", AS_OF) == 0
    line, = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("As-of bar coverage")]
    assert "unknown" in line and "0 of" not in line and "G3 needs 98%" in line


def test_list_coverage_is_unknown_when_the_store_has_no_bar_at_or_before_as_of(world, capsys):
    _seed_universe(world, session_stamp="2025-01-01T10:00:00.000000Z")       # a snapshot exists; the store's first bar is 2025-06-02
    assert run(world, "triage", "list", "--as-of", "2025-01-15", "--json") == 0
    assert json.loads(capsys.readouterr().out)["bar_coverage"] is None


# ----------------------------------------------------------------------------- plumbing

def test_the_triage_group_is_registered_with_every_command(capsys):
    assert main(["triage"]) == 0
    text = capsys.readouterr().out
    for name in ("list", "collect", "attach", "build", "show", "submit", "reject"):
        assert name in text


def test_the_default_yahoo_client_is_a_throttled_yahoo_client():
    client = triage_cmd._yahoo_client(load_config(), FrozenClock(EVENING))
    assert isinstance(client, YahooClient) and client.accessor_sleep_s >= 0.5


def test_gitignore_keeps_private_bodies_out_of_git():
    lines = (REPO / ".gitignore").read_text().splitlines()
    assert "data/archive/triage_private/" in lines             # its own line: data/archive/ may not always be ignored
    if (REPO / ".git").exists():                              # an export of the tree has no git to ask
        out = subprocess.run(["git", "check-ignore", "-q", "data/archive/triage_private/abc"], cwd=REPO)
        assert out.returncode == 0


def _protected():
    """Size and mtime of the repository's databases, and every file under its triage and archive directories."""
    files = {}
    for rel in ("quant.db", "quant_engine.db", "data/prices_daily.sqlite"):
        p = REPO / rel
        files[rel] = (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
    for root in ("data/triage", "data/archive"):
        for p in sorted((REPO / root).rglob("*")) if (REPO / root).exists() else []:
            files[p.relative_to(REPO).as_posix()] = (p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else "dir"
    return files


def test_commands_follow_the_path_overrides_and_leave_the_repository_databases_alone(world):
    """No --db here: every path comes from the QUANT_* overrides the world set, as it does in production."""
    before = _protected()
    circular = world.root / "circular.txt"
    circular.write_text("NSE circular: TESTCO special pre-open session. " + SPOS_EXCERPT + "\n")
    flow = [
        ["triage", "list", "--as-of", AS_OF],
        ["triage", "collect", "--as-of", AS_OF, "--all", *HUMAN],
        ["triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "TESTCO circular",
         "--published-at", "2025-10-10", "--file", str(circular), "--source-class", "exchange",
         "--relevance", "entity", *HUMAN],
        ["triage", "build", "--as-of", AS_OF, "--all"],
        ["triage", "show", "--isin", ISIN, "--ex-date", EX],
        ["data", "actions-list"],
    ]
    for argv in flow:
        assert main(argv) == 0, argv
    pid, _ = proposed_engine(world)
    assert main(["triage", "reject", "--proposal-id", pid, "--reason", "stale", *HUMAN]) == 0
    assert len(world.q("SELECT 1 FROM captures")) == 3 and world.q("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)) == [("rejected",)]
    assert (world.root / "d" / "triage" / "packs" / f"{pid}.json").is_file()      # the writes landed in the world ...
    assert _protected() == before                                                   # ... and not in the repository


def test_a_missing_state_database_fails_loudly_and_is_never_created(world, tmp_path):
    """The poison check: with the overrides pointing nowhere, no command may fall back to a default or create a file."""
    nowhere = tmp_path / "nowhere"
    for var in ("QUANT_DB_PATH", "QUANT_PRICES_DB_PATH", "QUANT_DATA_DIR", "QUANT_ARCHIVE_DIR", "QUANT_KNOWLEDGE_DIR"):
        world.monkeypatch.setenv(var, str(nowhere / var.lower()))
    before = _protected()
    proposal = write_json(world, "p.json", {"schema": "triage.proposal/1"})
    attempts = [
        ["triage", "list", "--as-of", AS_OF],
        ["triage", "show", "--isin", ISIN, "--ex-date", EX],
        ["triage", "collect", "--as-of", AS_OF, "--all", *HUMAN],
        ["triage", "collect", "--as-of", AS_OF, "--all", "--dry-run", *HUMAN],
        ["triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title", "t", "--url", "https://x.org",
         "--source-class", "news", "--relevance", "context", *HUMAN],
        ["triage", "build", "--as-of", AS_OF, "--all"],
        ["triage", "submit", "--as-of", AS_OF, "--file", str(proposal), *LLM],
        ["triage", "reject", "--proposal-id", "PT-none", "--reason", "stale", *HUMAN],
        ["data", "actions-list"],
    ]
    for argv in attempts:
        assert main(argv) in (1, 3), argv                   # 1 = cannot open the database; 3 = refused before it was opened
    assert not nowhere.exists()
    assert world.client.calls == [] and _protected() == before


# ----------------------------------------------------------------------------- data actions-list stays read-only

def test_actions_list_does_not_touch_the_price_store(world, capsys):
    prices = world.root / "p.db"
    before = (prices.stat().st_size, prices.stat().st_mtime_ns)
    assert main(["data", "actions-list", "--db", str(world.db), "--json"]) == 0
    capsys.readouterr()
    assert (prices.stat().st_size, prices.stat().st_mtime_ns) == before

    prices.unlink()                                         # a missing price store is never created; the list degrades
    assert main(["data", "actions-list", "--db", str(world.db), "--json"]) == 0
    out = capsys.readouterr()
    assert "price store not found: in-window flags unavailable" in out.err and not prices.exists()
    assert all(r["in_trailing_13m"] is None for r in json.loads(out.out))   # unknown, not "no"
