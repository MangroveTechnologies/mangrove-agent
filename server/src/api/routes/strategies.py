"""Strategy routes — auth-gated. All thin wrappers over strategy_service."""
from __future__ import annotations

from uuid import UUID
from pydantic import BaseModel, ConfigDict

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from src.services import strategy_service
from src.services.strategy_service import (
    StrategyDetailResponse,
    StrategyMarketContext,
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


@router.patch("/{strategy_id}/market", response_model=StrategyDetailResponse)
async def configure_market(strategy_id: str, req: StrategyMarketContext) -> StrategyDetailResponse:
    return await run_in_threadpool(strategy_service.configure_market, strategy_id, req)


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
    return await run_in_threadpool(strategy_service.update_status, strategy_id, req)


@router.post(
    "/{strategy_id}/evaluate",
    summary="Manually trigger a single evaluation tick",
    description="Same code path the cron job runs. Useful for debugging or power-user workflows.",
)
async def evaluate(strategy_id: str) -> dict:
    # Make sure the strategy exists; strategy_service.get_strategy raises
    # StrategyNotFound otherwise.
    strategy_service.get_strategy(strategy_id)
    await run_in_threadpool(strategy_service.tick, strategy_id)
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


class PaperScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: UUID
    interval_seconds: int
    max_evaluations: int
    confirm: bool = False


@router.get('/paper-schedules')
async def list_paper_schedules():
    from src.services import paper_schedule_service as paper
    return await run_in_threadpool(paper.list_schedules)


@router.post('/paper-schedules')
async def start_paper_schedule(req: PaperScheduleRequest):
    from src.services import paper_schedule_service as paper
    return await run_in_threadpool(paper.start, str(req.strategy_id), req.interval_seconds, req.max_evaluations, req.confirm)


@router.post('/paper-schedules/{schedule_id}/pause')
async def pause_paper_schedule(schedule_id: UUID):
    from src.services import paper_schedule_service as paper
    return await run_in_threadpool(paper.pause, str(schedule_id))


@router.get('/paper-schedules/{schedule_id}')
async def inspect_paper_schedule(schedule_id: UUID, refresh: bool = False):
    from src.services import paper_schedule_service as paper
    return await run_in_threadpool(paper.inspect, str(schedule_id), refresh)


@router.post('/paper-schedules/{schedule_id}/reconcile')
async def reconcile_paper_schedule(schedule_id: UUID):
    from src.services import paper_schedule_service as paper
    return await run_in_threadpool(paper.reconcile, str(schedule_id))
