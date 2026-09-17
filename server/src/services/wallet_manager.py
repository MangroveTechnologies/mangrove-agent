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
- Sign x402 payment authorizations (EIP-3009 TransferWithAuthorization) locally,
  behind their own narrow guard. Separate door from sign(); see below.
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
from typing import Literal

from eth_account import Account
from eth_account.datastructures import SignedTransaction
from eth_utils import to_checksum_address
from pydantic import BaseModel

from src.services.secret_vault import vault
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
# AND Base Sepolia testnet (chain_id 84532). New-user flows start on
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
# x402 payment signing guard — a SECOND narrow door, NOT a widening of the one
# above.
#
# An x402 payment is EIP-712 typed data (EIP-3009 TransferWithAuthorization),
# not a transaction dict. It has no top-level `to`, no tx `type` and no
# authorizationList, so _validate_sign_target can neither inspect it nor
# meaningfully refuse it — it is a different shape entirely.
#
# It is also NOT inert. The signature IS the payment instrument: whoever holds
# a signed authorization can submit it on-chain and pull `value` USDC from
# `from`, until validBefore expires or the nonce is spent. There is no second
# confirmation step where the user can change their mind. Signing here IS
# spending, so every check below runs BEFORE the key is decrypted, exactly as
# in sign().
#
# What this guard deliberately does NOT constrain: the payee. Paying an
# arbitrary counterparty is inherent to x402 — the receiving address is chosen
# by the server in its 402 envelope and may rotate — so there is no allowlist
# to check it against. It is made auditable instead: payee and value are
# logged before the key is touched. Aggregate spend limits are a separate
# concern and do not belong in a signing guard.
# ---------------------------------------------------------------------------

_X402_PRIMARY_TYPE = "TransferWithAuthorization"

# Canonical USDC deployments, keyed by chain id. Unlike the 1inch routers
# (one address across chains via deterministic deploy), USDC is deployed
# per chain, so this guard binds contract TO chain: a Base-mainnet USDC
# address inside a Base-Sepolia payload is refused, and vice versa. That
# pairing is the check — neither half means anything alone.
#
# Hardcoded on purpose, matching _ONEINCH_ROUTERS. X402_USDC_CONTRACT exists
# in config and is deliberately NOT read here: a guard whose allowlist comes
# from config can be widened by editing config, which defeats the point of
# having a guard. Adding a chain is a reviewed source change.
_X402_USDC_BY_CHAIN_ID: dict[int, str] = {
    8453: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # Base mainnet
    84532: "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # Base Sepolia
}

# The canonical EIP-3009 struct, pinned field-for-field and in order.
#
# Checking primaryType alone is not enough: EIP-712 derives its typehash from
# the type DEFINITION, so a payload naming "TransferWithAuthorization" while
# defining different fields signs a different struct than the one we think we
# are approving. Pinning the definition means the bytes we sign are the bytes
# we validated.
_X402_AUTHORIZATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("from", "address"),
    ("to", "address"),
    ("value", "uint256"),
    ("validAfter", "uint256"),
    ("validBefore", "uint256"),
    ("nonce", "bytes32"),
)

# The EIP712Domain fields a USDC payment carries. eth_account builds the
# domain separator from whatever keys `domain` holds, so anything outside this
# set changes the signed hash.
_X402_DOMAIN_KEYS = frozenset({"name", "version", "chainId", "verifyingContract"})

# USDC is 6-decimal on every chain in _X402_USDC_BY_CHAIN_ID. Only used to put
# a human-readable amount in the audit log, and only safe to apply because the
# guard has already pinned the token contract by then.
_USDC_DECIMALS = 6

_UINT256_MAX = 2**256 - 1


def _is_hex_address(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _as_uint256(value: object, field: str) -> int:
    """Coerce an EIP-712 uint256 field, refusing anything out of range.

    bool is excluded explicitly — it is an int subclass in Python, and a
    stray True would otherwise sign as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SigningError(
            f"Refused to sign x402 authorization: `{field}` must be an integer, "
            f"got {type(value).__name__}.",
            suggestion="The payment envelope is malformed. Treat a non-integer amount or timestamp as a bug or a tampered 402 response — do not coerce it.",
        )
    if not 0 <= value <= _UINT256_MAX:
        raise SigningError(
            f"Refused to sign x402 authorization: `{field}` is out of uint256 range.",
            suggestion="The payment envelope is malformed. Do not sign a payload whose amount or timestamps cannot be represented on-chain.",
        )
    return value


def _validate_x402_authorization(
    domain: dict,
    types: dict,
    primary_type: str,
    message: dict,
    wallet_address: str,
) -> tuple[str, int, int]:
    """Refuse to sign anything that is not a USDC TransferWithAuthorization.

    Raises SigningError on any mismatch. Must run BEFORE the private key is
    decrypted so rejected payloads never touch plaintext.

    Returns (payee, value, chain_id) for the audit log — read only after the
    payload has been fully validated, so nothing unvalidated is ever logged
    as though it were approved.
    """
    # -- 0. Shape. These three come from parsing a remote server's 402
    # envelope, so they are attacker-influenced input: a non-mapping must
    # produce a clean refusal, not an AttributeError from the checks below.
    for label, part in (("domain", domain), ("types", types), ("message", message)):
        if not isinstance(part, dict):
            raise SigningError(
                f"Refused to sign x402 authorization: `{label}` must be a mapping, "
                f"got {type(part).__name__}.",
                suggestion="The payment envelope is malformed — it did not decode to EIP-712 typed data. Treat as a tampered or non-x402 402 response.",
            )

    # -- 1. The struct must be the one we mean, by name AND by definition.
    if primary_type != _X402_PRIMARY_TYPE:
        raise SigningError(
            f"Refused to sign x402 authorization: primaryType is {primary_type!r}, "
            f"and the guard permits only {_X402_PRIMARY_TYPE!r}. The agent signs "
            "USDC payment authorizations and nothing else — no permits, no "
            "delegations, no arbitrary typed data.",
            suggestion="If a future x402 scheme legitimately needs a different struct, add a separate guarded entry point for it — do not widen this one.",
        )

    declared = types.get(primary_type)
    if declared is None:
        raise SigningError(
            f"Refused to sign x402 authorization: types define no {primary_type!r} struct.",
            suggestion="The payment envelope is malformed — the primary type must be defined in `types`.",
        )
    # Every entry must be a field mapping. Skipping non-mappings instead would
    # let six canonical fields plus one junk entry normalize to exactly the
    # canonical tuple — validating a struct that is not the one handed to the
    # signer.
    if not isinstance(declared, list) or not all(isinstance(f, dict) for f in declared):
        raise SigningError(
            f"Refused to sign x402 authorization: the {primary_type!r} type "
            "definition is not a list of field mappings.",
            suggestion="The payment envelope is malformed — EIP-712 type definitions are lists of {name, type} mappings.",
        )
    normalized_fields = tuple((f.get("name"), f.get("type")) for f in declared)
    if normalized_fields != _X402_AUTHORIZATION_FIELDS:
        raise SigningError(
            "Refused to sign x402 authorization: the TransferWithAuthorization "
            "struct does not match the canonical EIP-3009 definition. EIP-712 "
            "hashes the type definition, so a mismatched definition signs a "
            "different struct than the one the guard approved.",
            suggestion=f"Expected fields, in order: {list(_X402_AUTHORIZATION_FIELDS)}. A mismatch means a tampered envelope or an SDK bug — investigate, do not bypass.",
        )
    extra_types = set(types) - {primary_type, "EIP712Domain"}
    if extra_types:
        raise SigningError(
            f"Refused to sign x402 authorization: unexpected type definitions "
            f"{sorted(extra_types)}. A flat EIP-3009 authorization declares no "
            "nested structs.",
            suggestion="Extra type definitions can smuggle nested data into the signed hash. Treat this as a tampered envelope.",
        )

    # Pin both the EIP-712 structure and the deployed token's domain.
    unexpected_domain_keys = set(domain) - _X402_DOMAIN_KEYS
    if unexpected_domain_keys:
        raise SigningError(
            f"Refused to sign x402 authorization: unexpected domain fields "
            f"{sorted(unexpected_domain_keys)}. The EIP-712 domain separator is "
            "built from these keys, so an unexpected one changes what is signed.",
            suggestion=f"A USDC payment domain carries only {sorted(_X402_DOMAIN_KEYS)}. Treat extra fields as a tampered envelope.",
        )

    # Int only, deliberately: a stringified chainId would be validated here and
    # then handed to eth_account verbatim, so what we checked and what we sign
    # could diverge. That divergence is the exact failure class this guard
    # exists to prevent, and no caller needs the leniency.
    chain_id = domain.get("chainId")
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise SigningError(
            f"Refused to sign x402 authorization: domain.chainId is missing or "
            f"not an integer (got {chain_id!r}).",
            suggestion="The payment envelope is malformed. Never sign a payload that does not name the chain it applies to.",
        )

    expected_usdc = _X402_USDC_BY_CHAIN_ID.get(chain_id)
    if expected_usdc is None:
        raise SigningError(
            f"Refused to sign x402 authorization: chainId {chain_id} is not a "
            "supported payment chain.",
            suggestion=f"Supported chains: {sorted(_X402_USDC_BY_CHAIN_ID)} (Base mainnet, Base Sepolia). Adding one is a reviewed source change to _X402_USDC_BY_CHAIN_ID.",
        )

    verifying_contract = domain.get("verifyingContract")
    if not _is_hex_address(verifying_contract):
        raise SigningError(
            "Refused to sign x402 authorization: domain.verifyingContract is "
            f"missing or not an address (got {verifying_contract!r}).",
            suggestion="The payment envelope is malformed — the verifying contract is what binds the signature to a specific token.",
        )
    if verifying_contract.lower() != expected_usdc:
        raise SigningError(
            f"Refused to sign x402 authorization: verifyingContract "
            f"{verifying_contract} is not USDC on chain {chain_id}. The agent "
            "pays in USDC only — signing against an unknown token contract is "
            "how a forged envelope drains an arbitrary balance.",
            suggestion=f"Expected {expected_usdc} for chain {chain_id}. If the payment is genuinely denominated in another asset, that is a reviewed change, not a bypass.",
        )

    expected_name = {8453: "USD Coin", 84532: "USDC"}[chain_id]
    if domain.get("name") != expected_name or domain.get("version") != "2":
        raise SigningError(
            "Refused to sign x402 authorization: unexpected USDC domain name or version.",
            suggestion="Use the supported token domain for the configured chain; missing or altered domains are refused.",
        )

    # -- 3. The message must be exactly the canonical fields, well-formed.
    expected_keys = {name for name, _ in _X402_AUTHORIZATION_FIELDS}
    actual_keys = set(message)
    if actual_keys != expected_keys:
        raise SigningError(
            "Refused to sign x402 authorization: message fields do not match the "
            f"EIP-3009 authorization. Missing: {sorted(expected_keys - actual_keys)}; "
            f"unexpected: {sorted(actual_keys - expected_keys)}.",
            suggestion="The payment envelope is malformed. Do not sign a partially-populated or padded authorization.",
        )

    payer = message["from"]
    payee = message["to"]
    for label, addr in (("from", payer), ("to", payee)):
        if not _is_hex_address(addr):
            raise SigningError(
                f"Refused to sign x402 authorization: message.{label} is not an "
                f"address (got {addr!r}).",
                suggestion="The payment envelope is malformed. Both the payer and the payee must be well-formed addresses before anything is signed.",
            )

    # The signer IS the payer under EIP-3009 — USDC recovers the signature and
    # requires it to match `from`. A mismatch is never a working payment, so
    # refusing it here turns a silent on-chain rejection into a clear local
    # error, and stops one wallet's key being used to author another's debit.
    if payer.lower() != wallet_address.lower():
        raise SigningError(
            f"Refused to sign x402 authorization: message.from ({payer}) is not "
            f"the signing wallet ({wallet_address}). Under EIP-3009 the payer "
            "and the signer must be the same account.",
            suggestion="Check the code path that built this payload — the payer address must come from the same wallet whose key is being asked to sign.",
        )

    value = _as_uint256(message["value"], "value")
    _as_uint256(message["validAfter"], "validAfter")
    _as_uint256(message["validBefore"], "validBefore")

    nonce = message["nonce"]
    if isinstance(nonce, str):
        stripped = nonce[2:] if nonce.startswith("0x") else nonce
        try:
            nonce_bytes = bytes.fromhex(stripped)
        except ValueError:
            nonce_bytes = b""
    elif isinstance(nonce, (bytes, bytearray)):
        nonce_bytes = bytes(nonce)
    else:
        nonce_bytes = b""
    if len(nonce_bytes) != 32:
        raise SigningError(
            "Refused to sign x402 authorization: message.nonce is not 32 bytes. "
            "The nonce is what makes an authorization single-use.",
            suggestion="Each payment attempt must carry a fresh 32-byte nonce — a reused or malformed nonce is burned server-side and the retry will fail.",
        )

    return payee, value, chain_id


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

    # Generate the keypair LOCALLY, in-process — the private key is never
    # requested from or transmitted to any remote server. eth_account is the
    # same library sign() uses. This is the custody invariant: wallet keys are
    # born, stored, and signed with entirely on this machine; the MangroveMarkets
    # server only ever sees keyless routing/quotes (see MANGROVEMARKETS_BASE_URL).
    # EVM-only in v1 (guarded above); a fresh secp256k1 private key is chain-agnostic.
    acct = Account.create()
    secret = acct.key.hex()
    if not secret.startswith("0x"):
        secret = "0x" + secret
    address = acct.address
    secret_type: SecretType = "private_key"

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
    except Exception:  # noqa: BLE001
        raise SigningError(
            "Could not derive an EVM address from the provided secret.",
            suggestion="Verify the secret is a valid 0x-prefixed private key or BIP39 mnemonic.",
        ) from None

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


def _apply_tx_type(out: dict) -> None:
    """Mutate `out` to enforce consistent EIP-1559 vs legacy tx type fields."""
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

    _apply_tx_type(out)
    return out


def _account_from_secret(secret: str):
    """Derive the signing account from a decrypted secret (key or mnemonic).

    Callers own the plaintext's lifetime: decrypt into a local, call this,
    discard. Shared by sign() and sign_x402_authorization() so the
    key-vs-mnemonic branch exists once.
    """
    if secret.startswith("0x") or len(secret) == 64:
        return Account.from_key(secret)
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(secret)


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
        account = _account_from_secret(secret)
        signed: SignedTransaction = account.sign_transaction(normalized)
    except Exception:  # noqa: BLE001
        raise SigningError(
            "Failed to sign transaction with the stored wallet.",
            suggestion="Verify the tx dict has all EVM required fields. Pass chain_id explicitly if the SDK payload omits it.",
        ) from None
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


def sign_x402_authorization(
    *,
    domain: dict,
    types: dict[str, list[dict[str, str]]],
    primary_type: str,
    message: dict,
    wallet_address: str,
) -> bytes:
    """Sign an EIP-3009 TransferWithAuthorization for an x402 payment.

    This is the narrow typed helper that sign_message's docstring calls for:
    a specific payload shape with its own guard, NOT a reopening of general
    message signing. Anything that is not a USDC TransferWithAuthorization on
    a known chain is refused by _validate_x402_authorization before the key is
    decrypted.

    Arguments mirror the x402 ClientEvmSigner.sign_typed_data protocol
    (domain / types / primary_type / message) so the payer service can adapt
    this to the SDK without repacking, and returns the 65-byte ECDSA
    signature that protocol expects.

    NOTE: like sign(), this does NOT gate on backup confirmation — the gate
    lives one layer up. An x402 payment moves real funds, so the caller MUST
    call require_backup_confirmed(wallet_address) first, on the same rule as
    execute_swap and live strategy evaluation. Nor does it enforce any spend
    limit: a signing guard validates one payload's shape and cannot see an
    aggregate. Per-payment authorization and portfolio-wide spend control are
    separate concerns, deliberately kept in separate layers.
    """
    payee, value, chain_id = _validate_x402_authorization(
        domain, types, primary_type, message, wallet_address
    )

    # Log the approved destination and amount BEFORE the key is decrypted, so
    # the audit trail records what the guard accepted even if signing then
    # fails. The payee is uncapped by design, so it must at minimum always be
    # visible. Both the raw units and the USD amount are recorded: `value` is
    # what is actually signed, `value_usd` is what a human reads during an
    # incident without having to remember USDC's decimals.
    _log.info(
        "wallet.x402_authorization_approved",
        wallet_address=wallet_address,
        chain_id=chain_id,
        verifying_contract=domain.get("verifyingContract"),
        payee=payee,
        value=value,
        value_usd=value / 10**_USDC_DECIMALS,
    )

    # eth_account derives EIP712Domain from domain_data and rejects it being
    # declared in message_types, so drop it if the caller included it.
    signing_types = {k: v for k, v in types.items() if k != "EIP712Domain"}

    secret = _load_secret(wallet_address)
    try:
        account = _account_from_secret(secret)
        signed = account.sign_typed_data(
            domain_data=domain,
            message_types=signing_types,
            message_data=message,
        )
    except Exception:  # noqa: BLE001
        raise SigningError(
            "Failed to sign x402 authorization with the stored wallet.",
            suggestion="The payload passed the guard, so this is a signing-layer failure — check that the wallet's stored secret is intact and that domain/message field types match the EIP-3009 definition.",
        ) from None
    finally:
        del secret

    _log.info(
        "wallet.signed_x402_authorization",
        wallet_address=wallet_address,
        chain_id=chain_id,
        payee=payee,
        value=value,
        value_usd=value / 10**_USDC_DECIMALS,
    )
    return bytes(signed.signature)


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
