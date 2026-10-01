"""Authenticated local marketplace approval endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from src.services import marketplace
from src.shared.auth.dependency import require_api_key

router = APIRouter(prefix="/marketplace", tags=["marketplace"], dependencies=[Depends(require_api_key)])


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: str
    arguments: dict
    wallet_address: str


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approval_id: str
    confirm: bool = False


@router.post("/prepare")
def prepare(request: PrepareRequest) -> dict:
    """Return an exact action preview without signing."""
    return marketplace.prepare(request.operation, request.arguments, request.wallet_address)


@router.post("/submit")
def submit(request: SubmitRequest) -> dict:
    """Sign and submit only the previously prepared action."""
    return marketplace.submit(request.approval_id, request.confirm)
