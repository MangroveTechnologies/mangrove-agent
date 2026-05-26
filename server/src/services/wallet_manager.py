"""wallet_manager — local encrypted key storage + signing.

Responsibilities:
- Create wallets via mangrovemarkets.wallet.create(). Encrypt the returned
  seed/private_key with Fernet, persist the ciphertext in SQLite. Stash
  the plaintext in the in-process SecretVault and return only a vault_token
  in the MCP response — the plaintext never enters the Claude Code
  conversation context.
- Import externally-generated private keys via the stash-and-consume
  pattern: user's bash CLI posts the raw key to /internal/stash-secret
  and gets back a vault_token, then calls import_wallet with that id.
- List stored wallets (addresses + metadata only; never returns secrets).
- Sign arbitrary EVM transactions locally. The SDK never sees the key.
- Gate live trading on explicit user backup confirmation (backup_confirmed_at).
  Paper mode is unaffected.

Security:
- The plaintext key NEVER appears in an MCP tool response. Responses carry
  only the opaque vault_token, which is useful only via the localhost reveal
  CLI (out-of-band, never through Claude Code).
- sign() decrypts into a local bytes variable, derives the signing account,
  signs, discards the variable. Plaintext lifetime is <10ms per op.

Chain support (v1): EVM only. XRPL returns ChainNotSupportedInV1.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from eth_account import Account
from eth_account.datastructures import SignedTransaction
from eth_utils import to_checksum_address
from pydantic import BaseModel

from src.services.secret_vault import vault
from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.crypto.fernet import decrypt, encrypt, get_master_key_source
from src.shared.db.sqlite import get_connection
from src.shared.errors import (
    ChainNotSupportedInV1,
    SigningError,
    WalletAlreadyExists,
    WalletNotFound,
)
from src.shared.logging import get_logger

_log = get_logger(__name__)

_ENCRYPTION_METHOD = "fernet-v1"


# ---------------------------------------------------------------------------
# Signing guard — hardens against the EIP-7702 / arbitrary-message attack
# surface that drained a workshop test wallet on 2026-04-24.
#
# Invariant: the agent signs ONLY things directly related to a 1inch swap —
# a call to a 1inch AggregationRouter, OR the ERC-20 approve() whose spender
# is a 1inch router. Any other shape (arbitrary EOA transfers, non-1inch
# DEX routers, EIP-7702 set-code txs, authorization lists, personal_sign
# messages) is refused before the private key is decrypted.
# ---------------------------------------------------------------------------

# Canonical 1inch AggregationRouter deployments. 1inch uses deterministic
# CREATE2 deploys so the same addresses apply on Base mainnet (chain_id 8453)
# AND Base Sepolia testnet (chain_id 84532). Workshop flows start on
# testnet — the guard must not discriminate by chain. If 1inch ships a V7
# or a new chain deploys at a different address, add it here.
_ONEINCH_ROUTERS: set[str] = {
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # V5 AggregationRouter
    "0x111111125421ca6dc452d289314280a0f8842a65",  # V6 AggregationRouter
}

# ERC-20 `approve(address,uint256)` function selector.
_APPROVE_SELECTOR = "0x095ea7b3"


def _is_oneinch_router(addr: str | None) -> bool:
    if not addr:
        return False
    return addr.lower() in _ONEINCH_ROUTERS


def _extract_approve_spender(data: str | bytes | None) -> str | None:
    """If `data` is a well-formed ERC-20 approve() call, return the spender
    address (lowercase 0x-prefixed hex). Return None otherwise."""
    if data is None:
        return None
    s = data.decode() if isinstance(data, bytes) else str(data)
    s = s.lower()
    if not s.startswith(_APPROVE_SELECTOR):
        return None
    # 4-byte selector (10 hex chars including 0x) + 32-byte spender slot (64 hex chars)
    if len(s) < 10 + 64:
        return None
    spender_slot = s[10 : 10 + 64]
    # Spender is the last 20 bytes (40 hex chars) of the left-padded 32-byte slot.
    return "0x" + spender_slot[-40:]


def _validate_sign_target(normalized_tx: dict) -> None:
    """Refuse to sign anything that isn't a direct 1inch swap or a 1inch-bound approve().

    Raises SigningError if the payload is anything else. Must run BEFORE the
    private key is decrypted so rejected payloads never touch plaintext.
    """
    tx_type = normalized_tx.get("type")
    if tx_type in (3, 4):
        # 3 = EIP-4844 blob (not used for swaps), 4 = EIP-7702 set-code (the
        # exact shape of the 2026-04-24 workshop drain).
        raise SigningError(
            f"Refused to sign tx of type {tx_type}: only EIP-155 (type 0) and "
            "EIP-1559 (type 2) txs are permitted by the signing guard. "
            "Type 4 is EIP-7702 set-code — the attack shape that drained the "
            "workshop test wallet on 2026-04-24.",
            suggestion="If this was produced by the SDK, treat as a bug or supply-chain attack — do not bypass the guard without explicit review.",
        )
    if "authorizationList" in normalized_tx or "authorization_list" in normalized_tx:
        raise SigningError(
            "Refused to sign: tx contains an EIP-7702 authorization list. "
            "The agent only signs classic 1inch swap txs — authorization-based "
            "delegation is not permitted.",
            suggestion="Investigate the code path that built this payload — it should be using the standard dex.prepare_swap flow.",
        )

    to_addr = normalized_tx.get("to")
    if not to_addr:
        raise SigningError(
            "Refused to sign: tx has no `to` field (contract deployment or bare call). "
            "The signing guard permits only txs to 1inch routers or ERC-20 approve()-for-1inch.",
            suggestion="Check the SDK's prepare_swap output — a real swap always has `to` populated with a router address.",
        )

    # Happy path 1: direct call to a 1inch AggregationRouter (the swap itself).
    if _is_oneinch_router(to_addr):
        return

    # Happy path 2: ERC-20 approve() whose spender is a 1inch router (the
    # approve step that precedes the swap when allowance != max).
    spender = _extract_approve_spender(normalized_tx.get("data"))
    if spender is not None and _is_oneinch_router(spender):
        return

    raise SigningError(
        f"Refused to sign: `to` address {to_addr} is not a known 1inch router, "
        "and the tx is not an approve() with a 1inch spender. The signing guard "
        "permits only direct 1inch swaps and their required token approvals — "
        "no arbitrary transfers, no non-1inch DEX routing, no EIP-7702 delegation.",
        suggestion=f"Known 1inch routers: {sorted(_ONEINCH_ROUTERS)}. If the SDK legitimately routes through a different aggregator, the guard's allowlist must be explicitly expanded with review.",
    )


# ---------------------------------------------------------------------------
# Response / model types
# ---------------------------------------------------------------------------


SecretType = Literal["private_key", "mnemonic"]


class WalletCreateResponse(BaseModel):
    """Response for POST /wallet/create.

    The plaintext secret is NEVER included. The caller receives a vault_token
    pointing at an in-process vault entry (TTL-bound, single-read) and a
    reveal_cmd describing how to retrieve the plaintext out-of-band.
    """

    address: str
    chain: str
    network: str
    chain_id: int | None = None
    label: str | None = None
    created_at: datetime
    vault_token: str
    secret_type: SecretType
    master_key_source: str
    reveal_cmd: str
    secret_ttl_seconds: int
    backup_required: bool
    deposit_instructions: str
    safety_note: str


class WalletImportResponse(BaseModel):
    """Response for import_wallet. Metadata only — no secret material."""

    address: str
    chain: str
    network: str
    chain_id: int | None = None
    label: str | None = None
    created_at: datetime
    master_key_source: str
    backup_required: bool
    next_step: str


class StashSecretResponse(BaseModel):
    """Response for POST /internal/stash-secret. Opaque id only."""

    vault_token: str
    secret_ttl_seconds: int


class RevealSecretResponse(BaseModel):
    """Response for GET /internal/reveal-secret/{id} or /wallet/{addr}/reveal.

    Contains plaintext — only exposed over localhost to a CLI subprocess
    that is not Claude Code. NEVER return this from an MCP tool.
    """

    secret: str
    address: str | None = None


class WalletListItem(BaseModel):
    """Redacted view of a stored wallet. Never carries secrets."""

    address: str
    chain: str
    network: str
    chain_id: int | None = None
    label: str | None = None
    created_at: datetime
    backup_confirmed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deposit_instructions(address: str, chain: str, network: str) -> str:
    net_label = "mainnet (real funds)" if network == "mainnet" else f"{network}"
    chain_label = chain.upper() if chain == "evm" else chain
    return (
        f"Deposit to this {chain_label} address on {net_label}:\n"
        f"  {address}\n\n"
        "Start with a SMALL TEST AMOUNT (1-5 USDC). Verify via `get_balances` "
        "before sending more. This wallet is dedicated to the agent — keep it "
        "separate from your personal holdings."
    )


def _detect_secret_type(secret: str) -> SecretType:
    """Heuristic: 0x + 64 hex or 64 hex → private_key, else mnemonic."""
    s = secret.strip()
    if s.startswith("0x") and len(s) == 66:
        return "private_key"
    if len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
        return "private_key"
    return "mnemonic"


def _derive_address(secret: str) -> str:
    """Return the EVM address derived from the given secret."""
    s = secret.strip()
    if _detect_secret_type(s) == "private_key":
        return Account.from_key(s).address
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(s).address


def _extract_secret(create_result: Any) -> str:
    """Pull whichever sensitive field the SDK populated.

    SDK's WalletCreateResult can have any of: seed_phrase, private_key, secret.
    We store whichever is present, in priority order (seed_phrase > private_key > secret).
    """
    for attr in ("seed_phrase", "private_key", "secret"):
        val = getattr(create_result, attr, None)
        if val:
            return str(val)
    raise SigningError(
        "SDK wallet.create() returned no seed_phrase, private_key, or secret.",
        suggestion="Check the mangrovemarkets SDK version and the target chain's supported wallet_creation mode.",
    )


def _safety_note(secret_type: SecretType, master_key_source: str) -> str:
    src_blurb = {
        "keyfile": "your local keyfile (./agent-data/master.key, chmod 600)",
        "generated_keyfile": "your local keyfile (./agent-data/master.key, chmod 600)",
        "keychain": "your OS keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager)",
    }.get(master_key_source, master_key_source)

    import_ui = (
        "MetaMask → Import Account → Private Key"
        if secret_type == "private_key"
        else "MetaMask → Import Account → Secret Recovery Phrase"
    )
    return (
        f"Your secret (type: {secret_type}) is encrypted at rest with a Fernet "
        f"master key stored in {src_blurb}. Run the reveal_cmd ONCE to back it "
        f"up outside the agent (off-agent backup needed for disaster recovery "
        f"if the master key is ever lost). Import with: {import_ui}."
    )


def _secret_vault_ttl() -> int:
    from src.config import app_config
    try:
        return int(app_config.SECRET_VAULT_TTL_SECONDS)
    except (AttributeError, TypeError, ValueError):
        return 300


def _reveal_cmd_for(vault_token: str) -> str:
    return f"./scripts/reveal-secret.sh {vault_token}"


def _reveal_cmd_for_address(address: str) -> str:
    return f"./scripts/reveal-secret.sh --address {address}"


# ---------------------------------------------------------------------------
# Create wallet (secret stays in-process, MCP response has vault_token only)
# ---------------------------------------------------------------------------


def create_wallet(
    chain: str,
    network: str,
    chain_id: int | None = None,
    label: str | None = None,
) -> WalletCreateResponse:
    """Create a new wallet. Encrypts the secret, persists to SQLite, stashes
    plaintext in the in-process vault, returns a vault_token.
    """
    chain_normalized = chain.lower()
    if chain_normalized in {"xrpl", "xrp"}:
        raise ChainNotSupportedInV1(
            "XRPL wallet creation is not supported in v1.",
            suggestion="Use an EVM chain (e.g. Base, Ethereum, Arbitrum). XRPL support is planned for a future release.",
        )
    if chain_normalized != "evm":
        raise ChainNotSupportedInV1(
            f"Chain '{chain}' is not supported in v1.",
            suggestion="Supported: evm (with a valid chain_id).",
        )

    result = mangrove_markets_client().wallet.create(
        chain=chain_normalized, network=network, chain_id=chain_id,
    )
    secret = _extract_secret(result)
    address = str(result.address)
    secret_type = _detect_secret_type(secret)

    conn = get_connection()
    existing = conn.execute(
        "SELECT 1 FROM wallets WHERE address = ?", (address,),
    ).fetchone()
    if existing:
        raise WalletAlreadyExists(
            f"Wallet with address {address} is already stored.",
            suggestion="Use GET /wallet/list to see stored wallets.",
        )

    encrypted = encrypt(secret.encode())
    created_at = datetime.now(timezone.utc)
    wallet_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            wallet_id, address, chain_normalized, network, chain_id,
            encrypted, _ENCRYPTION_METHOD, label, created_at.isoformat(), None,
        ),
    )
    conn.commit()

    # Stash plaintext in vault. `secret` is not returned to the caller.
    vault_token = vault.stash_for_address(secret, address=address)

    _log.info(
        "wallet.created",
        wallet_id=wallet_id,
        address=address,
        chain=chain_normalized,
        network=network,
        chain_id=chain_id,
        secret_type=secret_type,
    )

    return WalletCreateResponse(
        address=address,
        chain=chain_normalized,
        network=network,
        chain_id=chain_id,
        label=label,
        created_at=created_at,
        vault_token=vault_token,
        secret_type=secret_type,
        master_key_source=get_master_key_source(),
        reveal_cmd=_reveal_cmd_for(vault_token),
        secret_ttl_seconds=_secret_vault_ttl(),
        backup_required=True,
        deposit_instructions=_deposit_instructions(address, chain_normalized, network),
        safety_note=_safety_note(secret_type, get_master_key_source()),
    )


# ---------------------------------------------------------------------------
# Import wallet (secret provided via stash_secret, consumed by vault_token)
# ---------------------------------------------------------------------------


def import_wallet(
    vault_token: str,
    chain: str = "evm",
    network: str = "mainnet",
    chain_id: int | None = 8453,
    label: str | None = None,
) -> WalletImportResponse:
    """Import an existing wallet whose secret has been stashed in the vault.

    The user's CLI flow:
      1. Run `./scripts/stash-secret.sh` — it prompts for the private key via
         `read -s`, POSTs to /internal/stash-secret, prints the returned id.
      2. Ask the agent to import that id.
      3. Agent calls import_wallet(vault_token=<id>).

    The private key never enters Claude Code's conversation context.
    """
    chain_normalized = chain.lower()
    if chain_normalized != "evm":
        raise ChainNotSupportedInV1(
            f"Chain '{chain}' is not supported for import in v1.",
            suggestion="Supported: evm (with a valid chain_id).",
        )

    try:
        secret = vault.reveal(vault_token)
    except KeyError as e:
        raise SigningError(
            "vault_token is unknown or has expired.",
            suggestion=(
                "Re-run `./scripts/stash-secret.sh` to stash your key and get a "
                "fresh vault_token, then retry the import. Each vault_token is "
                "single-read and TTL-bound."
            ),
        ) from e

    try:
        address = _derive_address(secret)
    except Exception as e:  # noqa: BLE001
        raise SigningError(
            f"Could not derive EVM address from the provided secret: {e}",
            suggestion="Verify the secret is a valid 0x-prefixed private key or BIP39 mnemonic.",
        ) from e

    conn = get_connection()
    existing = conn.execute(
        "SELECT 1 FROM wallets WHERE address = ?", (address,),
    ).fetchone()
    if existing:
        raise WalletAlreadyExists(
            f"Wallet with address {address} is already stored.",
            suggestion="Use GET /wallet/list to see stored wallets.",
        )

    encrypted = encrypt(secret.encode())
    # Drop reference to plaintext immediately.
    del secret
    created_at = datetime.now(timezone.utc)
    wallet_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at, metadata_json,
            backup_confirmed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            wallet_id, address, chain_normalized, network, chain_id,
            encrypted, _ENCRYPTION_METHOD, label, created_at.isoformat(), None,
            # Imported wallets: user already has the secret off-agent by
            # definition (they just typed it into stash-secret.sh). Auto-
            # confirm backup so they don't have to do it again.
            created_at.isoformat(),
        ),
    )
    conn.commit()

    _log.info(
        "wallet.imported",
        wallet_id=wallet_id,
        address=address,
        chain=chain_normalized,
        network=network,
        chain_id=chain_id,
    )

    return WalletImportResponse(
        address=address,
        chain=chain_normalized,
        network=network,
        chain_id=chain_id,
        label=label,
        created_at=created_at,
        master_key_source=get_master_key_source(),
        backup_required=False,  # imported: user already has it
        next_step=(
            "Verify balance with get_balances. The wallet is live — auto-"
            "confirmed as backed-up because you typed the key into the CLI, "
            "which means you have it off-agent already."
        ),
    )


# ---------------------------------------------------------------------------
# Reveal-on-demand (out-of-band via CLI, never through MCP)
# ---------------------------------------------------------------------------


def reveal_wallet_secret(address: str) -> RevealSecretResponse:
    """Decrypt and return the plaintext secret for a stored wallet.

    INTENDED FOR CALL BY THE LOCALHOST CLI ONLY. The server exposes this
    via a private REST endpoint; the bash script in scripts/reveal-secret.sh
    invokes it and prints to the user's terminal. MCP tools MUST NOT call
    this — doing so would leak the plaintext back through Claude Code.
    """
    secret = _load_secret(address)
    _log.info("wallet.secret_revealed", address=address)
    return RevealSecretResponse(secret=secret, address=address)


def stash_external_secret(secret: str, address_hint: str | None = None) -> str:
    """Accept a plaintext secret from the CLI, stash in the vault, return id.

    Called by /internal/stash-secret. The caller is expected to be the
    localhost bash CLI, which reads the secret via `read -s` and POSTs it
    here. The secret never enters Claude Code's context.
    """
    if not secret or not secret.strip():
        raise ValueError("secret must be non-empty")
    # If we can derive an address, tag the vault entry for later reveal-by-address.
    tag = address_hint
    if tag is None:
        try:
            tag = _derive_address(secret)
        except Exception:  # noqa: BLE001
            tag = None
    return vault.stash_for_address(secret, address=tag) if tag else vault.stash(secret)


# ---------------------------------------------------------------------------
# Backup confirmation (gates live trading)
# ---------------------------------------------------------------------------


def confirm_backup(address: str) -> WalletListItem:
    """Mark a wallet as backed-up by the user.

    The user invokes `./scripts/confirm-backup.sh <address>` AFTER they've
    saved the plaintext secret outside the agent. This flips the flag;
    downstream, execute_swap and update_strategy_status(live) unlock.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM wallets WHERE address = ?", (address,),
    ).fetchone()
    if not row:
        raise WalletNotFound(
            f"Wallet {address} not found.",
            suggestion="Use GET /wallet/list to see stored wallets.",
        )
    now = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE wallets SET backup_confirmed_at = ? WHERE address = ?",
        (now.isoformat(), address),
    )
    conn.commit()
    _log.info("wallet.backup_confirmed", address=address, confirmed_at=now.isoformat())
    updated = conn.execute(
        """SELECT address, chain, network, chain_id, label, created_at,
                  backup_confirmed_at
           FROM wallets WHERE address = ?""",
        (address,),
    ).fetchone()
    return WalletListItem(
        address=updated["address"],
        chain=updated["chain"],
        network=updated["network"],
        chain_id=updated["chain_id"],
        label=updated["label"],
        created_at=datetime.fromisoformat(updated["created_at"]),
        backup_confirmed_at=datetime.fromisoformat(updated["backup_confirmed_at"])
        if updated["backup_confirmed_at"] else None,
    )


def require_backup_confirmed(address: str) -> None:
    """Raise SigningError if the wallet has no backup confirmation.

    Called by execute_swap / live-promotion paths. Paper mode does not
    call this — no real funds at risk.
    """
    row = get_connection().execute(
        "SELECT backup_confirmed_at FROM wallets WHERE address = ?",
        (address,),
    ).fetchone()
    if not row:
        raise WalletNotFound(
            f"Wallet {address} not found.",
            suggestion="Use GET /wallet/list to see stored wallets.",
        )
    if not row["backup_confirmed_at"]:
        raise SigningError(
            f"Wallet {address} is not backed up. Live trading refused.",
            suggestion=(
                "Back up the wallet's secret OUTSIDE the agent first:\n"
                f"  ./scripts/reveal-secret.sh --address {address}\n"
                "Save the printed secret in a password manager / hardware "
                "wallet / paper. THEN confirm the backup with:\n"
                f"  ./scripts/confirm-backup.sh {address}\n"
                "After that the agent will unlock live trading for this wallet."
            ),
        )


# ---------------------------------------------------------------------------
# List / load / exists
# ---------------------------------------------------------------------------


def list_wallets() -> list[WalletListItem]:
    """Return all stored wallets. Secrets are NEVER returned."""
    rows = get_connection().execute(
        """SELECT address, chain, network, chain_id, label, created_at,
                  backup_confirmed_at
           FROM wallets ORDER BY created_at DESC""",
    ).fetchall()
    return [
        WalletListItem(
            address=r["address"],
            chain=r["chain"],
            network=r["network"],
            chain_id=r["chain_id"],
            label=r["label"],
            created_at=datetime.fromisoformat(r["created_at"]),
            backup_confirmed_at=datetime.fromisoformat(r["backup_confirmed_at"])
            if r["backup_confirmed_at"] else None,
        )
        for r in rows
    ]


def _load_secret(address: str) -> str:
    row = get_connection().execute(
        "SELECT encrypted_secret, encryption_method FROM wallets WHERE address = ?",
        (address,),
    ).fetchone()
    if not row:
        raise WalletNotFound(
            f"Wallet {address} not found.",
            suggestion="Use GET /wallet/list to see stored wallets or POST /wallet/create to add one.",
        )
    if row["encryption_method"] != _ENCRYPTION_METHOD:
        raise SigningError(
            f"Unknown encryption method: {row['encryption_method']}",
            suggestion="The wallet was encrypted with a different version; manual migration required.",
        )
    return decrypt(row["encrypted_secret"]).decode()


# ---------------------------------------------------------------------------
# Signing (unchanged from previous impl except for backup-gated callers)
# ---------------------------------------------------------------------------


_INT_FIELDS = {
    "nonce", "gas", "gasLimit", "gasPrice",
    "maxFeePerGas", "maxPriorityFeePerGas",
    "value", "chainId", "type",
}


def _normalize_payload(payload: dict, chain_id: int | None = None) -> dict:
    out: dict = {}
    for k, v in payload.items():
        if v is None:
            continue
        if k in _INT_FIELDS and isinstance(v, str):
            out[k] = int(v, 16) if v.startswith("0x") else int(v)
        elif k == "to" and isinstance(v, str) and v.startswith("0x"):
            out[k] = to_checksum_address(v)
        else:
            out[k] = v

    if chain_id is not None and "chainId" not in out:
        out["chainId"] = chain_id

    has_eip1559 = (
        out.get("maxFeePerGas") is not None
        and out.get("maxPriorityFeePerGas") is not None
    )
    if has_eip1559:
        out.setdefault("type", 2)
        out.pop("gasPrice", None)
    elif "gasPrice" in out:
        out.pop("maxFeePerGas", None)
        out.pop("maxPriorityFeePerGas", None)
        out.pop("type", None)

    return out


def sign(unsigned_tx: dict, wallet_address: str, chain_id: int | None = None) -> str:
    """Sign an EVM transaction with the wallet's key.

    NOTE: callers that represent live money movement (execute_swap, live
    strategy evaluator) MUST call require_backup_confirmed(wallet_address)
    first. sign() itself does not gate — some callers (paper mode, signing
    approval txs during a read-only quote path) legitimately need to sign
    without a backup. The gate lives one layer up.
    """
    normalized = _normalize_payload(unsigned_tx, chain_id=chain_id)

    # Signing guard: refuse non-1inch payloads BEFORE the key is decrypted.
    # Defense in depth against SDK compromise / rogue code paths / EIP-7702
    # phishing. See _validate_sign_target docstring for full rationale.
    _validate_sign_target(normalized)

    secret = _load_secret(wallet_address)
    try:
        if secret.startswith("0x") or len(secret) == 64:
            account = Account.from_key(secret)
        else:
            Account.enable_unaudited_hdwallet_features()
            account = Account.from_mnemonic(secret)

        signed: SignedTransaction = account.sign_transaction(normalized)
    except Exception as e:  # noqa: BLE001
        raise SigningError(
            f"Failed to sign transaction for {wallet_address}: {e}",
            suggestion="Verify the tx dict has all EVM required fields. Pass chain_id explicitly if the SDK payload omits it.",
        ) from e
    finally:
        del secret

    _log.info(
        "wallet.signed_tx",
        wallet_address=wallet_address,
        chain_id=normalized.get("chainId"),
        to=normalized.get("to"),
        tx_type=normalized.get("type", 0),
    )
    raw = signed.rawTransaction if hasattr(signed, "rawTransaction") else signed.raw_transaction
    raw_hex = raw.hex()
    return raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex


def sign_message(message: str | bytes, wallet_address: str) -> str:
    """Disabled by the signing guard.

    EIP-191 personal_sign is not required by any 1inch swap flow the agent
    currently supports. Enabling arbitrary message signing is the attack
    vector that produced the EIP-7702 drain on 2026-04-24 (the authorization
    signed by the wallet's key is a message, not a tx). The guard refuses
    all message-signing by default.

    If a future flow (e.g. 1inch limit orders, Fusion intents, gasless
    approves) legitimately needs to sign a structured message, add a narrow
    typed helper with its own guard that validates the specific payload
    shape — do NOT reopen this general-purpose personal_sign endpoint.
    """
    # Arguments deliberately accepted for signature compatibility — they are
    # not used, since we refuse all calls. Referenced here to avoid lint warnings.
    _ = (message, wallet_address)
    raise SigningError(
        "Refused to sign: sign_message is disabled by the wallet signing guard. "
        "The agent only signs 1inch swap txs and approve() calls for 1inch routers — "
        "arbitrary message signing is a known phishing surface (EIP-7702 authorizations "
        "were the shape that drained the workshop test wallet on 2026-04-24).",
        suggestion="If a specific 1inch flow needs message signing, add a narrow typed helper with its own guard — do not reopen the general personal_sign endpoint.",
    )


def _get_wallet_row(address: str) -> dict | None:
    row = get_connection().execute(
        """SELECT address, chain, network, chain_id, label, created_at,
                  backup_confirmed_at
           FROM wallets WHERE address = ?""",
        (address,),
    ).fetchone()
    return dict(row) if row else None


def wallet_exists(address: str) -> bool:
    return _get_wallet_row(address) is not None
