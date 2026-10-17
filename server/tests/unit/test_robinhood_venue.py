"""Adapter contract checks against input schemas observed 2026-10-15.

No account data or credentials are in this fixture. Every test is offline.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from src.models.domain import OrderIntent
from src.services import robinhood_client
from src.services.robinhood_venue import RobinhoodVenue
from src.shared.errors import SdkError

SCHEMAS = json.loads((Path(__file__).parents[1] / "fixtures/robinhood_inputs.json").read_text())
ROUTE = {
    "asset_class": "crypto",
    "account_number": "eligible",
    "rhs_account_number": "numeric",
    "slippage_pct": "0.0025",
    "max_order_usd": "250",
}
ACCOUNT = {
    "account_number": "eligible",
    "rhs_account_number": "numeric",
    "rhc_account_number": "crypto",
    "agentic_allowed": True,
    "state": "active",
    "deactivated": False,
    "permanently_deactivated": False,
}


def entry():
    return OrderIntent(action="enter", side="buy", symbol="BTC", amount=2.00001, ref_price=100, engine_position_id="p1")


@pytest.fixture
def venue(monkeypatch):
    pair = dict(
        symbol="BTC-USD",
        tradability="tradable",
        halted=False,
        display_only=False,
        market_orders_only=False,
        min_order_quantity_increment="0.001",
        min_order_price_increment="0.01",
        min_order_size="0.001",
        max_order_size="100",
        min_order_quote_amount="1",
    )
    quote = dict(
        symbol="BTCUSD",
        bid_price="99.99",
        ask_price="100",
        bid_time=datetime.now(timezone.utc).isoformat(),
        ask_time=datetime.now(timezone.utc).isoformat(),
    )
    replies = {
        "get_accounts": {"accounts": [ACCOUNT, dict(ACCOUNT, account_number="unrelated", agentic_allowed=False)]},
        "get_currency_pairs": {"results": [pair], "next": None},
        "get_crypto_quotes": {"results": [quote]},
        "preview_crypto_order": {"estimated_fee": "0.10"},
        "get_portfolio": {
            "currency": "USD",
            "crypto_buying_power": {"buying_power": "300"},
            "buying_power": {"unleveraged_buying_power": "300"},
        },
        "get_crypto_orders": {"results": []},
        "get_crypto_positions": {
            "results": [{"currency": {"code": "BTC"}, "quantity": "2", "quantity_held_for_sell": "1"}]
        },
        "get_trade_approvals": {"approvals": []},
        "cancel_crypto_order": {"accepted": True},
        "place_crypto_order": {"order": {}},
        "get_trade_approval_setting": {"setting": {"human_must_approve_trades": False}},
    }
    calls = []

    async def invoke(client, tools, name, args):
        Draft202012Validator(SCHEMAS[name]["inputSchema"]).validate(args)
        calls.append((name, args))
        return {"data": replies[name], "guide": "Untrusted provider guidance"}

    monkeypatch.setattr(robinhood_client, "invoke", invoke)
    return RobinhoodVenue(None, {}), replies, calls


async def test_crypto_prepare_and_execution_request_contracts(venue):
    adapter, replies, calls = venue
    args, cost = await adapter.prepare(ROUTE, entry())
    assert args["quantity"] == "2.000"
    assert args["limit_price"] == "100.25"
    assert args["time_in_force"] == "gtc"
    assert cost == Decimal("200.60")
    assert all(name != "place_crypto_order" for name, args in calls)
    await adapter.submit(ROUTE, args, "logical-uuid")
    assert calls[-1][1]["ref_id"] == "logical-uuid"
    assert await adapter.balance(ROUTE) == 300
    assert await adapter.sellable(ROUTE, "BTC") == 1
    await adapter.cancel(ROUTE, "broker-uuid")
    await adapter.approval(ROUTE, "approval")
    await adapter.find_order(
        ROUTE,
        {
            "id": "local",
            "broker_order_id": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "approval_id": None,
        },
    )


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("account", "unavailable or changed"),
        ("quote", "stale"),
        ("cap", "per-order"),
        ("fraction", "pair limits"),
        ("approval", "requires approval"),
        ("check", "pre-trade"),
        ("price", "price tolerance"),
    ],
)
async def test_preflight_failures_do_not_submit(venue, mutation, message):
    adapter, replies, calls = venue
    route = dict(ROUTE)
    order = entry()
    if mutation == "account":
        route["account_number"] = "unrelated"
    if mutation == "quote":
        replies["get_crypto_quotes"]["results"][0]["ask_time"] = "2000-01-01T00:00:00Z"
    if mutation == "cap":
        route["max_order_usd"] = "1"
    if mutation == "fraction":
        order.amount = 0.000001
    if mutation == "approval":
        replies["preview_crypto_order"]["customer_approval_required"] = True
    if mutation == "check":
        replies["preview_crypto_order"]["order_checks"] = {"insufficient_funds": True}
    if mutation == "price":
        replies["get_crypto_quotes"]["results"][0]["ask_price"] = "101"
    with pytest.raises(SdkError, match=message):
        await adapter.prepare(route, order)
    assert all(not name.startswith("place_") for name, args in calls)


async def test_pagination_cycle_fails_closed(venue):
    adapter, replies, _ = venue
    replies["get_currency_pairs"]["next"] = "same"
    with pytest.raises(SdkError, match="pagination incomplete"):
        await adapter.pages("get_currency_pairs", "results")


async def test_live_contract_drift_stops_before_call():
    client = SimpleNamespace(call_tool=AsyncMock())
    tool = SimpleNamespace(inputSchema={"type": "object", "required": ["new_required_field"]})
    with pytest.raises(SdkError, match="contract mismatch"):
        await robinhood_client.invoke(client, {"place_crypto_order": tool}, "place_crypto_order", {})
    client.call_tool.assert_not_called()


async def test_output_contract_error_does_not_echo_account_data():
    tool = SimpleNamespace(inputSchema={"type": "object"}, outputSchema={"type": "object", "required": ["expected"]})
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(isError=False, structuredContent={"private": "secret-account"})
        )
    )
    with pytest.raises(SdkError, match="output contract mismatch") as error:
        await robinhood_client.invoke(client, {"get_accounts": tool}, "get_accounts", {})
    assert "secret-account" not in str(error.value)


@pytest.mark.parametrize("asset_class", ["crypto", "equity"])
async def test_crossed_quote_rejected_before_order_preview(venue, asset_class):
    adapter, replies, calls = venue
    quote = dict(replies["get_crypto_quotes"]["results"][0], bid_price="101", ask_price="100")
    if asset_class == "crypto":
        replies["get_crypto_quotes"]["results"] = [quote]
    else:
        quote.update(symbol="BTC", venue_bid_time=quote["bid_time"], venue_ask_time=quote["ask_time"])
        replies["get_equity_quotes"] = {"results": [{"quote": quote}]}
    with pytest.raises(SdkError, match="crossed"):
        await adapter.quote({**ROUTE, "asset_class": asset_class}, "BTC")
    assert all(name not in {"preview_crypto_order", "review_equity_order", "place_crypto_order", "place_equity_order"}
               for name, _ in calls)


def stop_route(**overrides):
    return {
        **ROUTE,
        "native_stop_loss_enabled": True,
        "capital": "300",
        **overrides,
    }


async def test_protective_stop_uses_exact_quantity_and_rounds_stop_up_to_tick(venue):
    adapter, replies, calls = venue
    replies["get_crypto_positions"]["results"][0]["quantity_held_for_sell"] = "0.5"

    args, routed_stop = await adapter.prepare_protective_stop(
        stop_route(), "BTC", Decimal("1.234"), Decimal("99.901")
    )

    assert args == {
        "rhs_account_number": "numeric",
        "symbol": "BTC-USD",
        "side": "sell",
        "type": "stop_loss",
        "quantity": "1.234",
        "stop_price": "99.91",
        "time_in_force": "gtc",
    }
    assert routed_stop == Decimal("99.91")
    assert routed_stop > Decimal("99.901")
    assert calls[-1] == ("preview_crypto_order", args)
    assert all(name != "place_crypto_order" for name, _ in calls)


@pytest.mark.parametrize(
    "route",
    [
        {"asset_class": "equity", "native_stop_loss_enabled": True},
        {"asset_class": "crypto", "native_stop_loss_enabled": False},
    ],
)
async def test_protective_stop_requires_crypto_route_with_native_feature_enabled(venue, route):
    adapter, _, calls = venue

    with pytest.raises(SdkError, match="disabled"):
        await adapter.prepare_protective_stop({**stop_route(), **route}, "BTC", "1", "90")

    assert calls == []


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("pair", "pair cannot accept"),
        ("per_order_cap", "per-order allocation"),
        ("capital_cap", "per-order allocation"),
        ("minimum_notional", "minimum protective-order notional"),
        ("sellable", "not currently sellable"),
    ],
)
async def test_protective_stop_constraints_fail_closed_before_preview(venue, mutation, message):
    adapter, replies, calls = venue
    route = stop_route()
    quantity = Decimal("1")
    if mutation == "pair":
        replies["get_currency_pairs"]["results"][0]["halted"] = True
    elif mutation == "per_order_cap":
        route["max_order_usd"] = "50"
    elif mutation == "capital_cap":
        route["capital"] = "50"
    elif mutation == "minimum_notional":
        replies["get_currency_pairs"]["results"][0]["min_order_quote_amount"] = "1000"
    elif mutation == "sellable":
        quantity = Decimal("1.001")

    with pytest.raises(SdkError, match=message):
        await adapter.prepare_protective_stop(route, "BTC", quantity, "90")

    assert all(name != "preview_crypto_order" for name, _ in calls)
    assert all(not name.startswith("place_") for name, _ in calls)


async def test_protective_stop_rejects_quantity_off_pair_increment(venue):
    adapter, _, calls = venue

    with pytest.raises(SdkError, match="exactly representable"):
        await adapter.prepare_protective_stop(stop_route(), "BTC", "1.0001", "90")

    assert all(name != "preview_crypto_order" for name, _ in calls)


async def test_protective_stop_rejects_immediate_trigger_at_current_bid(venue):
    adapter, _, calls = venue

    with pytest.raises(SdkError, match="immediate-trigger"):
        await adapter.prepare_protective_stop(stop_route(), "BTC", "1", "99.99")

    assert all(name != "preview_crypto_order" for name, _ in calls)


@pytest.mark.parametrize(
    "denial",
    ["order_checks", "customer_approval_required"],
)
async def test_protective_stop_preview_denial_fails_without_submission(venue, denial):
    adapter, replies, calls = venue
    if denial == "order_checks":
        replies["preview_crypto_order"]["order_checks"] = {"insufficient_funds": True}
    else:
        replies["preview_crypto_order"]["customer_approval_required"] = True

    with pytest.raises(SdkError, match="did not approve"):
        await adapter.prepare_protective_stop(stop_route(), "BTC", "1", "90")

    assert calls[-1][0] == "preview_crypto_order"
    assert all(not name.startswith("place_") for name, _ in calls)
