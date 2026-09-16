"""x402 spend-budget routes — auth-gated. Thin wrappers over spend_service.

- GET  /api/v1/agent/x402/spend           budget: spent, remaining, exhausted
- GET  /api/v1/agent/x402/spend/payments  the payment ledger (audit trail)
- POST /api/v1/agent/x402/spend/reset     authorize a fresh budget (human)

A spent budget does not clear itself. Topping it up is a human decision, and
these routes are how a human makes it from outside a conversation; the
`x402_spend_status` / `x402_spend_reset` MCP tools are how they make it from
inside one, which is the common case.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from src.services import spend_service
from src.shared.auth.dependency import require_api_key


class SpendResetRequest(BaseModel):
    """What a human authorizes when they top the budget up."""

    cap_usd: float | None = Field(
        None,
        ge=0,
        description=(
            "New budget in dollars for the period being started. Omit to keep "
            "the current size. Recorded as the amount a human authorized, and "
            "it overrides X402_SPEND_CAP_USD from here on."
        ),
    )

router = APIRouter(
    prefix="/x402/spend",
    tags=["x402"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", summary="Outbound x402 spend budget")
async def get_spend_status() -> dict:
    """Spent, remaining, the budget and where it came from."""
    return spend_service.get_status()


@router.get("/payments", summary="Outbound x402 payment ledger")
async def list_spend_payments(
    limit: int = Query(50, ge=1, le=500, description="Rows to return, newest first."),
    period_id: int | None = Query(
        None,
        description="Restrict to one budget period. Omit for every payment ever authorized.",
    ),
) -> dict:
    """One row per payment AUTHORIZATION, newest first.

    Authorizations, not settlements: a row appears the moment the agent
    signs, and is marked `settled` or `released` once the outcome is known.
    See `spend_service` for why the budget is counted that way.
    """
    payments = spend_service.list_payments(limit=limit, period_id=period_id)
    return {"payments": payments, "count": len(payments)}


@router.post("/reset", summary="Authorize a fresh x402 budget (human top-up)")
async def reset_spend_cap(body: SpendResetRequest | None = None) -> dict:
    """Start a new budget period, optionally with a new size.

    Explicit human action: the budget never refills itself. History is kept
    — past payments keep their period id and stay visible in the ledger.
    """
    return spend_service.reset(cap_usd=body.cap_usd if body else None)
