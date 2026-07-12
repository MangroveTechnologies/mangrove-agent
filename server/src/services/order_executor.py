"""order_executor — SINGLE swap path for cron-driven + user-initiated trades.

Takes an OrderIntent and executes it:
- Paper mode: fetch current market price via mangroveai.crypto_assets,
  build a simulated Trade row with mode=paper, status=simulated,
  tx_hash=None.
- Live mode: full 6-step DEX swap via mangrovemarkets:
    1. dex_service.get_quote (human amount -> base units at the boundary)
    2. dex.approve_token (may return None if allowance already set)
    3. If approval returned: wallet_manager.sign → dex.broadcast → poll tx_status
    4. dex.prepare_swap
    5. wallet_manager.sign → dex.broadcast
    6. poll dex.tx_status until confirmed
  Build a Trade row with mode=live, tx_hash, fill price from quote.

Both paths end with trade_log.log_trade + optionally update_position.

Callers:
- strategy_service.tick (cron-driven): passes order_intents from
  mangroveai.execution.evaluate() + the strategy's mode + wallet_address
  from the allocation.
- POST /dex/swap route (user-initiated): builds an OrderIntent from the
  request body + hands to execute_one(mode='live').
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from src.config import app_config
from src.models.domain import OrderIntent, Trade
from src.services import dex_service, trade_log
from src.services.wallet_manager import sign as wallet_sign
from src.shared.clients.mangrove import mangrove_ai_client, mangrove_markets_client
from src.shared.errors import SdkError, SigningError
from src.shared.logging import get_logger

_log = get_logger(__name__)

# How long to wait for a tx to move past "pending" when broadcasting a live swap.
_TX_POLL_TIMEOUT_S = 60.0
_TX_POLL_INTERVAL_S = 2.0


def _fetch_mark_price(symbol: str) -> float:
    """Pull a current mark price via crypto_assets.get_market_data()."""
    resp = mangrove_ai_client().crypto_assets.get_market_data(symbol)
    data = getattr(resp, "data", None) or {}
    for key in ("current_price", "price", "usd_price"):
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    raise SdkError(
        f"No current price found in market data for {symbol}.",
        suggestion="Check the symbol is recognized by mangroveai.crypto_assets.get_market_data().",
    )


def _paper_fill(intent: OrderIntent, strategy_id: str, evaluation_id: str | None) -> Trade:
    """Simulate a paper fill at the current mark price."""
    mark = _fetch_mark_price(intent.symbol)
    # Convention: "buy" spends USDC for the asset; "sell" does the reverse.
    # For paper we're not actually moving funds; we just record what the fill
    # would have looked like.
    if intent.side == "buy":
        input_token, output_token = "USDC", intent.symbol
        input_amount = intent.amount * mark
        output_amount = intent.amount
    else:
        input_token, output_token = intent.symbol, "USDC"
        input_amount = intent.amount
        output_amount = intent.amount * mark

    trade = Trade(
        id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        evaluation_id=evaluation_id,
        order_intent=intent,
        mode="paper",
        tx_hash=None,
        input_token=input_token,
        input_amount=input_amount,
        output_token=output_token,
        output_amount=output_amount,
        fill_price=mark,
        fees={},
        status="simulated",
        executed_at=trade_log.now_utc(),
    )
    trade_log.log_trade(trade)
    _log.info(
        "order.paper.simulated",
        trade_id=trade.id,
        strategy_id=strategy_id,
        symbol=intent.symbol,
        side=intent.side,
        amount=intent.amount,
        fill_price=mark,
    )
    return trade


def _poll_tx(tx_hash: str, chain_id: int, venue_id: str | None = None) -> dict[str, Any]:
    """Poll dex.tx_status until it's out of 'pending' or timeout."""
    client = mangrove_markets_client()
    deadline = time.monotonic() + _TX_POLL_TIMEOUT_S
    last_status: Any = None
    while time.monotonic() < deadline:
        status = client.dex.tx_status(tx_hash=tx_hash, chain_id=chain_id, venue_id=venue_id)
        last_status = status
        s = (getattr(status, "status", "") or "").lower()
        if s and s != "pending":
            return {
                "tx_hash": tx_hash,
                "status": s,
                "block_number": getattr(status, "block_number", None),
                "error": getattr(status, "error_message", None),
            }
        time.sleep(_TX_POLL_INTERVAL_S)
    return {
        "tx_hash": tx_hash,
        "status": (getattr(last_status, "status", "timeout") or "timeout"),
        "block_number": None,
        "error": "poll timeout",
    }


def _validate_slippage(slippage_pct: float | None) -> None:
    """Raise SigningError if slippage_pct is missing or out of range."""
    if slippage_pct is None:
        raise SigningError(
            "slippage_pct is required for live swaps.",
            suggestion=(
                "Direct swap callers pass slippage_pct in the request body "
                "(decimal, e.g. 0.002 = 0.2%). Cron-driven swaps pull it "
                "from the active allocation — re-promote the strategy to "
                "live with an allocation block that includes slippage_pct."
            ),
        )
    if slippage_pct <= 0 or slippage_pct > 0.0025:
        raise SigningError(
            f"slippage_pct {slippage_pct} outside allowed range (0, 0.0025].",
            suggestion=(
                "Max allowed slippage is 0.25% (0.0025 decimal). Tighter "
                "values trade off fewer fills for better prices; looser "
                "values are refused to prevent rekt-on-illiquid-pair "
                "execution."
            ),
        )


def _resolve_tokens(intent: OrderIntent) -> tuple[str, str, float]:
    """Determine input/output token addresses and amount from an OrderIntent.

    Prefers explicit token addresses (user-initiated swaps via /dex/swap);
    falls back to USDC+symbol convention for cron strategy intents.
    """
    if intent.input_token_address and intent.output_token_address:
        return intent.input_token_address, intent.output_token_address, intent.amount
    if intent.side == "buy":
        return "USDC", intent.symbol, intent.amount
    return intent.symbol, "USDC", intent.amount


def _handle_approval(
    client: Any,
    input_token: str,
    chain_id: int,
    wallet_address: str,
    venue_id: str | None,
) -> str | None:
    """Steps 2-3: approve token + sign/broadcast + poll. Returns approval tx_hash or None."""
    try:
        approval_tx = client.dex.approve_token(
            token_address=input_token,
            chain_id=chain_id,
            wallet_address=wallet_address,
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.approve_token failed: {e}") from e

    if approval_tx is None:
        return None

    signed_approval = wallet_sign(
        getattr(approval_tx, "payload", {}), wallet_address, chain_id=chain_id,
    )
    _log.info("order.live.signed", kind="approval", wallet=wallet_address)
    try:
        broadcast_result = client.dex.broadcast(signed_tx=signed_approval, chain_id=chain_id, venue_id=venue_id)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.broadcast(approval) failed: {e}") from e

    approval_tx_hash = getattr(broadcast_result, "tx_hash", None)
    _log.info("order.live.broadcast", kind="approval", tx_hash=approval_tx_hash)
    if approval_tx_hash:
        poll_result = _poll_tx(approval_tx_hash, chain_id, venue_id)
        if poll_result["status"] not in ("confirmed", "success"):
            raise SdkError(
                f"Approval tx did not confirm: {poll_result['status']} ({poll_result.get('error')})",
            )
    return approval_tx_hash


def _live_swap(
    intent: OrderIntent,
    strategy_id: str,
    evaluation_id: str | None,
    wallet_address: str,
    chain_id: int | None = None,
    venue_id: str | None = None,
    slippage_pct: float | None = None,
) -> Trade:
    """Execute the full 6-step live swap flow.

    The SDK never receives the private key — signing happens locally via
    wallet_manager.sign(). The SDK sees unsigned tx payloads + signed tx
    hex strings.

    `slippage_pct` is the user's tolerance as a DECIMAL (0.005 = 0.5%).
    REQUIRED for live swaps — no fallback. Direct swaps supply it via
    SwapRequest; cron swaps via the active allocation (migration 004
    added `slippage_pct` to the allocations table). Capped at 0.0025
    (0.25%) at the input layer; re-checked here as defense-in-depth.
    Converted to the upstream's percentage convention (1.0 = 1%) at
    the `dex.prepare_swap()` boundary.
    """
    if chain_id is None:
        raise SigningError(
            "chain_id is required for live swaps.",
            suggestion="Pass chain_id in the OrderIntent metadata or strategy config.",
        )

    _validate_slippage(slippage_pct)

    client = mangrove_markets_client()
    input_token, output_token, input_amount = _resolve_tokens(intent)

    # 1. Quote — via dex_service, the single human<->base-units boundary.
    # `input_amount` is HUMAN units (the agent's convention everywhere);
    # dex_service resolves the token's decimals, converts to the base
    # units the backend expects, rejects dust that rounds to 0, and
    # converts the returned amounts back to human units.
    quote = dex_service.get_quote(
        input_token=input_token,
        output_token=output_token,
        amount=input_amount,
        chain_id=chain_id,
        venue_id=venue_id,
    )

    _log.info("order.executing", trade_symbol=intent.symbol, side=intent.side,
              input_token=input_token, output_token=output_token,
              input_amount=input_amount, quote_id=quote.get("quote_id"))

    fees: dict[str, Any] = {
        "venue_fee": quote.get("venue_fee", 0.0),
        "mangrove_fee": quote.get("mangrove_fee", 0.0),
        "price_impact_percent": quote.get("price_impact_percent", 0.0),
    }

    # 2-3. Conditional token approval
    approval_tx_hash = _handle_approval(client, input_token, chain_id, wallet_address, venue_id)

    # 4. Prepare swap
    # Upstream `dex.prepare_swap` takes slippage as a PERCENTAGE
    # (1.0 = 1%, documented in MangroveMarkets-MCP-Server/src/dex/tools.py).
    # Our API convention is DECIMAL (0.005 = 0.5%) to match the rest of
    # the trading stack (trading_defaults.backtest_defaults.slippage_pct
    # = 0.004 = 0.4%). Convert at the boundary.
    sdk_slippage = slippage_pct * 100.0  # type: ignore[operator]
    try:
        swap_tx = client.dex.prepare_swap(
            quote_id=quote["quote_id"],
            wallet_address=wallet_address,
            slippage=sdk_slippage,
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.prepare_swap failed: {e}") from e

    # 5. Sign + broadcast the swap
    signed_swap = wallet_sign(getattr(swap_tx, "payload", {}), wallet_address, chain_id=chain_id)
    _log.info("order.live.signed", kind="swap", wallet=wallet_address)
    try:
        broadcast_result = client.dex.broadcast(signed_tx=signed_swap, chain_id=chain_id, venue_id=venue_id)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.broadcast(swap) failed: {e}") from e
    tx_hash = getattr(broadcast_result, "tx_hash", None)
    _log.info("order.live.broadcast", kind="swap", tx_hash=tx_hash)

    # 6. Poll
    final = _poll_tx(tx_hash, chain_id, venue_id) if tx_hash else {"status": "failed", "error": "no tx_hash"}
    status_str = final["status"]
    confirmed_at = trade_log.now_utc() if status_str in ("confirmed", "success") else None
    trade_status = "confirmed" if status_str in ("confirmed", "success") else ("failed" if status_str == "failed" else "pending")
    _log.info("order.live.confirmed", tx_hash=tx_hash, status=status_str,
              block_number=final.get("block_number"))

    # dex_service already converted output_amount back to human units
    # (the raw value is quote["output_amount_base_units"]).
    output_amount = float(quote.get("output_amount", 0.0))

    # Fill price in the paper-mode convention: the price of intent.symbol
    # in the counter-token (input per output on a buy, output per input on
    # a sell). The quote's raw exchange_rate is a BASE-UNITS ratio (e.g.
    # 1.79e-9 for WETH->USDC), not a human price — never store it as one.
    if intent.side == "buy":
        fill_price = (input_amount / output_amount) if output_amount else 0.0
    else:
        fill_price = (output_amount / input_amount) if input_amount else 0.0

    trade = Trade(
        id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        evaluation_id=evaluation_id,
        order_intent=intent,
        mode="live",
        tx_hash=tx_hash,
        input_token=input_token,
        input_amount=input_amount,
        output_token=output_token,
        output_amount=output_amount,
        fill_price=fill_price,
        fees={**fees, "approval_tx_hash": approval_tx_hash},
        status=trade_status,
        executed_at=trade_log.now_utc(),
        confirmed_at=confirmed_at,
    )
    trade_log.log_trade(trade)
    return trade


def execute_one(
    intent: OrderIntent,
    mode: Literal["paper", "live"],
    strategy_id: str = "user-initiated",
    evaluation_id: str | None = None,
    wallet_address: str | None = None,
    chain_id: int | None = None,
    venue_id: str | None = None,
    slippage_pct: float | None = None,
) -> Trade:
    """Execute a single OrderIntent.

    `slippage_pct` is a DECIMAL (0.005 = 0.5%). REQUIRED for live mode —
    there is no fallback; _validate_slippage raises without it. Direct
    callers pass it in the request body, cron callers pull it from the
    active allocation (migration 004).

    strategy_id defaults to "user-initiated" for /dex/swap-style callers;
    cron-driven callers pass the real strategy UUID.
    """
    if mode == "paper":
        return _paper_fill(intent, strategy_id=strategy_id, evaluation_id=evaluation_id)
    if mode == "live":
        if not wallet_address:
            raise SigningError(
                "wallet_address is required for live execution.",
                suggestion="Pass a wallet_address from the strategy's active allocation, or from the /dex/swap request body.",
            )
        # Backup gate: live trading refuses on wallets the user hasn't
        # confirmed backing up. The user's signed-off backup is the
        # disaster-recovery path if the master key is ever lost; we will
        # not move real funds without it. Paper mode is exempt (above).
        from src.services.wallet_manager import require_backup_confirmed
        require_backup_confirmed(wallet_address)
        return _live_swap(
            intent,
            strategy_id=strategy_id,
            evaluation_id=evaluation_id,
            wallet_address=wallet_address,
            chain_id=chain_id,
            venue_id=venue_id,
            slippage_pct=slippage_pct,
        )
    raise SigningError(f"Unknown mode: {mode}")


def execute_many(
    intents: list[OrderIntent],
    mode: Literal["paper", "live"],
    strategy_id: str,
    evaluation_id: str | None = None,
    wallet_address: str | None = None,
    chain_id: int | None = None,
    venue_id: str | None = None,
    slippage_pct: float | None = None,
) -> list[Trade]:
    """Execute N intents in order. Failures on one do not abort the batch.

    Exceptions during a single intent's execution are logged as
    order.errored and the batch continues; the returned list only contains
    trades that were successfully logged to SQLite.
    """
    results: list[Trade] = []
    _ = app_config  # config access forces validation at import time in some tests
    for intent in intents:
        try:
            t = execute_one(
                intent,
                mode=mode,
                strategy_id=strategy_id,
                evaluation_id=evaluation_id,
                wallet_address=wallet_address,
                chain_id=chain_id,
                venue_id=venue_id,
                slippage_pct=slippage_pct,
            )
            results.append(t)
        except Exception as e:  # noqa: BLE001
            _log.error(
                "order.errored",
                strategy_id=strategy_id,
                symbol=intent.symbol,
                side=intent.side,
                exception=str(e),
            )
            # Intentional: don't re-raise. The caller (strategy_service.tick)
            # logs the evaluation and moves on.
    return results
