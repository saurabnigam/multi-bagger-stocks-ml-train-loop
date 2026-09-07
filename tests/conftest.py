from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import pytest

from quant.config import Config, load as load_config
from quant.db.core import apply_schema, connect
from quant.types import Actor, FrozenClock
from quant.run import RunContext


@pytest.fixture(autouse=True)
def disable_network(monkeypatch):
    """Guard against unmocked outbound network requests during offline test execution."""
    if os.environ.get("QUANT_NETWORK") == "1":
        return

    orig_connect = socket.socket.connect

    def blocked_connect(self, address):
        # Allow loopback / unix sockets
        if isinstance(address, tuple):
            host = address[0]
            if host in ("127.0.0.1", "localhost", "::1"):
                return orig_connect(self, address)
        raise RuntimeError(f"Network call blocked in offline mode: {address}")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)


@pytest.fixture
def spec_case():
    """Helper fixture to load golden case definitions."""
    cases_path = Path(__file__).resolve().parents[1] / "docs/spec/contracts/golden_cases.json"
    data = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]

    def _get_case(name: str) -> dict:
        if name not in data:
            raise KeyError(f"Golden case '{name}' not found in golden_cases.json")
        return data[name]

    return _get_case


@pytest.fixture
def clock():
    """Deterministic frozen test clock."""
    return FrozenClock("2026-10-01T00:00:00.000000Z")


@pytest.fixture
def cfg(tmp_path):
    """Test Config with all mutable paths pointed to tmp_path."""
    db_path = tmp_path / "state.db"
    prices_db_path = tmp_path / "prices.db"
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "archive"
    knowledge_dir = tmp_path / "knowledge"
    ui_dir = tmp_path / "ui"

    return load_config().with_paths(
        db=db_path,
        prices_db=prices_db_path,
        data_dir=data_dir,
        archive_dir=archive_dir,
        knowledge_dir=knowledge_dir,
        ui_dir=ui_dir,
    )


@pytest.fixture
def ctx(cfg, clock):
    """RunContext fixture over a temporary database with canonical state schema."""
    conn = connect(cfg.paths.db)
    apply_schema(conn, kind="state")
    conn.close()

    actor = Actor(kind="system", name="pytest")
    with RunContext(
        as_of="2026-09-30",
        kind="test",
        track="live",
        cfg=cfg,
        clock=clock,
        actor=actor,
    ) as run_ctx:
        yield run_ctx


@pytest.fixture
def fake_client():
    """Fake Yahoo client fixture recording requests and returning mock responses."""
    class FakeClient:
        def __init__(self):
            self.calls = []
            self.sleeps = []

        def bundle(self, ticker: str):
            self.calls.append(("bundle", ticker))
            return None

        def download_batch(self, tickers: list[str], start: str, end: str):
            self.calls.append(("download_batch", tickers, start, end))
            return None

    return FakeClient()
