"""Domain Pydantic models — OrderIntent, Evaluation, Trade, Position.

These are the core data contracts that flow through the agent:
- OrderIntent: the unit of intent returned by mangroveai.execution.evaluate
  (or built from a user's /dex/swap request). Passed to order_executor.
- Evaluation: a single cron-tick log record. Stores the SDK response
  verbatim so we never lose audit trail.
- Trade: an actual execution event — live (with tx_hash) or paper
  (simulated).
- Position: open-or-closed position derived from entry/exit trade pairs.

All models match the SQLite schema in docs/specification.md exactly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class OrderIntent(BaseModel):
    """Pure output of strategy evaluation (or user-initiated swap request).

    No side effects. Input to order_executor which turns it into either a
    live DEX swap or a simulated paper fill.

    Token addresses are optional. When populated (user-initiated swaps via
    /dex/swap), the executor uses them verbatim. When absent (cron-driven
    strategies against a USDC-quoted asset), the executor falls back to
    {USDC, symbol} convention for the live DEX call.
    """

    action: Literal["enter", "exit"]
    side: Literal["buy", "sell"]
    symbol: str
    amount: float
    reason: str = ""  # which signal fired, or "user-initiated" for /dex/swap
    stop_loss: float | None = None
    take_profit: float | None = None
    # Explicit chain-level addresses for live execution. Both or neither.
    input_token_address: str | None = None
    output_token_address: str | None = None


class Evaluation(BaseModel):
    """A record of one cron-tick evaluation.

    OrderIntents come from the SDK (mangroveai.execution.evaluate), not from
    local logic — the agent does not evaluate strategies itself.
    """

    id: str
    strategy_id: str
    timestamp: datetime
    market_snapshot: dict = Field(default_factory=dict)  # optional context sent/received
    sdk_response: dict = Field(default_factory=dict)     # verbatim SDK EvaluateResult
    order_intents: list[OrderIntent] = Field(default_factory=list)
    duration_ms: int
    status: Literal["ok", "error", "skipped"]
    error_msg: str | None = None


class Trade(BaseModel):
    """A single order execution — live, paper, or validate (dry-run).

    Venue-agnostic. A DEX swap uses ``input_token``/``output_token``; a CEX
    fill (e.g. Kraken) uses ``base``/``quote``/``side``/``qty``. The venue +
    identity fields are all optional, so one record represents a DEX swap, a
    CEX fill, or a paper sim. ``strategy_id``/``order_intent`` are null for
    venue-direct trades (e.g. CEX BYOK) that aren't driven by a strategy.
    """

    id: str
    strategy_id: str | None = None
    evaluation_id: str | None = None  # null for user-initiated swaps
    order_intent: OrderIntent | None = None  # null for venue-direct (CEX) trades
    mode: Literal["live", "paper", "validate"]
    tx_hash: str | None = None  # on-chain (DEX) only; null for CEX spot / paper
    # DEX token in/out (optional aliases); CEX fills use base/quote/side/qty below.
    input_token: str | None = None
    input_amount: float | None = None
    output_token: str | None = None
    output_amount: float | None = None
    fill_price: float
    fees: dict = Field(default_factory=dict)  # gas, protocol, slippage
    status: Literal["pending", "confirmed", "failed", "simulated"]
    executed_at: datetime
    confirmed_at: datetime | None = None
    p_and_l: float | None = None  # filled when the position closes

    # -- venue + identity (CEX / telemetry; null for legacy DEX strategy trades) --
    venue: str | None = None            # kraken | 1inch | xpmarket | jupiter
    user_id: str | None = None          # server-stamped on ingestion; never client-set
    venue_order_ref: str | None = None  # e.g. Kraken ordertxid
    venue_trade_ref: str | None = None  # e.g. Kraken trade_id
    # CEX amount shape (DEX uses input/output_token above)
    base: str | None = None
    quote: str | None = None
    side: Literal["buy", "sell"] | None = None
    qty: float | None = None


class Position(BaseModel):
    """Open-or-closed position for a strategy."""

    id: str
    strategy_id: str
    asset: str
    entry_trade_id: str
    exit_trade_id: str | None = None
    entry_price: float
    entry_amount: float
    entry_time: datetime
    exit_price: float | None = None
    exit_amount: float | None = None
    exit_time: datetime | None = None
    status: Literal["open", "closed"]
    stop_loss: float | None = None
    take_profit: float | None = None


class Allocation(BaseModel):
    """Per-strategy fund commitment (live strategies only)."""

    id: str
    strategy_id: str
    wallet_address: str
    token_address: str
    token_symbol: str
    amount: float
    active: bool
    created_at: datetime
    released_at: datetime | None = None
    # Per-allocation slippage tolerance (DECIMAL, 0.005 = 0.5%). Capped
    # at 0.0025 (0.25%) in the Pydantic input layer. Nullable for rows
    # from pre-migration-004 databases; cron-driven swaps raise on None
    # rather than silently falling back.
    slippage_pct: float | None = None


def _to_db(value: Any) -> Any:
    """Convert a domain value to a SQLite-friendly primitive.

    - datetime -> ISO 8601 string (UTC)
    - dict/list -> JSON string
    - bool -> 0 or 1
    - None -> None
    - other primitives -> as-is
    """
    import json

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value
