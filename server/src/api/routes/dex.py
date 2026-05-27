"""DEX routes — auth-gated.

- GET  /api/v1/agent/dex/venues
- GET  /api/v1/agent/dex/pairs?venue_id
- POST /api/v1/agent/dex/quote
- POST /api/v1/agent/dex/swap (requires confirm=true)

venues/pairs/quote pass through to mangrovemarkets.dex directly. swap
builds an OrderIntent and hands it to order_executor — the SINGLE swap
path used for both user-initiated and cron-driven trades.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.models.domain import OrderIntent
from src.services.order_executor import execute_one
from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.errors import ConfirmationRequired, SdkError

router = APIRouter(
    prefix="/dex",
    dependencies=[Depends(require_api_key)],
    tags=["dex"],
)


@router.get("/venues", summary="List DEX venues")
async def dex_venues() -> list[Any]:
    try:
        venues = mangrove_markets_client().dex.supported_venues()
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.supported_venues failed: {e}") from e
    return [v.model_dump() if hasattr(v, "model_dump") else v for v in venues]


@router.get("/pairs", summary="List trading pairs for a venue")
async def dex_pairs(venue_id: str) -> list[Any]:
    try:
        pairs = mangrove_markets_client().dex.supported_pairs(venue_id=venue_id)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.supported_pairs failed: {e}") from e
    return [p.model_dump() if hasattr(p, "model_dump") else p for p in pairs]


class QuoteRequest(BaseModel):
    input_token: str
    output_token: str
    amount: float
    chain_id: int
    venue_id: str | None = None


@router.post("/quote", summary="Get a swap quote")
async def dex_quote(req: QuoteRequest) -> dict:
    try:
        quote = mangrove_markets_client().dex.get_quote(
            input_token=req.input_token,
            output_token=req.output_token,
            amount=req.amount,
            chain_id=req.chain_id,
            venue_id=req.venue_id,
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.get_quote failed: {e}") from e
    return quote.model_dump() if hasattr(quote, "model_dump") else quote


class SwapRequest(BaseModel):
    input_token: str
    output_token: str
    amount: float
    chain_id: int
    wallet_address: str
    slippage_pct: float = Field(
        ...,
        gt=0,
        le=0.0025,
        description=(
            "Slippage tolerance as DECIMAL. REQUIRED — no default. "
            "Range: (0, 0.0025] — max 0.25%. Typical values: 0.001 "
            "(0.1%), 0.002 (0.2%), 0.0025 (0.25% = cap). Anything "
            "higher is refused to prevent rekt-on-illiquid-pair "
            "execution. Picking a tolerance is a risk decision the "
            "user must make explicitly for live trades. Converted "
            "to the upstream percentage convention (multiplied by "
            "100) at the dex.prepare_swap() boundary."
        ),
    )
    mev_protection: bool = False
    venue_id: str | None = None
    confirm: bool = Field(
        False,
        description="Must be true. Protects against agent-initiated swaps without user approval.",
    )


@router.post(
    "/swap",
    summary="Execute a DEX swap",
    description=(
        "Full 6-step flow: quote → conditional approve → sign → broadcast → poll → "
        "prepare → sign → broadcast → poll. Signing is client-side; SDK never sees keys. "
        "Requires confirm=true."
    ),
)
async def dex_swap(req: SwapRequest) -> dict:
    if not req.confirm:
        raise ConfirmationRequired(
            "DEX swaps require confirm=true.",
            suggestion="Re-submit with confirm=true. This is intentional — protects against agent-initiated swaps without user approval.",
        )

    # Build an OrderIntent from the user's request. side=buy means "spend
    # input_token to get output_token"; from the intent's perspective the
    # symbol is the non-USDC leg.
    if req.output_token.upper() == "USDC":
        side = "sell"
        symbol = req.input_token
    else:
        side = "buy"
        symbol = req.output_token

    intent = OrderIntent(
        action="enter",
        side=side,
        symbol=symbol,
        amount=req.amount,
        reason="user-initiated",
        input_token_address=req.input_token,
        output_token_address=req.output_token,
    )

    trade = execute_one(
        intent,
        mode="live",
        wallet_address=req.wallet_address,
        chain_id=req.chain_id,
        venue_id=req.venue_id,
        slippage_pct=req.slippage_pct,
    )
    return {
        "tx_hash": trade.tx_hash,
        "status": trade.status,
        "input_token": trade.input_token,
        "input_amount": trade.input_amount,
        "output_token": trade.output_token,
        "output_amount": trade.output_amount,
        "fill_price": trade.fill_price,
        "fees": trade.fees,
        "approval_tx_hash": trade.fees.get("approval_tx_hash"),
        "trade_log_id": trade.id,
    }


def _dump(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


@router.get(
    "/tx-status",
    summary="Check the status of a broadcast transaction",
    description=(
        "Pass-through to mangrovemarkets.dex.tx_status. Use after "
        "execute_swap to verify a transaction landed + its final state "
        "(confirmed | pending | failed). Workshop-critical: lets the "
        "agent/user confirm a swap actually settled before acting on "
        "balance changes."
    ),
)
async def tx_status(
    tx_hash: str,
    chain_id: int,
    venue_id: str | None = None,
) -> Any:
    try:
        return _dump(mangrove_markets_client().dex.tx_status(
            tx_hash=tx_hash, chain_id=chain_id, venue_id=venue_id,
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.tx_status failed: {e}") from e


@router.get(
    "/token-info",
    summary="Look up token metadata by contract address",
    description=(
        "Pass-through to mangrovemarkets.dex.token_info. Returns "
        "symbol, decimals, name, and any venue-specific metadata for "
        "the token at `address` on `chain_id`."
    ),
)
async def token_info(chain_id: int, address: str) -> Any:
    try:
        return _dump(mangrove_markets_client().dex.token_info(
            chain_id=chain_id, address=address,
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.token_info failed: {e}") from e


@router.get(
    "/spot-price",
    summary="Current spot price for one or more tokens",
    description=(
        "Pass-through to mangrovemarkets.dex.spot_price. `tokens` is a "
        "comma-separated list of symbols or addresses."
    ),
)
async def spot_price(chain_id: int, tokens: str) -> Any:
    try:
        return _dump(mangrove_markets_client().dex.spot_price(
            chain_id=chain_id, tokens=tokens,
        ))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.spot_price failed: {e}") from e


@router.get(
    "/gas-price",
    summary="Current gas price estimate for the chain",
    description=(
        "Pass-through to mangrovemarkets.dex.gas_price. Useful as a "
        "pre-flight check before a swap — estimate how much the tx "
        "will cost before committing."
    ),
)
async def gas_price(chain_id: int) -> Any:
    try:
        return _dump(mangrove_markets_client().dex.gas_price(chain_id=chain_id))
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.gas_price failed: {e}") from e
