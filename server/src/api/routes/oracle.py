"""Oracle routes — auth-gated. Thin wrappers over services/oracle.py.

Surface:
- POST /api/v1/oracle/sieve     score 1-99 strategies through SIEVE
- POST /api/v1/oracle/data/query query the corpus (results / ohlcv)
- POST /api/v1/oracle/backtest   run a single backtest synchronously
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.services import oracle as oracle_service
from src.services.oracle import (
    DataQueryInput,
    OracleBacktestInput,
    SieveScoreInput,
)
from src.shared.auth.dependency import require_api_key
from src.shared.errors import SdkError

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
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/backtest",
    summary="Run a single strategy through Oracle's engine",
    description=(
        "Synchronous backtest. Blocks until the engine finishes (typically "
        "30-120s on multi-month windows). Returns metrics + trade history. "
        "For batch work, see the async variant via the SDK directly."
    ),
)
async def post_backtest(payload: OracleBacktestInput) -> dict[str, Any]:
    try:
        return oracle_service.backtest(payload)
    except SdkError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
