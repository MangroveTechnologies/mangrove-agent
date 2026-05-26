"""Wallet routes — auth-gated.

- POST /api/v1/agent/wallet/create
      Create + encrypt a wallet. Response carries vault_token (no plaintext).
- POST /api/v1/agent/wallet/import
      Import an existing wallet by vault_token (from a prior stash).
- POST /api/v1/agent/wallet/stash-secret
      Accept a raw plaintext secret from the localhost CLI (not MCP).
      Returns vault_token. Called by scripts/stash-secret.sh.
- GET  /api/v1/agent/wallet/reveal-secret/{vault_token}
      Reveal + consume a stashed secret. Called by scripts/reveal-secret.sh.
- GET  /api/v1/agent/wallet/{address}/reveal
      Reveal-on-demand: decrypt a stored wallet's secret. Called by
      scripts/reveal-secret.sh --address <addr>. Not an MCP tool.
- POST /api/v1/agent/wallet/{address}/confirm-backup
      Flip backup_confirmed_at. Called by scripts/confirm-backup.sh.
- GET  /api/v1/agent/wallet/list
- GET  /api/v1/agent/wallet/{address}/balances
- GET  /api/v1/agent/wallet/{address}/portfolio
- GET  /api/v1/agent/wallet/{address}/history

The reveal/stash endpoints live under the wallet router and are
auth-gated like the rest — the agent's API key is shared with the user's
localhost scripts via local-config.json, so the CLI authenticates the
same way Claude Code's MCP does. These endpoints are NOT exposed via MCP
tools; only the localhost bash scripts call them.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.services.secret_vault import vault
from src.services.wallet_manager import (
    RevealSecretResponse,
    StashSecretResponse,
    WalletCreateResponse,
    WalletImportResponse,
    WalletListItem,
    confirm_backup,
    create_wallet,
    import_wallet,
    list_wallets,
    reveal_wallet_secret,
    stash_external_secret,
)
from src.shared.auth.dependency import require_api_key
from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.errors import SdkError, SigningError

router = APIRouter(
    prefix="/wallet",
    dependencies=[Depends(require_api_key)],
    tags=["wallet"],
)


class WalletCreateRequest(BaseModel):
    chain: str = Field("evm", description="evm | xrpl (xrpl stubbed 501 in v1)")
    network: str = Field("mainnet", description="mainnet | testnet")
    chain_id: int | None = Field(8453, description="Required for evm; default 8453 (Base)")
    label: str | None = None


class WalletImportRequest(BaseModel):
    vault_token: str = Field(..., description="From POST /wallet/stash-secret")
    chain: str = "evm"
    network: str = "mainnet"
    chain_id: int | None = 8453
    label: str | None = None


class StashSecretRequest(BaseModel):
    secret: str = Field(..., description="Plaintext private key or mnemonic")
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
    summary="Create a new wallet (secret stashed, not returned)",
    description=(
        "Creates + encrypts a wallet locally. Response carries only a "
        "vault_token pointing at the in-process vault — run the reveal_cmd "
        "out-of-band to back up the plaintext. The MCP transport never "
        "sees the key."
    ),
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
    response_model=WalletImportResponse,
    summary="Import an existing wallet by vault_token",
    description=(
        "Expects the secret to already be in the vault via a prior call to "
        "POST /wallet/stash-secret. This endpoint consumes the vault entry, "
        "derives the address, encrypts, and persists."
    ),
    status_code=201,
)
async def wallet_import(req: WalletImportRequest) -> WalletImportResponse:
    return import_wallet(
        vault_token=req.vault_token,
        chain=req.chain,
        network=req.network,
        chain_id=req.chain_id,
        label=req.label,
    )


@router.post(
    "/stash-secret",
    response_model=StashSecretResponse,
    summary="Stash a plaintext secret in the in-process vault",
    description=(
        "Called ONLY by the localhost CLI (scripts/stash-secret.sh). The "
        "CLI reads the secret via `read -s` so it doesn't echo to the "
        "terminal, POSTs here, and prints the returned id. The agent can "
        "then consume the id via /wallet/import — the plaintext never "
        "enters Claude Code's transcript or context."
    ),
)
async def wallet_stash_secret(req: StashSecretRequest) -> StashSecretResponse:
    from src.config import app_config
    sid = stash_external_secret(req.secret, address_hint=req.address_hint)
    return StashSecretResponse(
        vault_token=sid,
        secret_ttl_seconds=int(getattr(app_config, "SECRET_VAULT_TTL_SECONDS", 300)),
    )


@router.get(
    "/reveal-secret/{vault_token}",
    response_model=RevealSecretResponse,
    summary="Reveal + consume a stashed secret (single-read)",
    description=(
        "Called ONLY by the localhost CLI (scripts/reveal-secret.sh). "
        "Returns plaintext from the vault and evicts the entry. NEVER "
        "called from an MCP tool."
    ),
)
async def wallet_reveal_secret(vault_token: str) -> RevealSecretResponse:
    try:
        secret = vault.reveal(vault_token)
    except KeyError as e:
        raise SigningError(
            "vault_token unknown or expired.",
            suggestion="Re-create or re-stash the secret to get a fresh id.",
        ) from e
    return RevealSecretResponse(secret=secret, address=None)


@router.get(
    "/{address}/reveal",
    response_model=RevealSecretResponse,
    summary="Reveal-on-demand: decrypt a stored wallet's secret",
    description=(
        "Called ONLY by the localhost CLI (scripts/reveal-secret.sh "
        "--address <addr>). Decrypts the wallet's encrypted_secret with "
        "the master key and returns the plaintext. MCP tools MUST NOT "
        "call this — it would leak plaintext back through Claude Code."
    ),
)
async def wallet_reveal_by_address(address: str) -> RevealSecretResponse:
    return reveal_wallet_secret(address)


@router.post(
    "/{address}/confirm-backup",
    response_model=ConfirmBackupResponse,
    summary="Confirm the user has backed up this wallet's secret",
    description=(
        "Flips wallets.backup_confirmed_at. Unlocks execute_swap and "
        "update_strategy_status(live) for this wallet. The CLI "
        "(scripts/confirm-backup.sh) calls this AFTER the user has "
        "revealed the secret and stored it somewhere safe."
    ),
)
async def wallet_confirm_backup(address: str) -> ConfirmBackupResponse:
    item = confirm_backup(address)
    return ConfirmBackupResponse(
        address=item.address,
        backup_confirmed_at=item.backup_confirmed_at.isoformat() if item.backup_confirmed_at else "",
        message=(
            "Backup confirmed. Live trading is now unlocked for this wallet. "
            "If the master key is ever lost, you can recover funds with the "
            "secret you backed up."
        ),
    )


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
