"""Retired client-side reference library; no local strategy definitions."""
from fastapi import APIRouter, Depends, HTTPException

from src.shared.auth.dependency import require_api_key

router = APIRouter(prefix="/reference-strategies", tags=["reference-strategies"], dependencies=[Depends(require_api_key)])


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
@router.get("/search", include_in_schema=False)
@router.post("/{reference_id}/build", include_in_schema=False)
async def retired(reference_id: str = ""):
    raise HTTPException(status_code=410, detail="Local references are retired. Load strategy-composition from MangroveAI over MCP.")
