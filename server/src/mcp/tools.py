"""MCP tool definitions for the mangrove-agent.

Every tool mirrors a REST route by calling the same service function.
Zero duplicated business logic — the MCP layer is just a different
interface over the same code.

Auth: tools accept an `api_key` parameter; `has_valid_api_key` validates
against config. Returns the spec-shaped `AgentError` JSON on failure.
Discovery tools (`status`, `list_tools`) bypass auth.

Naming: plain verb_resource form (no project prefix). The MCP server
namespace is enough. See docs/specification.md MCP Tools table.
"""
from __future__ import annotations

import json
from typing import Any

from httpx import HTTPStatusError
from mangrove_ai.exceptions import APIError as AIAPIError
from mangrove_markets.exceptions import APIError as MarketsAPIError
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from src.mcp.registry import ToolEntry, ToolParam, clear_tools, register_tool
from src.shared.auth.middleware import get_request_api_key, has_valid_api_key
from src.shared.errors import AgentError, upstream_access_error
from src.shared.logging import get_logger

_log = get_logger(__name__)



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err(code: str, message: str, suggestion: str | None = None, status: int = 400) -> str:
    return json.dumps({
        "error": True,
        "code": code,
        "message": message,
        "suggestion": suggestion,
        "correlation_id": None,
    })


def _auth_error() -> str:
    return _err(
        "AUTH_INVALID_API_KEY",
        "API key required or invalid.",
        "Pass a valid api_key parameter matching the configured API_KEYS.",
        status=401,
    )


def _handle_agent_error(e: Exception) -> str:
    return _handle_upstream_error("SDK_ERROR", e)


def _handle_upstream_error(code: str, error: Exception) -> str:
    """Preserve agent errors while withholding untrusted SDK error text."""
    access_error = upstream_access_error(error)
    if access_error is not None:
        return json.dumps(access_error.to_dict())
    if isinstance(error, AgentError):
        return json.dumps(error.to_dict())
    return _err(code, "The upstream service request failed.")


def _require(api_key: str) -> bool:
    """Return True if the call is authenticated, False otherwise.

    Accepts the key either as the explicit `api_key` tool parameter OR via the
    request's `X-API-Key` HTTP header. Claude Code registers this server with
    the key as a header (`claude mcp add --header "X-API-Key: <key>"`), which
    FastMCP tools never receive as a param — src/app.py bridges that header into
    a ContextVar that we consult here. The explicit param wins when supplied.
    """
    return has_valid_api_key(api_key or get_request_api_key())


# Authentication is required, but the argument is optional when supplied by header.
_APIKEY = ToolParam(name="api_key", type="string", required=False,
                    description="Local agent API key; omit when supplied in the X-API-Key header.")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(server: FastMCP):
    """Register all agent MCP tools + the x402 demo tool."""
    clear_tools()
    _register_discovery(server)
    _register_wallet(server)
    from src.mcp.marketplace_tools import register_marketplace
    register_marketplace(server)
    _register_dex(server)
    from src.mcp.broker_tools import register as register_brokers
    register_brokers(server)
    from src.mcp.paper_tools import register as register_paper
    register_paper(server)
    _register_x402_spend(server)
    _register_hello_mangrove(server)


# ---------------------------------------------------------------------------
# Discovery (free reads, authenticated naming)
# ---------------------------------------------------------------------------


def _register_discovery(server: FastMCP) -> None:
    @server.tool()
    async def status() -> str:
        """Return agent status: user-chosen name, version, wallets count, strategies by status,
        active cron jobs, db path, uptime. Free, no auth required."""
        from src.api.routes.discovery import status as route
        return json.dumps(await route())

    register_tool(ToolEntry(
        name="status",
        description="Agent name, local execution record counts and uptime. Strategy counts are local, not the account's server-saved strategies. Free, no auth.",
        access="free",
        parameters=[],
    ))

    @server.tool()
    async def set_agent_name(name: str, api_key: str = "") -> CallToolResult:
        """Save the name the user chose for this agent, shared across AI clients.
        Use only after the user chooses or requests a new name. Local, no cost.
        The current name is returned by status; null means no name chosen yet."""
        failed = False
        if not _require(api_key):
            response, failed = _auth_error(), True
        else:
            from src.services import agent_identity
            try:
                response = json.dumps(agent_identity.set_name(name))
            except AgentError as exc:
                response, failed = _handle_agent_error(exc), True
        return CallToolResult(content=[TextContent(type="text", text=response)], isError=failed)

    register_tool(ToolEntry(
        name="set_agent_name",
        description="Save the user-chosen agent name for all clients. Local, no cost; auth required.",
        access="auth",
        parameters=[ToolParam(name="name", type="string", required=True,
                              description="The user's chosen agent name, 1 to 64 printable characters."),
                    _APIKEY],
    ))

    @server.tool()
    async def list_tools() -> str:
        """List all registered MCP tools with their access tier, parameters,
        and pricing. Free, no auth."""
        from src.api.routes.discovery import tools as route
        return json.dumps(await route())

    register_tool(ToolEntry(
        name="list_tools",
        description="MCP tool catalog (name, tier, params, pricing). Free, no auth.",
        access="free",
        parameters=[],
    ))


# ---------------------------------------------------------------------------
# Wallet (auth)
# ---------------------------------------------------------------------------


def _register_wallet(server: FastMCP) -> None:
    @server.tool()
    async def create_wallet(
        chain: str = "evm", network: str = "mainnet",
        chain_id: int | None = 8453, label: str | None = None,
        api_key: str = "",
    ) -> str:
        """Create + encrypt a wallet locally.

        The plaintext secret is NEVER returned in this response — it would
        land in the Claude Code transcript and get sent to Anthropic. Instead
        the response carries a `vault_token` referencing an in-process vault.
        Tell the user to run the `reveal_cmd` in a terminal to back up the
        secret. The id is TTL-bound (default 300s) and single-read.
        EVM and XRPL wallets. Base mainnet (chain_id 8453) remains the default.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.wallet_manager import create_wallet as svc
            result = svc(chain=chain, network=network, chain_id=chain_id, label=label)
            return json.dumps(result.model_dump(mode="json"))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="create_wallet",
        description=(
            "Create + encrypt a wallet. Response carries only vault_token + "
            "reveal_cmd — plaintext never enters the Claude Code transcript. "
            "Supports EVM and XRPL family-seed wallets."
        ),
        access="auth",
        parameters=[
            ToolParam(name="chain", type="string", required=False, description="evm (default) or xrpl; chain_id is ignored for xrpl."),
            ToolParam(name="network", type="string", required=False, description="mainnet (default) | testnet"),
            ToolParam(name="chain_id", type="integer", required=False, description="Default 8453 (Base mainnet)"),
            ToolParam(name="label", type="string", required=False, description="Human-friendly name"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def import_wallet(
        vault_token: str,
        chain: str = "evm", network: str = "mainnet",
        chain_id: int | None = 8453, label: str | None = None,
        api_key: str = "",
    ) -> str:
        """Import an existing wallet whose secret has been stashed in the vault.

        The user's flow: run `./scripts/stash-secret.sh` in a terminal (it
        prompts for the private key via `read -s` so it isn't echoed, posts
        to /internal/stash-secret, prints the returned vault_token). Then
        tell the agent to import that id. The private key NEVER enters
        Claude Code's conversation context — this tool only handles the id.

        Supports EVM keys/mnemonics and XRPL family seeds.
        Do NOT accept a raw private key, mnemonic or seed as input to this tool,
        and do NOT suggest the user paste one. If a user pastes a key in
        chat, tell them to run stash-secret.sh instead and purge the key
        from their message.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.wallet_manager import import_wallet as svc
            result = svc(
                vault_token=vault_token,
                chain=chain, network=network,
                chain_id=chain_id, label=label,
            )
            return json.dumps(result.model_dump(mode="json"))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="import_wallet",
        description=(
            "Import an existing wallet from a stashed vault_token. The user "
            "must obtain the id by running scripts/stash-secret.sh in a "
            "terminal FIRST — this tool refuses raw keys by design."
        ),
        access="auth",
        parameters=[
            ToolParam(name="vault_token", type="string", required=True, description="From scripts/stash-secret.sh output"),
            ToolParam(name="chain", type="string", required=False, description="evm (default) or xrpl"),
            ToolParam(name="network", type="string", required=False, description="mainnet (default) | testnet"),
            ToolParam(name="chain_id", type="integer", required=False, description="Default 8453 (Base mainnet)"),
            ToolParam(name="label", type="string", required=False, description="Human-friendly name"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def list_wallets(api_key: str = "") -> str:
        """List stored wallets (addresses + metadata only)."""
        if not _require(api_key):
            return _auth_error()
        from src.services.wallet_manager import list_wallets as svc
        return json.dumps([w.model_dump(mode="json") for w in svc()])

    register_tool(ToolEntry(
        name="list_wallets",
        description="List stored wallets (secrets never returned).",
        access="auth",
        parameters=[_APIKEY],
    ))


def _register_dex(server: FastMCP) -> None:
    def _cex_err(e: Exception) -> str:
        return _handle_upstream_error("CEX_ERROR", e)

    @server.tool()
    async def cex_status(api_key: str = "") -> str:
        """Is a Kraken (CEX) account connected on this machine? Free of Kraken key."""
        if not _require(api_key):
            return _auth_error()
        from src.services import cex_service
        return json.dumps(cex_service.status())

    register_tool(ToolEntry(
        name="cex_status", description="Whether a Kraken account is connected (BYOK).",
        access="auth", parameters=[_APIKEY],
    ))

    @server.tool()
    async def cex_connect_kraken(vault_token: str, api_key: str = "") -> str:
        """Connect Kraken by consuming a vault_token from scripts/stash-kraken-secret.sh.
        The key is persisted ENCRYPTED at rest; it never enters this chat."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services import cex_service
            return json.dumps(cex_service.connect_from_vault(vault_token))
        except Exception as e:  # noqa: BLE001
            return _cex_err(e)

    register_tool(ToolEntry(
        name="cex_connect_kraken",
        description="Connect Kraken via a vault_token (creds stashed out-of-band, stored encrypted).",
        access="auth",
        parameters=[
            ToolParam(name="vault_token", type="string", required=True, description="From scripts/stash-kraken-secret.sh"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def cex_balances(api_key: str = "") -> str:
        """Kraken balances (BYOK — talks to Kraken directly with the local key)."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services import cex_service
            return json.dumps({"balances": cex_service.get_balances()})
        except Exception as e:  # noqa: BLE001
            return _cex_err(e)

    register_tool(ToolEntry(
        name="cex_balances", description="Kraken balances (BYOK).",
        access="auth", parameters=[_APIKEY],
    ))

    @server.tool()
    async def cex_validate_order(
        pair: str, side: str, volume: float,
        ordertype: str = "market", price: float | None = None, api_key: str = "",
    ) -> str:
        """Dry-run a Kraken order (validate=true) — no fill. Use before any live order."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services import cex_service
            return json.dumps(cex_service.validate_order(
                pair=pair, side=side, volume=volume, ordertype=ordertype, price=price,
            ))
        except Exception as e:  # noqa: BLE001
            return _cex_err(e)

    register_tool(ToolEntry(
        name="cex_validate_order",
        description="Dry-run a Kraken order (validate=true, no fill).",
        access="auth",
        parameters=[
            ToolParam(name="pair", type="string", required=True, description="Kraken pair, e.g. XBTUSD"),
            ToolParam(name="side", type="string", required=True, description="buy | sell"),
            ToolParam(name="volume", type="number", required=True, description="Order volume in base units"),
            ToolParam(name="ordertype", type="string", required=False, description="market (default) | limit"),
            ToolParam(name="price", type="number", required=False, description="Limit price (for limit orders)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def cex_sync_fills(mode: str = "live", api_key: str = "") -> str:
        """Pull the user's Kraken fills and emit them to telemetry (authed by the
        Mangrove key). The Kraken key never leaves this machine."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services import cex_service
            return json.dumps(cex_service.sync_fills(mode=mode))
        except Exception as e:  # noqa: BLE001
            return _cex_err(e)

    register_tool(ToolEntry(
        name="cex_sync_fills",
        description="Pull Kraken fills and emit them to per-user telemetry.",
        access="auth",
        parameters=[
            ToolParam(name="mode", type="string", required=False, description="live (default) | paper | validate"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def execute_swap(
        input_token: str, output_token: str, amount: float,
        chain_id: int, wallet_address: str, slippage_pct: float,
        venue_id: str | None = None,
        confirm: bool = False,
        api_key: str = "",
    ) -> str:
        """Execute a swap. Requires confirm=true + explicit slippage_pct.

        Full 6-step flow with client-side signing; SDK never sees keys.

        `slippage_pct` is REQUIRED and specified as a DECIMAL, capped
        at 0.0025 (0.25%). Typical values: 0.001 (0.1%), 0.002 (0.2%),
        0.0025 (0.25% = max). Higher values are refused to prevent
        rekt-on-illiquid-pair execution. No default — picking a
        slippage tolerance is a risk decision the user must make
        explicitly. Converted to the upstream percentage convention
        (multiplied by 100) at the `dex.prepare_swap()` boundary.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.models.domain import OrderIntent
            from src.services.order_executor import execute_one
            from src.shared.errors import ConfirmationRequired
            if not confirm:
                raise ConfirmationRequired(
                    "DEX swaps require confirm=true.",
                    suggestion="Re-invoke with confirm=true.",
                )
            side = "sell" if output_token.upper() == "USDC" else "buy"
            symbol = input_token if side == "sell" else output_token
            intent = OrderIntent(action="enter", side=side, symbol=symbol,
                                 amount=amount, reason="user-initiated")
            trade = execute_one(intent, mode="live",
                                wallet_address=wallet_address,
                                chain_id=chain_id, venue_id=venue_id,
                                slippage_pct=slippage_pct)
            return json.dumps({
                "tx_hash": trade.tx_hash, "status": trade.status,
                "input_token": trade.input_token, "input_amount": trade.input_amount,
                "output_token": trade.output_token, "output_amount": trade.output_amount,
                "fill_price": trade.fill_price, "fees": trade.fees,
                "trade_log_id": trade.id,
            })
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="execute_swap",
        description=(
            "Execute a DEX swap (requires confirm=true + explicit "
            "slippage_pct). Single code path shared with cron-driven "
            "trades. Slippage is always user-specified — no default — "
            "because picking a tolerance is a risk decision."
        ),
        access="auth",
        parameters=[
            ToolParam(name="input_token", type="string", required=True, description="Input token"),
            ToolParam(name="output_token", type="string", required=True, description="Output token"),
            ToolParam(name="amount", type="number", required=True, description="Input amount"),
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="wallet_address", type="string", required=True, description="Wallet from local store"),
            ToolParam(name="slippage_pct", type="number", required=True, description="Slippage tolerance as DECIMAL, capped at 0.0025 (0.25%). Typical: 0.001 (0.1%), 0.002 (0.2%), 0.0025 (max). Higher values refused."),
            ToolParam(name="venue_id", type="string", required=False, description="Optional specific venue"),
            ToolParam(name="confirm", type="boolean", required=False, description="Must be true to execute; omission safely refuses the action."),
            _APIKEY,
        ],
    ))



# Bound on the one-time x402 facilitator handshake performed while registering
# the hello_mangrove demo tool. The facilitator is an EXTERNAL service; this cap
# guarantees a slow/unreachable one can't stall startup past the ~30s window the
# setup/verify scripts wait on /health. See issue #106.
_X402_STARTUP_INIT_TIMEOUT_S = 6.0


def _run_bounded(fn, timeout_s: float) -> Any:
    """Run ``fn()`` but give up after ``timeout_s`` seconds.

    Used so a slow/unreachable external dependency can't stall import-time
    startup. On timeout the worker thread is abandoned (it finishes or errors
    harmlessly on its own); we never block the port bind waiting on it.
    """
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    finally:
        # Don't wait on the worker — if it's still blocked on the network we
        # must let registration (and the port bind) proceed regardless.
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# x402 spend budget (auth)
# ---------------------------------------------------------------------------


def _register_x402_spend(server: FastMCP) -> None:
    """The budget the agent pays MangroveAI out of, and the top-up.

    These are MCP tools and not just REST routes for a specific reason. The
    budget does not refill itself -- a human has to authorize more -- and if
    the only way to give that consent were a terminal, the control would fire
    hardest exactly where the risk it guards against does not exist: in a
    live conversation, with the user right there. A cron tick at 3am still
    finds nobody to ask and stays stopped, which is the case that matters.
    """

    @server.tool()
    async def x402_spend_status(limit: int = 20, api_key: str = "") -> str:
        """Outbound x402 budget: spent, remaining, and what the money went on.

        Call this when a payment is refused for budget reasons, BEFORE asking
        the user to authorize more — show them the ledger first so the answer
        is informed. Also worth a look when the user asks why data calls
        stopped working, or what the agent has been spending.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.api.routes.x402_spend import (
                get_spend_status as status_route,
            )
            from src.api.routes.x402_spend import (
                list_spend_payments as payments_route,
            )
            status = await status_route()
            payments = await payments_route(limit=limit, period_id=None)
            # Nested, not merged. Flattening two independently-evolving
            # payloads into one namespace works until the day a field name
            # appears in both, at which point one silently wins and no test
            # notices.
            return json.dumps({"budget": status, **payments})
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="x402_spend_status",
        description=(
            "Outbound x402 budget (spent / remaining / exhausted) plus the "
            "recent payment ledger. Read this before asking the user to "
            "authorize more spending."
        ),
        access="auth",
        parameters=[
            ToolParam(name="limit", type="integer", required=False,
                      description="Ledger rows to include, newest first. Default 20."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def x402_spend_reset(
        confirm: bool = False, cap_usd: float | None = None, api_key: str = "",
    ) -> str:
        """Authorize a fresh x402 budget after the user has agreed to it.

        THE USER'S CONSENT IS THE CONTROL — this tool records it, it does not
        substitute for it. Never call it to get past your own refused
        payment. The sequence is: show them `x402_spend_status`, tell them
        what is left to do and roughly what it will cost, ask, and call this
        only once they have said yes.

        `cap_usd` is the budget THEY chose; omit it to start the same size
        again. Past payments stay on the ledger under their old period.
        """
        if not _require(api_key):
            return _auth_error()
        if not confirm:
            return _err(
                "CONFIRMATION_REQUIRED",
                "Authorizing more x402 spending needs the user's explicit agreement.",
                "Show the user x402_spend_status, ask whether to continue and at "
                "what budget, then call again with confirm=true.",
            )
        try:
            from src.api.routes.x402_spend import SpendResetRequest
            from src.api.routes.x402_spend import reset_spend_cap as route
            return json.dumps(await route(SpendResetRequest(cap_usd=cap_usd)))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="x402_spend_reset",
        description=(
            "Start a fresh x402 budget period after the USER has agreed to "
            "it. Requires confirm=true. Optional cap_usd sets the budget they "
            "authorized. Never call this to unblock your own payment."
        ),
        access="auth",
        parameters=[
            ToolParam(name="confirm", type="boolean", required=False,
                      description="Must be true. Asserts the user agreed to spend more."),
            ToolParam(name="cap_usd", type="number", required=False,
                      description="Budget in dollars the user chose. Omit to keep the current size."),
            _APIKEY,
        ],
    ))


def _register_hello_mangrove(server: FastMCP) -> None:
    """Register hello_mangrove via the x402 library's MCP payment wrapper.

    The wrapper intercepts tool calls, reads payment from MCP ``_meta``, verifies
    and settles via the shared x402ResourceServer, and attaches the settlement
    receipt to the result's ``_meta``. Clients using ``x402.mcp.x402MCPClient``
    auto-handle the empty-payment -> sign -> retry round-trip.

    Building the payment wrapper requires a one-time handshake with the EXTERNAL
    x402 facilitator (``initialize()`` fetches ``/supported`` over the network).
    That handshake must NEVER gate app startup: the free + auth tiers (health,
    discovery, wallets, strategies, backtests, KB) have to come up even when the
    facilitator is unreachable, slow, or firewalled. Previously this ran eagerly
    and un-guarded at import time, so an unreachable facilitator crashed/stalled
    ``create_app()`` before uvicorn bound its port and ``/health`` never answered
    (issue #106). We bound the attempt and degrade gracefully: if the facilitator
    can't be reached the tool is still registered, but returns a clear error
    instead of taking the whole agent down with it.
    """
    from x402 import ResourceConfig
    from x402.mcp import create_payment_wrapper
    from x402.schemas import ResourceInfo as X402ResourceInfo

    from src.services.hello_mangrove import get_hello_mangrove as _impl
    from src.shared.x402.config import get_network, get_pay_to
    from src.shared.x402.server import _ensure_initialized

    description = f"x402 demo: $0.05 USDC on {get_network()}. Smoke test for the payment path."

    def _build_payment_wrapper():
        resource_server = _ensure_initialized()  # external facilitator /supported fetch
        accepts = resource_server.build_payment_requirements(
            ResourceConfig(
                scheme="exact",
                network=get_network(),
                pay_to=get_pay_to(),
                price="$0.05",
            )
        )
        return create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=X402ResourceInfo(
                url="mcp://hello_mangrove",
                description="hello_mangrove message — $0.05 USDC donation",
            ),
        )

    wrapper = None
    try:
        wrapper = _run_bounded(_build_payment_wrapper, _X402_STARTUP_INIT_TIMEOUT_S)
    except Exception as exc:  # facilitator unreachable / slow / errored
        _log.warning(
            "x402.hello_mangrove.facilitator_unavailable",
            error_type=type(exc).__name__,  # e.g. TimeoutError, ConnectError
            error=str(exc),
            facilitator_timeout_s=_X402_STARTUP_INIT_TIMEOUT_S,
            detail="x402 payment demo disabled this run; free + auth tiers unaffected",
        )

    if wrapper is not None:
        @server.tool(
            name="hello_mangrove",
            description=description,
        )
        @wrapper
        async def hello_mangrove() -> str:
            return json.dumps(_impl())
    else:
        # Retain direct diagnostics while excluding the demo from business discovery.
        @server.tool(
            name="hello_mangrove",
            description="x402 demo (payment facilitator was unreachable at startup).",
        )
        async def hello_mangrove() -> str:
            return json.dumps({
                "error": True,
                "code": "X402_FACILITATOR_UNAVAILABLE",
                "message": (
                    "The x402 payment facilitator was unreachable when the agent "
                    "started, so the paid demo is disabled in this environment. "
                    "Restart the agent once outbound access to the facilitator is "
                    "available. The agent's free and API-key tiers are unaffected."
                ),
            })

    register_tool(ToolEntry(
        name="hello_mangrove",
        description=description,
        access="x402",
        price="$0.05 USDC",
        network=get_network(),
        parameters=[],
    ))
