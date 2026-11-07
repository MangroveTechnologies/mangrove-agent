"""Wallet metadata/create routes and permanently retired secret endpoints.

Ordinary API_KEYS never authorizes backup, export, secret import, or backup
confirmation. These operations run directly as the isolated custody identity
in an administrator-controlled terminal, outside HTTP/MCP and agent RPC.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.services.wallet_manager import (
    WalletCreateResponse,
    WalletListItem,
    create_wallet,
    list_wallets,
)
from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.errors import SdkError

router = APIRouter(
    prefix="/wallet",
    dependencies=[Depends(require_api_key)],
    tags=["wallet"],
)


class WalletCreateRequest(BaseModel):
    chain: str = Field("evm", description="evm | xrpl")
    network: str = Field("mainnet", description="mainnet | testnet")
    chain_id: int | None = Field(8453, description="Required for evm; default 8453 (Base)")
    label: str | None = None


class WalletImportRequest(BaseModel):
    vault_token: str = Field(..., description="Deprecated: no agent token authorizes import")
    chain: str = "evm"
    network: str = "mainnet"
    chain_id: int | None = 8453
    label: str | None = None


class StashSecretRequest(BaseModel):
    secret: str = Field(..., description="Plaintext private key, mnemonic or XRPL seed")
    address_hint: str | None = Field(
        None,
        description="Optional — tags the vault entry so reveal-by-address can find it later",
    )


class ConfirmBackupResponse(BaseModel):
    address: str
    backup_confirmed_at: str
    message: str


@router.post(
    "/create",
    response_model=WalletCreateResponse,
    summary="Create a wallet in the isolated custody service",
    description="Creates a wallet under the separate custody identity. Returns public metadata and human backup instructions; no export token.",
    status_code=201,
)
async def wallet_create(req: WalletCreateRequest) -> WalletCreateResponse:
    return create_wallet(
        chain=req.chain,
        network=req.network,
        chain_id=req.chain_id,
        label=req.label,
    )


@router.post(
    "/import",
    summary="Retired: use human custody administration",
    description="Retired. Use direct human custody administration; no HTTP authority.",
    status_code=201,
)
async def wallet_import():
    raise HTTPException(status_code=410, detail="This operation is unavailable over the agent API. Use the isolated custody administrator terminal.")


@router.post(
    "/stash-secret",
    summary="Retired: human custody import only",
    description="Retired. Use direct human custody administration; no HTTP authority.",
)
async def wallet_stash_secret():
    raise HTTPException(status_code=410, detail="This operation is unavailable over the agent API. Use the isolated custody administrator terminal.")


@router.get(
    "/reveal-secret/{vault_token}",
    summary="Retired: no token-based export",
    description="Retired. Use direct human custody administration; no HTTP authority.",
)
async def wallet_reveal_secret():
    raise HTTPException(status_code=410, detail="This operation is unavailable over the agent API. Use the isolated custody administrator terminal.")


@router.get(
    "/{address}/reveal",
    summary="Retired: no wallet export through agent API",
    description="Retired. Use direct human custody administration; no HTTP authority.",
)
async def wallet_reveal_by_address():
    raise HTTPException(status_code=410, detail="This operation is unavailable over the agent API. Use the isolated custody administrator terminal.")


@router.post(
    "/{address}/confirm-backup",
    summary="Retired: human custody backup confirmation only",
    description="Retired. Use direct human custody administration; no HTTP authority.",
)
async def wallet_confirm_backup():
    raise HTTPException(status_code=410, detail="This operation is unavailable over the agent API. Use the isolated custody administrator terminal.")


@router.get(
    "/list",
    response_model=list[WalletListItem],
    summary="List stored wallets",
    description="Returns addresses + metadata only. Never includes secrets.",
)
async def wallet_list() -> list[WalletListItem]:
    return list_wallets()


@router.get(
    "/{address}/balances",
    summary="Token balances",
    description="Pass-through to mangrovemarkets.dex.balances(chain_id, wallet).",
)
async def wallet_balances(address: str, chain_id: int) -> Any:
    try:
        result = mangrove_markets_client().dex.balances(chain_id=chain_id, wallet=address)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.balances failed: {e}") from e
    return result.model_dump() if hasattr(result, "model_dump") else result


@router.get(
    "/{address}/portfolio",
    summary="Portfolio aggregate",
    description="Combined value + P&L + tokens + DeFi via mangrovemarkets.portfolio.*.",
)
async def wallet_portfolio(address: str, chain_id: int | None = None) -> dict:
    client = mangrove_markets_client()
    try:
        value = client.portfolio.value(addresses=address, chain_id=chain_id)
        pnl = client.portfolio.pnl(addresses=address, chain_id=chain_id)
        tokens = client.portfolio.tokens(addresses=address, chain_id=chain_id)
        defi = client.portfolio.defi(addresses=address, chain_id=chain_id)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"portfolio query failed: {e}") from e
    return {
        "value": value.model_dump() if hasattr(value, "model_dump") else value,
        "pnl": pnl.model_dump() if hasattr(pnl, "model_dump") else pnl,
        "tokens": tokens.model_dump() if hasattr(tokens, "model_dump") else tokens,
        "defi": defi.model_dump() if hasattr(defi, "model_dump") else defi,
    }


@router.get(
    "/{address}/history",
    summary="Transaction history",
    description="Pass-through to mangrovemarkets.portfolio.history.",
)
async def wallet_history(address: str, limit: int = 50) -> list[Any]:
    try:
        items = mangrove_markets_client().portfolio.history(address=address, limit=limit)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"portfolio.history failed: {e}") from e
    return [i.model_dump() if hasattr(i, "model_dump") else i for i in items]
