"""R-13: the agent contract and prompt name the exact JSON paths of the pack and its hash in ``triage show --json``.

An agent that reads the wrong key (the top-level ``pack`` object is a wrapper, the pack itself is ``pack.data``)
copies the wrong hash into ``producer.pack_sha256`` and every proposal fails V1. The documents say where the
values are. This file builds a disposable fixture world, runs the real ``triage build`` and ``triage show --json``,
and checks that every documented path exists in that output and holds what the document says it holds.

Offline: a tmp state database and price store, a FrozenClock, no Yahoo client is built, and the conftest network
guard stays on. Nothing touches a real database.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.cli import main
from quant.commands import triage as triage_cmd
from quant.config import load as load_config
from quant.data.prices import PriceStore
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.triage import store as triage_store
from quant.types import Actor, FrozenClock

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "operations"
CONTRACT = DOCS / "triage_agent_contract.md"
PROMPT = DOCS / "triage_agent_prompt.md"
WRAPPER = ROOT / ".claude" / "agents" / "evidence-triage.md"
RUNS = ROOT / "tests" / "fixtures" / "triage" / "llm_runs"

AS_OF = "2026-10-30"
EVENING = "2026-10-30T13:00:00.000000Z"           # 18:30 IST on the cohort day, before the cutoff
OBSERVED = "2026-10-30T10:06:00.000000Z"
ISIN = "INE000000001"
EX = "2025-10-14"
SECTION = "Reading `triage show --json`"


@pytest.fixture(scope="module")
def shown(tmp_path_factory):
    """The parsed output of the real ``triage show --json`` on a fixture world with one demerger-sized fall."""
    import quant.db.core as dbcore
    root = tmp_path_factory.mktemp("triage_contract")
    db, prices = root / "state.db", root / "p.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
        mp.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))
        for name, value in (("DB_PATH", db), ("PRICES_DB_PATH", prices), ("DATA_DIR", root / "d"),
                            ("ARCHIVE_DIR", root / "a"), ("KNOWLEDGE_DIR", root / "k")):
            mp.setenv(f"QUANT_{name}", str(value))
        mp.setattr(triage_cmd, "_clock", lambda: FrozenClock(EVENING))
        conn = connect(db)
        apply_schema(conn, kind="state")
        conn.close()
        cfg = load_config().with_paths(db=db, prices_db=prices, data_dir=root / "d", archive_dir=root / "a",
                                       knowledge_dir=root / "k", ui_dir=root / "u")
        dates = pd.bdate_range("2025-09-01", AS_OF).strftime("%Y-%m-%d")
        flat = np.full(len(dates), 600.0)
        drop = flat.copy()
        drop[list(dates).index(EX):] = 360.0                  # gross factor 0.6
        with RunContext(as_of=AS_OF, kind="test", track="live", cfg=cfg, clock=FrozenClock(EVENING),
                        actor=Actor(kind="human", name="owner")) as ctx:
            ctx.store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
            for sid, series in ((1, drop), (2, flat), (3, flat)):
                ctx.conn.execute(
                    "INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                    "VALUES (?, ?, ?, '2025-01-01', ?, 'listed')",
                    (sid, f"INE00000000{sid}", "Testco Industries Limited" if sid == 1 else f"Flatco {sid} Limited", AS_OF))
                sym = "TESTCO" if sid == 1 else f"FLAT{sid}"
                ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                                 "VALUES (?, ?, ?, '2025-01-01', 'test')", (sid, sym, f"{sym}.NS"))
                ctx.store.ingest(ctx, pd.DataFrame({"date": dates, "close": series, "volume": 1e5}),
                                 {"security_id": sid, "close_basis": "raw", "observed_at": OBSERVED})
        excerpt = "Previous close Rs 600.00; special pre-open price Rs 360.00"
        assert main(["triage", "attach", "--as-of", AS_OF, "--isin", ISIN, "--ex-date", EX, "--title",
                     "TESTCO: special pre-open session for demerger", "--published-at", "2025-10-10", "--url",
                     "https://www.nseindia.com/circular/1", "--source-class", "exchange", "--relevance", "entity",
                     "--excerpt", excerpt, "--field", "prev_close=600", "--field", "spos_price=360", "--field",
                     "action_type=demerger", "--actor-kind", "human", "--by", "human:owner", "--db", str(db)]) == 0
        assert main(["triage", "build", "--as-of", AS_OF, "--all", "--db", str(db)]) == 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["triage", "show", "--isin", ISIN, "--ex-date", EX, "--json", "--db", str(db)])
        assert code == 0
    return json.loads(out.getvalue())


# ------------------------------------------------------------------ reading the documented paths

def _section(path: Path) -> str:
    text = path.read_text()
    assert f"## {SECTION}" in text, f"{path.name} has no '{SECTION}' section"
    return text.split(f"## {SECTION}", 1)[1].split("\n## ", 1)[0]


def documented_paths(path: Path = CONTRACT) -> dict[str, str]:
    """``{item: path}`` from the table in the contract: first cell the item, second cell the dotted path."""
    out = {}
    for line in _section(path).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 2 and cells[0].startswith("`"):
            out[cells[0].strip("`")] = cells[1].strip("`")
    return out


def resolve(doc, path: str) -> list:
    """Every value at a dotted path. A segment ending in ``[]`` fans out over an array. Raises KeyError if absent."""
    nodes = [doc]
    for seg in path.split("."):
        many = seg.endswith("[]")
        key = seg[:-2] if many else seg
        nxt = []
        for n in nodes:
            if not isinstance(n, dict) or key not in n:
                raise KeyError(f"{path}: no key {key!r}")
            v = n[key]
            if many:
                if not isinstance(v, list) or not v:
                    raise KeyError(f"{path}: {key!r} is not a non-empty array")
                nxt.extend(v)
            else:
                nxt.append(v)
        nodes = nxt
    return nodes


def test_the_contract_documents_the_pack_and_its_hash_paths():
    paths = documented_paths()
    assert paths["pack"] == "pack.data" and paths["pack_sha256"] == "pack.sha256"
    assert {"pack", "pack_sha256", "pack_status", "engine_pack_sha256", "evidence"} <= set(paths)


def test_every_documented_path_exists_in_real_show_json_output(shown):
    for item, path in documented_paths().items():
        values = resolve(shown, path)
        assert values and all(v is not None for v in values), f"{item}: {path} is null or empty in the real output"


def test_the_documented_pack_is_the_pack_object_and_its_hash_is_the_documented_one(shown):
    paths = documented_paths()
    (pack,) = resolve(shown, paths["pack"])
    (sha,) = resolve(shown, paths["pack_sha256"])
    (status,) = resolve(shown, paths["pack_status"])
    assert status == "ok" and pack["schema"] == "triage.pack/1"
    assert triage_store.pack_bytes(pack)[0] == sha                        # the hash an agent copies is the hash of that object
    engine = [p for p in shown["proposals"] if p["producer"] == "engine"]
    assert [p["payload"]["producer"]["pack_sha256"] for p in engine] == [sha]
    assert resolve(shown, paths["engine_pack_sha256"]) == [sha]           # one engine proposal here, so one value
    assert shown["pack"]["sha256"] == sha and "pack_sha256" not in shown["pack"]
    assert "pack_sha256" not in shown                                     # nothing at the top level carries the hash


def test_the_recorded_runs_embed_the_object_at_the_documented_path(shown):
    """A recorded run's ``pack`` is ``pack.data`` of ``triage show --json``, not the wrapper around it."""
    (pack,) = resolve(shown, documented_paths()["pack"])
    for path in sorted(RUNS.rglob("*.json")):
        run = json.loads(path.read_text())
        assert set(run["pack"]) == set(pack), path.name
        assert run["pack"]["schema"] == pack["schema"] and "data" not in run["pack"]


def test_the_wrapper_object_has_the_keys_the_documents_name(shown):
    assert set(shown["pack"]) == {"status", "path", "sha256", "data"}


# ------------------------------------------------------------------ the prompt and the wrapper agree with the contract

@pytest.mark.parametrize("path", [CONTRACT, PROMPT], ids=lambda p: p.name)
def test_the_contract_and_the_prompt_both_name_the_paths(path):
    text = path.read_text()
    for must in ("`pack.data`", "`pack.sha256`", "`pack.status`"):
        assert must in text, f"{path.name} never says {must}"


def test_the_prompt_says_to_copy_the_hash_from_the_documented_path_into_the_proposal():
    text = PROMPT.read_text()
    assert re.search(r"producer\.pack_sha256`? (?:to|from|equal to)[^.\n]*`pack\.sha256`", text), \
        "the prompt does not tie producer.pack_sha256 to pack.sha256"
    assert "pack.status" in text and re.search(r"not `?ok`?", text)       # what to do when the pack is not intact


def test_no_agent_document_still_calls_the_whole_wrapper_the_pack():
    stale = ("the `pack` object of `triage show --json`", "pack object of `triage show --json`")
    for path in (CONTRACT, PROMPT, WRAPPER):
        text = path.read_text()
        for s in stale:
            assert s not in text, f"{path.name}: {s}"


def test_the_wrapper_agent_points_at_the_same_paths():
    text = WRAPPER.read_text()
    assert "pack.data" in text and "pack.sha256" in text
