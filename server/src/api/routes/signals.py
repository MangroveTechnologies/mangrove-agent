"""Signal routes — pass-through to mangroveai.signals."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, SdkError

router = APIRouter(
    prefix="/signals",
    dependencies=[Depends(require_api_key)],
    tags=["signals"],
)


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get("", summary="List available signals (optionally filtered)")
async def list_signals(
    category: str | None = None,
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    regime_direction: str | None = None,
    role: str | None = None,
) -> dict:
    from starlette.concurrency import run_in_threadpool

    from src.services.signals import list_signals as list_signals_service

    return await run_in_threadpool(
        list_signals_service, category=category, search=search, limit=limit, offset=offset,
        regime_direction=regime_direction, role=role,
    )


@router.get("/{name}", summary="Signal detail with parameter spec")
async def get_signal(name: str) -> Any:
    try:
        return _dump(mangrove_ai_client().signals.get(name))
    except AgentError:
        raise
    except Exception:
        raise SdkError("Could not fetch signal details from the upstream service.") from None
