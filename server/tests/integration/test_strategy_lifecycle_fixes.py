"""Route/tick-level regression tests for the 2026-09 strategy lifecycle fixes.

Reuses the SDK-mocked fixtures from test_strategy_service / test_strategy_routes.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

from tests.existing_strategies import StrategyManualRequest
from tests.existing_strategies import existing_strategy as create_manual

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
        StrategyStatusUpdate,
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
        StrategyStatusUpdate,
        tick,
        update_status,
    )
    from src.services.trade_log import list_evaluations

    s = _paper(create_manual, update_status, StrategyManualRequest, StrategyStatusUpdate)
    tick(s.id)  # fixture evaluate response dumps {"orders": []}
    assert list_evaluations(s.id)[0].market_snapshot == {}


# -- Finding 6: status model -------------------------------------------------


def test_existing_inactive_record_can_promote_to_paper(temp_db, mock_ai_sdk):  # noqa: F811
    from src.services.strategy_service import (
        StrategyStatusUpdate,
        update_status,
    )

    s = create_manual(StrategyManualRequest(
        name="st", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    assert s.status == "inactive"
    assert update_status(s.id, StrategyStatusUpdate(status="paper")).status == "paper"


# -- Findings 5 + 3 via REST ---------------------------------------------------


def test_retired_authoring_routes_do_not_create_records(client):  # noqa: F811
    before = client.get("/api/v1/agent/strategies", headers=_auth()).json()
    for method, path, payload in (
        ("post", "/api/v1/agent/strategies/autonomous", {"goal": "momentum", "asset": "ETH", "timeframe": "1h"}),
        ("get", "/api/v1/agent/reference-strategies/search", None),
        ("post", "/api/v1/agent/reference-strategies/ref-004/build", {}),
    ):
        result = client.request(method, path, headers=_auth(), json=payload)
        assert result.status_code == 410
    assert client.get("/api/v1/agent/strategies", headers=_auth()).json() == before
