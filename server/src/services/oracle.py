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

import time
from typing import Any

from mangrove_ai.exceptions import APIError
from mangrove_ai.models.oracle import (
    DataQueryRequest,
    OracleBacktestRequest,
    OracleBulkBacktestRequest,
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


# ---------------------------------------------------------------------------
# Async + bulk backtest wrappers
# ---------------------------------------------------------------------------


def backtest_async(payload: OracleBacktestInput) -> dict[str, Any]:
    """Submit a backtest for async execution.

    Returns ``{backtest_id, status}`` immediately. Customers poll
    ``backtest_poll(backtest_id)`` to retrieve the full result once
    the engine finishes. Use this for long windows / heavy parameters
    where the 30-120s sync block is unworkable.
    """
    client = mangrove_ai_client()
    result = client.oracle.backtest_async(
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
    _log.info("oracle.backtest_async", extra={"backtest_id": result.backtest_id})
    return result.model_dump()


def backtest_poll(backtest_id: str) -> dict[str, Any]:
    """Poll status / result of an async backtest."""
    client = mangrove_ai_client()
    result = client.oracle.backtest_poll(backtest_id)
    _log.info(
        "oracle.backtest_poll",
        extra={"backtest_id": backtest_id, "status": result.status},
    )
    return result.model_dump()


def backtest_bulk(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate many strategies over a shared date range in one call.

    Payload mirrors ``mangrove_ai.models.oracle.OracleBulkBacktestRequest``
    — supply ``strategy_ids`` (DB lookups), ``strategy_configs`` (inline
    dicts), or both, plus the shared date range / risk parameters. The
    server fetches OHLCV once per unique ``(asset, timeframe)`` and
    re-uses it across all strategies that need it.
    """
    client = mangrove_ai_client()
    result = client.oracle.backtest_bulk(OracleBulkBacktestRequest(**payload))
    _log.info(
        "oracle.backtest_bulk",
        extra={
            "success": result.success,
            "data_fetches": result.data_fetches,
            "n_results": len(result.results),
        },
    )
    return result.model_dump()


# ---------------------------------------------------------------------------
# Experiment lifecycle wrappers
# ---------------------------------------------------------------------------

def create_experiment(config: dict[str, Any]) -> dict[str, Any]:
    """Create a draft experiment from a config dict. Returns experiment_id."""
    client = mangrove_ai_client()
    result = client.oracle.create_experiment(config)
    _log.info("oracle.create_experiment", extra={"experiment_id": result.experiment_id})
    return result.model_dump()


def list_experiments() -> list[dict[str, Any]]:
    """List all experiments for the calling org (summary view)."""
    client = mangrove_ai_client()
    items = client.oracle.list_experiments()
    return [item.model_dump() for item in items]


def get_experiment(experiment_id: str) -> dict[str, Any]:
    """Fetch full experiment config including current progress."""
    client = mangrove_ai_client()
    return client.oracle.get_experiment(experiment_id)


def update_experiment(experiment_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Replace a draft experiment's config (PUT semantics).

    Only ``draft``-status experiments can be updated; validated /
    launched / paused experiments reject mutation with HTTP 400.
    """
    client = mangrove_ai_client()
    result = client.oracle.update_experiment(experiment_id, config)
    _log.info("oracle.update_experiment", extra={"experiment_id": experiment_id})
    return result.model_dump()


def delete_experiment(experiment_id: str) -> dict[str, Any]:
    """Delete an experiment + cancel any in-flight child backtests."""
    client = mangrove_ai_client()
    result = client.oracle.delete_experiment(experiment_id)
    _log.info("oracle.delete_experiment", extra={"experiment_id": experiment_id})
    return result.model_dump()


def validate_experiment(experiment_id: str) -> dict[str, Any]:
    """Validate a draft → required before launch.

    Returns the server's validation result:
        {"valid": bool, "total_runs": int, "errors": [...], "warnings": [...]}

    NOTE — SDK contract bug (mangroveai <= 1.5.0): the SDK types this
    endpoint's response as ``ExperimentStatus`` (``experiment_id`` +
    ``status``), but the server returns the ``{valid, total_runs,
    errors, warnings}`` shape above. ``client.oracle.validate_experiment``
    therefore raises a pydantic ``ValidationError`` while parsing an
    otherwise-successful 200. We call the transport directly and return
    the raw validation result until the SDK ships a correct
    ``ExperimentValidation`` model. Tracked: MangroveAI-SDK contract fix.
    """
    client = mangrove_ai_client()
    raw = client.oracle._core.request(
        "POST", f"/oracle/experiments/{experiment_id}/validate"
    ).json()
    _log.info(
        "oracle.validate_experiment",
        extra={"experiment_id": experiment_id, "valid": raw.get("valid"),
               "total_runs": raw.get("total_runs")},
    )
    return raw


def launch_experiment(experiment_id: str) -> dict[str, Any]:
    """Fan out a validated experiment into up to 99 child backtests.

    Launch is asynchronous and non-idempotent. The call can return a gateway 504
    even though the launch SUCCEEDED server-side (MangroveOracle#296); we do NOT
    re-send it (a retry would hit the single-flight 409 / concurrent-cap 429). On
    a gateway 5xx we confirm by polling ``get_experiment`` until the experiment
    leaves ``validated``/``draft``. Returns the launch status body; callers keep
    polling ``get_experiment`` for completion.
    """
    client = mangrove_ai_client()
    try:
        body = client.oracle.launch_experiment(experiment_id).model_dump()
    except APIError as exc:
        if getattr(exc, "status_code", None) not in (502, 503, 504):
            raise
        _log.warning(
            "oracle.launch_experiment gateway_timeout — confirming via poll",
            extra={"experiment_id": experiment_id, "status_code": exc.status_code},
        )
        body = _confirm_launch(client, experiment_id)
    _log.info(
        "oracle.launch_experiment",
        extra={"experiment_id": experiment_id, "status": body.get("status")},
    )
    return body


def _confirm_launch(
    client: Any,
    experiment_id: str,
    *,
    poll_interval: float = 3.0,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Confirm a launch that returned a gateway 5xx actually took effect.

    Polls ``get_experiment`` until the experiment leaves ``validated``/``draft``
    (proof the launch ran), returning a normalized status body. Raises
    ``TimeoutError`` if the status never advances within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exp = client.oracle.get_experiment(experiment_id)
        if exp.get("status") not in ("validated", "draft", None):
            return {
                "experiment_id": experiment_id,
                "status": exp.get("status"),
                "total_runs": exp.get("total_runs"),
                "confirmed_via": "poll",
            }
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Experiment {experiment_id} did not leave 'validated' within {timeout:.0f}s "
        f"after launch"
    )


def pause_experiment(experiment_id: str) -> dict[str, Any]:
    """Halt a running experiment without losing completed results.

    Returns the server's `{"status": "paused"}` body.

    NOTE — same SDK contract bug as `validate_experiment` (mangroveai
    <= 1.5.0): the pause endpoint returns `{"status": "paused"}` with NO
    `experiment_id`, but the SDK types it as `ExperimentStatus`
    (`experiment_id` + `status`, both required), so
    `client.oracle.pause_experiment` raises a pydantic `ValidationError`
    on an otherwise-successful 200. We call the transport directly until
    the SDK ships a tolerant model. Tracked: MangroveAI-SDK contract fix.
    """
    client = mangrove_ai_client()
    raw = client.oracle._core.request(
        "POST", f"/oracle/experiments/{experiment_id}/pause"
    ).json()
    _log.info(
        "oracle.pause_experiment",
        extra={"experiment_id": experiment_id, "status": raw.get("status")},
    )
    return raw


# ---------------------------------------------------------------------------
# Results pagination
# ---------------------------------------------------------------------------

def list_results(
    experiment_id: str, *, limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """Read backtest results materializing under an experiment, paginated.

    ``experiment_id`` is required — Oracle rejects unfiltered reads at
    the proxy layer to prevent cross-tenant fan-out.
    """
    client = mangrove_ai_client()
    result = client.oracle.list_results(
        experiment_id=experiment_id, limit=limit, offset=offset,
    )
    return result.model_dump()


# ---------------------------------------------------------------------------
# Metadata catalogs (free / non-billable)
# ---------------------------------------------------------------------------

def list_datasets() -> list[dict[str, Any]]:
    """List the OHLCV datasets the engine can run experiments against."""
    client = mangrove_ai_client()
    return client.oracle.list_datasets()


def list_signals() -> list[dict[str, Any]]:
    """List signals with typed param specs available to experiments."""
    client = mangrove_ai_client()
    return client.oracle.list_signals()


def list_templates() -> list[dict[str, Any]]:
    """List predefined strategy templates to seed experiments from."""
    client = mangrove_ai_client()
    return client.oracle.list_templates()
