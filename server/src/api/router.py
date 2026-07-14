"""REST API routers.

/api/v1/*       -- Free and auth-gated endpoints
/api/v1/agent/* -- mangrove-agent endpoints (free discovery + auth-gated actions)
/api/x402/*     -- x402 payment-gated endpoints
"""
from fastapi import APIRouter

from src.api.routes.cex import router as cex_router
from src.api.routes.dex import router as dex_router
from src.api.routes.discovery import router as discovery_router
from src.api.routes.hello_mangrove import router as hello_mangrove_router
from src.api.routes.kb import router as kb_router
from src.api.routes.logs import router as logs_router
from src.api.routes.market import router as market_router
from src.api.routes.on_chain import router as on_chain_router
from src.api.routes.oracle import router as oracle_router
from src.api.routes.portfolio import router as portfolio_router
from src.api.routes.reference_strategies import router as reference_strategies_router
from src.api.routes.signals import router as signals_router
from src.api.routes.strategies import router as strategies_router
from src.api.routes.wallet import router as wallet_router

# Free + auth-gated
api_router = APIRouter(prefix="/api/v1")

# mangrove-agent namespace
agent_router = APIRouter(prefix="/agent")
agent_router.include_router(discovery_router, tags=["discovery"])
agent_router.include_router(wallet_router)
agent_router.include_router(cex_router)
agent_router.include_router(dex_router)
agent_router.include_router(market_router)
agent_router.include_router(on_chain_router)
agent_router.include_router(signals_router)
agent_router.include_router(strategies_router)
agent_router.include_router(portfolio_router)
agent_router.include_router(oracle_router)
agent_router.include_router(reference_strategies_router)
agent_router.include_router(logs_router)
agent_router.include_router(kb_router)

api_router.include_router(agent_router)

# x402 payment-gated (hello_mangrove is the smoke test for the payment path)
x402_router = APIRouter(prefix="/api/x402")
x402_router.include_router(hello_mangrove_router, tags=["x402"])
