"""oracle_service — score strategies through SIEVE, query the corpus,
run backtests against MangroveOracle.

Thin orchestrator over ``client.oracle.*`` from mangrove-ai >= 1.0.
Routes and MCP tools both call into this single module so business
logic isn't duplicated.

The Oracle surface lives behind MangroveAI's authenticated reverse
proxy at ``/api/v1/oracle/*``. Calls require an API key; the SDK
singleton in ``shared/clients/mangrove.py`` carries the auth header
forward.
"""
from __future__ import annotations

from typing import Any

from mangrove_ai.models.oracle import (
    DataQueryRequest,
    OracleBacktestRequest,
    SieveScoreRequest,
)
from pydantic import BaseModel, Field

from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import SdkError
from src.shared.logging import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Request models — server-facing shapes that wrap the SDK ones
# ---------------------------------------------------------------------------

class SieveScoreInput(BaseModel):
    """Score one strategy through the Mangrove SIEVE classifier.

    Server flattens to ``SieveScoreRequest(strategies=[...])`` and forwards.
    Limit batch size with ``BACKTEST_BATCH_CAP`` if the caller passes more.
    """

    strategies: list[dict[str, Any]] = Field(default_factory=list)


class DataQueryInput(BaseModel):
    """Query the curated Oracle corpus (results / ohlcv) through the
    BigQuery proxy. Columns and filter operators are whitelisted
    server-side; the agent only sees a safe surface.
    """

    table: str
    select: list[str]
    filters: list[dict[str, Any]] = Field(default_factory=list)
    order_by: list[str] | None = None
    limit: int = 100
    offset: int = 0


class OracleBacktestInput(BaseModel):
    """Backtest a single strategy through Oracle's engine.

    Mirrors ``mangrove_ai.models.oracle.OracleBacktestRequest`` but
    keeps the agent's local validation surface compact.
    """

    asset: str
    interval: str
    strategy_json: str
    lookback_months: int | None = 12
    initial_balance: float | None = None
    max_risk_per_trade: float | None = None
    execution_config: dict[str, Any] | None = None
    mode: str | None = None


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

def sieve_score(payload: SieveScoreInput) -> dict[str, Any]:
    """Score 1-99 strategies through SIEVE; return predictions + provenance.

    Returns the SDK response as a plain dict so the route layer can shape
    it for downstream consumers. The SDK already enforces the 99-item
    cap client-side and raises ValueError before sending.
    """
    if not payload.strategies:
        raise SdkError("sieve_score requires at least one strategy")

    client = mangrove_ai_client()
    try:
        result = client.oracle.sieve_score(
            SieveScoreRequest(strategies=payload.strategies)
        )
    except ValueError as exc:
        # Client-side rejection (e.g. >99 items, both/neither input set).
        raise SdkError(f"sieve_score validation failed: {exc}") from exc

    _log.info(
        "sieve_score",
        extra={
            "count": result.count,
            "model_version": result.model_version,
            "code_version": result.code_version,
        },
    )
    return result.model_dump()


def data_query(payload: DataQueryInput) -> dict[str, Any]:
    """Run a whitelisted query against the Oracle corpus."""
    client = mangrove_ai_client()
    result = client.oracle.data_query(
        DataQueryRequest(
            table=payload.table,
            select=payload.select,
            filters=payload.filters,
            order_by=payload.order_by,
            limit=payload.limit,
            offset=payload.offset,
        )
    )
    _log.info(
        "oracle.data_query",
        extra={
            "table": payload.table,
            "row_count": result.row_count,
            "code_version": result.code_version,
        },
    )
    return result.model_dump()


def backtest(payload: OracleBacktestInput) -> dict[str, Any]:
    """Run a single-strategy backtest synchronously through Oracle's engine.

    For long-running or many-strategy work, prefer ``backtest_async``
    (returns a backtest_id you poll). The synchronous variant blocks
    until the engine finishes; can be 30-120s for multi-month windows.
    """
    client = mangrove_ai_client()
    result = client.oracle.backtest(
        OracleBacktestRequest(
            asset=payload.asset,
            interval=payload.interval,
            strategy_json=payload.strategy_json,
            lookback_months=payload.lookback_months,
            initial_balance=payload.initial_balance,
            max_risk_per_trade=payload.max_risk_per_trade,
            execution_config=payload.execution_config,
            mode=payload.mode,
        )
    )
    _log.info(
        "oracle.backtest",
        extra={
            "asset": payload.asset,
            "success": result.success,
            "trade_count": result.trade_count,
        },
    )
    return result.model_dump()
