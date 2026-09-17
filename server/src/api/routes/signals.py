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
) -> dict:
    category = (category.strip().lower() or None) if category else None
    try:
        client = mangrove_ai_client()
        if search:
            from mangrove_ai.models import SearchSignalsRequest
            page = client.signals.search(SearchSignalsRequest(query=search, limit=limit, offset=offset))
        else:
            kwargs = {"category": category} if category else {}
            page = client.signals.list(limit=limit, offset=offset, **kwargs)
    except AgentError:
        raise
    except Exception:
        raise SdkError("Could not list signals from the upstream service.") from None

    items = [_dump(s) for s in getattr(page, "items", [])]
    if category:
        cat_lower = category.lower()
        items = [s for s in items if (s.get("category") or "").lower() == cat_lower]

    return {
        "items": items,
        "total": getattr(page, "total", len(items)),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{name}", summary="Signal detail with parameter spec")
async def get_signal(name: str) -> Any:
    try:
        return _dump(mangrove_ai_client().signals.get(name))
    except AgentError:
        raise
    except Exception:
        raise SdkError("Could not fetch signal details from the upstream service.") from None
