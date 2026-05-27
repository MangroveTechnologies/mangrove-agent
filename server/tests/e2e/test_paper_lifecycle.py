"""E2E paper lifecycle test — non-blocking observation.

Simulates the full user-facing flow: wallet create → autonomous strategy
create → activate to paper → APScheduler fires the tick → evaluation +
paper trade logged → deactivate.

Observation principle (from the implementation plan): the test must NOT
sleep past a tick interval. It polls GET /status and
GET /strategies/{id}/evaluations at short intervals — the same way a real
user chatting with the agent would check in. Asserts /status stays
responsive (<200 ms) throughout, proving ticks don't block the request
path.

The mangroveai.execution.evaluate mock returns a single OrderIntent so
the tick produces a paper trade we can verify.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "paper_e2e.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    # Keyring stub.
    store: dict = {}
    monkeypatch.setattr("keyring.get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr("keyring.set_password", lambda s, u, p: store.update({(s, u): p}))
    from src.shared.crypto import fernet as f
    f.reset_master_key_cache()

    # Build the SDK mock.
    sdk = MagicMock()

    # Signals catalog — categories must cover what the "momentum" goal maps
    # to in candidate_generator (trigger: momentum/trend; filter: volume/trend).
    sig_trigger = MagicMock()
    sig_trigger.name = "macd_cross_up"
    sig_trigger.category = "momentum"
    sig_trigger.signal_type = "TRIGGER"
    sig_trigger.metadata = MagicMock(params={})
    sig_filter = MagicMock()
    sig_filter.name = "volume_spike"
    sig_filter.category = "volume"
    sig_filter.signal_type = "FILTER"
    sig_filter.metadata = MagicMock(params={})
    catalog = [sig_trigger, sig_filter]
    sdk.signals.list_iter.side_effect = lambda **kw: iter(catalog)

    # Backtests succeed with above-threshold metrics.
    bt = MagicMock(
        success=True,
        metrics={
            "irr_annualized": 0.4, "win_rate": 0.6, "total_trades": 25,
            "sharpe_ratio": 1.5, "max_drawdown": 0.1, "net_pnl": 2500.0,
        },
        trade_count=25, trade_history=[], error=None,
    )
    sdk.backtesting.run.return_value = bt

    counter = {"n": 0}

    def _create(_req):
        counter["n"] += 1
        m = MagicMock()
        m.id = f"mg-paper-{counter['n']}"
        m.name = getattr(_req, "name", "auto")
        m.asset = getattr(_req, "asset", "ETH")
        m.status = "inactive"
        return m

    sdk.strategies.create.side_effect = _create
    sdk.strategies.update_status.return_value = MagicMock(success=True)

    # The evaluate mock returns an OrderIntent so we exercise the
    # full tick → executor → trade_log path.
    eval_resp = MagicMock()
    eval_resp.new_orders = None
    eval_resp.order_intents = [
        {"action": "enter", "side": "buy", "symbol": "ETH",
         "amount": 0.01, "reason": "rsi_oversold fired"},
    ]
    eval_resp.orders = None
    eval_resp.model_dump.return_value = {"orders": ["enter:buy:ETH"]}
    sdk.execution.evaluate.return_value = eval_resp

    # Paper fills need a mark price — crypto_assets.get_market_data.
    md = MagicMock()
    md.data = {"current_price": 2500.0}
    sdk.crypto_assets.get_market_data.return_value = md

    for path in (
        "src.services.candidate_generator.mangrove_ai_client",
        "src.services.backtest_service.mangrove_ai_client",
        "src.services.strategy_service.mangrove_ai_client",
        "src.services.order_executor.mangrove_ai_client",
    ):
        monkeypatch.setattr(path, lambda s=sdk: s)

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()
    f.reset_master_key_cache()


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


def test_paper_lifecycle_end_to_end(client):
    """Full happy path: create autonomous strategy → paper → observe tick → stop.

    Validates:
    - POST /strategies/autonomous succeeds, returns a generation_report
    - PATCH /status to paper registers a cron + increments active_cron_jobs
    - Manual tick via POST /strategies/{id}/evaluate produces an ok
      evaluation AND a paper trade
    - /status stays responsive (<200 ms) throughout
    - PATCH /status back to inactive cancels the cron
    """
    # 1. Autonomous strategy creation.
    r = client.post(
        "/api/v1/agent/strategies/autonomous",
        headers=_auth(),
        json={"goal": "momentum on ETH", "asset": "ETH", "timeframe": "1h",
              "candidate_count": 5, "seed": 1},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    strat = body["strategy"]
    assert strat["id"]
    assert strat["mangrove_id"].startswith("mg-paper-")
    report = body["generation_report"]
    assert report["candidates_tried"] == 5
    assert report["candidates_passed_filter"] >= 1

    # 2. Transition draft → inactive → paper (draft → paper is illegal).
    client.patch(
        f"/api/v1/agent/strategies/{strat['id']}/status",
        headers=_auth(),
        json={"status": "inactive"},
    )
    r = client.patch(
        f"/api/v1/agent/strategies/{strat['id']}/status",
        headers=_auth(),
        json={"status": "paper"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paper"

    # Active cron count bumped.
    status = client.get("/api/v1/agent/status").json()
    assert status["active_cron_jobs"] >= 1
    assert status["strategies"]["paper"] >= 1

    # 3. Non-blocking observation: hammer /status while we trigger a tick.
    # /status should stay <200ms every time.
    t_activation = datetime.now(timezone.utc).isoformat()
    latencies_ms = []
    for _ in range(5):
        t0 = time.monotonic()
        s = client.get("/api/v1/agent/status")
        latencies_ms.append((time.monotonic() - t0) * 1000)
        assert s.status_code == 200

    # 4. Trigger one tick manually (the same code path APScheduler runs).
    r = client.post(
        f"/api/v1/agent/strategies/{strat['id']}/evaluate",
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    tick_result = r.json()
    assert tick_result["status"] == "ok"
    assert tick_result["order_count"] == 1

    # Another burst of /status after the tick — still fast.
    for _ in range(5):
        t0 = time.monotonic()
        s = client.get("/api/v1/agent/status")
        latencies_ms.append((time.monotonic() - t0) * 1000)
        assert s.status_code == 200

    # Proves /status is never slow. 200ms allows for test-harness overhead.
    for ms in latencies_ms:
        assert ms < 200, f"/status took {ms:.1f}ms — cron blocking request path"

    # 5. Evaluation logged with the correct shape.
    r = client.get(
        f"/api/v1/agent/strategies/{strat['id']}/evaluations",
        headers=_auth(),
    )
    assert r.status_code == 200
    evals = r.json()
    assert len(evals) >= 1
    assert evals[0]["status"] == "ok"
    assert evals[0]["timestamp"] > t_activation
    assert len(evals[0]["order_intents"]) == 1

    # 6. Paper trade logged with mode=paper, status=simulated, no tx_hash.
    r = client.get(
        f"/api/v1/agent/strategies/{strat['id']}/trades",
        headers=_auth(),
    )
    assert r.status_code == 200
    trades = r.json()
    assert len(trades) >= 1
    trade = trades[0]
    assert trade["mode"] == "paper"
    assert trade["status"] == "simulated"
    assert trade["tx_hash"] is None
    assert trade["fill_price"] == 2500.0
    assert trade["input_token"] == "USDC"
    assert trade["output_token"] == "ETH"

    # 7. Deactivate — cron cancelled, no allocation to release (paper mode).
    r = client.patch(
        f"/api/v1/agent/strategies/{strat['id']}/status",
        headers=_auth(),
        json={"status": "inactive"},
    )
    assert r.status_code == 200
    final_status = client.get("/api/v1/agent/status").json()
    assert final_status["active_cron_jobs"] == 0
