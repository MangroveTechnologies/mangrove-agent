"""Integration tests for strategy routes."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "sr.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    # Reuse the catalog fixture from candidate_generator tests.
    from tests.unit.test_candidate_generator import _catalog

    sdk = MagicMock()
    sdk.signals.list_iter.side_effect = lambda **kw: iter(_catalog())

    bt = MagicMock()
    bt.success = True
    bt.metrics = {"irr_annualized": 0.4, "win_rate": 0.6, "total_trades": 25,
                  "sharpe_ratio": 1.5, "max_drawdown": 0.1, "net_pnl": 2500.0}
    bt.trade_count = 25
    bt.trade_history = [{"entry": "2026-01-01", "pnl": 10}]
    bt.error = None
    sdk.backtesting.run.return_value = bt

    counter = {"n": 0}

    def _create(_req):
        counter["n"] += 1
        m = MagicMock()
        m.id = f"mg-{counter['n']}"
        m.name = getattr(_req, "name", "x")
        m.asset = getattr(_req, "asset", "ETH")
        m.status = "inactive"
        return m

    sdk.strategies.create.side_effect = _create
    sdk.strategies.update_status.return_value = MagicMock(success=True)

    eval_resp = MagicMock()
    eval_resp.new_orders = None
    eval_resp.order_intents = []
    eval_resp.orders = None
    eval_resp.model_dump.return_value = {"orders": []}
    sdk.execution.evaluate.return_value = eval_resp

    for path in (
        "src.services.candidate_generator.mangrove_ai_client",
        "src.services.backtest_service.mangrove_ai_client",
        "src.services.strategy_service.mangrove_ai_client",
    ):
        monkeypatch.setattr(path, lambda s=sdk: s)

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


def test_create_autonomous(client):
    r = client.post(
        "/api/v1/agent/strategies/autonomous",
        headers=_auth(),
        json={"goal": "momentum on ETH", "asset": "ETH", "timeframe": "1h",
              "candidate_count": 5, "seed": 1},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["strategy"]["mangrove_id"].startswith("mg-")
    assert body["generation_report"]["candidates_tried"] == 5
    assert body["generation_report"]["candidates_passed_filter"] >= 1


def test_create_manual(client):
    r = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "my strat", "asset": "ETH", "timeframe": "1h",
              "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER",
                         "timeframe": "1h", "params": {}}]},
    )
    assert r.status_code == 201
    assert r.json()["asset"] == "ETH"


def test_create_manual_bad_composition_returns_400(client):
    r = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "bad", "asset": "ETH", "timeframe": "1h",
              "entry": [
                  {"name": "a", "signal_type": "TRIGGER"},
                  {"name": "b", "signal_type": "TRIGGER"},
              ]},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "STRATEGY_INVALID_COMPOSITION"


def test_list_and_get(client):
    created = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "s", "asset": "ETH", "timeframe": "1h",
              "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER",
                         "timeframe": "1h"}]},
    ).json()

    lst = client.get("/api/v1/agent/strategies", headers=_auth())
    assert lst.status_code == 200
    assert any(s["id"] == created["id"] for s in lst.json())

    got = client.get(f"/api/v1/agent/strategies/{created['id']}", headers=_auth())
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]


def test_get_missing_returns_404(client):
    r = client.get("/api/v1/agent/strategies/nope", headers=_auth())
    assert r.status_code == 404
    assert r.json()["code"] == "STRATEGY_NOT_FOUND"


def test_patch_status_requires_confirm_for_live(client):
    created = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "s", "asset": "ETH", "timeframe": "1h",
              "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER",
                         "timeframe": "1h"}]},
    ).json()
    client.patch(f"/api/v1/agent/strategies/{created['id']}/status",
                 headers=_auth(), json={"status": "inactive"})
    # inactive → live without confirm
    r = client.patch(
        f"/api/v1/agent/strategies/{created['id']}/status",
        headers=_auth(),
        json={"status": "live",
              "allocation": {"wallet_address": "0xabc", "token": "USDC",
                              "token_address": "0xusdc", "amount": 100,
                              "slippage_pct": 0.002}},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "CONFIRMATION_REQUIRED"


def test_backtest_full(client):
    created = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "s", "asset": "ETH", "timeframe": "1h",
              "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER",
                         "timeframe": "1h"}]},
    ).json()
    r = client.post(
        f"/api/v1/agent/strategies/{created['id']}/backtest",
        headers=_auth(),
        json={"mode": "full", "lookback_months": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["metrics"]["irr_annualized"] == 0.4
    assert body["trade_history"] == [{"entry": "2026-01-01", "pnl": 10}]


def test_evaluate_manual_tick(client):
    """POST /evaluate runs a tick and returns the latest evaluation row."""
    created = client.post(
        "/api/v1/agent/strategies/manual",
        headers=_auth(),
        json={"name": "s", "asset": "ETH", "timeframe": "1h",
              "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER",
                         "timeframe": "1h"}]},
    ).json()
    # Activate to paper so tick is meaningful.
    client.patch(f"/api/v1/agent/strategies/{created['id']}/status",
                 headers=_auth(), json={"status": "inactive"})
    client.patch(f"/api/v1/agent/strategies/{created['id']}/status",
                 headers=_auth(), json={"status": "paper"})

    r = client.post(f"/api/v1/agent/strategies/{created['id']}/evaluate",
                    headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["order_count"] == 0


def test_auth_required(client):
    assert client.get("/api/v1/agent/strategies").status_code == 401
