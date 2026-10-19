"""MangroveAI-owned reads have no REST copy in this client."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "research.db"))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    from src.app import create_app
    with TestClient(create_app()) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/agent/knowledge/documents"),
    ("get", "/api/v1/agent/knowledge/stats"),
    ("post", "/api/v1/agent/knowledge/query"),
])
def test_retired_knowledge_routes_are_absent(client, method, path):
    assert getattr(client, method)(path, headers=_auth()).status_code == 404


# -- backtests -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/agent/backtests", "/api/v1/agent/backtests/b1"])
def test_stored_backtests_are_read_from_mangroveai_not_here(client, path):
    """get_backtest / list_backtests are MangroveAI MCP tools; no local copy."""
    assert client.get(path, headers=_auth()).status_code == 404


@pytest.mark.parametrize("path", [
    "/api/v1/agent/market/benchmark?asset=ETH&lookback_days=5",
    "/api/v1/agent/market/ohlcv?symbol=BTC",
    "/api/v1/agent/signals",
    "/api/v1/agent/kb/search?q=rsi",
    "/api/v1/agent/on-chain/whale-activity?symbol=ETH",
    "/api/v1/agent/oracle/results",
    "/api/v1/agent/oracle/backtest/async/job-1",
])
def test_mangroveai_reads_have_no_local_copy(client, path):
    """Market, signal, KB, on-chain, benchmark and Oracle operations are MangroveAI MCP
    tools, reached through the MCP proxy; this client keeps no REST copy of them."""
    assert client.get(path, headers=_auth()).status_code == 404
