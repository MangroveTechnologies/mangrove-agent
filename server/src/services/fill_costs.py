"""Backtest fill costs shared with paper execution.

MangroveAI's backtester reads ``fee_pct`` and ``slippage_pct`` from
``trading_defaults.json`` (``backtest_defaults``) when a run omits them.
``client.config.trading_defaults()`` is that same payload. Execution
defaults deliberately leave the two rates out; they are canon, not a
per-strategy execution_config knob.

Published schedule (OpenAPI ``BacktestRequest``):

- Slippage on each leg is a draw from ``Uniform(0, slippage_pct)``,
  adverse to the order. Paper uses the mean, ``slippage_pct / 2``, so a
  paper fill and the expected backtest fill match. One backtest draw
  differs from that mean by at most half of ``slippage_pct`` of the mark
  notional on that leg (``slippage_realization_bound``).
- The fee is a size-dependent rate on the closed entry notional. Trades
  of $10,000 and below pay ``fee_pct``. Trades of $1,000,000 and above
  pay 0.0589 percent, and never more than ``fee_pct``. Sizes in between
  pay the linear rate. The fee is charged only when the slipped round
  trip is profitable.

Paper and this deterministic model agree within
``PAPER_BACKTEST_REL_TOLERANCE`` (1e-9 relative) for the same order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.shared.errors import SdkError

# Relative agreement required between a paper fill and the deterministic
# backtest cost for the same mark, side, quantity, and parameters.
PAPER_BACKTEST_REL_TOLERANCE = 1e-9

_SMALL_NOTIONAL = Decimal("10000")
_LARGE_NOTIONAL = Decimal("1000000")
# 0.0589% — the published rate for notionals of $1,000,000 and above.
_LARGE_TRADE_FEE_RATE = Decimal("0.000589")


@dataclass(frozen=True)
class CostParams:
    """Canon backtest fee and slippage, as decimals (0.0085 = 0.85%)."""

    fee_pct: Decimal
    slippage_pct: Decimal


@dataclass(frozen=True)
class LegQuote:
    """One fill after expected slippage, before a round-trip fee."""

    fill_price: Decimal
    notional: Decimal
    params: CostParams

    @property
    def slippage_applied(self) -> Decimal:
        return self.params.slippage_pct / 2


def _rate(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise SdkError(f"Backtest {name} is missing from trading defaults.") from exc
    if not number.is_finite() or number < 0:
        raise SdkError(f"Backtest {name} must be a non-negative finite decimal.")
    return number


def params_from_payload(payload: object) -> CostParams:
    """Read ``backtest_defaults`` from a trading-defaults response."""
    if not isinstance(payload, dict):
        raise SdkError("Trading defaults did not include backtest cost parameters.")
    block = payload.get("backtest_defaults")
    if not isinstance(block, dict):
        raise SdkError("Trading defaults are missing backtest_defaults.")
    return CostParams(
        fee_pct=_rate(block.get("fee_pct"), "fee_pct"),
        slippage_pct=_rate(block.get("slippage_pct"), "slippage_pct"),
    )


def _fetch_trading_defaults() -> object:
    """GET the canon trading defaults the backtester fills from."""
    from src.shared.clients.mangrove import mangrove_ai_client

    try:
        payload = mangrove_ai_client().config.trading_defaults()
    except SdkError:
        raise
    except Exception as exc:
        raise SdkError(
            "Could not read backtest cost parameters from trading defaults."
        ) from exc
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    return payload


def load_cost_params() -> CostParams:
    """Parameters a backtest uses when the request omits fee and slippage."""
    return params_from_payload(_fetch_trading_defaults())


def slippage_realization_bound(mark_notional: Decimal, params: CostParams) -> Decimal:
    """Largest gap between paper and one backtest slippage draw, per leg.

    The backtester draws ``Uniform(0, slippage_pct)``. Paper uses the mean.
    The bound is half of ``slippage_pct`` times the pre-cost mark notional.
    """
    if mark_notional < 0:
        raise SdkError("Mark notional must be non-negative.")
    return mark_notional * params.slippage_pct / 2


def fee_rate(notional: Decimal, fee_pct: Decimal) -> Decimal:
    """Linear schedule from ``fee_pct`` at $10k down to the large-trade floor."""
    if notional < 0 or not fee_pct.is_finite() or fee_pct < 0:
        raise SdkError("Fee schedule inputs must be non-negative.")
    floor = min(_LARGE_TRADE_FEE_RATE, fee_pct)
    if notional <= _SMALL_NOTIONAL:
        return fee_pct
    if notional >= _LARGE_NOTIONAL:
        return floor
    span = _LARGE_NOTIONAL - _SMALL_NOTIONAL
    weight = (notional - _SMALL_NOTIONAL) / span
    return fee_pct + weight * (floor - fee_pct)


def leg_fee(notional: Decimal, fee_pct: Decimal) -> Decimal:
    """Cash fee for a closed notional at the size-dependent rate."""
    return fee_rate(notional, fee_pct) * notional


def quote_leg(mark: Decimal, side: str, quantity: Decimal, params: CostParams) -> LegQuote:
    """Backtest fill at the expected adverse slippage. No fee yet.

    Buys pay more than the mark. Sells receive less. The fee is a property
    of the closed round trip, not of the opening leg.
    """
    if side not in {"buy", "sell"}:
        raise SdkError("Paper cost model supports buy and sell legs only.")
    if mark <= 0 or quantity <= 0:
        raise SdkError("Paper fill requires a positive mark and quantity.")
    slip = params.slippage_pct / 2
    price = mark * (1 + slip) if side == "buy" else mark * (1 - slip)
    if price <= 0:
        raise SdkError("Slippage removed the entire paper fill price.")
    return LegQuote(fill_price=price, notional=price * quantity, params=params)


def within_tolerance(actual: Decimal, expected: Decimal) -> bool:
    """True when ``actual`` matches ``expected`` within the stated relative tolerance."""
    if expected == 0:
        return abs(actual) <= Decimal(str(PAPER_BACKTEST_REL_TOLERANCE))
    return abs(actual - expected) / abs(expected) <= Decimal(str(PAPER_BACKTEST_REL_TOLERANCE))


def _decimal_text(number: Decimal) -> str:
    """Plain decimal text without trailing zeros from intermediate precision."""
    return format(number.normalize(), "f")


def fee_record(params: CostParams, notional: Decimal, charged: Decimal) -> dict[str, str]:
    """Journal payload. Strings keep the decimal schedule exact in SQLite."""
    rate = fee_rate(notional, params.fee_pct) if notional > 0 else Decimal(0)
    return {
        "model": "backtest",
        "source": "trading_defaults.backtest_defaults",
        "fee_pct": _decimal_text(params.fee_pct),
        "slippage_pct": _decimal_text(params.slippage_pct),
        "slippage_applied": _decimal_text(params.slippage_pct / 2),
        "notional": _decimal_text(notional),
        "fee_rate": _decimal_text(rate),
        "leg_fee": _decimal_text(rate * notional),
        "fee": _decimal_text(charged),
    }


def journal_complete(fees: object) -> bool:
    """Legacy zero-cost rows and modeled backtest costs are complete journals.

    Any other fee payload (native gas, a venue fee, an unknown shape) still
    holds new entries. Modeled costs are the paper book's own accounting.
    """
    if fees is None:
        return True
    if not isinstance(fees, dict):
        return False
    if not fees:
        return True
    if fees.get("model") != "backtest" or fees.get("source") != "trading_defaults.backtest_defaults":
        return False
    try:
        fee_pct = Decimal(fees["fee_pct"])
        slippage_pct = Decimal(fees["slippage_pct"])
        applied = Decimal(fees["slippage_applied"])
        notional = Decimal(fees["notional"])
        charged = Decimal(fees["fee"])
    except Exception:
        return False
    numbers = (fee_pct, slippage_pct, applied, notional, charged)
    if any(not number.is_finite() or number < 0 for number in numbers):
        return False
    return applied == slippage_pct / 2


def params_from_fees(fees: dict, fallback: CostParams) -> CostParams:
    """Keep an open position on the rates recorded at its entry."""
    if not isinstance(fees, dict) or fees.get("model") != "backtest":
        return fallback
    try:
        return CostParams(Decimal(fees["fee_pct"]), Decimal(fees["slippage_pct"]))
    except Exception:
        return fallback


def round_trip_fee(
    *,
    entry_notional: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    fee_pct: Decimal,
) -> Decimal:
    """Fee for a long close. Zero unless the slipped round trip made money."""
    if quantity <= 0 or entry_notional < 0:
        raise SdkError("Round-trip fee requires a positive close and a known entry notional.")
    gross = (exit_price - entry_price) * quantity
    if gross <= 0:
        return Decimal(0)
    return leg_fee(entry_notional, fee_pct)


def round_trip_pnl(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    fee: Decimal,
) -> Decimal:
    """Net cash change of a long round trip after slippage and the fee."""
    return (exit_price - entry_price) * quantity - fee
