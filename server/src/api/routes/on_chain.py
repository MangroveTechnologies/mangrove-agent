"""On-chain analytics routes — pass-through to mangroveai.on_chain."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import SdkError

router = APIRouter(
    prefix="/on-chain",
    dependencies=[Depends(require_api_key)],
    tags=["on-chain"],
)


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get("/smart-money", summary="Smart money sentiment for a token")
async def smart_money(symbol: str, chain: str | None = None) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_smart_money_sentiment(symbol, chain=chain))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_smart_money_sentiment failed: {e}") from e


@router.get("/whale-activity", summary="Whale activity summary for a token")
async def whale_activity(symbol: str, hours_back: int = 24) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_whale_activity(symbol, hours_back=hours_back))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_whale_activity failed: {e}") from e


@router.get("/token-holders/{symbol}", summary="Holder distribution + concentration")
async def token_holders(symbol: str) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_token_holders(symbol))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_token_holders failed: {e}") from e
