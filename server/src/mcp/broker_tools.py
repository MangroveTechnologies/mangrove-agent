"""Mangrove-owned Robinhood execution venue tools, all locally authenticated."""

import json

import anyio
from mcp.types import ToolAnnotations
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

    @server.tool(
        title="Robinhood venue status",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def agent_robinhood_status(api_key: str = "") -> str:
        """Read Robinhood venue readiness, masked account and available buying power. No trades."""
        return await call(api_key, broker_execution.inspect)

    async def deployment_call(api_key, operation, *args):
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(await anyio.to_thread.run_sync(lambda: operation(*args)))
        except (ValueError, ValidationError):
            return _err("VALIDATION_ERROR", "Invalid deployment arguments.")
        except Exception as error:
            return _handle_agent_error(error)

    @server.tool(
        title="Adopt saved strategy",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
    )
    async def agent_adopt_strategy(server_strategy_id: str, asset_class: str, rebind: bool = False, api_key: str = "") -> str:
        """Prepare an owned saved MangroveAI strategy for local Robinhood execution.

        Reads the server via configured API-key/x402 access (normal charges apply).
        Never activates or trades. Returns a local deployment_id for configuration.
        rebind explicitly accepts revised rules only while inactive without execution
        history, and clears the previous allocation for review. No local authoring.
        """
        from src.services import local_deployment
        return await deployment_call(api_key, local_deployment.adopt, server_strategy_id, asset_class, rebind)

    @server.tool(
        title="Inspect local deployment",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def agent_get_deployment(deployment_id: str, api_key: str = "") -> str:
        """Read the local binding, fingerprint, configuration and pending-order count."""
        from src.services import local_deployment
        return await deployment_call(api_key, local_deployment.inspect, deployment_id)

    @server.tool(
        title="Activate local deployment",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
    )
    async def agent_activate_deployment(deployment_id: str, confirm: bool = False, api_key: str = "") -> str:
        """Start the existing local scheduler for a configured Robinhood deployment.

        Requires explicit user authorization (confirm=true) for live trading and
        recurring evaluation/validation charges. Rechecks the saved revision and
        its passing backtest. Does not change Robinhood approval preferences.
        """
        from src.services import local_deployment
        return await deployment_call(api_key, local_deployment.activate, deployment_id, confirm)

    @server.tool(
        title="Stop deployment entries",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
    )
    async def agent_stop_deployment(deployment_id: str, confirm: bool = False, api_key: str = "") -> str:
        """Stop new entries with explicit authorization. Exit-only monitoring remains
        while exposure or unresolved orders remain. Pending broker orders are NOT
        canceled; the existing reconciliation worker continues tracking them.
        """
        from src.services import local_deployment
        return await deployment_call(api_key, local_deployment.stop, deployment_id, confirm)

    @server.tool(
        title="Configure strategy execution venue",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_configure_execution_venue(strategy_id: str, allocation: dict, api_key: str = "") -> str:
        """Bind an inactive Mangrove strategy to Robinhood and a capital mandate.

        allocation: venue=robinhood, asset_class=equity|crypto, capital,
        max_order_usd, slippage_pct. Does not activate trading. Strategy creation,
        research, sizing and signals remain in Mangrove. Existing live activation
        authorizes execution within this allocation without per-order prompts.
        """
        return await call(api_key, broker_execution.configure, strategy_id, allocation)

    @server.tool(
        title="Configure strategy market",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
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

    @server.tool(
        title="List local venue orders",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def agent_list_venue_orders(strategy_id: str, api_key: str = "") -> str:
        """Read local broker order states and confirmed quantities; account IDs are masked."""
        if not _require(api_key):
            return _auth_error()
        return json.dumps(broker_execution.journal(strategy_id))

    @server.tool(
        title="Reconcile venue order outcomes",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_reconcile_venue_orders(strategy_id: str, api_key: str = "") -> str:
        """Read broker outcomes and persist confirmed fills. Never submits or retries an order."""
        return await call(api_key, broker_execution.reconcile, strategy_id)

    @server.tool(
        title="Cancel venue order",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_cancel_venue_order(order_id: str, api_key: str = "") -> str:
        """Request cancellation of a known local venue order and reconcile its outcome.

        Cancellation can race with a fill. Pending cancellation is not a final
        canceled state. Pausing a strategy alone does not cancel broker orders.
        """
        return await call(api_key, broker_execution.cancel, order_id)

    strategy = ToolParam(name="strategy_id", type="string", required=True, description="Local deployment UUID returned by agent_adopt_strategy, not the server strategy ID")
    deployment = ToolParam(name="deployment_id", type="string", required=True, description="Local deployment UUID")
    confirmation = ToolParam(name="confirm", type="boolean", required=False, description="True only after explicit user authorization")
    definitions = [
        ("agent_adopt_strategy", "Prepare a saved server strategy for local Robinhood execution without starting trading.", [
            ToolParam(name="server_strategy_id", type="string", required=True, description="Owned saved MangroveAI strategy UUID"),
            ToolParam(name="asset_class", type="string", required=True, description="crypto or equity"),
            ToolParam(name="rebind", type="boolean", required=False, description="Explicitly accept a changed server revision; clears allocation"),
        ]),
        ("agent_get_deployment", "Inspect a local server-strategy deployment.", [deployment]),
        ("agent_activate_deployment", "Authorize live Robinhood execution using the existing local scheduler.", [deployment, confirmation]),
        ("agent_stop_deployment", "Stop new local work without claiming pending orders were canceled.", [deployment, confirmation]),
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
