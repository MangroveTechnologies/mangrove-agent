"""Payload for legacy execution-record backtests; no generation policy."""
from typing import Any

from pydantic import BaseModel


class StrategyCandidate(BaseModel):
    """A proposed strategy — ready to backtest or submit to mangroveai."""

    name: str
    asset: str
    timeframe: str
    entry: list[dict[str, Any]]
    exit: list[dict[str, Any]]

