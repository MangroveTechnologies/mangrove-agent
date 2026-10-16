"""Mangrove-owned Robinhood execution venue tools, all locally authenticated."""

import json

import anyio
from pydantic import ValidationError

from src.mcp.registry import ToolEntry, ToolParam, register_tool
from src.services import broker_execution


def register(server):
    from src.mcp.tools import _APIKEY, _auth_error, _err, _handle_agent_error, _require

    async def call(api_key, operation, *args):
        if not _require(api_key):
            return _auth_error()
        try:
            result = await anyio.to_thread.run_sync(lambda: broker_execution.run(operation, *args))
            return json.dumps(result)
        except ValidationError:
            return _err("VALIDATION_ERROR", "Invalid execution venue configuration.")
        except Exception as error:
            return _handle_agent_error(error)

    @server.tool()
    async def agent_robinhood_status(api_key: str = "") -> str:
        """Read Robinhood venue readiness, masked account and available buying power. No trades."""
        return await call(api_key, broker_execution.inspect)

    @server.tool()
    async def agent_configure_execution_venue(strategy_id: str, allocation: dict, api_key: str = "") -> str:
        """Bind an inactive Mangrove strategy to Robinhood and a capital mandate.

        allocation: venue=robinhood, asset_class=equity|crypto, capital,
        max_order_usd, slippage_pct. Does not activate trading. Strategy creation,
        research, sizing and signals remain in Mangrove. Existing live activation
        authorizes execution within this allocation without per-order prompts.
        """
        return await call(api_key, broker_execution.configure, strategy_id, allocation)

    @server.tool()
    async def agent_configure_strategy_market(strategy_id: str, market_context: dict, api_key: str = "") -> str:
        """Set explicit market data for a fresh inactive stateless strategy. Does not activate or trade."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import StrategyMarketContext, configure_market
            result = await anyio.to_thread.run_sync(
                lambda: configure_market(strategy_id, StrategyMarketContext.model_validate(market_context))
            )
            return json.dumps(result.model_dump(mode="json"))
        except ValidationError:
            return _err("VALIDATION_ERROR", "Supply base_token, quote_token and market_data_venue in uppercase.")
        except Exception as error:
            return _handle_agent_error(error)

    @server.tool()
    async def agent_list_venue_orders(strategy_id: str, api_key: str = "") -> str:
        """Read local broker order states and confirmed quantities; account IDs are masked."""
        if not _require(api_key):
            return _auth_error()
        return json.dumps(broker_execution.journal(strategy_id))

    @server.tool()
    async def agent_reconcile_venue_orders(strategy_id: str, api_key: str = "") -> str:
        """Read broker outcomes and persist confirmed fills. Never submits or retries an order."""
        return await call(api_key, broker_execution.reconcile, strategy_id)

    @server.tool()
    async def agent_cancel_venue_order(order_id: str, api_key: str = "") -> str:
        """Request cancellation of a known local venue order and reconcile its outcome.

        Cancellation can race with a fill. Pending cancellation is not a final
        canceled state. Pausing a strategy alone does not cancel broker orders.
        """
        return await call(api_key, broker_execution.cancel, order_id)

    strategy = ToolParam(name="strategy_id", type="string", required=True, description="Local Mangrove strategy UUID")
    definitions = [
        (
            "agent_configure_strategy_market",
            "Set market data for a fresh inactive stateless strategy without activating it.",
            [strategy, ToolParam(name="market_context", type="object", required=True,
                                description="Explicit base_token, quote_token and market_data_venue")],
        ),
        ("agent_robinhood_status", "Robinhood execution venue readiness.", []),
        (
            "agent_configure_execution_venue",
            "Bind an inactive strategy to a funded execution mandate.",
            [
                strategy,
                ToolParam(
                    name="allocation",
                    type="object",
                    required=True,
                    description="Venue, asset class, capital, per-order maximum, slippage",
                ),
            ],
        ),
        ("agent_list_venue_orders", "Local broker order journal.", [strategy]),
        ("agent_reconcile_venue_orders", "Reconcile broker orders without submission.", [strategy]),
        (
            "agent_cancel_venue_order",
            "Cancel a known pending venue order.",
            [ToolParam(name="order_id", type="string", required=True, description="Local broker order UUID")],
        ),
    ]
    for name, description, parameters in definitions:
        register_tool(ToolEntry(name=name, description=description, access="auth", parameters=[*parameters, _APIKEY]))
