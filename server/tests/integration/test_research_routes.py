"""Integration tests for the knowledge, benchmark and backtest-history routes.

The knowledge graph is real (bundled with mangrove-kb, offline); the
mangroveai SDK is mocked for OHLCV and stored backtests.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"
TODAY = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def sdk():
    client = MagicMock()
    client.crypto_assets.get_ohlcv.return_value = {"success": True, "symbol": "ETH", "data": [
        {"timestamp": (TODAY - timedelta(days=200 - i)).isoformat(), "close": 2000 + i * 2.5}
        for i in range(201)
    ]}
    client.users.get_my_backtests.return_value = SimpleNamespace(total=1, items=[SimpleNamespace(
        id="b1", asset="ETH", status="completed", result="FAIL",
        start_date="2026-03-17T00:00:00+00:00", end_date="2026-09-13T00:00:00+00:00",
        initial_balance=10000.0, total_return=1.3, irr_annualized=2.7, sharpe_ratio=0.4,
        win_rate=50.0, max_drawdown=1.1, total_trades=4, execution_time=11.0,
        created_at="2026-09-13T20:15:09+00:00", archived=False,
        metrics={"huge": "payload"}, trade_history=[{"x": 1}],
    )])
    start = (TODAY - timedelta(days=100)).isoformat()
    end = (TODAY - timedelta(days=10)).isoformat()
    from mangrove_ai.models.backtesting import BacktestResult

    client.backtesting.get.return_value = BacktestResult.model_validate({
        "id": "b1", "asset": "ETH", "status": "completed", "strategy_id": None,
        "config": {"name": "eth momentum", "asset": "ETH",
                   "entry": [{"name": "rsi_cross_up", "timeframe": "1h"}],
                   "exit": [{"name": "sma_cross_down", "timeframe": "15m"}],
                   "execution_config": {"reward_factor": 2}},
        "metrics": {"total_return": 3.0, "win_rate": 50.0, "total_trades": 2},
        "trade_history": [{"pnl": 1}, {"pnl": -1}],
        "error_message": None, "start_date": start, "end_date": end,
        "initial_balance": 10000.0, "execution_time_seconds": 9.5,
        "created_at": end, "completed_at": end,
    })
    return client


@pytest.fixture
def client(tmp_path, monkeypatch, sdk):
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "research.db"))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()
    for path in (
        "src.services.benchmark_service.mangrove_ai_client",
        "src.services.backtest_service.mangrove_ai_client",
    ):
        monkeypatch.setattr(path, lambda s=sdk: s)

    from src.app import create_app
    with TestClient(create_app()) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


# -- knowledge -----------------------------------------------------------------


def test_knowledge_stats(client):
    r = client.get("/api/v1/agent/knowledge/stats", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["nodes"] > 0
    assert "source" not in body["result"]


def test_knowledge_find_and_get(client):
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(),
                    json={"op": "find", "q": "divergence", "limit": 3})
    assert r.status_code == 200
    assert r.json()["result"]["returned"] >= 1
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(), json={"op": "get", "q": "rsi"})
    assert r.status_code == 200
    assert r.json()["result"]["name"] == "RSI"


def test_knowledge_outputs_with_infinite_ranges_serialize(client):
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(),
                    json={"op": "outputs", "q": "histogram"})
    assert r.status_code == 200


def test_knowledge_errors(client):
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(), json={"op": "get"})
    assert r.status_code == 400
    assert r.json()["code"] == "KNOWLEDGE_QUERY_INVALID"
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(), json={"op": "get", "q": "zzz_no_such_node"})
    assert r.status_code == 400
    assert "no node matching" in r.json()["message"]
    r = client.post("/api/v1/agent/knowledge/query", headers=_auth(), json={"op": "search"})
    assert r.status_code == 422


# -- benchmark -----------------------------------------------------------------


def test_benchmark_trailing(client):
    r = client.get("/api/v1/agent/market/benchmark", headers=_auth(),
                   params={"asset": "eth", "lookback_days": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["asset"] == "ETH"
    assert body["buy_and_hold_return_pct"] == 25.0  # 2000 -> 2500
    assert body["covered_window"]["bars"] == 201


def test_benchmark_requires_a_window(client):
    r = client.get("/api/v1/agent/market/benchmark", headers=_auth(), params={"asset": "ETH"})
    assert r.status_code == 400
    assert r.json()["code"] == "VALIDATION_ERROR"


# -- backtests -----------------------------------------------------------------


def test_list_backtests_returns_headline_fields_only(client, sdk):
    r = client.get("/api/v1/agent/backtests", headers=_auth(), params={"asset": "eth", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["count"] == 1
    row = body["backtests"][0]
    assert row["id"] == "b1" and row["result"] == "FAIL" and row["win_rate"] == 50.0
    assert "metrics" not in row and "trade_history" not in row
    assert body["metric_units"]["win_rate"] == "percent_0_100"
    kwargs = sdk.users.get_my_backtests.call_args.kwargs
    assert kwargs["asset"] == "ETH" and kwargs["limit"] == 5


def test_get_backtest_with_benchmark(client, sdk):
    r = client.get("/api/v1/agent/backtests/b1", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["backtest_id"] == "b1"
    assert body["strategy_name"] == "eth momentum"
    assert body["interval"] == "15m"
    assert body["trade_count"] == 2
    assert "trade_history" not in body
    assert body["benchmark"]["available"] is True
    assert "strategy_minus_benchmark_pct" in body["benchmark"]
    sdk.backtesting.get.assert_called_with("b1")

    r = client.get("/api/v1/agent/backtests/b1", headers=_auth(),
                   params={"include_trades": True, "include_benchmark": False})
    body = r.json()
    assert body["trade_history"] == [{"pnl": 1}, {"pnl": -1}]
    assert "benchmark" not in body


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/agent/knowledge/stats"),
    ("post", "/api/v1/agent/knowledge/query"),
    ("get", "/api/v1/agent/market/benchmark?asset=ETH&lookback_days=5"),
    ("get", "/api/v1/agent/backtests"),
    ("get", "/api/v1/agent/backtests/b1"),
])
def test_auth_required(client, method, path):
    r = getattr(client, method)(path, json={"op": "stats"}) if method == "post" else client.get(path)
    assert r.status_code == 401
