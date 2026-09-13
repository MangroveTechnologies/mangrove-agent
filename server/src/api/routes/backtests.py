"""Backtest history routes — auth-gated. Thin wrappers over backtest_service.

- GET /api/v1/agent/backtests                 the caller's stored runs, newest first
- GET /api/v1/agent/backtests/{backtest_id}   one stored run in full

Every full `backtest_strategy` run is persisted server-side by MangroveAI
under the API key's user, so a past result is read back here instead of
paying for the same measurement again.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.services import backtest_service
from src.shared.auth.dependency import require_api_key

router = APIRouter(
    prefix="/backtests",
    dependencies=[Depends(require_api_key)],
    tags=["backtests"],
)


@router.get("", summary="List stored backtest runs (newest first)")
async def list_backtests(
    asset: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
    include_archived: bool = False,
) -> dict[str, Any]:
    return backtest_service.list_backtests(
        asset=asset, status=status, date_from=date_from, date_to=date_to,
        limit=limit, offset=offset, include_archived=include_archived,
    )


@router.get("/{backtest_id}", summary="Get one stored backtest run")
async def get_backtest(
    backtest_id: str, include_trades: bool = False, include_benchmark: bool = True,
) -> dict[str, Any]:
    return backtest_service.get_backtest(
        backtest_id, include_trades=include_trades, include_benchmark=include_benchmark,
    )
