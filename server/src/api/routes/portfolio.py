"""Portfolio risk routes — auth-gated. Thin wrappers over portfolio_risk_service.

- GET  /api/v1/agent/portfolio/risk        current kill-switch state
- POST /api/v1/agent/portfolio/risk/reset  clear a tripped switch (human action)

The portfolio kill switch (#146) is LATCHED: once tripped it pauses all live
strategies and stays tripped until a human resets it here. The reset endpoint is
the explicit human re-activation gate.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.services import portfolio_risk_service
from src.shared.auth.dependency import require_api_key

router = APIRouter(
    prefix="/portfolio",
    tags=["portfolio"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/risk", summary="Portfolio kill-switch state")
async def get_portfolio_risk() -> dict:
    """Live-book high-water mark, current drawdown, and trip state."""
    return portfolio_risk_service.get_status()


@router.post("/risk/reset", summary="Clear a tripped portfolio kill switch (human re-activation)")
async def reset_portfolio_risk() -> dict:
    """Clear the latch and re-baseline to current book value.

    Explicit human action: the switch never auto-resumes. After reset the
    operator can promote strategies back to live without an immediate re-trip.
    """
    return portfolio_risk_service.reset()
