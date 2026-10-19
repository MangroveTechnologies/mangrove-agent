"""Strategy routes — auth-gated. All thin wrappers over strategy_service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.services import strategy_service
from src.services.strategy_service import (
    StrategyDetailResponse,
    StrategyStatusUpdate,
)
from src.shared.auth.dependency import require_api_key

router = APIRouter(
    prefix="/strategies",
    dependencies=[Depends(require_api_key)],
    tags=["strategies"],
)


@router.post("/autonomous", include_in_schema=False)
async def create_autonomous():
    raise HTTPException(status_code=410, detail="Local strategy creation is retired. Use MangroveAI MCP load_skill(strategy-composition) and save_strategy.")


@router.post("/manual", include_in_schema=False)
async def create_manual():
    raise HTTPException(status_code=410, detail="Local strategy creation is retired. Use MangroveAI MCP save_strategy.")


@router.get(
    "",
    response_model=list[StrategyDetailResponse],
    summary="List strategies",
)
async def list_strategies(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[StrategyDetailResponse]:
    return strategy_service.list_strategies(status=status, limit=limit, offset=offset)


@router.get(
    "/{strategy_id}",
    response_model=StrategyDetailResponse,
    summary="Get a strategy by ID",
)
async def get_strategy(strategy_id: str) -> StrategyDetailResponse:
    return strategy_service.get_strategy(strategy_id)


@router.patch(
    "/{strategy_id}/status",
    response_model=StrategyDetailResponse,
    summary="Update strategy status (single source of truth for lifecycle)",
    description=(
        "Transitions: draft→inactive, inactive→{paper,live,archived}, "
        "paper→{live,inactive,archived}, live→{inactive,archived}. "
        "Target=live requires confirm=true AND an allocation block. "
        "Transition off live requires confirm=true."
    ),
)
async def update_status(strategy_id: str, req: StrategyStatusUpdate) -> StrategyDetailResponse:
    return strategy_service.update_status(strategy_id, req)


@router.post(
    "/{strategy_id}/evaluate",
    summary="Manually trigger a single evaluation tick",
    description="Same code path the cron job runs. Useful for debugging or power-user workflows.",
)
async def evaluate(strategy_id: str) -> dict:
    # Make sure the strategy exists; strategy_service.get_strategy raises
    # StrategyNotFound otherwise.
    strategy_service.get_strategy(strategy_id)
    strategy_service.tick(strategy_id)
    # Return the latest evaluation row for this strategy.
    from src.services.trade_log import list_evaluations
    evs = list_evaluations(strategy_id, limit=1)
    if not evs:
        return {"strategy_id": strategy_id, "status": "no_evaluation_recorded"}
    e = evs[0]
    return {
        "strategy_id": strategy_id,
        "evaluation_id": e.id,
        "status": e.status,
        "order_count": len(e.order_intents),
        "duration_ms": e.duration_ms,
        "error_msg": e.error_msg,
    }
