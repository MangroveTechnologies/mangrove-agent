from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.robinhood_preflight import PreflightError, check_strategy


def _status():
    return {
        "connected": True,
        "account": "****1234",
        "account_state": "active",
        "crypto_linked": True,
        "trade_approvals": True,
        "buying_power": "200.00",
        "crypto_buying_power": "200.00",
        "tool_count": 12,
    }


def _strategy(**overrides):
    return {
        "id": "strategy-123",
        "status": "inactive",
        "execution_venue": {
            "venue": "robinhood",
            "asset_class": "crypto",
            "capital": "200",
            "max_order_usd": "25",
            "slippage_pct": "0.0025",
        },
        "market_context": {"base_token": "XLM", "quote_token": "USD"},
        **overrides,
    }


def test_preflight_uses_correct_read_only_agent_routes_and_redacts_account():
    responses = iter([_status(), _strategy()])
    calls = []

    def request(origin, api_key, path):
        calls.append((origin, api_key, path))
        return next(responses)

    result = check_strategy("strategy-123", "http://localhost:9080", "local-key", request)

    assert calls == [
        ("http://localhost:9080", "local-key", "/api/v1/agent/brokers/robinhood/status"),
        ("http://localhost:9080", "local-key", "/api/v1/agent/strategies/strategy-123"),
    ]
    assert result["ready_for_human_review"] is True
    assert result["state_changed"] is False
    assert result["evaluation_run"] is False
    assert result["order_submitted"] is False
    assert result["robinhood"]["account"] == "****1234"
    assert "account_number" not in str(result)


@pytest.mark.parametrize(
    "status,strategy,error",
    [
        ({"connected": False}, _strategy(), "connected account"),
        (_status(), _strategy(status="live"), "must be inactive"),
        (_status(), _strategy(execution_venue=None), "not configured for Robinhood"),
    ],
)
def test_preflight_fails_closed(status, strategy, error):
    responses = iter([status, strategy])
    with pytest.raises(PreflightError, match=error):
        check_strategy("strategy-123", "http://127.0.0.1:9080", "key", lambda *args: next(responses))


@pytest.mark.parametrize("url", ["https://localhost:9080", "http://example.com", "http://localhost/path"])
def test_preflight_rejects_non_local_or_non_origin_url(url):
    with pytest.raises(PreflightError, match="loopback origin"):
        check_strategy("strategy-123", url, "key", lambda *args: pytest.fail("must not request"))
