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


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    return obj


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
    _register_strategy(server)
    _register_logs(server)
    _register_x402_spend(server)
    _register_hello_mangrove(server)


# ---------------------------------------------------------------------------
# Discovery (free)
# ---------------------------------------------------------------------------


def _register_discovery(server: FastMCP) -> None:
    @server.tool()
    async def status() -> str:
        """Return agent status: version, wallets count, strategies by status,
        active cron jobs, db path, uptime. Free, no auth required."""
        from src.api.routes.discovery import status as route
        return json.dumps(await route())

    register_tool(ToolEntry(
        name="status",
        description="Agent status + counts + uptime. Free, no auth.",
        access="free",
        parameters=[],
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

    @server.tool()
    async def get_balances(address: str, chain_id: int, api_key: str = "") -> str:
        """Token balances for a wallet via mangrovemarkets.dex.balances."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.balances(chain_id=chain_id, wallet=address)
            return json.dumps(_dump(result))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="get_balances",
        description="Token balances for a wallet.",
        access="auth",
        parameters=[
            ToolParam(name="address", type="string", required=True, description="Wallet address"),
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            _APIKEY,
        ],
    ))

    # --- Portfolio (on-chain aggregate view of a wallet) -----------------
    # Thin wrappers over mangrovemarkets.portfolio.*. The SDK accepts
    # `addresses` as a comma-separated string (one or more wallets) and
    # optional `chain_id` to pin the query.

    @server.tool()
    async def portfolio_value(
        addresses: str, chain_id: int | None = None, api_key: str = "",
    ) -> str:
        """Aggregate USD value of one or more wallets.

        `addresses` is a comma-separated list (agent can query multiple
        wallets at once). Omit `chain_id` to get a cross-chain total.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().portfolio.value(
                addresses=addresses, chain_id=chain_id,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("PORTFOLIO_VALUE_FAILED", e)

    register_tool(ToolEntry(
        name="portfolio_value",
        description="Aggregate USD value of one or more wallet addresses.",
        access="auth",
        parameters=[
            ToolParam(name="addresses", type="string", required=True, description="Comma-separated wallet addresses"),
            ToolParam(name="chain_id", type="integer", required=False, description="Optional: pin to a single chain"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def portfolio_pnl(
        addresses: str, chain_id: int | None = None, api_key: str = "",
    ) -> str:
        """Running P&L across one or more wallets.

        Returns realized + unrealized P&L based on the upstream's
        cost-basis accounting. The answer to "how am I doing?"
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().portfolio.pnl(
                addresses=addresses, chain_id=chain_id,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("PORTFOLIO_PNL_FAILED", e)

    register_tool(ToolEntry(
        name="portfolio_pnl",
        description="Realized + unrealized P&L for one or more wallets.",
        access="auth",
        parameters=[
            ToolParam(name="addresses", type="string", required=True, description="Comma-separated wallet addresses"),
            ToolParam(name="chain_id", type="integer", required=False, description="Optional: pin to a single chain"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def portfolio_tokens(
        addresses: str, chain_id: int | None = None, api_key: str = "",
    ) -> str:
        """Per-token holdings for one or more wallets.

        More detail than `get_balances` — includes USD value per
        token, price, cost basis, and position P&L.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().portfolio.tokens(
                addresses=addresses, chain_id=chain_id,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("PORTFOLIO_TOKENS_FAILED", e)

    register_tool(ToolEntry(
        name="portfolio_tokens",
        description="Per-token holdings with USD value + per-position P&L.",
        access="auth",
        parameters=[
            ToolParam(name="addresses", type="string", required=True, description="Comma-separated wallet addresses"),
            ToolParam(name="chain_id", type="integer", required=False, description="Optional: pin to a single chain"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def portfolio_defi(
        addresses: str, chain_id: int | None = None, api_key: str = "",
    ) -> str:
        """DeFi positions (LPs, lending, staking) for one or more wallets."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().portfolio.defi(
                addresses=addresses, chain_id=chain_id,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("PORTFOLIO_DEFI_FAILED", e)

    register_tool(ToolEntry(
        name="portfolio_defi",
        description="DeFi positions (LP, lending, staking) across wallets.",
        access="auth",
        parameters=[
            ToolParam(name="addresses", type="string", required=True, description="Comma-separated wallet addresses"),
            ToolParam(name="chain_id", type="integer", required=False, description="Optional: pin to a single chain"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def portfolio_history(
        address: str, limit: int = 50, api_key: str = "",
    ) -> str:
        """On-chain transaction history for a SINGLE wallet (not comma-separated).

        Different from our local `agent_list_trades` (which covers strategy-
        executed swaps only). This tool covers EVERY on-chain tx for
        the wallet — deposits, withdrawals, external swaps, etc.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            items = mangrove_markets_client().portfolio.history(
                address=address, limit=limit,
            )
            return json.dumps([_dump(i) for i in items])
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("PORTFOLIO_HISTORY_FAILED", e)

    register_tool(ToolEntry(
        name="portfolio_history",
        description="On-chain tx history for a single wallet (all txs, not just strategy-driven).",
        access="auth",
        parameters=[
            ToolParam(name="address", type="string", required=True, description="Single wallet address"),
            ToolParam(name="limit", type="integer", required=False, description="Max results (default 50)"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# DEX (auth)
# ---------------------------------------------------------------------------


def _register_dex(server: FastMCP) -> None:
    @server.tool()
    async def list_dex_venues(api_key: str = "") -> str:
        """List supported DEX venues."""
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_markets_client
        venues = mangrove_markets_client().dex.supported_venues()
        return json.dumps([_dump(v) for v in venues])

    register_tool(ToolEntry(
        name="list_dex_venues",
        description="List supported DEX venues.",
        access="auth",
        parameters=[_APIKEY],
    ))

    # -- CEX (Kraken) BYOK tools --------------------------------------------
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
    async def get_swap_quote(
        input_token: str, output_token: str, amount: float,
        chain_id: int, venue_id: str | None = None,
        mode: str | None = None,
        api_key: str = "",
    ) -> str:
        """Get a DEX swap quote.

        `amount` is the quantity of `input_token` in HUMAN units (e.g.
        0.001 = 0.001 ETH, 25 = 25 USDC) — NOT base units. The agent
        converts it to the token's smallest units (base units / wei)
        before calling the backend, and converts the returned
        input_amount/output_amount back to human units (raw values kept
        as input_amount_base_units / output_amount_base_units). `mode` is
        an optional routing hint recognized by some venues (e.g. 1inch
        supports modes that bias for gas-cost vs price-improvement).
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services import dex_service
            q = dex_service.get_quote(
                input_token=input_token,
                output_token=output_token,
                amount=amount,
                chain_id=chain_id,
                venue_id=venue_id,
                mode=mode,
            )
            return json.dumps(q)
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="get_swap_quote",
        description="Get a DEX swap quote. Optionally pin a venue + mode.",
        access="auth",
        parameters=[
            ToolParam(name="input_token", type="string", required=True, description="Input token (contract address; native ETH = 0xEeee…EEeE)"),
            ToolParam(name="output_token", type="string", required=True, description="Output token (contract address)"),
            ToolParam(name="amount", type="number", required=True, description="Input amount in HUMAN units (e.g. 0.001 = 0.001 ETH, 25 = 25 USDC). Converted to base units internally."),
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="venue_id", type="string", required=False, description="Optional specific venue"),
            ToolParam(name="mode", type="string", required=False, description="Optional routing hint (venue-specific)"),
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

    @server.tool()
    async def get_tx_status(
        tx_hash: str, chain_id: int,
        venue_id: str | None = None,
        api_key: str = "",
    ) -> str:
        """Check the status of a broadcast transaction.

        Post-swap verification: execute_swap returns
        a tx_hash before the tx is finalized. Call this tool after to
        confirm the transaction landed (status: confirmed | pending |
        failed). Pass-through to mangrovemarkets.dex.tx_status.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.tx_status(
                tx_hash=tx_hash, chain_id=chain_id, venue_id=venue_id,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_TX_STATUS_FAILED", e)

    register_tool(ToolEntry(
        name="get_tx_status",
        description=(
            "Verify a broadcast transaction's final state. Call after "
            "execute_swap — the returned tx_hash isn't confirmed yet. "
            "Returns status: confirmed | pending | failed + block info."
        ),
        access="auth",
        parameters=[
            ToolParam(name="tx_hash", type="string", required=True, description="Transaction hash returned by execute_swap"),
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id (8453 = Base mainnet)"),
            ToolParam(name="venue_id", type="string", required=False, description="Optional: pin to a specific venue"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_token_info(
        chain_id: int, address: str, api_key: str = "",
    ) -> str:
        """Look up token metadata (symbol, decimals, name) by contract address.

        ⚠️ CURRENTLY BROKEN pending upstream SDK fix. The mangrovemarkets
        SDK's TokenInfo pydantic model expects flat top-level fields
        (address, symbol, name, decimals) but the server response nests
        them under a `token` sub-dict. Every call returns
        DEX_TOKEN_INFO_FAILED with a 4-validation-error message.
        Tracked: https://github.com/MangroveTechnologies/MangroveMarkets-MCP-Server/issues/62
        Fall back to kb_glossary_get or kb_search for token concept
        lookups until this is fixed and the SDK version bumped.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.token_info(
                chain_id=chain_id, address=address,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_TOKEN_INFO_FAILED", e)

    register_tool(ToolEntry(
        name="get_token_info",
        description="⚠️ BROKEN upstream (MangroveMarkets-MCP-Server#62). Use kb_glossary_get / kb_search for token concepts until SDK bump.",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="address", type="string", required=True, description="Token contract address"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_spot_price(
        chain_id: int, tokens: str, api_key: str = "",
    ) -> str:
        """Current spot price for one or more tokens.

        `tokens` is a COMMA-SEPARATED LIST OF CONTRACT ADDRESSES.
        Symbols are NOT accepted (upstream 1inch backend rejects
        them with 400 Bad Request). Use get_token_info first if
        you only have a symbol — though that tool is currently
        broken (see its docstring). Reliable path: hardcode known
        addresses (USDC on Base = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913,
        WETH on Base = 0x4200000000000000000000000000000000000006).

        Prices are returned as wei-denominated integers (string form).
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.spot_price(
                chain_id=chain_id, tokens=tokens,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_SPOT_PRICE_FAILED", e)

    register_tool(ToolEntry(
        name="get_spot_price",
        description="Current spot price for one or more tokens on a chain.",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="tokens", type="string", required=True, description="Comma-separated token symbols or addresses"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_gas_price(chain_id: int, api_key: str = "") -> str:
        """Current gas price estimate for a chain.

        Pre-flight check before a swap. Returns a `GasPrice` payload
        where the SDK's flat top-level fields (`low`, `medium`, `high`,
        `base_fee`) are currently null; real values are nested under
        the `gas` key:
            gas.baseFee                      — current base fee in wei
            gas.low.maxPriorityFeePerGas     — tip for slow tx
            gas.low.maxFeePerGas             — total cap for slow tx
            gas.medium.{maxPriorityFeePerGas,maxFeePerGas}
            gas.high.{maxPriorityFeePerGas,maxFeePerGas}

        To estimate total cost in ETH for a swap, multiply a chosen
        tier's maxFeePerGas by the gas limit returned by a quote.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.gas_price(chain_id=chain_id)
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_GAS_PRICE_FAILED", e)

    register_tool(ToolEntry(
        name="get_gas_price",
        description="Gas price estimate for a chain (pre-flight before execute_swap).",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_token_search(
        chain_id: int, query: str, api_key: str = "",
    ) -> str:
        """Fuzzy-search tokens by symbol or partial name.

        Lets the agent resolve a symbol the user typed into a concrete
        contract address. Pairs with the other DEX tools that need
        addresses (get_spot_price, get_quote with address inputs, etc).
        Current workaround for the broken get_token_info.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            results = mangrove_markets_client().dex.token_search(
                chain_id=chain_id, query=query,
            )
            return json.dumps([_dump(r) for r in results])
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_TOKEN_SEARCH_FAILED", e)

    register_tool(ToolEntry(
        name="get_token_search",
        description="Fuzzy token search by symbol / partial name. Returns candidate contract addresses.",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="query", type="string", required=True, description="Symbol or partial name (e.g. 'USDC', 'Pepe')"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_dex_chart(
        chain_id: int, token0: str, token1: str, period: str = "1h",
        api_key: str = "",
    ) -> str:
        """OHLC chart candles for a token pair on a DEX.

        ⚠️ CURRENTLY BROKEN upstream. The mangrovemarkets SDK's
        chart() wrapper sends token0/token1 fields but the upstream
        1inch chart tool requires an `address` field. Every real
        call returns DEX_CHART_FAILED with a validation error.
        Fall back to get_ohlcv (CEX-aggregated from MangroveAI) for
        price history until this is fixed.

        Different from get_ohlcv: that one hits MangroveAI's
        CEX-aggregated crypto_assets data; this one (when fixed)
        pulls DEX-native candles for a specific on-chain pair.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.chart(
                chain_id=chain_id, token0=token0, token1=token1, period=period,
            )
            return json.dumps([_dump(c) for c in result])
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_CHART_FAILED", e)

    register_tool(ToolEntry(
        name="get_dex_chart",
        description="⚠️ BROKEN upstream (SDK sends token0/token1, server wants `address`). Use get_ohlcv for price history until fixed.",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="token0", type="string", required=True, description="Base token (symbol or address)"),
            ToolParam(name="token1", type="string", required=True, description="Quote token (symbol or address)"),
            ToolParam(name="period", type="string", required=False, description="Bar period (default '1h')"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_allowances(
        chain_id: int, wallet: str, spender: str, api_key: str = "",
    ) -> str:
        """ERC-20 allowance check — has the wallet approved `spender`?

        Debugging / pre-approval check before execute_swap. Useful to
        diagnose "my swap keeps failing" — often an expired approval.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_markets_client
            result = mangrove_markets_client().dex.allowances(
                chain_id=chain_id, wallet=wallet, spender=spender,
            )
            return json.dumps(_dump(result))
        except Exception as e:  # noqa: BLE001
            return _handle_upstream_error("DEX_ALLOWANCES_FAILED", e)

    register_tool(ToolEntry(
        name="get_allowances",
        description="Check ERC-20 allowances a wallet has granted a spender (approve_token output).",
        access="auth",
        parameters=[
            ToolParam(name="chain_id", type="integer", required=True, description="EVM chain id"),
            ToolParam(name="wallet", type="string", required=True, description="Wallet address"),
            ToolParam(name="spender", type="string", required=True, description="Spender contract address (e.g. a router)"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# Market data (auth)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Signals (auth)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# DeFi (auth)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Social (auth)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Docs (auth)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Strategy (auth)
# ---------------------------------------------------------------------------


def _register_strategy(server: FastMCP) -> None:
    @server.tool(name="agent_create_strategy_autonomous")
    async def create_strategy_autonomous(
        goal: str, asset: str, timeframe: str,
        candidate_count: int = 7, backtest_lookback_months: int = 3,
        seed: int | None = None, api_key: str = "",
    ) -> str:
        """Autonomous strategy creation: goal → candidates → backtest → rank → winner."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import (
                StrategyAutonomousRequest,
                create_autonomous,
            )
            detail, report = create_autonomous(StrategyAutonomousRequest(
                goal=goal, asset=asset, timeframe=timeframe,
                candidate_count=candidate_count,
                backtest_lookback_months=backtest_lookback_months,
                seed=seed,
            ))
            return json.dumps({"strategy": detail.model_dump(mode="json"),
                               "generation_report": report})
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_create_strategy_autonomous",
        description="Create a strategy from a natural-language goal.",
        access="auth",
        parameters=[
            ToolParam(name="goal", type="string", required=True, description="Natural-language goal"),
            ToolParam(name="asset", type="string", required=True, description="Asset symbol"),
            ToolParam(name="timeframe", type="string", required=True, description="5m | 15m | 30m | 1h | 4h | 1d (1m not supported)"),
            ToolParam(name="candidate_count", type="integer", required=False, description="5-10"),
            ToolParam(name="backtest_lookback_months", type="integer", required=False, description="Default: auto by timeframe (5m-1h=3mo, 4h=6mo, 1d=12mo)"),
            ToolParam(name="seed", type="integer", required=False, description="Reproducibility seed"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_create_strategy_manual")
    async def create_strategy_manual(
        name: str, asset: str, timeframe: str,
        entry: list[dict], exit: list[dict] | None = None,
        execution_config: dict | None = None, api_key: str = "",
    ) -> str:
        """Manual strategy creation with explicit rules."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import (
                StrategyManualRequest,
                create_manual,
            )
            detail = create_manual(StrategyManualRequest(
                name=name, asset=asset, timeframe=timeframe,
                entry=entry, exit=exit or [],
                execution_config=execution_config,
            ))
            return json.dumps(detail.model_dump(mode="json"))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_create_strategy_manual",
        description=(
            "Create (persist) a strategy with explicit entry/exit rules — also the "
            "step that saves an agent_build_strategy_from_reference payload. Saved with "
            "status `inactive` (saved, not scheduled; MangroveAI `draft` means "
            "unproven and cannot be promoted, so it is not used). Next: "
            "agent_backtest_strategy, then agent_update_strategy_status(status='paper')."
        ),
        access="auth",
        parameters=[
            ToolParam(name="name", type="string", required=True, description="Strategy name"),
            ToolParam(name="asset", type="string", required=True, description="Asset symbol"),
            ToolParam(name="timeframe", type="string", required=True, description="5m | 15m | 30m | 1h | 4h | 1d (1m not supported)"),
            ToolParam(name="entry", type="array", required=True, description="Entry rules"),
            ToolParam(name="exit", type="array", required=False, description="Exit rules"),
            ToolParam(name="execution_config", type="object", required=False, description="Override exec params"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_search_reference_strategies")
    async def search_reference_strategies(
        asset: str,
        timeframe: str | None = None,
        category: str | None = None,
        goal_hint: str | None = None,
        limit: int = 5,
        strict: bool = False,
        api_key: str = "",
    ) -> str:
        """Search curated reference strategies — Mechanism 2 of /create-strategy.

        The agent calls this BEFORE picking signals/params manually. Each
        returned reference has known-good entry/exit signals + parameter
        choices. The agent picks one that matches user intent, then calls
        agent_build_strategy_from_reference to materialize it.

        asset/timeframe/category RANK, they do not filter: exact matches
        come first (asset+timeframe+category > asset+timeframe > asset >
        category), then the list is padded. Each result carries
        `match` (exact|partial|none) + `matched_on`/`unmatched`; the
        envelope carries `exact_match_count`. strict=true returns exact
        matches only. Auto-detects category from goal_hint if not supplied.
        """
        if not _require(api_key):
            return _auth_error()
        from src.services import reference_strategies_service
        return json.dumps(reference_strategies_service.search_response(
            asset=asset,
            timeframe=timeframe,
            category=category,
            goal_hint=goal_hint,
            limit=limit,
            strict=strict,
        ))

    register_tool(ToolEntry(
        name="agent_search_reference_strategies",
        description=(
            "Find curated reference strategies that match the user's goal "
            "and asset. Returns ranked candidates with signals + parameter "
            "choices that have worked in backtests. ALWAYS call this "
            "before picking signals manually — it's the primary source of "
            "parameter intuition. asset/timeframe/category RANK results, they "
            "do not filter them: check each result's `match` "
            "(exact|partial|none) and `unmatched`, or pass strict=true for "
            "exact matches only. References are portable — a partial match "
            "can still be retargeted with agent_build_strategy_from_reference."
        ),
        access="auth",
        parameters=[
            ToolParam(name="asset", type="string", required=True, description="Asset symbol (e.g. BTC, ETH)"),
            ToolParam(name="timeframe", type="string", required=False, description="5m | 15m | 30m | 1h | 4h | 1d — ranks exact-timeframe references first; does not exclude others unless strict=true"),
            ToolParam(name="category", type="string", required=False, description="momentum | mean_reversion | trend_following | breakout | volatility"),
            ToolParam(name="goal_hint", type="string", required=False, description="Free text from the user's goal — auto-detects category if category is not supplied"),
            ToolParam(name="limit", type="integer", required=False, description="Max results (default 5)"),
            ToolParam(name="strict", type="boolean", required=False, description="true = only references matching every supplied filter (may return 0); default false = ranked + padded"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_build_strategy_from_reference")
    async def build_strategy_from_reference(
        reference_id: str,
        timeframe: str | None = None,
        asset: str | None = None,
        name: str | None = None,
        api_key: str = "",
    ) -> str:
        """Materialize a reference into an agent_create_strategy_manual payload.

        Does NOT save anything: the response has `persisted: false` and a
        `next_step` pointing at agent_create_strategy_manual (REST: POST
        /api/v1/agent/strategies/manual). Only that call returns a
        strategy_id you can backtest or promote.

        Copies the reference's signals EXACTLY (names and params untouched).
        `timeframe` and `asset` are free-to-override — reference strategies
        are portable signal combos, not pins to a specific asset/TF. Bulk
        pattern: loop over the top N references from search, build each
        onto the user's target (asset, timeframe), backtest all, rank.
        """
        if not _require(api_key):
            return _auth_error()
        from src.services import reference_strategies_service
        try:
            payload = reference_strategies_service.build_from_reference(
                reference_id=reference_id,
                timeframe_override=timeframe,
                asset_override=asset,
                name=name,
            )
        except ValueError as e:
            return json.dumps({"error": str(e), "code": "REFERENCE_NOT_FOUND"})
        return json.dumps(payload)

    register_tool(ToolEntry(
        name="agent_build_strategy_from_reference",
        description=(
            "After agent_search_reference_strategies returns candidates, call this "
            "to produce an agent_create_strategy_manual payload. It does NOT save "
            "anything (`persisted: false`): pass the payload to "
            "agent_create_strategy_manual to get a strategy_id. Signals and params "
            "are copied exactly — the agent must NOT modify them. `timeframe` "
            "and `asset` are free overrides: a reference is a portable combo, "
            "so retarget onto the user's asset/TF and bulk-backtest the top "
            "matches rather than single-pick by label."
        ),
        access="auth",
        parameters=[
            ToolParam(name="reference_id", type="string", required=True, description="e.g. ref-001 — from agent_search_reference_strategies"),
            ToolParam(name="timeframe", type="string", required=False, description="Override the reference's timeframe (canonicalized)"),
            ToolParam(name="asset", type="string", required=False, description="Retarget onto a different asset — reference strategies are portable"),
            ToolParam(name="name", type="string", required=False, description="Optional strategy name override"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_list_strategies")
    async def list_strategies(status: str | None = None, limit: int = 50,
                              offset: int = 0, api_key: str = "") -> str:
        """List strategies, optionally filtered by status."""
        if not _require(api_key):
            return _auth_error()
        from src.services.strategy_service import list_strategies as svc
        items = svc(status=status, limit=limit, offset=offset)
        return json.dumps([s.model_dump(mode="json") for s in items])

    register_tool(ToolEntry(
        name="agent_list_strategies",
        description="List strategies.",
        access="auth",
        parameters=[
            ToolParam(name="status", type="string", required=False, description="Filter: draft|inactive|paper|live|archived"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_get_strategy")
    async def get_strategy(strategy_id: str, api_key: str = "") -> str:
        """Get a strategy by ID."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import get_strategy as svc
            return json.dumps(svc(strategy_id).model_dump(mode="json"))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_get_strategy",
        description="Get a strategy by ID.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_update_strategy_status")
    async def update_strategy_status(
        strategy_id: str, status: str, confirm: bool = False,
        allocation: dict | None = None, api_key: str = "",
    ) -> str:
        """Transition strategy status. live + live→inactive require confirm=true;
        live requires an allocation block."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import (
                StrategyAllocationInput,
                StrategyStatusUpdate,
                update_status,
            )
            alloc = StrategyAllocationInput(**allocation) if allocation else None
            detail = update_status(strategy_id, StrategyStatusUpdate(
                status=status, confirm=confirm, allocation=alloc,
            ))
            return json.dumps(detail.model_dump(mode="json"))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_update_strategy_status",
        description="Transition strategy lifecycle status.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            ToolParam(name="status", type="string", required=True, description="Target status"),
            ToolParam(name="confirm", type="boolean", required=False, description="Required for live + live→inactive"),
            ToolParam(name="allocation", type="object", required=False, description="Required for live"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_backtest_strategy")
    async def backtest_strategy(
        strategy_id: str, mode: str = "full",
        lookback_months: int | None = None,
        lookback_days: int | None = None,
        lookback_hours: int | None = None,
        start_date: str | None = None, end_date: str | None = None,
        config: dict | None = None,
        include_benchmark: bool = True,
        api_key: str = "",
    ) -> str:
        """Run a backtest against an existing strategy (mode=quick|full).

        Full-mode results carry `backtest_id` (the run is stored server-side;
        read it back later with get_backtest instead of re-running),
        `metric_units` (percent-typed metrics are 0-100: 0.52 means 0.52%), and
        `benchmark` — buy-and-hold over the same window, so a strategy's return
        is never quoted without what holding the asset did. Set
        include_benchmark=false to skip that one extra OHLCV call.

        Async-backed (SDK >=1.14): the SDK submits to the async surface and
        polls status internally, so long windows work -- there is no gateway
        timeout ceiling. Warm windows return in seconds; a cold long window
        (first request for that asset/range) can take tens of seconds while
        historical data is fetched. Pick windows for statistical coverage,
        not transport limits.

        Mode semantics: `full` runs the real engine -- every position gets a
        system ATR stop-loss/take-profit bracket plus time-based exits from
        execution_config, so entry-only strategies (empty exit list) are
        first-class and close positions normally. `quick` is a
        signal-frequency screen with NO risk management (no SL/TP/time
        exits): entry-only strategies there hold one position to
        end-of-window, so quick metrics are for relative screening only --
        never quote them as performance.

        Window resolution (first non-null wins):
          start_date+end_date > lookback_hours > lookback_days
          > lookback_months > timeframes.recommended_lookback_months
          (5m/15m/30m/1h → 3 mo, 4h → 6 mo, 1d → 12 mo).

        `config` is a single dict that merges over the canonical
        trading_defaults.json. Any SDK BacktestRequest field is valid —
        slippage_pct, fee_pct, max_hold_time_hours, initial_balance,
        max_risk_per_trade, reward_factor, atr_period, etc. Omit the
        argument entirely to get a pure trading-defaults backtest.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.api.routes.strategies import BacktestInput, backtest
            return json.dumps(await backtest(strategy_id, BacktestInput(
                mode=mode,
                lookback_months=lookback_months,
                lookback_days=lookback_days,
                lookback_hours=lookback_hours,
                start_date=start_date,
                end_date=end_date,
                config=config,
                include_benchmark=include_benchmark,
            )))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_backtest_strategy",
        description=(
            "Backtest a strategy (quick or full). Async-backed (SDK >=1.14: "
            "submit + poll under the hood), so long windows work — no "
            "gateway timeout; cold long windows may take tens of seconds. "
            "Window precedence: "
            "start+end > hours > days > months > timeframe-aware auto "
            "(5m-1h=3mo, 4h=6mo, 1d=12mo). `config` is a single dict "
            "that merges over trading_defaults.json — use it for "
            "slippage_pct, fee_pct, max_hold_time_hours, initial_balance, "
            "max_risk_per_trade, reward_factor, atr_period, or any other "
            "BacktestRequest field. Returns full SDK metrics (percent-typed "
            "on a 0-100 scale, see metric_units), trade history, a "
            "resolved_window block, the stored run's backtest_id, and "
            "`benchmark` (buy-and-hold over the same window)."
        ),
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            ToolParam(name="mode", type="string", required=False, description="quick | full (default full)"),
            ToolParam(name="lookback_months", type="integer", required=False, description="Window in months (auto by timeframe if all window fields omitted)"),
            ToolParam(name="lookback_days", type="integer", required=False, description="Window in days (overrides lookback_months)"),
            ToolParam(name="lookback_hours", type="integer", required=False, description="Window in hours — use for short backtests"),
            ToolParam(name="start_date", type="string", required=False, description="ISO 8601 — paired with end_date, overrides all lookback_* fields"),
            ToolParam(name="end_date", type="string", required=False, description="ISO 8601"),
            ToolParam(name="config", type="object", required=False, description="Merges over trading_defaults.json (slippage_pct, max_risk_per_trade, initial_balance, reward_factor, atr_*, etc.)"),
            ToolParam(name="include_benchmark", type="boolean", required=False, description="Attach buy-and-hold over the same window (default true)"),
            _APIKEY,
        ],
    ))





    @server.tool(name="agent_evaluate_strategy")
    async def evaluate_strategy(strategy_id: str, api_key: str = "") -> str:
        """Manually trigger a single evaluation tick."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.api.routes.strategies import evaluate
            return json.dumps(await evaluate(strategy_id))
        except (AgentError, AIAPIError, MarketsAPIError, HTTPStatusError) as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="agent_evaluate_strategy",
        description="Manually trigger one evaluation tick.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            _APIKEY,
        ],
    ))










# ---------------------------------------------------------------------------
# Logs (auth)
# ---------------------------------------------------------------------------


def _register_logs(server: FastMCP) -> None:
    @server.tool(name="agent_list_evaluations")
    async def list_evaluations(strategy_id: str, limit: int = 50,
                                offset: int = 0, api_key: str = "") -> str:
        """Evaluation log for a strategy."""
        if not _require(api_key):
            return _auth_error()
        from src.services.trade_log import list_evaluations as svc
        return json.dumps([e.model_dump(mode="json") for e in
                           svc(strategy_id, limit=limit, offset=offset)])

    register_tool(ToolEntry(
        name="agent_list_evaluations",
        description="Evaluation log for a strategy.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Strategy UUID"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_list_trades")
    async def list_trades(strategy_id: str, limit: int = 50,
                          offset: int = 0, api_key: str = "") -> str:
        """Trades for a strategy."""
        if not _require(api_key):
            return _auth_error()
        from src.services.trade_log import list_trades as svc
        return json.dumps([t.model_dump(mode="json") for t in
                           svc(strategy_id, limit=limit, offset=offset)])

    register_tool(ToolEntry(
        name="agent_list_trades",
        description="Trades for a strategy.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Strategy UUID"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool(name="agent_list_all_trades")
    async def list_all_trades(limit: int = 50,
                               strategy_id: str | None = None,
                               mode: str | None = None,
                               api_key: str = "") -> str:
        """All trades across strategies."""
        if not _require(api_key):
            return _auth_error()
        from src.services.trade_log import list_all_trades as svc
        return json.dumps([t.model_dump(mode="json") for t in
                           svc(limit=limit, strategy_id=strategy_id, mode=mode)])  # type: ignore[arg-type]

    register_tool(ToolEntry(
        name="agent_list_all_trades",
        description="All trades across strategies (optional filters).",
        access="auth",
        parameters=[
            ToolParam(name="limit", type="integer", required=False, description="Max results"),
            ToolParam(name="strategy_id", type="string", required=False, description="Filter"),
            ToolParam(name="mode", type="string", required=False, description="live | paper"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# Knowledge Base (auth)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Oracle (auth) — SIEVE + data query + Oracle backtest
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# x402 demo (unchanged)
# ---------------------------------------------------------------------------


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
        # Degraded registration: keep the tool in the catalog so discovery is
        # stable, but make the call return an actionable error rather than
        # silently giving away the paid resource or 500-ing.
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
