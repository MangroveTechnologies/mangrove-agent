"""Route/tick-level regression tests for the 2026-09 dogfood findings.

Reuses the SDK-mocked fixtures from test_strategy_service / test_strategy_routes.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

from tests.integration.test_strategy_routes import _auth, client  # noqa: E402,F401
from tests.integration.test_strategy_service import mock_ai_sdk, temp_db  # noqa: E402,F401

# -- Finding 7: market_snapshot ---------------------------------------------


def _paper(create_manual, update_status, StrategyManualRequest, StrategyStatusUpdate):
    s = create_manual(StrategyManualRequest(
        name="snap", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    update_status(s.id, StrategyStatusUpdate(status="paper"))
    return s


def test_tick_populates_market_snapshot_from_engine_response(temp_db, mock_ai_sdk):  # noqa: F811
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        tick,
        update_status,
    )
    from src.services.trade_log import list_evaluations

    resp = MagicMock()
    resp.new_orders = None
    resp.order_intents = []
    resp.orders = None
    resp.model_dump.return_value = {
        "success": True, "asset": "ETH", "current_price": 2508.96,
        "timestamp": "2026-09-13T22:44:51.513580+00:00", "new_orders": [],
    }
    mock_ai_sdk.execution.evaluate.return_value = resp

    s = _paper(create_manual, update_status, StrategyManualRequest, StrategyStatusUpdate)
    tick(s.id)

    ev = list_evaluations(s.id)[0]
    assert ev.market_snapshot == {
        "price": 2508.96,
        "timestamp": "2026-09-13T22:44:51.513580+00:00",
        "asset": "ETH",
        "source": "mangroveai.execution.evaluate",
    }


def test_market_snapshot_empty_when_engine_reports_nothing(temp_db, mock_ai_sdk):  # noqa: F811
    """No price/timestamp in the response → {} (never invented)."""
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        tick,
        update_status,
    )
    from src.services.trade_log import list_evaluations

    s = _paper(create_manual, update_status, StrategyManualRequest, StrategyStatusUpdate)
    tick(s.id)  # fixture evaluate response dumps {"orders": []}
    assert list_evaluations(s.id)[0].market_snapshot == {}


# -- Finding 6: status model -------------------------------------------------


def test_manual_create_is_inactive_and_promotes_straight_to_paper(temp_db, mock_ai_sdk):  # noqa: F811
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        update_status,
    )

    s = create_manual(StrategyManualRequest(
        name="st", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    sent = mock_ai_sdk.strategies.create.call_args[0][0]
    assert sent.status == "inactive"
    assert s.status == "inactive"
    assert update_status(s.id, StrategyStatusUpdate(status="paper")).status == "paper"


# -- Findings 5 + 3 via REST ---------------------------------------------------


def test_backtest_route_returns_verdict(client):  # noqa: F811
    r = client.post("/api/v1/agent/strategies/manual", headers=_auth(), json={
        "name": "v", "asset": "ETH", "timeframe": "1h",
        "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h", "params": {}}],
    })
    sid = r.json()["id"]
    body = client.post(f"/api/v1/agent/strategies/{sid}/backtest", headers=_auth(),
                       json={"mode": "full", "lookback_months": 6}).json()
    v = body["verdict"]
    assert v["verdict"] in {"PASS", "MARGINAL", "FAIL", "INSUFFICIENT_TRADES"}
    assert v["total_checks"] == 6 and len(v["checks"]) == 6
    wr = next(c for c in v["checks"] if c["metric"] == "win_rate")
    assert wr["raw"] == 60.0 and wr["actual"] == 0.6 and wr["passed"] is True
    assert body["verdict_note"] is None


def test_backtest_route_quick_mode_has_no_verdict(client):  # noqa: F811
    sid = client.post("/api/v1/agent/strategies/manual", headers=_auth(), json={
        "name": "q", "asset": "ETH", "timeframe": "1h",
        "entry": [{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h", "params": {}}],
    }).json()["id"]
    body = client.post(f"/api/v1/agent/strategies/{sid}/backtest", headers=_auth(),
                       json={"mode": "quick"}).json()
    assert body["verdict"] is None
    assert "quick mode" in body["verdict_note"]


def test_autonomous_report_carries_verdict(client):  # noqa: F811
    r = client.post("/api/v1/agent/strategies/autonomous", headers=_auth(),
                    json={"goal": "momentum on ETH", "asset": "ETH", "timeframe": "1h",
                          "candidate_count": 5, "seed": 1})
    assert r.status_code == 201
    assert r.json()["generation_report"]["verdict"]["verdict"] in {"PASS", "MARGINAL", "FAIL", "INSUFFICIENT_TRADES"}


# -- Findings 1 + 4 via REST -------------------------------------------------


def test_search_route_strict_and_match_fields(client):  # noqa: F811
    loose = client.get("/api/v1/agent/reference-strategies/search",
                       params={"asset": "ETH", "timeframe": "1h", "limit": 10}, headers=_auth()).json()
    assert loose["count"] == 10 and loose["strict"] is False
    assert any(s["match"] != "exact" for s in loose["strategies"])
    strict = client.get("/api/v1/agent/reference-strategies/search",
                        params={"asset": "ETH", "timeframe": "1h", "limit": 10, "strict": "true"},
                        headers=_auth()).json()
    assert strict["count"] == strict["exact_match_count"]
    assert {s["timeframe"] for s in strict["strategies"]} == {"1h"}


def test_build_response_is_marked_unpersisted_and_posts_as_is(client):  # noqa: F811
    built = client.post("/api/v1/agent/reference-strategies/ref-004/build", headers=_auth(),
                        json={"asset": "ETH", "timeframe": "1h"}).json()
    assert built["persisted"] is False
    assert built["next_step"]["rest"] == "POST /api/v1/agent/strategies/manual"
    before = client.get("/api/v1/agent/strategies", headers=_auth()).json()
    created = client.post("/api/v1/agent/strategies/manual", headers=_auth(), json=built)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "inactive"
    after = client.get("/api/v1/agent/strategies", headers=_auth()).json()
    assert len(after) == len(before) + 1
