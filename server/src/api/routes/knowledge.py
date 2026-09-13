"""Knowledge graph routes — auth-gated. Thin wrappers over knowledge_service.

- POST /api/v1/agent/knowledge/query   one op against the graph
- GET  /api/v1/agent/knowledge/stats   counts + the vocabulary every filter accepts

The graph ships inside the `mangrove-kb` package and is queried offline.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.services import knowledge_service
from src.services.knowledge_service import KnowledgeQuery
from src.shared.auth.dependency import require_api_key

router = APIRouter(
    prefix="/knowledge",
    dependencies=[Depends(require_api_key)],
    tags=["knowledge"],
)


@router.post("/query", summary="Query the knowledge graph (stats, find, ask, get, neighbors, outputs, path)")
async def query(req: KnowledgeQuery) -> dict[str, Any]:
    return knowledge_service.query_knowledge(req)


@router.get("/stats", summary="Graph counts and the complete filter vocabulary")
async def stats() -> dict[str, Any]:
    return knowledge_service.query_knowledge(KnowledgeQuery(op="stats"))
