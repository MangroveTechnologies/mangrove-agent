"""FastAPI application factory.

Serves dual protocols on a single port:
- REST API at /api/v1/* (free + auth) and /api/x402/* (payment-gated)
- MCP server at /mcp (Streamable HTTP transport)
- OpenAPI docs at /docs and /openapi.json

x402 payment middleware (official Coinbase SDK) protects /api/x402/*.
MCP tools enforce auth and x402 at the tool level (same x402ResourceServer).
All config loaded from per-environment JSON via app_config singleton.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from x402.http.middleware.fastapi import payment_middleware

from src.api.router import api_router, x402_router
from src.config import app_config
from src.health import health_payload
from src.shared.auth.middleware import has_valid_api_key
from src.shared.errors import AgentError, agent_error_handler
from src.shared.logging import CorrelationIdMiddleware, get_logger
from src.shared.logging import configure as configure_logging
from src.shared.x402.config import get_network, get_pay_to
from src.shared.x402.server import get_x402_server

# Configure structured logging at import time so any log calls below
# (including x402 setup errors) are already structured.
configure_logging(str(app_config.ENVIRONMENT))
_log = get_logger(__name__)


def _setup_x402():
    """Set up x402 payment middleware using the shared x402ResourceServer."""
    server = get_x402_server()
    network = get_network()
    pay_to = get_pay_to()

    routes = {
        "GET /api/x402/hello-mangrove": {
            "accepts": {
                "scheme": "exact",
                "network": network,
                "payTo": pay_to,
                "price": "$0.05",
            },
            "resource": "hello_mangrove",
            "description": "Thank you for supporting the project and strengthening the ecosystem",
        },
    }

    return payment_middleware(routes, server)


x402_handler = _setup_x402()


from src.mcp.server import create_mcp_server, reset_mcp_server  # noqa: E402


def create_app() -> FastAPI:
    """Build a FastAPI app with REST routes + a fresh MCP server mounted at /mcp.

    Each call returns a NEW app + a NEW MCP server. This matters for tests:
    multiple TestClient lifespans must not share the same MCP session manager
    (its task group is closed after the first lifespan exit).
    """
    reset_mcp_server()
    mcp_server = create_mcp_server()
    mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from src.services.scheduler_service import shutdown as scheduler_shutdown
        from src.services.scheduler_service import start as scheduler_start
        from src.shared.db.sqlite import init_db

        init_db()  # emits db.migrated log event; idempotent
        scheduler_start()  # emits scheduler.started; non-blocking BackgroundScheduler
        # FastMCP's session manager must be running for the /mcp endpoint to
        # handle requests. Without this, calls into /mcp raise
        # "RuntimeError: Task group is not initialized."
        async with mcp_server.session_manager.run():
            _log.info("app.startup", version=application.version, environment=str(app_config.ENVIRONMENT))
            yield
            _log.info("app.shutdown")
        scheduler_shutdown()  # wait=False so we don't block on in-flight ticks

    application = FastAPI(
        title="Mangrove Agent",
        description=(
            "FastAPI + MCP service template with three-tier access control.\n\n"
            "## For Agents\n\n"
            "- **REST discovery**: GET `/openapi.json` for the full OpenAPI 3.0 spec\n"
            "- **MCP tool catalog**: GET `/api/v1/docs/tools` for tool names, parameters, access tiers, and pricing\n"
            "- **MCP endpoint**: Connect to `/mcp` via Streamable HTTP transport\n\n"
            "## Access Tiers\n\n"
            "| Tier | How to access |\n"
            "|------|---------------|\n"
            "| Free | No credentials needed |\n"
            "| Auth | `X-API-Key` header |\n"
            "| x402 | Payment via x402 protocol (or API key for free access) |\n"
        ),
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "discovery", "description": "API and tool discovery endpoints (free, no auth)"},
            {"name": "x402", "description": "x402 payment-gated endpoints (e.g. hello_mangrove)"},
        ],
    )

    @application.middleware("http")
    async def x402_middleware(request: Request, call_next):
        # Bypass x402 only for *valid* API keys. A previous version
        # short-circuited on header presence alone, which let any
        # X-API-Key value skip payment. See tests/test_x402.py
        # ::test_invalid_api_key_does_not_bypass_x402.
        api_key = request.headers.get("x-api-key")
        if api_key and has_valid_api_key(api_key):
            return await call_next(request)
        return await x402_handler(request, call_next)

    application.add_exception_handler(AgentError, agent_error_handler)
    application.add_middleware(CorrelationIdMiddleware)

    application.include_router(api_router)
    application.include_router(x402_router)
    application.mount("/mcp", mcp_app)

    @application.get(
        "/health",
        summary="Health check",
        description="Returns service health status and timestamp. Free, no auth required.",
        tags=["discovery"],
    )
    async def health():
        return health_payload()

    return application


# Module-level app instance for uvicorn (`uvicorn src.app:app`).
# Tests should call create_app() to get a fresh instance per test.
app = create_app()
