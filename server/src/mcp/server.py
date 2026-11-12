"""MCP server -- unified entry point for all agent tools.

Mounted at /mcp on the FastAPI app via Streamable HTTP transport.
"""
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import UrlElicitationRequiredError
from pydantic import ValidationError

from src.shared.logging import get_logger

_log = get_logger(__name__)

_mcp_server = None

# These adapters still use API-key/payment SDKs, not the MangroveAI MCP
# transport. An OAuth grant for that transport cannot authorize Markets or
# recurring paper execution. Keep local custody and direct broker tools usable.
_OAUTH_UNSUPPORTED_LOCAL = frozenset({
    'agent_start_paper_schedule', 'agent_list_paper_schedules',
    'agent_pause_paper_schedule', 'agent_get_paper_schedule',
    'agent_reconcile_paper_schedule', 'execute_swap', 'cex_sync_fills',
    'marketplace_prepare', 'marketplace_submit',
    'agent_adopt_strategy', 'agent_activate_deployment', 'agent_get_deployment',
})


def _error_message(exc: Exception) -> str:
    """The client-facing text of a tool error, with validation input values left out."""
    cause = exc if isinstance(exc, ValidationError) else exc.__cause__
    if not isinstance(cause, ValidationError):
        return str(exc)
    details = '; '.join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in cause.errors(include_input=False, include_url=False)
    )
    prefix = 'Invalid arguments' if cause is exc else str(exc).split(':', 1)[0]
    return f'{prefix}: {details}'


class PricedFastMCP(FastMCP):
    """Attach current price hints without mutating registered tool definitions."""

    async def list_tools(self):
        from src.services import mangrove_oauth
        from src.services.tool_pricing import enrich_tools, price_hint

        oauth_mode = mangrove_oauth.enabled()
        tools = [tool for tool in await super().list_tools()
                 if tool.name != 'hello_mangrove'
                 and not (oauth_mode and tool.name in _OAUTH_UNSUPPORTED_LOCAL)]
        entries = await enrich_tools([{"name": tool.name} for tool in tools])
        result = []
        for tool, entry in zip(tools, entries, strict=True):
            pricing = entry.get("pricing")
            if pricing is not None:
                tool = tool.model_copy(update={
                    "description": (tool.description or "") + "\n\n" + price_hint(pricing),
                    "meta": {**(tool.meta or {}), "mangrove/pricing": pricing},
                })
            result.append(tool)
        from src.mcp import mangrove_proxy, marketplace_proxy
        from src.shared.errors import AgentError

        try:
            remote = [] if oauth_mode else await marketplace_proxy.list_tools()
        except AgentError:
            remote = []
        local_names = {tool.name for tool in result}
        if any(tool.name in local_names for tool in remote):
            raise ValueError("Markets MCP name conflicts with a local tool")
        result += remote
        try:
            upstream = await mangrove_proxy.catalog()
            self.mangroveai_available = True
            self.mangroveai_error = None
        except AgentError as error:
            self.mangroveai_available = False
            self.mangroveai_error = {
                'code': error.code,
                'message': ('Mangrove OAuth connection could not be verified. ' + mangrove_oauth.RECONNECT_ACTION
                            if oauth_mode else 'MangroveAI tool discovery failed. Check the configured connection.'),
            }
            upstream = []
        names = {tool.name for tool in result}
        if any(tool.name in names for tool in upstream):
            raise ValueError("MangroveAI MCP name conflicts with a local or Markets tool")
        return result + upstream

    async def call_tool(self, name, arguments):
        """Apply the client-independent secret guard around every tool call.

        Every MCP client reaches tools through this method, so the guard
        covers Claude Code, the command client used by other hosts, the
        restricted launcher and remote clients alike. Arguments are scanned
        before dispatch and results after it. A raised error is re-raised with
        argument validation details stripped of input values and with every
        argument value it repeats removed; an error that still holds a secret
        shape is replaced by a SECRET_IN_ERROR failure. See
        src/shared/secret_guard.py and .claude/hooks/README.md.
        """
        from src.mcp import mangrove_proxy
        from src.shared import secret_guard

        found = secret_guard.find_in_arguments(arguments or {})
        if found:
            _log.warning('mcp.secret_guard.arguments_refused', tool=name, kind=found)
            return mangrove_proxy.failure(
                'SECRET_IN_ARGUMENTS',
                f'The tool arguments contain {secret_guard.LABELS[found]}. The call was refused '
                'and nothing was sent to any tool or upstream service. Wallet import runs only '
                'in the independent custody administrator terminal (custody-service.py admin '
                'import; see docs/custody.md), never through an agent. Remove the secret from '
                'the chat.',
            )
        try:
            result = await self._dispatch(name, arguments)
        except UrlElicitationRequiredError:
            raise
        except Exception as exc:
            message, found = secret_guard.redact_error(_error_message(exc), arguments or {})
            if found:
                _log.error('mcp.secret_guard.error_withheld', tool=name, kind=found)
                return mangrove_proxy.failure(
                    'SECRET_IN_ERROR',
                    f'The tool failed and its error message was withheld because it contained '
                    f'{secret_guard.LABELS[found]}. The operation may have completed; inspect its '
                    'state before retrying.',
                )
            if message != str(exc):
                raise ToolError(message) from None
            raise
        found = secret_guard.find_in_result(result)
        if found:
            _log.error('mcp.secret_guard.result_withheld', tool=name, kind=found)
            return mangrove_proxy.failure(
                'SECRET_IN_RESULT',
                f'The tool response contained {secret_guard.LABELS[found]} in a secret field and '
                'was withheld. The operation may have completed; inspect its state before '
                'retrying. Report this to the maintainers: wallet secrets stay in isolated '
                'custody and tool responses must never carry one.',
            )
        return result

    async def _dispatch(self, name, arguments):
        from src.mcp import mangrove_proxy, marketplace_proxy
        from src.services import mangrove_oauth
        from src.services.marketplace_catalog import RETIRED_READ_TOOLS, is_markets_tool

        # Guard stale catalogs and direct calls too, before any SDK or wallet
        # work. Never fall back to an API key or payment after OAuth selection.
        if mangrove_oauth.enabled() and (name in _OAUTH_UNSUPPORTED_LOCAL or is_markets_tool(name)):
            return mangrove_proxy.failure(
                'OAUTH_TOOL_UNAVAILABLE',
                'This tool requires an API-key or payment integration that does not support '
                'the current Mangrove OAuth connection. No operation or wallet payment was attempted.',
            )

        if name in RETIRED_READ_TOOLS:
            return mangrove_proxy.failure(
                'RETIRED_TOOL', 'This agent wrapper is retired. Discover the current Markets tool and use its schema.',
            )
        if is_markets_tool(name):
            return await marketplace_proxy.call_tool(name, arguments or {})
        if name in self._tool_manager._tools:
            return await super().call_tool(name, arguments)
        return await mangrove_proxy.call_tool(name, arguments or {})


def reset_mcp_server() -> None:
    """Drop the cached MCP server. Used between TestClient lifespans where
    the prior session manager has been closed and can't be reused."""
    global _mcp_server
    _mcp_server = None


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all tools registered.

    Idempotent -- returns the same server instance on repeated calls
    to avoid duplicate tool registration warnings. Call reset_mcp_server()
    first if you need a fresh instance.
    """
    global _mcp_server
    if _mcp_server is not None:
        return _mcp_server

    # streamable_http_path="/" so when mounted at /mcp on the parent app,
    # the final endpoint is /mcp/ (instead of /mcp/mcp).
    # stateless_http=True so each request stands alone — no per-session task
    # group state means the session manager is safe to reuse across multiple
    # TestClient lifespans (and matches our single-user, request-driven model).
    _mcp_server = PricedFastMCP(
        "mangrove-agent", streamable_http_path="/", stateless_http=True, json_response=True
    )

    from src.mcp.tools import register
    register(_mcp_server)

    return _mcp_server
