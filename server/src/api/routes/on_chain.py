"""On-chain analytics routes — pass-through to mangrove_ai.on_chain."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

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


# ---------------------------------------------------------------------------
# Legacy GET routes (3 endpoints, ship since v0)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Nansen Pro coverage — 5 POST endpoints added in mangroveai 1.1.0+
#
# These take rich JSON filters / order_by passed straight through to
# Nansen. The agent surfaces them as POST routes (not GET) because the
# filter shape is too complex for query-string encoding.
# ---------------------------------------------------------------------------


class _SmartMoneyHistoricalHoldingsBody(BaseModel):
    chains: list[str] | None = Field(default=None, description="Chain filter, e.g. ['ethereum', 'solana']. Default ['ethereum'].")
    date_from: str | None = Field(default=None, description="ISO date 'YYYY-MM-DD'.")
    date_to: str | None = Field(default=None, description="ISO date 'YYYY-MM-DD'.")
    filters: dict[str, Any] | None = Field(default=None, description="Nansen filter dict (include_smart_money_labels, etc.)")
    order_by: list[dict[str, str]] | None = Field(default=None, description="Sort spec, e.g. [{'field': 'block_timestamp', 'direction': 'DESC'}]")
    page: int = 1
    per_page: int = 100


@router.post("/smart-money/historical-holdings", summary="Smart Money historical holdings (Nansen)")
async def smart_money_historical_holdings(body: _SmartMoneyHistoricalHoldingsBody) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_smart_money_historical_holdings(
            **body.model_dump(exclude_none=True),
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_smart_money_historical_holdings failed: {e}") from e


class _SmartMoneyDexTradesBody(BaseModel):
    chains: list[str] | None = None
    filters: dict[str, Any] | None = None
    order_by: list[dict[str, str]] | None = None
    page: int = 1
    per_page: int = 100


@router.post("/smart-money/dex-trades", summary="Smart Money DEX trades (Nansen)")
async def smart_money_dex_trades(body: _SmartMoneyDexTradesBody) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_smart_money_dex_trades(
            **body.model_dump(exclude_none=True),
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_smart_money_dex_trades failed: {e}") from e


class _SmartMoneyPerpTradesBody(BaseModel):
    filters: dict[str, Any] | None = None
    order_by: list[dict[str, str]] | None = None
    page: int = 1
    per_page: int = 100


@router.post("/smart-money/perp-trades", summary="Smart Money Hyperliquid perp trades (Nansen)")
async def smart_money_perp_trades(body: _SmartMoneyPerpTradesBody) -> Any:
    """Hyperliquid-only. No chain filter — upstream doesn't accept one."""
    try:
        return _dump(mangrove_ai_client().on_chain.get_smart_money_perp_trades(
            **body.model_dump(exclude_none=True),
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_smart_money_perp_trades failed: {e}") from e


class _TokenDexTradesBody(BaseModel):
    chain: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    filters: dict[str, Any] | None = None
    order_by: list[dict[str, str]] | None = None
    page: int = 1
    per_page: int = 100


@router.post("/token-dex-trades/{symbol}", summary="DEX trades for a token across all participants (Nansen)")
async def token_dex_trades(symbol: str, body: _TokenDexTradesBody) -> Any:
    try:
        return _dump(mangrove_ai_client().on_chain.get_token_dex_trades(
            symbol, **body.model_dump(exclude_none=True),
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_token_dex_trades failed: {e}") from e


class _TokenFlowsBody(BaseModel):
    chain: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    filters: dict[str, Any] | None = None
    order_by: list[dict[str, str]] | None = None
    page: int = 1
    per_page: int = 100


@router.post("/token-flows/{symbol}", summary="Per-wallet-category flow data for a token (Nansen)")
async def token_flows(symbol: str, body: _TokenFlowsBody) -> Any:
    """Stablecoins are not supported (Nansen returns 404)."""
    try:
        return _dump(mangrove_ai_client().on_chain.get_token_flows(
            symbol, **body.model_dump(exclude_none=True),
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"on_chain.get_token_flows failed: {e}") from e
