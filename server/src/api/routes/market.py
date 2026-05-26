"""Market data routes — auth-gated; pure pass-through to mangroveai.crypto_assets."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import SdkError

router = APIRouter(
    prefix="/market",
    dependencies=[Depends(require_api_key)],
    tags=["market"],
)


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get("/ohlcv", summary="OHLCV bars for an asset")
async def ohlcv(
    symbol: str, lookback_days: int = 30, provider: str | None = None,
) -> Any:
    """Mirrors `mangroveai.crypto_assets.get_ohlcv(symbol, *, days, provider)`.

    No `timeframe` param — the SDK method doesn't accept one; bar
    granularity is provider-native. Optional `provider` selects a
    specific data source.
    """
    try:
        kwargs: dict[str, Any] = {"symbol": symbol, "days": lookback_days}
        if provider is not None:
            kwargs["provider"] = provider
        return _dump(mangrove_ai_client().crypto_assets.get_ohlcv(**kwargs))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"crypto_assets.get_ohlcv failed: {e}") from e


@router.get("/data", summary="Current market data (price, market cap, volume)")
async def market_data(symbol: str, provider: str | None = None) -> Any:
    """Mirrors `mangroveai.crypto_assets.get_market_data(symbol, *, provider)`."""
    try:
        kwargs: dict[str, Any] = {"symbol": symbol}
        if provider is not None:
            kwargs["provider"] = provider
        return _dump(mangrove_ai_client().crypto_assets.get_market_data(**kwargs))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"crypto_assets.get_market_data failed: {e}") from e


@router.get("/trending", summary="Trending assets")
async def trending() -> Any:
    try:
        return _dump(mangrove_ai_client().crypto_assets.get_trending())
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"crypto_assets.get_trending failed: {e}") from e


@router.get("/global", summary="Global market data (BTC dominance, total cap, 24h change)")
async def global_market() -> Any:
    try:
        return _dump(mangrove_ai_client().crypto_assets.get_global_market())
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"crypto_assets.get_global_market failed: {e}") from e
