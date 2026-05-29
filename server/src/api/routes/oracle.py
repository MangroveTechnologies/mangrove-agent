"""Oracle routes — auth-gated. Thin wrappers over services/oracle.py.

Surface (18 endpoints):
- SIEVE / data / sync backtest (3 legacy):
  POST   /oracle/sieve
  POST   /oracle/data/query
  POST   /oracle/backtest
- Async + bulk backtest (3):
  POST   /oracle/backtest/async
  GET    /oracle/backtest/async/{backtest_id}
  POST   /oracle/backtest/bulk
- Experiment lifecycle (8):
  POST   /oracle/experiments
  GET    /oracle/experiments
  GET    /oracle/experiments/{id}
  PUT    /oracle/experiments/{id}
  DELETE /oracle/experiments/{id}
  POST   /oracle/experiments/{id}/validate
  POST   /oracle/experiments/{id}/launch
  POST   /oracle/experiments/{id}/pause
- Results + catalogs (4):
  GET    /oracle/results
  GET    /oracle/datasets
  GET    /oracle/signals
  GET    /oracle/templates
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from mangrove_ai.exceptions import APIError as SDKAPIError

from src.services import oracle as oracle_service
from src.services.oracle import (
    DataQueryInput,
    OracleBacktestInput,
    SieveScoreInput,
)
from src.shared.auth.dependency import require_api_key
from src.shared.errors import SdkError


def _raise_from_sdk(exc: SDKAPIError) -> "HTTPException":
    """Translate a mangrove_ai APIError into a FastAPI HTTPException
    that preserves the upstream status code + code + message. The agent
    is a transparent proxy here — surfacing the SDK's structured error
    is more useful to clients than collapsing everything to 400.
    """
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": exc.error,
            "message": exc.message,
            "code": exc.code,
            "correlation_id": exc.correlation_id,
        },
    )

router = APIRouter(
    prefix="/oracle",
    dependencies=[Depends(require_api_key)],
    tags=["oracle"],
)


@router.post(
    "/sieve",
    summary="Score strategies through Mangrove SIEVE",
    description=(
        "Run candidate strategies through the Mangrove SIEVE classifier "
        "(binary + 4-class probabilities). Maximum 99 strategies per "
        "request. Response carries `model_version` (content hash of the "
        "bundled SIEVE artifacts) and `code_version` (Oracle dependency "
        "stack) for provenance."
    ),
)
async def post_sieve_score(payload: SieveScoreInput) -> dict[str, Any]:
    try:
        return oracle_service.sieve_score(payload)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/data/query",
    summary="Query the Oracle corpus (results / ohlcv)",
    description=(
        "Run a whitelisted query against the Oracle corpus. Columns and "
        "filter operators are enforced server-side. Tenancy is enforced "
        "via the API key — `WHERE org_id = <caller's org>` is injected "
        "by Oracle, you can never read another tenant's rows."
    ),
)
async def post_data_query(payload: DataQueryInput) -> dict[str, Any]:
    try:
        return oracle_service.data_query(payload)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/backtest",
    summary="Run a single strategy through Oracle's engine",
    description=(
        "Synchronous backtest. Blocks until the engine finishes (typically "
        "30-120s on multi-month windows). Returns metrics + trade history. "
        "For long jobs see ``POST /backtest/async``; for batch see ``POST /backtest/bulk``."
    ),
)
async def post_backtest(payload: OracleBacktestInput) -> dict[str, Any]:
    try:
        return oracle_service.backtest(payload)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Async + bulk backtests
# ---------------------------------------------------------------------------


@router.post(
    "/backtest/async",
    summary="Submit a backtest for async execution",
    description=(
        "Returns ``{backtest_id, status}`` immediately. Poll "
        "``GET /backtest/async/{backtest_id}`` for the full result. Use "
        "this over the sync variant when the window is long enough that "
        "blocking the request would exceed the agent's HTTP timeout."
    ),
)
async def post_backtest_async(payload: OracleBacktestInput) -> dict[str, Any]:
    try:
        return oracle_service.backtest_async(payload)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/backtest/async/{backtest_id}",
    summary="Poll the status / result of an async backtest",
    description="Returns the full backtest payload once the engine finishes.",
)
async def get_backtest_poll(backtest_id: str) -> dict[str, Any]:
    try:
        return oracle_service.backtest_poll(backtest_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/backtest/bulk",
    summary="Bulk-evaluate many strategies against a shared date range",
    description=(
        "Body must include the shared date / risk parameters plus either "
        "``strategy_ids`` (DB lookup) or ``strategy_configs`` (inline "
        "dicts) — or both. The server fetches OHLCV once per unique "
        "``(asset, timeframe)`` and shares it across strategies that "
        "need it. Per-strategy failures are captured in each result's "
        "``error`` field without aborting the batch."
    ),
)
async def post_backtest_bulk(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return oracle_service.backtest_bulk(payload)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Experiment lifecycle
# ---------------------------------------------------------------------------


@router.post(
    "/experiments",
    summary="Create a draft experiment",
    description=(
        "Body is the full experiment config dict (passed through to the "
        "Oracle ``ExperimentConfig`` shape). Returns ``{experiment_id, "
        "status: 'draft', created_at, org_id}``."
    ),
)
async def post_create_experiment(config: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return oracle_service.create_experiment(config)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/experiments",
    summary="List experiments for the calling org",
    description="Compact summary view: experiment_id, name, status, total_runs, completed, search_mode, created_at.",
)
async def get_list_experiments() -> list[dict[str, Any]]:
    try:
        return oracle_service.list_experiments()
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/experiments/{experiment_id}",
    summary="Fetch full experiment config + current progress",
)
async def get_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return oracle_service.get_experiment(experiment_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/experiments/{experiment_id}",
    summary="Replace a draft experiment's config (PUT semantics)",
    description=(
        "Only draft-status experiments can be updated. Validated, "
        "launched, or paused experiments reject mutation with HTTP 400."
    ),
)
async def put_update_experiment(
    experiment_id: str, config: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        return oracle_service.update_experiment(experiment_id, config)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/experiments/{experiment_id}",
    summary="Delete an experiment + cancel any in-flight children",
)
async def delete_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return oracle_service.delete_experiment(experiment_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/experiments/{experiment_id}/validate",
    summary="Validate a draft experiment (transition draft -> validated)",
    description="Required before ``launch``. Returns 400 with structured errors if the config is incomplete.",
)
async def post_validate_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return oracle_service.validate_experiment(experiment_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/experiments/{experiment_id}/launch",
    summary="Fan out a validated experiment into child backtests",
    description=(
        "Up to 99 child backtests per launch. Returns immediately with "
        "``status: 'launched'`` — the fan-out is asynchronous. Poll "
        "``GET /experiments/{id}`` for completion progress or "
        "``GET /results?experiment_id={id}`` for materializing results."
    ),
)
async def post_launch_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return oracle_service.launch_experiment(experiment_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/experiments/{experiment_id}/pause",
    summary="Pause a running experiment",
    description="Halts the fan-out without losing already-completed results. Resume by relaunching.",
)
async def post_pause_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        return oracle_service.pause_experiment(experiment_id)
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Results pagination
# ---------------------------------------------------------------------------


@router.get(
    "/results",
    summary="Read backtest results materializing under an experiment",
    description=(
        "``experiment_id`` is required — Oracle rejects unfiltered "
        "reads at the proxy layer to prevent cross-tenant fan-out."
    ),
)
async def get_list_results(
    experiment_id: str, limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    try:
        return oracle_service.list_results(
            experiment_id, limit=limit, offset=offset,
        )
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Metadata catalogs (free / non-billable)
# ---------------------------------------------------------------------------


@router.get(
    "/datasets",
    summary="List the OHLCV datasets experiments can run against",
)
async def get_list_datasets() -> list[dict[str, Any]]:
    try:
        return oracle_service.list_datasets()
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/signals",
    summary="List signals + typed param specs available to experiments",
)
async def get_list_signals() -> list[dict[str, Any]]:
    try:
        return oracle_service.list_signals()
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/templates",
    summary="List predefined strategy templates",
)
async def get_list_templates() -> list[dict[str, Any]]:
    try:
        return oracle_service.list_templates()
    except SDKAPIError as exc:
        raise _raise_from_sdk(exc) from exc
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
