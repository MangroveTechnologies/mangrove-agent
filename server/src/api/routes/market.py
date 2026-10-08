"""Market data routes — auth-gated; pass-through to mangroveai.crypto_assets,
plus the server-owned buy-and-hold benchmark (benchmark_service)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from src.services import benchmark_service
from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, SdkError, ValidationError, upstream_access_error

router = APIRouter(
    prefix="/market",
    dependencies=[Depends(require_api_key)],
    tags=["market"],
)


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get("/ohlcv", summary="OHLCV bars for an asset")
async def ohlcv(
    request: Request, symbol: str, lookback_days: int = 30, provider: str | None = None,
) -> Any:
    """Mirrors `mangroveai.crypto_assets.get_ohlcv(symbol, *, days, provider)`.

    No `timeframe` param — the SDK method doesn't accept one; daily
    bars are requested by the server. Optional `provider` selects a
    specific data source.
    """
    if set(request.query_params) - {"symbol", "lookback_days", "provider"}:
        raise ValidationError("OHLCV supports symbol, lookback_days and provider; bars are daily.")
    try:
        kwargs: dict[str, Any] = {"symbol": symbol, "days": lookback_days}
        if provider is not None:
            kwargs["provider"] = provider
        return _dump(await run_in_threadpool(mangrove_ai_client().crypto_assets.get_ohlcv, **kwargs))
    except AgentError:
        raise
    except Exception as exc:
        access_error = upstream_access_error(exc)
        if access_error is not None:
            raise access_error from None
        raise SdkError("crypto_assets.get_ohlcv failed at the upstream service.") from None


@router.get("/data", summary="Current market data (price, market cap, volume)")
async def market_data(symbol: str, provider: str | None = None) -> Any:
    """Mirrors `mangroveai.crypto_assets.get_market_data(symbol, *, provider)`."""
    try:
        kwargs: dict[str, Any] = {"symbol": symbol}
        if provider is not None:
            kwargs["provider"] = provider
        return _dump(await run_in_threadpool(mangrove_ai_client().crypto_assets.get_market_data, **kwargs))
    except AgentError:
        raise
    except Exception as exc:
        access_error = upstream_access_error(exc)
        if access_error is not None:
            raise access_error from None
        raise SdkError("crypto_assets.get_market_data failed at the upstream service.") from None


@router.get("/trending", summary="Trending assets")
async def trending() -> Any:
    try:
        return _dump(await run_in_threadpool(mangrove_ai_client().crypto_assets.get_trending))
    except AgentError:
        raise
    except Exception as exc:
        access_error = upstream_access_error(exc)
        if access_error is not None:
            raise access_error from None
        raise SdkError("crypto_assets.get_trending failed at the upstream service.") from None


@router.get("/global", summary="Global market data (BTC dominance, total cap, 24h change)")
async def global_market() -> Any:
    try:
        return _dump(await run_in_threadpool(mangrove_ai_client().crypto_assets.get_global_market))
    except AgentError:
        raise
    except Exception as exc:
        access_error = upstream_access_error(exc)
        if access_error is not None:
            raise access_error from None
        raise SdkError("crypto_assets.get_global_market failed at the upstream service.") from None


@router.get("/benchmark", summary="Buy-and-hold return for an asset over a window")
async def benchmark(
    asset: str,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
    base_token: str | None = None, quote_token: str | None = None,
    market_data_venue: str | None = None,
) -> dict[str, Any]:
    """First close to last close, as a percentage on the 0-100 scale.

    Window: `start_date` + `end_date` (ISO), or `lookback_days` ending now.
    `covered_window` reports what the bars actually covered.
    """
    return await run_in_threadpool(benchmark_service.get_benchmark,
        asset, start_date=start_date, end_date=end_date, lookback_days=lookback_days,
        base_token=base_token, quote_token=quote_token, market_data_venue=market_data_venue,
    )
