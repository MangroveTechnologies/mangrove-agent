"""CEX (Kraken) BYOK routes — auth-gated.

- POST /api/v1/agent/cex/stash-kraken   stash {api_key, api_secret} -> vault_token
                                        (called by scripts/stash-kraken-secret.sh)
- POST /api/v1/agent/cex/connect        consume vault_token -> persist creds (encrypted)
- GET  /api/v1/agent/cex/status         connected?
- GET  /api/v1/agent/cex/balances       Kraken balances (BYOK, direct to Kraken)
- POST /api/v1/agent/cex/validate-order Kraken AddOrder validate=true (dry-run)
- POST /api/v1/agent/cex/sync-fills     pull Kraken fills -> emit to telemetry

The Kraken key never enters chat: it's stashed out-of-band via the script and
this server keeps it encrypted at rest. Fills are emitted to the markets server
(authed by the Mangrove key); the Kraken key never leaves this machine.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.config import app_config
from src.services import cex_service
from src.services.secret_vault import vault
from src.shared.auth.dependency import require_api_key
from src.shared.errors import SdkError

router = APIRouter(
    prefix="/cex",
    dependencies=[Depends(require_api_key)],
    tags=["cex"],
)


class StashKrakenRequest(BaseModel):
    api_key: str = Field(..., description="Kraken API key (never logged/echoed)")
    api_secret: str = Field(..., description="Kraken private key / secret")


class ConnectRequest(BaseModel):
    vault_token: str = Field(..., description="From POST /cex/stash-kraken")


class ValidateOrderRequest(BaseModel):
    pair: str
    side: str  # buy | sell
    volume: float
    ordertype: str = "market"
    price: float | None = None


class SyncFillsRequest(BaseModel):
    mode: str = "live"


@router.post("/stash-kraken", summary="Stash Kraken creds out-of-band (returns vault_token)")
async def stash_kraken(req: StashKrakenRequest) -> dict:
    token = vault.stash(json.dumps({"api_key": req.api_key, "api_secret": req.api_secret}))
    return {"vault_token": token, "secret_ttl_seconds": int(getattr(app_config, "SECRET_VAULT_TTL_SECONDS", 300))}


@router.post("/connect", summary="Connect Kraken by consuming a stashed vault_token")
async def connect(req: ConnectRequest) -> dict:
    try:
        return cex_service.connect_from_vault(req.vault_token)
    except KeyError as e:
        raise SdkError(f"vault_token unknown or expired: {e}") from e


@router.get("/status", summary="Is a Kraken account connected?")
async def status() -> dict:
    return cex_service.status()


@router.get("/balances", summary="Kraken balances (BYOK)")
async def balances() -> dict:
    try:
        return {"balances": cex_service.get_balances()}
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"cex balances failed: {e}") from e


@router.post("/validate-order", summary="Dry-run a Kraken order (validate=true)")
async def validate_order(req: ValidateOrderRequest) -> dict:
    try:
        return cex_service.validate_order(
            pair=req.pair, side=req.side, volume=req.volume,
            ordertype=req.ordertype, price=req.price,
        )
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"cex validate-order failed: {e}") from e


@router.post("/sync-fills", summary="Pull Kraken fills and emit them to telemetry")
async def sync_fills(req: SyncFillsRequest) -> dict:
    try:
        return cex_service.sync_fills(mode=req.mode)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"cex sync-fills failed: {e}") from e
