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

from mcp.server.fastmcp import FastMCP

from src.mcp.registry import ToolEntry, ToolParam, clear_tools, register_tool
from src.shared.auth.middleware import get_request_api_key, has_valid_api_key
from src.shared.errors import AgentError
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


def _handle_agent_error(e: AgentError) -> str:
    return json.dumps(e.to_dict())


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


# Shorthand for the "api_key required" parameter in the discovery catalog.
_APIKEY = ToolParam(name="api_key", type="string", required=True, description="Valid API key")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(server: FastMCP):
    """Register all agent MCP tools + the x402 demo tool."""
    clear_tools()
    _register_discovery(server)
    _register_wallet(server)
    _register_dex(server)
    _register_market(server)
    _register_signals(server)
    _register_on_chain(server)
    _register_defi(server)
    _register_social(server)
    _register_docs(server)
    _register_strategy(server)
    _register_logs(server)
    _register_kb(server)
    _register_oracle(server)
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
        EVM only in v1. Base mainnet (chain_id 8453) is the default.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.wallet_manager import create_wallet as svc
            result = svc(chain=chain, network=network, chain_id=chain_id, label=label)
            return json.dumps(result.model_dump(mode="json"))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="create_wallet",
        description=(
            "Create + encrypt a wallet. Response carries only vault_token + "
            "reveal_cmd — plaintext never enters the Claude Code transcript. "
            "EVM only in v1."
        ),
        access="auth",
        parameters=[
            ToolParam(name="chain", type="string", required=False, description="evm (default). xrpl stubbed 501 in v1."),
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

        Do NOT accept a raw private key or mnemonic as input to this tool,
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
        except AgentError as e:
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
            ToolParam(name="chain", type="string", required=False, description="evm (default)"),
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
        except AgentError as e:
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
            return _err("PORTFOLIO_VALUE_FAILED", str(e))

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
            return _err("PORTFOLIO_PNL_FAILED", str(e))

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
            return _err("PORTFOLIO_TOKENS_FAILED", str(e))

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
            return _err("PORTFOLIO_DEFI_FAILED", str(e))

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

        Different from our local `list_trades` (which covers strategy-
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
            return _err("PORTFOLIO_HISTORY_FAILED", str(e))

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
        return json.dumps({"error": True, "code": "CEX_ERROR", "message": str(e)})

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
        except AgentError as e:
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
        except AgentError as e:
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
            ToolParam(name="confirm", type="boolean", required=True, description="Must be true"),
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
            return _err("DEX_TX_STATUS_FAILED", str(e))

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
            return _err("DEX_TOKEN_INFO_FAILED", str(e))

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
            return _err("DEX_SPOT_PRICE_FAILED", str(e))

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
            return _err("DEX_GAS_PRICE_FAILED", str(e))

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
            return _err("DEX_TOKEN_SEARCH_FAILED", str(e))

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
            return _err("DEX_CHART_FAILED", str(e))

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
            return _err("DEX_ALLOWANCES_FAILED", str(e))

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


def _register_market(server: FastMCP) -> None:
    @server.tool()
    async def get_ohlcv(symbol: str, lookback_days: int = 30,
                        provider: str | None = None,
                        api_key: str = "") -> str:
        """OHLCV bars for an asset.

        Thin wrapper over `mangroveai.crypto_assets.get_ohlcv(symbol, days,
        provider)`. The SDK does NOT accept a timeframe — the upstream
        endpoint returns the provider's native bar granularity (1h for
        most). A previous version of this tool advertised a `timeframe`
        parameter; it was silently dropped by the SDK. Removed to stop
        misleading callers. To backtest at a *specific* timeframe, use the
        Oracle datasets / backtest tools (`oracle_list_datasets`,
        `oracle_backtest`, `backtest_strategy`), which are timeframe-aware.
        """
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        kwargs: dict[str, Any] = {"symbol": symbol, "days": lookback_days}
        if provider is not None:
            kwargs["provider"] = provider
        result = mangrove_ai_client().crypto_assets.get_ohlcv(**kwargs)
        return json.dumps(_dump(result))

    register_tool(ToolEntry(
        name="get_ohlcv",
        description=(
            "OHLCV bars for an asset. Bar granularity is set by the "
            "data provider (typically 1h). No `timeframe` parameter — "
            "the SDK / upstream endpoint don't support overriding bar "
            "size at this call site."
        ),
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol (e.g. BTC, ETH)"),
            ToolParam(name="lookback_days", type="integer", required=False, description="History window in days (default 30)"),
            ToolParam(name="provider", type="string", required=False, description="Optional CEX provider override"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_market_data(
        symbol: str, provider: str | None = None, api_key: str = "",
    ) -> str:
        """Current market data for an asset.

        Thin wrapper over `mangroveai.crypto_assets.get_market_data(symbol,
        *, provider)`. `provider` selects a specific data source; omit to
        use the SDK default.
        """
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        kwargs: dict[str, Any] = {"symbol": symbol}
        if provider is not None:
            kwargs["provider"] = provider
        return json.dumps(_dump(mangrove_ai_client().crypto_assets.get_market_data(**kwargs)))

    register_tool(ToolEntry(
        name="get_market_data",
        description="Current price, market cap, volume, 24h/7d change. Optionally pin a provider.",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol"),
            ToolParam(name="provider", type="string", required=False, description="Optional data provider override"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_trending(api_key: str = "") -> str:
        """Current trending crypto assets.

        Pass-through to `mangroveai.crypto_assets.get_trending()`.
        Useful for "what's hot right now" quick-glance. No filters.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            return json.dumps(_dump(mangrove_ai_client().crypto_assets.get_trending()))
        except Exception as e:  # noqa: BLE001
            return _err("CRYPTO_TRENDING_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_trending",
        description="Trending crypto assets right now.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def list_approved_assets(
        min_score: float | None = None, limit: int = 100,
        api_key: str = "",
    ) -> str:
        """List approved-universe crypto assets (with optional min_score filter).

        Defaults to approved_only=True (the safe curated subset).
        Use this to see what's tradeable.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            kwargs: dict[str, Any] = {"approved_only": True, "limit": limit}
            if min_score is not None:
                kwargs["min_score"] = min_score
            items = mangrove_ai_client().crypto_assets.list(**kwargs)
            return json.dumps([_dump(i) for i in items])
        except Exception as e:  # noqa: BLE001
            return _err("CRYPTO_LIST_FAILED", str(e))

    register_tool(ToolEntry(
        name="list_approved_assets",
        description="Approved crypto asset universe (agent-safe set).",
        access="auth",
        parameters=[
            ToolParam(name="min_score", type="number", required=False, description="Optional quality threshold"),
            ToolParam(name="limit", type="integer", required=False, description="Max results (default 100)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_asset(symbol: str, api_key: str = "") -> str:
        """Single-asset detail (score, approval state, metadata).

        More focused than get_market_data — returns the MangroveAI
        approval/score/categorization rather than price/volume.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            return json.dumps(_dump(mangrove_ai_client().crypto_assets.get(symbol)))
        except Exception as e:  # noqa: BLE001
            return _err("CRYPTO_GET_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_asset",
        description="Single-asset metadata + score + approval state.",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol (e.g. BTC, ETH)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_global_market(api_key: str = "") -> str:
        """Global market overview (total market cap, BTC dominance, 24h change).

        Context tool — useful when the agent wants to ground a
        "market regime" observation before recommending strategies.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            return json.dumps(_dump(mangrove_ai_client().crypto_assets.get_global_market()))
        except Exception as e:  # noqa: BLE001
            return _err("CRYPTO_GLOBAL_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_global_market",
        description="Global market overview (total cap, BTC dominance, 24h change).",
        access="auth",
        parameters=[_APIKEY],
    ))


# ---------------------------------------------------------------------------
# Signals (auth)
# ---------------------------------------------------------------------------


def _register_signals(server: FastMCP) -> None:
    @server.tool()
    async def list_signals(category: str | None = None, search: str | None = None,
                           limit: int = 50, api_key: str = "") -> str:
        """List available signals (optionally filtered by category or search)."""
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        client = mangrove_ai_client()
        if search:
            from mangrove_ai.models import SearchSignalsRequest
            page = client.signals.search(SearchSignalsRequest(query=search, limit=limit))
            items = [_dump(s) for s in getattr(page, "items", [])]
        else:
            all_signals = list(client.signals.list_iter(limit_per_page=min(limit, 100)))
            items = [_dump(s) for s in all_signals[:limit]]
        if category:
            items = [s for s in items if (s.get("category") or "").lower() == category.lower()]
        return json.dumps({"items": items, "total": len(items)})

    register_tool(ToolEntry(
        name="list_signals",
        description="List / search available signals.",
        access="auth",
        parameters=[
            ToolParam(name="category", type="string", required=False, description="Filter by category"),
            ToolParam(name="search", type="string", required=False, description="Search query"),
            ToolParam(name="limit", type="integer", required=False, description="Max results"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_signal(signal_name: str, api_key: str = "") -> str:
        """Fetch a single signal's full metadata (params, description, category).

        More detail than list_signals for a single name. Useful when the
        agent knows which signal it wants but needs the parameter schema
        before writing a strategy rule.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            return json.dumps(_dump(mangrove_ai_client().signals.get(signal_name)))
        except Exception as e:  # noqa: BLE001
            return _err("SIGNAL_GET_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_signal",
        description="Fetch a single signal's full metadata + param schema.",
        access="auth",
        parameters=[
            ToolParam(name="signal_name", type="string", required=True, description="Exact signal name (e.g. 'rsi_cross_up')"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def match_signals(
        description: str, top_k: int = 5,
        similarity_threshold: float = 0.5,
        api_key: str = "",
    ) -> str:
        """Semantic match: find signals matching a natural-language description.

        Backs /create-strategy Phase C: the agent has a user idea
        ('bullish momentum on liquid crypto'), calls this to find
        candidate signals, then falls through to kb_search for
        parameter guidance. Higher-quality than text search over
        signal names.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            r = mangrove_ai_client().signals.match(
                description=description, top_k=top_k,
                similarity_threshold=similarity_threshold,
            )
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("SIGNAL_MATCH_FAILED", str(e))

    register_tool(ToolEntry(
        name="match_signals",
        description="Semantic match of signals against a natural-language description.",
        access="auth",
        parameters=[
            ToolParam(name="description", type="string", required=True, description="Natural-language description of what you want"),
            ToolParam(name="top_k", type="integer", required=False, description="Max results (default 5)"),
            ToolParam(name="similarity_threshold", type="number", required=False, description="Min similarity (default 0.5)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def search_signals(query: str, limit: int = 50, offset: int = 0,
                             api_key: str = "") -> str:
        """Text search over signals (complements list_signals's exhaustive iteration).

        list_signals paginates the full catalog; search_signals filters
        by a text query server-side. Prefer match_signals for
        description-level intent; use this for name / keyword search.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from mangrove_ai.models import SearchSignalsRequest

            from src.shared.clients.mangrove import mangrove_ai_client
            req = SearchSignalsRequest(query=query, limit=limit, offset=offset)
            page = mangrove_ai_client().signals.search(req)
            items = [_dump(s) for s in getattr(page, "items", [])]
            return json.dumps({
                "items": items,
                "total": getattr(page, "total", len(items)),
                "limit": limit, "offset": offset,
            })
        except Exception as e:  # noqa: BLE001
            return _err("SIGNAL_SEARCH_FAILED", str(e))

    register_tool(ToolEntry(
        name="search_signals",
        description="Text search signals (keyword/name). For intent-based matching, prefer match_signals.",
        access="auth",
        parameters=[
            ToolParam(name="query", type="string", required=True, description="Text query"),
            ToolParam(name="limit", type="integer", required=False, description="Page size (default 50)"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# On-chain intelligence (auth)
# ---------------------------------------------------------------------------


def _register_on_chain(server: FastMCP) -> None:
    """Whale activity, smart-money sentiment, token holders, exchange flows.

    These tools back the /create-strategy skill's "cite Mangrove
    intelligence" rule — they provide the 'why now' evidence for a
    candidate strategy.
    """
    from src.shared.clients.mangrove import mangrove_ai_client

    @server.tool()
    async def get_whale_activity(
        symbol: str, hours_back: int = 24, api_key: str = "",
    ) -> str:
        """Whale buying/selling activity for an asset over the last N hours."""
        if not _require(api_key):
            return _auth_error()
        try:
            r = mangrove_ai_client().on_chain.get_whale_activity(
                symbol=symbol, hours_back=hours_back,
            )
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_WHALE_ACTIVITY_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_whale_activity",
        description="Whale buying/selling activity for an asset.",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol (e.g. ETH)"),
            ToolParam(name="hours_back", type="integer", required=False, description="Window (default 24)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_whale_transactions(
        symbol: str | None = None, min_value: float = 500_000,
        hours_back: int = 24, api_key: str = "",
    ) -> str:
        """Individual whale transactions above min_value USD."""
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {
                "min_value": min_value, "hours_back": hours_back,
            }
            if symbol is not None:
                kwargs["symbol"] = symbol
            r = mangrove_ai_client().on_chain.get_whale_transactions(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_WHALE_TXS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_whale_transactions",
        description="Whale transactions above a USD threshold.",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=False, description="Optional filter by asset"),
            ToolParam(name="min_value", type="number", required=False, description="Min USD value (default 500000)"),
            ToolParam(name="hours_back", type="integer", required=False, description="Window (default 24)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_smart_money_sentiment(
        symbol: str, chain: str | None = None, api_key: str = "",
    ) -> str:
        """Smart-money wallet sentiment for an asset (bullish/bearish signal).

        ⚠️ Upstream netflow data is currently unavailable for most
        assets on ethereum — real calls return 404 RESOURCE_NOT_FOUND
        with a clear 'No Smart Money netflow data found' message.
        Tool wiring is correct; upstream data pipeline issue.
        Pair this with get_whale_activity / get_exchange_flows while
        the data source is being populated.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"symbol": symbol}
            if chain is not None:
                kwargs["chain"] = chain
            r = mangrove_ai_client().on_chain.get_smart_money_sentiment(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_SMART_MONEY_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_smart_money_sentiment",
        description="Smart-money sentiment for an asset (aggregate of tracked wallets).",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol"),
            ToolParam(name="chain", type="string", required=False, description="Optional chain filter"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def screen_smart_money(
        chains: list[str] | None = None, timeframe: str = "24h",
        limit: int = 20, api_key: str = "",
    ) -> str:
        """Discover which assets smart money is currently accumulating."""
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"timeframe": timeframe, "limit": limit}
            if chains is not None:
                kwargs["chains"] = chains
            r = mangrove_ai_client().on_chain.screen_smart_money(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_SMART_MONEY_SCREEN_FAILED", str(e))

    register_tool(ToolEntry(
        name="screen_smart_money",
        description="Screen assets smart money is currently accumulating.",
        access="auth",
        parameters=[
            ToolParam(name="chains", type="array", required=False, description="Optional list of chain names"),
            ToolParam(name="timeframe", type="string", required=False, description="Lookback (default '24h')"),
            ToolParam(name="limit", type="integer", required=False, description="Max results (default 20)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_token_holders(symbol: str, api_key: str = "") -> str:
        """Top holders + distribution metrics for a token."""
        if not _require(api_key):
            return _auth_error()
        try:
            r = mangrove_ai_client().on_chain.get_token_holders(symbol=symbol)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_HOLDERS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_token_holders",
        description="Top holders + distribution for a token.",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Asset symbol"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_exchange_flows(
        symbol: str | None = None, hours_back: int = 24, api_key: str = "",
    ) -> str:
        """Net exchange inflows / outflows (risk-off vs risk-on proxy).

        Inflows to exchanges ≈ selling pressure; outflows ≈ accumulation.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"hours_back": hours_back}
            if symbol is not None:
                kwargs["symbol"] = symbol
            r = mangrove_ai_client().on_chain.get_exchange_flows(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_EXCHANGE_FLOWS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_exchange_flows",
        description="Net exchange inflows/outflows (selling pressure vs accumulation).",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=False, description="Optional filter by asset"),
            ToolParam(name="hours_back", type="integer", required=False, description="Window (default 24)"),
            _APIKEY,
        ],
    ))

    # ----------------------------------------------------------------- #
    # Nansen Pro coverage (5 endpoints added in mangroveai 1.1.0)
    # All take optional `filters` / `order_by` dicts passed straight
    # through to Nansen — give agents the full Pro plan reach (Fund-
    # labelled wallets, side-filtered DEX trades, etc.).
    # ----------------------------------------------------------------- #

    @server.tool()
    async def get_smart_money_historical_holdings(
        chains: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        filters: dict[str, Any] | None = None,
        order_by: list[dict[str, str]] | None = None,
        page: int = 1,
        per_page: int = 100,
        api_key: str = "",
    ) -> str:
        """Date-stamped Smart Money holdings snapshots across chains (Nansen).

        Show how Fund/VC/CEX-labelled wallets shifted positions over a
        window. Use with `filters={"include_smart_money_labels": ["Fund"]}`
        to restrict to a single label class.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"page": page, "per_page": per_page}
            for k, v in (("chains", chains), ("date_from", date_from), ("date_to", date_to),
                         ("filters", filters), ("order_by", order_by)):
                if v is not None:
                    kwargs[k] = v
            r = mangrove_ai_client().on_chain.get_smart_money_historical_holdings(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_SM_HISTORICAL_HOLDINGS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_smart_money_historical_holdings",
        description="Smart Money historical holdings snapshots across chains (Nansen).",
        access="auth",
        parameters=[
            ToolParam(name="chains", type="array", required=False, description="Chain filter, e.g. ['ethereum', 'solana']. Default ['ethereum']."),
            ToolParam(name="date_from", type="string", required=False, description="ISO date 'YYYY-MM-DD'."),
            ToolParam(name="date_to", type="string", required=False, description="ISO date 'YYYY-MM-DD'."),
            ToolParam(name="filters", type="object", required=False, description="Nansen filter dict (include_smart_money_labels, etc.)"),
            ToolParam(name="order_by", type="array", required=False, description="Sort spec, e.g. [{'field': 'block_timestamp', 'direction': 'DESC'}]"),
            ToolParam(name="page", type="integer", required=False, description="Page (default 1)"),
            ToolParam(name="per_page", type="integer", required=False, description="Items per page (default 100)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_smart_money_dex_trades(
        chains: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        order_by: list[dict[str, str]] | None = None,
        page: int = 1,
        per_page: int = 100,
        api_key: str = "",
    ) -> str:
        """Recent DEX trades from Smart Money wallets (Nansen).

        Filters accept: ``include_smart_money_labels``, ``token_address``,
        ``side`` ('buy' | 'sell'), ``min_amount_usd``.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"page": page, "per_page": per_page}
            for k, v in (("chains", chains), ("filters", filters), ("order_by", order_by)):
                if v is not None:
                    kwargs[k] = v
            r = mangrove_ai_client().on_chain.get_smart_money_dex_trades(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_SM_DEX_TRADES_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_smart_money_dex_trades",
        description="Recent DEX trades from Smart Money wallets (Nansen).",
        access="auth",
        parameters=[
            ToolParam(name="chains", type="array", required=False, description="Chain filter."),
            ToolParam(name="filters", type="object", required=False, description="Nansen filter dict."),
            ToolParam(name="order_by", type="array", required=False, description="Sort spec."),
            ToolParam(name="page", type="integer", required=False, description="Page (default 1)."),
            ToolParam(name="per_page", type="integer", required=False, description="Items per page (default 100)."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_smart_money_perp_trades(
        filters: dict[str, Any] | None = None,
        order_by: list[dict[str, str]] | None = None,
        page: int = 1,
        per_page: int = 100,
        api_key: str = "",
    ) -> str:
        """Perpetual-futures trades from Smart Money on Hyperliquid (Nansen).

        Hyperliquid-only; upstream doesn't accept a chain filter.

        Filters accept: ``action``, ``side`` ('Long' | 'Short'),
        ``token_symbol``, ``type`` ('Market' | 'Limit'),
        ``value_usd`` ({min, max}), ``only_new_positions``.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"page": page, "per_page": per_page}
            for k, v in (("filters", filters), ("order_by", order_by)):
                if v is not None:
                    kwargs[k] = v
            r = mangrove_ai_client().on_chain.get_smart_money_perp_trades(**kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_SM_PERP_TRADES_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_smart_money_perp_trades",
        description="Smart Money perp trades on Hyperliquid (Nansen).",
        access="auth",
        parameters=[
            ToolParam(name="filters", type="object", required=False, description="Nansen filter dict."),
            ToolParam(name="order_by", type="array", required=False, description="Sort spec."),
            ToolParam(name="page", type="integer", required=False, description="Page (default 1)."),
            ToolParam(name="per_page", type="integer", required=False, description="Items per page (default 100)."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_token_dex_trades(
        symbol: str,
        chain: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        filters: dict[str, Any] | None = None,
        order_by: list[dict[str, str]] | None = None,
        page: int = 1,
        per_page: int = 100,
        api_key: str = "",
    ) -> str:
        """All DEX trades on a single token in a date window (Nansen).

        Token-scoped (not wallet-scoped) — sees every counterparty, not
        just Smart Money. Useful for liquidity / activity diagnostics.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"page": page, "per_page": per_page}
            for k, v in (("chain", chain), ("date_from", date_from), ("date_to", date_to),
                         ("filters", filters), ("order_by", order_by)):
                if v is not None:
                    kwargs[k] = v
            r = mangrove_ai_client().on_chain.get_token_dex_trades(symbol, **kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_TOKEN_DEX_TRADES_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_token_dex_trades",
        description="All DEX trades for a single token in a date window (Nansen).",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Token symbol (e.g. 'uniswap')."),
            ToolParam(name="chain", type="string", required=False, description="Chain (default 'ethereum')."),
            ToolParam(name="date_from", type="string", required=False, description="ISO 'YYYY-MM-DD'."),
            ToolParam(name="date_to", type="string", required=False, description="ISO 'YYYY-MM-DD'."),
            ToolParam(name="filters", type="object", required=False, description="Nansen filter dict."),
            ToolParam(name="order_by", type="array", required=False, description="Sort spec."),
            ToolParam(name="page", type="integer", required=False, description="Page (default 1)."),
            ToolParam(name="per_page", type="integer", required=False, description="Items per page (default 100)."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_token_flows(
        symbol: str,
        chain: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        filters: dict[str, Any] | None = None,
        order_by: list[dict[str, str]] | None = None,
        page: int = 1,
        per_page: int = 100,
        api_key: str = "",
    ) -> str:
        """Per-wallet-category flow data for a token in a date window (Nansen).

        Aggregates trades by trader category (Fund, CEX, Smart Trader,
        etc.) over each date. **Stablecoins are not supported** — Nansen
        returns 404.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            kwargs: dict[str, Any] = {"page": page, "per_page": per_page}
            for k, v in (("chain", chain), ("date_from", date_from), ("date_to", date_to),
                         ("filters", filters), ("order_by", order_by)):
                if v is not None:
                    kwargs[k] = v
            r = mangrove_ai_client().on_chain.get_token_flows(symbol, **kwargs)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("ONCHAIN_TOKEN_FLOWS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_token_flows",
        description="Per-wallet-category flow data for a token across a date window (Nansen).",
        access="auth",
        parameters=[
            ToolParam(name="symbol", type="string", required=True, description="Token symbol (non-stablecoin)."),
            ToolParam(name="chain", type="string", required=False, description="Chain (default 'ethereum')."),
            ToolParam(name="date_from", type="string", required=False, description="ISO 'YYYY-MM-DD'."),
            ToolParam(name="date_to", type="string", required=False, description="ISO 'YYYY-MM-DD'."),
            ToolParam(name="filters", type="object", required=False, description="Nansen filter dict."),
            ToolParam(name="order_by", type="array", required=False, description="Sort spec."),
            ToolParam(name="page", type="integer", required=False, description="Page (default 1)."),
            ToolParam(name="per_page", type="integer", required=False, description="Items per page (default 100)."),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# DeFi (auth)
# ---------------------------------------------------------------------------


def _register_defi(server: FastMCP) -> None:
    """Macro DeFi metrics (TVL, stablecoin supply) + DeFiLlama Pro signals.

    The Pro tools (token unlocks, perp funding, treasuries, ETF flows, lending
    rates) require the caller's plan to include DeFi Pro (Pro / Startup /
    Enterprise). On an unentitled plan the underlying call returns 403 and the
    tool surfaces a structured error advising an upgrade.
    """
    from src.shared.clients.mangrove import mangrove_ai_client

    @server.tool()
    async def get_chain_tvl(chain: str, api_key: str = "") -> str:
        """Total value locked in DeFi on a given chain."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_chain_tvl(chain=chain)))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_CHAIN_TVL_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_chain_tvl",
        description="Total value locked (TVL) in DeFi on a given chain.",
        access="auth",
        parameters=[
            ToolParam(name="chain", type="string", required=True, description="Chain (e.g. 'base', 'ethereum')"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_protocol_tvl(protocol: str, api_key: str = "") -> str:
        """Total value locked in a specific DeFi protocol."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_protocol_tvl(protocol=protocol)))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_PROTOCOL_TVL_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_protocol_tvl",
        description="TVL for a specific DeFi protocol (e.g. 'aave', 'uniswap').",
        access="auth",
        parameters=[
            ToolParam(name="protocol", type="string", required=True, description="Protocol slug"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_stablecoin_metrics(api_key: str = "") -> str:
        """Stablecoin supply + flow metrics (macro liquidity proxy)."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_stablecoin_metrics()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_STABLECOIN_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_stablecoin_metrics",
        description="Stablecoin supply + flow metrics (macro liquidity proxy).",
        access="auth",
        parameters=[_APIKEY],
    ))

    # --- DeFiLlama Pro (require a Pro / Startup / Enterprise plan) -----------

    @server.tool()
    async def get_token_unlocks(api_key: str = "") -> str:
        """Token unlock schedules + supply metrics (supply-shock signal). Pro plan."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_token_unlocks()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_TOKEN_UNLOCKS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_token_unlocks",
        description="Token unlock schedules + supply metrics across tokens (tradeable supply-shock signal). Requires a Pro/Startup/Enterprise plan.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def get_perp_funding(api_key: str = "") -> str:
        """Aggregated DeFi perpetual funding rates across venues. Pro plan."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_perp_funding()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_PERP_FUNDING_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_perp_funding",
        description="Aggregated DeFi perpetual funding rates across venues. Requires a Pro/Startup/Enterprise plan.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def get_treasuries(api_key: str = "") -> str:
        """Protocol treasury holdings (crowd-positioning signal). Pro plan."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_treasuries()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_TREASURIES_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_treasuries",
        description="Protocol treasury holdings (crowd-positioning signal). Requires a Pro/Startup/Enterprise plan.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def get_etf_flows(api_key: str = "") -> str:
        """Crypto ETF net flows (institutional flow signal). Pro plan."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_etf_flows()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_ETF_FLOWS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_etf_flows",
        description="Crypto ETF net flows (institutional flow signal; daily BTC ETF flows correlate with spot). Requires a Pro/Startup/Enterprise plan.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def get_lending_borrow_rates(api_key: str = "") -> str:
        """Lending-pool borrow rates (rate-spread features). Pro plan."""
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().defi.get_lending_borrow_rates()))
        except Exception as e:  # noqa: BLE001
            return _err("DEFI_LENDING_RATES_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_lending_borrow_rates",
        description="DeFi lending-pool borrow rates (rate-spread features). Requires a Pro/Startup/Enterprise plan.",
        access="auth",
        parameters=[_APIKEY],
    ))


# ---------------------------------------------------------------------------
# Social (auth)
# ---------------------------------------------------------------------------


def _register_social(server: FastMCP) -> None:
    """Twitter/X sentiment + influence + mentions. Experimental context."""
    from src.shared.clients.mangrove import mangrove_ai_client

    @server.tool()
    async def get_sentiment(
        topic: str, hours_back: int = 24, api_key: str = "",
    ) -> str:
        """Aggregate social sentiment for a topic (asset symbol or keyword)."""
        if not _require(api_key):
            return _auth_error()
        try:
            r = mangrove_ai_client().social.get_sentiment(topic=topic, hours_back=hours_back)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("SOCIAL_SENTIMENT_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_sentiment",
        description="Aggregate social (X/Twitter) sentiment for a topic or asset.",
        access="auth",
        parameters=[
            ToolParam(name="topic", type="string", required=True, description="Asset symbol or keyword"),
            ToolParam(name="hours_back", type="integer", required=False, description="Window (default 24)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_mentions(
        topic: str, hours_back: int = 24, limit: int = 20, api_key: str = "",
    ) -> str:
        """Recent social mentions of a topic (raw posts)."""
        if not _require(api_key):
            return _auth_error()
        try:
            r = mangrove_ai_client().social.get_mentions(
                topic=topic, hours_back=hours_back, limit=limit,
            )
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("SOCIAL_MENTIONS_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_mentions",
        description="Recent social mentions of a topic (raw posts).",
        access="auth",
        parameters=[
            ToolParam(name="topic", type="string", required=True, description="Asset symbol or keyword"),
            ToolParam(name="hours_back", type="integer", required=False, description="Window (default 24)"),
            ToolParam(name="limit", type="integer", required=False, description="Max posts (default 20)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_influence_score(username: str, api_key: str = "") -> str:
        """Influence score for a social username."""
        if not _require(api_key):
            return _auth_error()
        try:
            r = mangrove_ai_client().social.get_influence_score(username=username)
            return json.dumps(_dump(r))
        except Exception as e:  # noqa: BLE001
            return _err("SOCIAL_INFLUENCE_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_influence_score",
        description="Influence score for a social username.",
        access="auth",
        parameters=[
            ToolParam(name="username", type="string", required=True, description="Social username (no @)"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# Docs (auth)
# ---------------------------------------------------------------------------


def _register_docs(server: FastMCP) -> None:
    """MangroveAI developer docs (API reference + guides)."""
    from src.shared.clients.mangrove import mangrove_ai_client

    @server.tool()
    async def list_docs(api_key: str = "") -> str:
        """List MangroveAI developer docs.

        ⚠️ Upstream returns 404 'Documentation directory not found'
        as of 2026-04-23. Tool wiring is correct; upstream docs
        directory is either missing or mis-configured server-side.
        Falls through cleanly if the docs come back online.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            items = mangrove_ai_client().docs.list()
            return json.dumps([_dump(i) for i in items])
        except Exception as e:  # noqa: BLE001
            return _err("DOCS_LIST_FAILED", str(e))

    register_tool(ToolEntry(
        name="list_docs",
        description="List MangroveAI developer docs (API reference + guides).",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def get_doc_content(path: str, api_key: str = "") -> str:
        """Fetch a MangroveAI doc by path.

        Different from kb_get_document (KB content DB). This hits the
        MangroveAI developer documentation — API reference, SDK migration
        guides, etc.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            return json.dumps(_dump(mangrove_ai_client().docs.get_content(path=path)))
        except Exception as e:  # noqa: BLE001
            return _err("DOCS_GET_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_doc_content",
        description="Fetch a MangroveAI developer doc by path (API reference, guides).",
        access="auth",
        parameters=[
            ToolParam(name="path", type="string", required=True, description="Doc path (from list_docs)"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# Strategy (auth)
# ---------------------------------------------------------------------------


def _register_strategy(server: FastMCP) -> None:
    @server.tool()
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
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="create_strategy_autonomous",
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

    @server.tool()
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
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="create_strategy_manual",
        description="Create a strategy with explicit entry/exit rules.",
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

    @server.tool()
    async def search_reference_strategies(
        asset: str,
        timeframe: str | None = None,
        category: str | None = None,
        goal_hint: str | None = None,
        limit: int = 5,
        api_key: str = "",
    ) -> str:
        """Search curated reference strategies — Mechanism 2 of /create-strategy.

        The agent calls this BEFORE picking signals/params manually. Each
        returned reference has known-good entry/exit signals + parameter
        choices. The agent picks one that matches user intent, then calls
        build_strategy_from_reference to materialize it.

        Ranks by specificity: asset+timeframe+category > asset+timeframe
        > asset > category. Auto-detects category from goal_hint if not
        supplied.
        """
        if not _require(api_key):
            return _auth_error()
        from src.services import reference_strategies_service
        items = reference_strategies_service.search(
            asset=asset,
            timeframe=timeframe,
            category=category,
            goal_hint=goal_hint,
            limit=limit,
        )
        return json.dumps({
            "asset": asset.upper(),
            "timeframe": timeframe,
            "category": category,
            "count": len(items),
            "strategies": [r.model_dump() for r in items],
        })

    register_tool(ToolEntry(
        name="search_reference_strategies",
        description=(
            "Find curated reference strategies that match the user's goal "
            "and asset. Returns ranked candidates with signals + parameter "
            "choices that have worked in backtests. ALWAYS call this "
            "before picking signals manually — it's the primary source of "
            "parameter intuition."
        ),
        access="auth",
        parameters=[
            ToolParam(name="asset", type="string", required=True, description="Asset symbol (e.g. BTC, ETH)"),
            ToolParam(name="timeframe", type="string", required=False, description="5m | 15m | 30m | 1h | 4h | 1d"),
            ToolParam(name="category", type="string", required=False, description="momentum | mean_reversion | trend_following | breakout | volatility"),
            ToolParam(name="goal_hint", type="string", required=False, description="Free text from the user's goal — auto-detects category if category is not supplied"),
            ToolParam(name="limit", type="integer", required=False, description="Max results (default 5)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def build_strategy_from_reference(
        reference_id: str,
        timeframe: str | None = None,
        asset: str | None = None,
        name: str | None = None,
        api_key: str = "",
    ) -> str:
        """Materialize a reference into a create_strategy_manual payload.

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
        name="build_strategy_from_reference",
        description=(
            "After search_reference_strategies returns candidates, call this "
            "to produce a create_strategy_manual payload. Signals and params "
            "are copied exactly — the agent must NOT modify them. `timeframe` "
            "and `asset` are free overrides: a reference is a portable combo, "
            "so retarget onto the user's asset/TF and bulk-backtest the top "
            "matches rather than single-pick by label."
        ),
        access="auth",
        parameters=[
            ToolParam(name="reference_id", type="string", required=True, description="e.g. ref-001 — from search_reference_strategies"),
            ToolParam(name="timeframe", type="string", required=False, description="Override the reference's timeframe (canonicalized)"),
            ToolParam(name="asset", type="string", required=False, description="Retarget onto a different asset — reference strategies are portable"),
            ToolParam(name="name", type="string", required=False, description="Optional strategy name override"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def list_strategies(status: str | None = None, limit: int = 50,
                              offset: int = 0, api_key: str = "") -> str:
        """List strategies, optionally filtered by status."""
        if not _require(api_key):
            return _auth_error()
        from src.services.strategy_service import list_strategies as svc
        items = svc(status=status, limit=limit, offset=offset)
        return json.dumps([s.model_dump(mode="json") for s in items])

    register_tool(ToolEntry(
        name="list_strategies",
        description="List strategies.",
        access="auth",
        parameters=[
            ToolParam(name="status", type="string", required=False, description="Filter: draft|inactive|paper|live|archived"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_strategy(strategy_id: str, api_key: str = "") -> str:
        """Get a strategy by ID."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import get_strategy as svc
            return json.dumps(svc(strategy_id).model_dump(mode="json"))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="get_strategy",
        description="Get a strategy by ID.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            _APIKEY,
        ],
    ))

    @server.tool()
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
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="update_strategy_status",
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

    @server.tool()
    async def backtest_strategy(
        strategy_id: str, mode: str = "full",
        lookback_months: int | None = None,
        lookback_days: int | None = None,
        lookback_hours: int | None = None,
        start_date: str | None = None, end_date: str | None = None,
        config: dict | None = None,
        api_key: str = "",
    ) -> str:
        """Run a backtest against an existing strategy (mode=quick|full).

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
            )))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="backtest_strategy",
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
            "BacktestRequest field. Returns full SDK metrics, trade "
            "history, and a resolved_window block for fallback detection."
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
            _APIKEY,
        ],
    ))

    @server.tool()
    async def evaluate_strategy(strategy_id: str, api_key: str = "") -> str:
        """Manually trigger a single evaluation tick."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.api.routes.strategies import evaluate
            return json.dumps(await evaluate(strategy_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="evaluate_strategy",
        description="Manually trigger one evaluation tick.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent strategy UUID"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def list_account_positions(
        account_id: str | None = None,
        status: str | None = None,
        skip: int = 0, limit: int = 100,
        api_key: str = "",
    ) -> str:
        """List positions on MangroveAI's execution side.

        Hits `mangroveai.execution.list_positions`. Note: our
        architecture executes trades locally via order_executor and
        writes to our own SQLite trades/evaluations — so our user's
        strategies don't populate MangroveAI execution accounts
        unless a strategy was authored through the MangroveAI copilot
        path. This tool is exposed for completeness + cases where a
        user has both mangrove-agent AND copilot-authored strategies.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            kwargs: dict[str, Any] = {"skip": skip, "limit": limit}
            if account_id is not None:
                kwargs["account_id"] = account_id
            if status is not None:
                kwargs["status"] = status
            items = mangrove_ai_client().execution.list_positions(**kwargs)
            return json.dumps([_dump(i) for i in items])
        except Exception as e:  # noqa: BLE001
            return _err("EXECUTION_POSITIONS_FAILED", str(e))

    register_tool(ToolEntry(
        name="list_account_positions",
        description="List positions on MangroveAI's execution side (copilot-authored strategies).",
        access="auth",
        parameters=[
            ToolParam(name="account_id", type="string", required=False, description="Optional filter"),
            ToolParam(name="status", type="string", required=False, description="Optional: open | closed | etc"),
            ToolParam(name="skip", type="integer", required=False, description="Page offset"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def get_account_position(position_id: str, api_key: str = "") -> str:
        """Fetch a single MangroveAI execution-side position by id."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            return json.dumps(_dump(mangrove_ai_client().execution.get_position(position_id)))
        except Exception as e:  # noqa: BLE001
            return _err("EXECUTION_POSITION_GET_FAILED", str(e))

    register_tool(ToolEntry(
        name="get_account_position",
        description="Fetch a single MangroveAI execution position.",
        access="auth",
        parameters=[
            ToolParam(name="position_id", type="string", required=True, description="Position id"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def list_account_trades(
        account_id: str | None = None,
        asset: str | None = None,
        outcome: str | None = None,
        skip: int = 0, limit: int = 100,
        api_key: str = "",
    ) -> str:
        """List trades on MangroveAI's execution side.

        Distinct from our local `list_trades` (which covers every
        DEX swap the agent executed, stored in our SQLite). This
        hits MangroveAI's copilot-execution trade log — different
        data source, different use case.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.shared.clients.mangrove import mangrove_ai_client
            kwargs: dict[str, Any] = {"skip": skip, "limit": limit}
            if account_id is not None:
                kwargs["account_id"] = account_id
            if asset is not None:
                kwargs["asset"] = asset
            if outcome is not None:
                kwargs["outcome"] = outcome
            items = mangrove_ai_client().execution.list_trades(**kwargs)
            return json.dumps([_dump(i) for i in items])
        except Exception as e:  # noqa: BLE001
            return _err("EXECUTION_TRADES_FAILED", str(e))

    register_tool(ToolEntry(
        name="list_account_trades",
        description="List trades on MangroveAI's execution side (copilot-authored strategies).",
        access="auth",
        parameters=[
            ToolParam(name="account_id", type="string", required=False, description="Optional filter"),
            ToolParam(name="asset", type="string", required=False, description="Optional asset filter"),
            ToolParam(name="outcome", type="string", required=False, description="Optional outcome filter"),
            ToolParam(name="skip", type="integer", required=False, description="Page offset"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def delete_strategy(strategy_id: str, api_key: str = "") -> str:
        """Delete a strategy upstream on MangroveAI.

        New users create throwaway strategies and will want
        to clean up. This hits mangroveai.strategies.delete — the
        upstream strategy row is removed. Our LOCAL SQLite cache of
        the strategy stays; the local row is harmless once the
        upstream is gone, and we'd prefer to preserve the audit
        trail for any trades/evaluations that referenced it.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.strategy_service import get_strategy
            from src.shared.clients.mangrove import mangrove_ai_client
            # Look up the mangrove_id from our local cache.
            detail = get_strategy(strategy_id)
            r = mangrove_ai_client().strategies.delete(detail.mangrove_id)
            return json.dumps(_dump(r))
        except AgentError as e:
            return _handle_agent_error(e)
        except Exception as e:  # noqa: BLE001
            return _err("STRATEGY_DELETE_FAILED", str(e))

    register_tool(ToolEntry(
        name="delete_strategy",
        description="Delete a strategy upstream (local audit trail preserved).",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Agent (local) strategy UUID"),
            _APIKEY,
        ],
    ))


# ---------------------------------------------------------------------------
# Logs (auth)
# ---------------------------------------------------------------------------


def _register_logs(server: FastMCP) -> None:
    @server.tool()
    async def list_evaluations(strategy_id: str, limit: int = 50,
                                offset: int = 0, api_key: str = "") -> str:
        """Evaluation log for a strategy."""
        if not _require(api_key):
            return _auth_error()
        from src.services.trade_log import list_evaluations as svc
        return json.dumps([e.model_dump(mode="json") for e in
                           svc(strategy_id, limit=limit, offset=offset)])

    register_tool(ToolEntry(
        name="list_evaluations",
        description="Evaluation log for a strategy.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Strategy UUID"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def list_trades(strategy_id: str, limit: int = 50,
                          offset: int = 0, api_key: str = "") -> str:
        """Trades for a strategy."""
        if not _require(api_key):
            return _auth_error()
        from src.services.trade_log import list_trades as svc
        return json.dumps([t.model_dump(mode="json") for t in
                           svc(strategy_id, limit=limit, offset=offset)])

    register_tool(ToolEntry(
        name="list_trades",
        description="Trades for a strategy.",
        access="auth",
        parameters=[
            ToolParam(name="strategy_id", type="string", required=True, description="Strategy UUID"),
            ToolParam(name="limit", type="integer", required=False, description="Page size"),
            ToolParam(name="offset", type="integer", required=False, description="Page offset"),
            _APIKEY,
        ],
    ))

    @server.tool()
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
        name="list_all_trades",
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


def _register_kb(server: FastMCP) -> None:
    @server.tool()
    async def kb_search(q: str, limit: int = 20, api_key: str = "") -> str:
        """Full-text search the knowledge base."""
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        return json.dumps(_dump(mangrove_ai_client().kb.search.query(q=q, limit=limit)))

    register_tool(ToolEntry(
        name="kb_search",
        description="Full-text KB search.",
        access="auth",
        parameters=[
            ToolParam(name="q", type="string", required=True, description="Search query"),
            ToolParam(name="limit", type="integer", required=False, description="Max results"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def kb_glossary_get(term: str, api_key: str = "") -> str:
        """Look up a single glossary term (definition + backlinks).

        Cheaper + more focused than kb_search when the agent already
        knows the exact term it wants. Backlinks field shows related
        indicators and documents.
        """
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        try:
            return json.dumps(_dump(mangrove_ai_client().kb.glossary.get(term)))
        except Exception as e:  # noqa: BLE001
            return _err("KB_GLOSSARY_FAILED", str(e))

    register_tool(ToolEntry(
        name="kb_glossary_get",
        description="Look up a KB glossary term (definition + backlinks).",
        access="auth",
        parameters=[
            ToolParam(name="term", type="string", required=True, description="Glossary term (exact match)"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def kb_get_document(slug: str, api_key: str = "") -> str:
        """Fetch a full KB document by slug.

        Use when kb_search surfaces a document and the agent needs the
        full body (not just the search snippet). Real documents run
        up to ~25k chars — use sparingly and cite specific sections.

        Response field: body lives under `content` (not `body`).
        """
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        try:
            return json.dumps(_dump(mangrove_ai_client().kb.documents.get(slug)))
        except Exception as e:  # noqa: BLE001
            return _err("KB_DOCUMENT_NOT_FOUND", str(e))

    register_tool(ToolEntry(
        name="kb_get_document",
        description="Fetch a KB document by slug (full body, not search snippet).",
        access="auth",
        parameters=[
            ToolParam(name="slug", type="string", required=True, description="Document slug (e.g. 'momentum-strategies')"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def kb_list_indicators(
        category: str | None = None, api_key: str = "",
    ) -> str:
        """List KB indicator docs. Optionally filter by category.

        Useful for the /create-strategy skill's Phase C: agent picks
        a signal from list_signals, then calls this to find the KB
        docs explaining that indicator family.

        Category values are TITLE-CASE. Known values (as of 2026-04-23,
        70 indicators total):
            "Patterns"   (27)   "Trend"       (15)
            "Momentum"   (11)   "Volume"       (9)
            "Volatility"  (5)   "Returns"      (3)
        Lowercase (e.g. "momentum") returns empty.
        """
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        kwargs: dict[str, Any] = {}
        if category is not None:
            kwargs["category"] = category
        try:
            return json.dumps([_dump(i) for i in mangrove_ai_client().kb.indicators.list(**kwargs)])
        except Exception as e:  # noqa: BLE001
            return _err("KB_INDICATORS_FAILED", str(e))

    register_tool(ToolEntry(
        name="kb_list_indicators",
        description="List KB indicator docs (optionally by category).",
        access="auth",
        parameters=[
            ToolParam(name="category", type="string", required=False, description="Filter: momentum | trend | mean_reversion | volatility | volume | pattern"),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def kb_list_tags(api_key: str = "") -> str:
        """List all KB tags — useful for navigation or kb_search filtering."""
        if not _require(api_key):
            return _auth_error()
        from src.shared.clients.mangrove import mangrove_ai_client
        try:
            return json.dumps([_dump(t) for t in mangrove_ai_client().kb.tags.list()])
        except Exception as e:  # noqa: BLE001
            return _err("KB_TAGS_FAILED", str(e))

    register_tool(ToolEntry(
        name="kb_list_tags",
        description="List KB tags (navigation + kb_search filtering).",
        access="auth",
        parameters=[_APIKEY],
    ))


# ---------------------------------------------------------------------------
# Oracle (auth) — SIEVE + data query + Oracle backtest
# ---------------------------------------------------------------------------


def _register_oracle(server: FastMCP) -> None:
    """SIEVE scoring + curated corpus query + Oracle backtest tools.

    All three are auth-gated. The agent forwards to the mangrove-ai SDK's
    `client.oracle.*` surface, which proxies through MangroveAI's
    `/api/v1/oracle/*` to MangroveOracle.
    """

    @server.tool()
    async def sieve_score(
        strategies: list[dict[str, Any]],
        api_key: str = "",
    ) -> str:
        """Score up to 99 candidate strategies through the Mangrove SIEVE
        classifier. Returns binary go/no-go and 4-class outcome
        probabilities per strategy, with `model_version` + `code_version`
        for provenance.

        Use BEFORE paying for backtests: SIEVE cheaply rules out
        strategies the model predicts will produce no trades, win nothing,
        or lose. Then backtest only the survivors.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import SieveScoreInput
            from src.services.oracle import sieve_score as svc
            result = svc(SieveScoreInput(strategies=strategies))
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="sieve_score",
        description=(
            "Score 1-99 strategies through Mangrove SIEVE before paying for "
            "backtests. Returns binary + 4-class probabilities per item, "
            "with model + code provenance."
        ),
        access="auth",
        parameters=[
            ToolParam(
                name="strategies",
                type="array<Strategy>",
                required=True,
                description="MangroveAI-shaped Strategy objects (1-99 items).",
            ),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_data_query(
        table: str,
        select: list[str],
        filters: list[dict[str, Any]] | None = None,
        order_by: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        api_key: str = "",
    ) -> str:
        """Query the curated Oracle corpus (results / ohlcv) through the
        BigQuery proxy. Columns and filter operators are whitelisted
        server-side. Tenancy is enforced: `WHERE org_id = <caller's org>`
        is injected by Oracle — you can never read another tenant's rows.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import DataQueryInput
            from src.services.oracle import data_query as svc
            result = svc(DataQueryInput(
                table=table,
                select=select,
                filters=filters or [],
                order_by=order_by,
                limit=limit,
                offset=offset,
            ))
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_data_query",
        description=(
            "Query the curated Oracle corpus (results / ohlcv). Whitelist-"
            "enforced columns + filter ops; tenancy injected server-side."
        ),
        access="auth",
        parameters=[
            ToolParam(name="table", type="string", required=True, description="'results' | 'ohlcv'"),
            ToolParam(name="select", type="array<string>", required=True, description="Columns to return."),
            ToolParam(name="filters", type="array<{col,op,value}>", required=False, description="Filter clauses."),
            ToolParam(name="order_by", type="array<string>", required=False, description="Optional ORDER BY clauses."),
            ToolParam(name="limit", type="integer", required=False, description="Default 100, max 1000."),
            ToolParam(name="offset", type="integer", required=False, description="Default 0."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_backtest(
        asset: str,
        interval: str,
        strategy_json: str,
        lookback_months: int | None = 12,
        api_key: str = "",
    ) -> str:
        """Backtest a single strategy synchronously through Oracle's engine.
        Blocks until the engine finishes (30-120s on multi-month windows).
        Returns metrics + trade history. For batch work, prefer the SDK's
        backtest_async / backtest_bulk directly.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import OracleBacktestInput
            from src.services.oracle import backtest as svc
            result = svc(OracleBacktestInput(
                asset=asset,
                interval=interval,
                strategy_json=strategy_json,
                lookback_months=lookback_months,
            ))
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_backtest",
        description=(
            "Backtest one strategy through Oracle's engine (synchronous)."
        ),
        access="auth",
        parameters=[
            ToolParam(name="asset", type="string", required=True, description="e.g. BTC, ETH"),
            ToolParam(name="interval", type="string", required=True, description="e.g. 1h, 4h, 1d"),
            ToolParam(name="strategy_json", type="string", required=True, description="Strategy JSON (MangroveAI shape)."),
            ToolParam(name="lookback_months", type="integer", required=False, description="Default 12."),
            _APIKEY,
        ],
    ))

    # ----------------------------------------------------------------- #
    # Async + bulk backtests (3 tools)
    # ----------------------------------------------------------------- #

    @server.tool()
    async def oracle_backtest_async(
        asset: str,
        interval: str,
        strategy_json: str,
        lookback_months: int | None = 12,
        api_key: str = "",
    ) -> str:
        """Submit a backtest for async execution. Returns
        ``{backtest_id, status}`` immediately; poll
        ``oracle_backtest_poll(backtest_id)`` for the full result.
        Use when the window is too long for the sync variant's 30-120s block.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import OracleBacktestInput
            from src.services.oracle import backtest_async as svc
            result = svc(OracleBacktestInput(
                asset=asset,
                interval=interval,
                strategy_json=strategy_json,
                lookback_months=lookback_months,
            ))
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_backtest_async",
        description="Submit an Oracle backtest for async execution. Returns backtest_id to poll.",
        access="auth",
        parameters=[
            ToolParam(name="asset", type="string", required=True, description="e.g. BTC, ETH"),
            ToolParam(name="interval", type="string", required=True, description="e.g. 1h, 4h, 1d"),
            ToolParam(name="strategy_json", type="string", required=True, description="Strategy JSON (MangroveAI shape)."),
            ToolParam(name="lookback_months", type="integer", required=False, description="Default 12."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_backtest_poll(backtest_id: str, api_key: str = "") -> str:
        """Poll the status / result of an async backtest by ID."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import backtest_poll as svc
            result = svc(backtest_id)
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_backtest_poll",
        description="Poll the status / full result of an async Oracle backtest.",
        access="auth",
        parameters=[
            ToolParam(name="backtest_id", type="string", required=True, description="ID returned by oracle_backtest_async."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_backtest_bulk(
        request: dict[str, Any], api_key: str = "",
    ) -> str:
        """Bulk-evaluate many strategies against a shared date range.

        ``request`` mirrors ``OracleBulkBacktestRequest`` — supply
        ``strategy_ids``, ``strategy_configs``, or both, plus the shared
        risk + date fields. OHLCV is fetched once per unique
        ``(asset, timeframe)`` and shared across strategies.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import backtest_bulk as svc
            result = svc(request)
            return json.dumps(result)
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_backtest_bulk",
        description="Bulk-evaluate N strategies via Oracle with shared market-data fetches.",
        access="auth",
        parameters=[
            ToolParam(
                name="request",
                type="object",
                required=True,
                description="OracleBulkBacktestRequest dict (strategy_ids, strategy_configs, shared risk + date fields).",
            ),
            _APIKEY,
        ],
    ))

    # ----------------------------------------------------------------- #
    # Experiment lifecycle (8 tools)
    # ----------------------------------------------------------------- #

    @server.tool()
    async def oracle_create_experiment(
        config: dict[str, Any], api_key: str = "",
    ) -> str:
        """Create a draft experiment from a config dict.

        ``config`` is passed through to Oracle's ``ExperimentConfig``.
        At minimum ``name`` is required. Returns
        ``{experiment_id, status: 'draft', created_at, org_id}``.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import create_experiment as svc
            return json.dumps(svc(config))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_create_experiment",
        description="Create an Oracle sweep experiment in draft status.",
        access="auth",
        parameters=[
            ToolParam(name="config", type="object", required=True, description="ExperimentConfig dict (name required)."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_list_experiments(api_key: str = "") -> str:
        """List experiments for the calling org (compact summary view).

        Returns one row per experiment with experiment_id, name, status,
        total_runs, completed, search_mode, created_at. Note: this
        endpoint can 504 under load — fall back to per-id
        ``oracle_get_experiment`` if needed.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import list_experiments as svc
            return json.dumps(svc())
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_list_experiments",
        description="List Oracle experiments (summary view) for the calling org.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def oracle_get_experiment(experiment_id: str, api_key: str = "") -> str:
        """Fetch full experiment config + current progress (completed_runs)."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import get_experiment as svc
            return json.dumps(svc(experiment_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_get_experiment",
        description="Get full Oracle experiment config + live progress.",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="ID returned by oracle_create_experiment."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_update_experiment(
        experiment_id: str, config: dict[str, Any], api_key: str = "",
    ) -> str:
        """Replace a draft experiment's config (PUT semantics).

        Only ``draft``-status experiments can be updated; validated /
        launched / paused reject mutation with HTTP 400.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import update_experiment as svc
            return json.dumps(svc(experiment_id, config))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_update_experiment",
        description="Replace a draft Oracle experiment's config (PUT semantics).",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Experiment to update."),
            ToolParam(name="config", type="object", required=True, description="Full replacement ExperimentConfig."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_delete_experiment(experiment_id: str, api_key: str = "") -> str:
        """Delete an experiment + cancel any in-flight child backtests."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import delete_experiment as svc
            return json.dumps(svc(experiment_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_delete_experiment",
        description="Delete an Oracle experiment and cancel in-flight children.",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Experiment to delete."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_validate_experiment(experiment_id: str, api_key: str = "") -> str:
        """Validate a draft (required before launch).

        Server returns 400 with structured ``errors`` if the config is
        incomplete (no datasets, no entry signals, etc.). The
        agent surfaces those messages verbatim.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import validate_experiment as svc
            return json.dumps(svc(experiment_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_validate_experiment",
        description="Validate a draft Oracle experiment (transition draft -> validated).",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Draft experiment to validate."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_launch_experiment(experiment_id: str, api_key: str = "") -> str:
        """Fan out a validated experiment into child backtests.

        Up to 99 children per launch. The fan-out is asynchronous — poll
        ``oracle_get_experiment(id)`` for progress or
        ``oracle_list_results(experiment_id=id)`` for materializing rows.
        Launch is non-idempotent; a success here means the sweep is running
        (confirmed even if the upstream gateway timed out) — do NOT re-launch,
        just poll.

        Bills: 1 unit per HTTP call (children not billed individually).
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import launch_experiment as svc
            return json.dumps(svc(experiment_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_launch_experiment",
        description="Launch a validated Oracle experiment into up to 99 child backtests.",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Validated experiment to launch."),
            _APIKEY,
        ],
    ))

    @server.tool()
    async def oracle_pause_experiment(experiment_id: str, api_key: str = "") -> str:
        """Pause a running experiment. Resume by relaunching."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import pause_experiment as svc
            return json.dumps(svc(experiment_id))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_pause_experiment",
        description="Pause a running Oracle experiment without losing completed results.",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Running experiment to pause."),
            _APIKEY,
        ],
    ))

    # ----------------------------------------------------------------- #
    # Results pagination (1 tool)
    # ----------------------------------------------------------------- #

    @server.tool()
    async def oracle_list_results(
        experiment_id: str, limit: int = 100, offset: int = 0, api_key: str = "",
    ) -> str:
        """Read backtest results materializing under an experiment.

        ``experiment_id`` is required — Oracle rejects unfiltered reads.
        Returns ``{total, offset, limit, results}``; results are
        wide-format Oracle backtest result rows.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import list_results as svc
            return json.dumps(svc(experiment_id, limit=limit, offset=offset))
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_list_results",
        description="Paginated read of Oracle backtest results under an experiment.",
        access="auth",
        parameters=[
            ToolParam(name="experiment_id", type="string", required=True, description="Experiment whose results to read."),
            ToolParam(name="limit", type="integer", required=False, description="Default 100; max 1000."),
            ToolParam(name="offset", type="integer", required=False, description="Default 0."),
            _APIKEY,
        ],
    ))

    # ----------------------------------------------------------------- #
    # Metadata catalogs (3 tools — free / non-billable)
    # ----------------------------------------------------------------- #

    @server.tool()
    async def oracle_list_datasets(api_key: str = "") -> str:
        """List the OHLCV datasets experiments can run against.

        Each dataset entry carries asset, timeframe, file, hash,
        start_date, end_date. Curated immutable snapshots.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import list_datasets as svc
            return json.dumps(svc())
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_list_datasets",
        description="List curated OHLCV datasets available to Oracle experiments.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def oracle_list_signals(api_key: str = "") -> str:
        """List signals with typed param specs available to experiments.

        Each entry carries name, type (TRIGGER/FILTER), params (typed),
        constraints, description, requires (OHLCV cols), category.
        Use this to construct ExperimentConfig.entry_signals /
        exit_signals programmatically with valid signal names + params.
        """
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import list_signals as svc
            return json.dumps(svc())
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_list_signals",
        description="List signals with typed param specs available to Oracle experiments.",
        access="auth",
        parameters=[_APIKEY],
    ))

    @server.tool()
    async def oracle_list_templates(api_key: str = "") -> str:
        """List predefined strategy templates to seed experiments from."""
        if not _require(api_key):
            return _auth_error()
        try:
            from src.services.oracle import list_templates as svc
            return json.dumps(svc())
        except AgentError as e:
            return _handle_agent_error(e)

    register_tool(ToolEntry(
        name="oracle_list_templates",
        description="List predefined strategy templates to seed Oracle experiments.",
        access="auth",
        parameters=[_APIKEY],
    ))


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
            description="x402 demo: $0.05 USDC on Base. Smoke test for the payment path.",
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
        description="x402 demo: $0.05 USDC on Base. Smoke test for the payment path.",
        access="x402",
        price="$0.05 USDC",
        network="base",
        parameters=[],
    ))
