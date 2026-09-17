"""Knowledge Base routes — pass-through to mangroveai.kb."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, SdkError

router = APIRouter(
    prefix="/kb",
    dependencies=[Depends(require_api_key)],
    tags=["kb"],
)


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get("/search", summary="Full-text search the knowledge base")
async def search(q: str, limit: int = 20) -> Any:
    try:
        return _dump(mangrove_ai_client().kb.search.query(q=q, limit=limit))
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.search.query failed at the upstream service.") from None


@router.get("/glossary/{term}", summary="Glossary term lookup with backlinks")
async def glossary(term: str) -> Any:
    try:
        return _dump(mangrove_ai_client().kb.glossary.get(term))
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.glossary.get failed at the upstream service.") from None


@router.get("/documents", summary="List all KB documents (summary only)")
async def documents_list() -> Any:
    try:
        return [_dump(d) for d in mangrove_ai_client().kb.documents.list()]
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.documents.list failed at the upstream service.") from None


@router.get("/documents/{slug}", summary="Full KB document by slug")
async def documents_get(slug: str) -> Any:
    try:
        return _dump(mangrove_ai_client().kb.documents.get(slug))
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.documents.get failed at the upstream service.") from None


@router.get("/indicators", summary="List KB indicator docs (optionally filtered by category)")
async def indicators_list(category: str | None = None) -> Any:
    try:
        kwargs: dict[str, Any] = {}
        if category is not None:
            kwargs["category"] = category
        return [_dump(i) for i in mangrove_ai_client().kb.indicators.list(**kwargs)]
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.indicators.list failed at the upstream service.") from None


@router.get("/tags", summary="List all KB tags")
async def tags_list() -> Any:
    try:
        return [_dump(t) for t in mangrove_ai_client().kb.tags.list()]
    except AgentError:
        raise
    except Exception:
        raise SdkError("kb.tags.list failed at the upstream service.") from None
