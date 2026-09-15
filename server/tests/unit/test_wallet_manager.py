"""Unit tests for wallet_manager — key gen, encryption, signing."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from eth_account import Account  # noqa: E402

# A funded-looking private key for signing tests (never sent anywhere).
_TEST_PRIVKEY = "0x" + "11" * 32
_TEST_ADDRESS = Account.from_key(_TEST_PRIVKEY).address


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_wallet.db"
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    from src.shared.db.sqlite import init_db
    init_db()
    yield db_file
    db_mod.reset_connection()


@pytest.fixture
def stub_keyring(monkeypatch):
    """Stub keyring with an in-memory store so tests don't touch the real OS keychain."""
    store: dict[tuple[str, str], str] = {}

    def _get(service, user):
        return store.get((service, user))

    def _set(service, user, password):
        store[(service, user)] = password

    monkeypatch.setattr("keyring.get_password", _get)
    monkeypatch.setattr("keyring.set_password", _set)

    from src.shared.crypto import fernet as f
    f.reset_master_key_cache()
    yield store
    f.reset_master_key_cache()


@pytest.fixture
def mock_sdk_create(monkeypatch):
    """Pin create_wallet's LOCAL key generation to a fixed EVM keypair.

    create_wallet now generates keys in-process via eth_account (no SDK / no
    network), so we patch Account.create() to a deterministic key — tests that
    reference _TEST_ADDRESS / _TEST_PRIVKEY downstream stay stable.
    """
    fixed = Account.from_key(_TEST_PRIVKEY)
    monkeypatch.setattr(
        "src.services.wallet_manager.Account.create",
        lambda *a, **k: fixed,
    )
    return fixed


# -- crypto/fernet -----------------------------------------------------------


def test_master_key_generated_on_first_call(stub_keyring):
    from src.shared.crypto.fernet import get_master_key

    key = get_master_key()
    assert len(key) > 0
    # Should be a valid Fernet key
    Fernet(key)
    # Second call returns same (cached)
    assert get_master_key() == key


def test_master_key_persists_in_keychain(stub_keyring):
    from src.shared.crypto.fernet import get_master_key, reset_master_key_cache

    k1 = get_master_key()
    reset_master_key_cache()
    k2 = get_master_key()
    assert k1 == k2  # re-read from keychain


def test_encrypt_decrypt_round_trip(stub_keyring):
    from src.shared.crypto.fernet import decrypt, encrypt

    plaintext = b"super secret seed"
    ct = encrypt(plaintext)
    assert ct != plaintext
    assert decrypt(ct) == plaintext


def test_decrypt_invalid_raises_signing_error(stub_keyring):
    from src.shared.crypto.fernet import decrypt
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        decrypt(b"not valid ciphertext")


# -- wallet_manager.create_wallet --------------------------------------------


def test_create_wallet_evm_persists_encrypted(temp_db, stub_keyring, mock_sdk_create):
    from src.services.secret_vault import vault
    from src.services.wallet_manager import create_wallet
    from src.shared.db.sqlite import get_connection

    response = create_wallet(chain="evm", network="testnet", chain_id=84532, label="test")
    assert response.address == _TEST_ADDRESS
    # The plaintext is NOT in the response — only a vault_token pointing at the vault.
    assert response.vault_token and len(response.vault_token) >= 16
    assert response.secret_type == "private_key"
    assert response.master_key_source in {"keyfile", "keychain", "generated_keyfile"}
    assert response.reveal_cmd.startswith("./scripts/reveal-secret.sh ")
    assert response.backup_required is True
    # No seed_phrase/private_key field on the response model.
    resp_dump = response.model_dump()
    assert "seed_phrase" not in resp_dump
    assert "private_key" not in resp_dump
    # Deposit instructions still guide the user.
    assert _TEST_ADDRESS in response.deposit_instructions
    assert "small test amount" in response.deposit_instructions.lower()

    # The vault must have the plaintext (so reveal-secret.sh can retrieve it).
    plaintext = vault.reveal(response.vault_token)
    assert plaintext == _TEST_PRIVKEY

    row = get_connection().execute(
        "SELECT address, chain, chain_id, label, encrypted_secret, encryption_method, backup_confirmed_at FROM wallets WHERE address=?",
        (_TEST_ADDRESS,),
    ).fetchone()
    assert row["chain"] == "evm"
    assert row["chain_id"] == 84532
    assert row["label"] == "test"
    assert row["encrypted_secret"] != _TEST_PRIVKEY.encode()  # actually encrypted
    assert row["encryption_method"] == "fernet-v1"
    # Fresh wallet: backup not confirmed yet.
    assert row["backup_confirmed_at"] is None


def test_create_wallet_generates_locally_no_markets_client(temp_db, stub_keyring):
    """create_wallet mints the keypair in-process via eth_account — no SDK, no
    network. Runs WITHOUT mock_sdk_create so it exercises real local generation:
    two calls yield distinct, valid keys, and wallet_manager no longer even
    imports a markets client (the custody invariant)."""
    import src.services.wallet_manager as wm
    from src.services.wallet_manager import create_wallet

    r1 = create_wallet(chain="evm", network="testnet", chain_id=84532)
    r2 = create_wallet(chain="evm", network="testnet", chain_id=84532)

    assert r1.address != r2.address  # real randomness, not a fixture
    for r in (r1, r2):
        assert r.address.startswith("0x") and len(r.address) == 42
        assert r.vault_token
    # No remote key-gen path exists in the wallet service at all.
    assert not hasattr(wm, "mangrove_markets_client")


def test_create_wallet_xrpl_raises(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import create_wallet
    from src.shared.errors import ChainNotSupportedInV1

    with pytest.raises(ChainNotSupportedInV1):
        create_wallet(chain="xrpl", network="testnet")


def test_create_wallet_duplicate_raises(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import create_wallet
    from src.shared.errors import WalletAlreadyExists

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    with pytest.raises(WalletAlreadyExists):
        create_wallet(chain="evm", network="testnet", chain_id=84532)


# -- list_wallets ------------------------------------------------------------


def test_list_wallets_redacts_secret(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import WalletListItem, create_wallet, list_wallets

    create_wallet(chain="evm", network="testnet", chain_id=84532, label="one")
    items = list_wallets()
    assert len(items) == 1
    assert isinstance(items[0], WalletListItem)
    assert items[0].address == _TEST_ADDRESS
    # No secret fields on WalletListItem
    serialized = items[0].model_dump()
    for forbidden in ("secret", "seed_phrase", "private_key", "encrypted_secret"):
        assert forbidden not in serialized


def test_list_wallets_empty(temp_db, stub_keyring):
    from src.services.wallet_manager import list_wallets

    assert list_wallets() == []


# -- sign --------------------------------------------------------------------


def test_sign_round_trip(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import create_wallet, sign

    create_wallet(chain="evm", network="testnet", chain_id=84532)

    unsigned = {
        "nonce": 0,
        "gas": 21000,
        "maxFeePerGas": 2_000_000_000,
        "maxPriorityFeePerGas": 1_000_000_000,
        # 1inch V6 AggregationRouter — sign guard requires a known 1inch target
        "to": "0x111111125421cA6dc452d289314280a0f8842A65",
        "value": 0,
        "data": b"",
        "chainId": 84532,
    }
    raw_hex = sign(unsigned, _TEST_ADDRESS)
    assert raw_hex.startswith("0x")
    # Decode to bytes and compare with a direct sign via eth_account — should match.
    direct = Account.from_key(_TEST_PRIVKEY).sign_transaction(unsigned)
    direct_raw = direct.rawTransaction if hasattr(direct, "rawTransaction") else direct.raw_transaction
    direct_hex = direct_raw.hex()
    if not direct_hex.startswith("0x"):
        direct_hex = "0x" + direct_hex
    assert raw_hex == direct_hex


def test_sign_unknown_wallet_raises(temp_db, stub_keyring):
    from src.services.wallet_manager import sign
    from src.shared.errors import WalletNotFound

    # Use a guard-compliant payload so the WalletNotFound path is what's being
    # exercised (not the signing guard).
    payload = {
        "nonce": 0,
        "gas": 21000,
        "to": "0x111111125421cA6dc452d289314280a0f8842A65",
        "value": 0,
        "chainId": 84532,
    }
    with pytest.raises(WalletNotFound):
        sign(payload, "0x" + "33" * 20)


def test_sign_message_blocked_by_guard(temp_db, stub_keyring, mock_sdk_create):
    """sign_message must refuse by default — no legitimate 1inch swap flow
    requires EIP-191 personal_sign, and the signing guard hardens against the
    EIP-7702 / arbitrary-message attack surface that drained the workshop
    test wallet on 2026-04-24."""
    from src.services.wallet_manager import create_wallet, sign_message
    from src.shared.errors import SigningError

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    with pytest.raises(SigningError, match="disabled|guard|refuse"):
        sign_message("hello", _TEST_ADDRESS)


# -- wallet_exists -----------------------------------------------------------


def test_wallet_exists(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import create_wallet, wallet_exists

    assert not wallet_exists(_TEST_ADDRESS)
    create_wallet(chain="evm", network="testnet", chain_id=84532)
    assert wallet_exists(_TEST_ADDRESS)


# -- payload normalization (regression — caught by the real Base mainnet swap)


def test_normalize_coerces_stringified_numerics():
    from src.services.wallet_manager import _normalize_payload

    sdk_payload = {
        "to": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "data": "0x095ea7b3",
        "value": "0",
        "gas": 60000,
        "nonce": 26,
        "maxPriorityFeePerGas": "1000000",
        "maxFeePerGas": "11000000",
    }
    out = _normalize_payload(sdk_payload, chain_id=8453)
    assert out["value"] == 0
    assert out["maxFeePerGas"] == 11_000_000
    assert out["maxPriorityFeePerGas"] == 1_000_000
    assert out["chainId"] == 8453  # injected
    assert out["type"] == 2         # eip-1559
    assert out["to"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # checksum


def test_normalize_drops_null_fields_for_legacy_tx():
    """prepare_swap returns legacy (gasPrice populated, EIP-1559 nulls).
    eth_account validator rejects `maxFeePerGas=None`, so we drop them."""
    from src.services.wallet_manager import _normalize_payload

    sdk_payload = {
        "to": "0x111111125421ca6dc452d289314280a0f8842a65",
        "data": "0xabcdef",
        "value": "0",
        "gas": 207909,
        "nonce": 28,
        "gasPrice": "7800000",
        "maxFeePerGas": None,
        "maxPriorityFeePerGas": None,
    }
    out = _normalize_payload(sdk_payload, chain_id=8453)
    assert out["gasPrice"] == 7_800_000
    assert "maxFeePerGas" not in out
    assert "maxPriorityFeePerGas" not in out
    assert "type" not in out  # legacy tx, no type marker


def test_normalize_no_chain_id_injection_if_already_present():
    from src.services.wallet_manager import _normalize_payload

    out = _normalize_payload({"chainId": 1, "value": 0}, chain_id=8453)
    assert out["chainId"] == 1  # caller's value wins


def test_normalize_raises_nothing_when_chain_id_omitted():
    """Caller may sign without chainId — eth_account handles that."""
    from src.services.wallet_manager import _normalize_payload

    out = _normalize_payload({"value": "0"})
    assert out["value"] == 0
    assert "chainId" not in out


def test_sign_accepts_sdk_style_payload_with_chain_id(temp_db, stub_keyring, mock_sdk_create):
    """End-to-end: wallet_manager.sign accepts a payload shaped exactly like
    what mangrovemarkets SDK returns (stringified numerics, no chainId)."""
    from src.services.wallet_manager import create_wallet, sign

    create_wallet(chain="evm", network="testnet", chain_id=84532)

    sdk_style_payload = {
        # 1inch V6 AggregationRouter (same address mainnet + testnet — canonical deploy)
        "to": "0x111111125421cA6dc452d289314280a0f8842A65",
        "data": "0x",
        "value": "0",
        "gas": 21000,
        "nonce": 0,
        "maxPriorityFeePerGas": "1000000",
        "maxFeePerGas": "11000000",
        # Note: NO chainId. Agent must inject it.
    }
    raw = sign(sdk_style_payload, _TEST_ADDRESS, chain_id=84532)
    assert raw.startswith("0x")
    # Signed tx must encode chainId=84532 (0x14a34). After tx type byte (02)
    # and RLP list prefix, the first list element is chainId. Quick sanity:
    # not the chainId=0 signature bug we hit on the real swap.
    assert "021482" not in raw[:20]  # would be chainId=0 (01 48 = type+empty)


# -- SecretVault flow (phase-2 contract: plaintext never in MCP responses) ----


def test_secret_vault_single_read(temp_db, stub_keyring):
    from src.services.secret_vault import vault

    sid = vault.stash("hello-world")
    assert vault.reveal(sid) == "hello-world"
    # Second read fails — single-read semantics.
    with pytest.raises(KeyError):
        vault.reveal(sid)


def test_stash_external_secret_then_import(temp_db, stub_keyring):
    """End-to-end for the CLI-stash-then-MCP-import flow."""
    from src.services.wallet_manager import import_wallet, stash_external_secret
    from src.shared.db.sqlite import get_connection

    sid = stash_external_secret(_TEST_PRIVKEY)
    assert sid

    response = import_wallet(vault_token=sid, chain="evm", network="testnet", chain_id=84532)
    assert response.address == _TEST_ADDRESS
    # Imported wallets auto-confirm backup (user typed the key into the CLI,
    # so they have it off-agent by definition).
    assert response.backup_required is False

    row = get_connection().execute(
        "SELECT backup_confirmed_at FROM wallets WHERE address=?",
        (_TEST_ADDRESS,),
    ).fetchone()
    assert row["backup_confirmed_at"] is not None


def test_import_wallet_expired_vault_token_raises(temp_db, stub_keyring):
    from src.services.wallet_manager import import_wallet
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="unknown or has expired"):
        import_wallet(vault_token="bogus-id-that-was-never-stashed")


def test_reveal_wallet_secret_roundtrip(temp_db, stub_keyring, mock_sdk_create):
    """reveal_wallet_secret decrypts from DB — used by the reveal-by-address CLI."""
    from src.services.wallet_manager import create_wallet, reveal_wallet_secret

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    result = reveal_wallet_secret(_TEST_ADDRESS)
    assert result.secret == _TEST_PRIVKEY
    assert result.address == _TEST_ADDRESS


# -- Backup gate (phase-2 contract: live trading refuses on unconfirmed wallets) -


def test_require_backup_confirmed_null_raises(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import create_wallet, require_backup_confirmed
    from src.shared.errors import SigningError

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    # Fresh wallet, no backup confirmation → must raise.
    with pytest.raises(SigningError, match="not backed up"):
        require_backup_confirmed(_TEST_ADDRESS)


def test_confirm_backup_unlocks(temp_db, stub_keyring, mock_sdk_create):
    from src.services.wallet_manager import (
        confirm_backup,
        create_wallet,
        require_backup_confirmed,
    )

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    item = confirm_backup(_TEST_ADDRESS)
    assert item.backup_confirmed_at is not None
    # After confirmation, the gate is open.
    require_backup_confirmed(_TEST_ADDRESS)  # must not raise


def test_confirm_backup_unknown_wallet_raises(temp_db, stub_keyring):
    from src.services.wallet_manager import confirm_backup
    from src.shared.errors import WalletNotFound

    with pytest.raises(WalletNotFound):
        confirm_backup("0x" + "99" * 20)


# -- Fernet keyfile + guard (phase-1 contract: never silently regenerate) ------


def test_fernet_never_silently_regenerates_with_stubbed_keyring(tmp_path, monkeypatch):
    """If a keyfile exists, fernet MUST return that key, not generate a new one."""
    from cryptography.fernet import Fernet

    from src.config import app_config
    from src.shared.crypto import fernet as f

    # Write a known keyfile.
    key_path = tmp_path / "master.key"
    known_key = Fernet.generate_key()
    key_path.write_bytes(known_key)
    key_path.chmod(0o600)

    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(key_path))
    f.reset_master_key_cache()

    assert f.get_master_key() == known_key
    assert f.get_master_key_source() == "keyfile"

    f.reset_master_key_cache()


# -- Signing guard (hardens against the EIP-7702 / arbitrary-message attack
# surface that drained the workshop test wallet on 2026-04-24) ---------------

# Canonical 1inch AggregationRouter deployments. Same address across chains
# by 1inch's deterministic-deploy pattern, so these work on Base mainnet
# (chain_id 8453) AND Base Sepolia testnet (chain_id 84532).
_ONEINCH_V5 = "0x1111111254EEB25477B68fb85Ed929f73A960582"
_ONEINCH_V6 = "0x111111125421cA6dc452d289314280a0f8842A65"
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_APPROVE_SELECTOR_HEX = "0x095ea7b3"


def _approve_calldata(spender: str, amount: int = 100) -> str:
    """Build an ERC-20 `approve(address,uint256)` calldata hex string."""
    spender_hex = spender.lower().removeprefix("0x").rjust(64, "0")
    amount_hex = format(amount, "064x")
    return _APPROVE_SELECTOR_HEX + spender_hex + amount_hex


class TestSignGuard:
    """Sign-layer hard constraint: the agent refuses to sign anything that
    isn't a direct 1inch swap or a 1inch-bound ERC-20 approve.

    Context: on 2026-04-24 the workshop test wallet was drained $10 via an
    EIP-7702 authorization that delegated code execution to an attacker-
    deployed contract. The mangrove-agent agent must not be signable for
    ANY flow other than 1inch swaps, end of story. This is defense in
    depth against SDK compromise, supply-chain attacks, and rogue code
    paths that could otherwise call sign() with a phishing payload.
    """

    def _make_wallet(self):
        from src.services.wallet_manager import create_wallet
        create_wallet(chain="evm", network="testnet", chain_id=84532)

    def _swap_payload(self, to: str, chain_id: int = 84532) -> dict:
        return {
            "nonce": 0,
            "gas": 21000,
            "maxFeePerGas": 2_000_000_000,
            "maxPriorityFeePerGas": 1_000_000_000,
            "to": to,
            "value": 0,
            "data": "0x",
            "chainId": chain_id,
        }

    def test_accepts_1inch_v6_router(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign
        self._make_wallet()
        raw = sign(self._swap_payload(_ONEINCH_V6), _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_accepts_1inch_v5_router(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign
        self._make_wallet()
        raw = sign(self._swap_payload(_ONEINCH_V5), _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_accepts_testnet_swap_on_base_sepolia(self, temp_db, stub_keyring, mock_sdk_create):
        """Workshop starts on testnet — guard MUST not discriminate by chain."""
        from src.services.wallet_manager import sign
        self._make_wallet()
        raw = sign(self._swap_payload(_ONEINCH_V6, chain_id=84532), _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_accepts_mainnet_swap_on_base(self, temp_db, stub_keyring, mock_sdk_create):
        """Mainnet must also work once user graduates off testnet."""
        from src.services.wallet_manager import sign
        self._make_wallet()
        raw = sign(self._swap_payload(_ONEINCH_V6, chain_id=8453), _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_rejects_non_1inch_to_address(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        # A random destination — the shape of the 2026-04-24 drain.
        bad = self._swap_payload("0x14F9f76863b57e89f2D482557828337234DCacaD")
        with pytest.raises(SigningError, match="1inch|guard|refuse"):
            sign(bad, _TEST_ADDRESS)

    def test_rejects_plain_eoa_transfer(self, temp_db, stub_keyring, mock_sdk_create):
        """Bare ETH/token transfer to an EOA — refused even if it looks innocuous."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        with pytest.raises(SigningError, match="1inch|guard|refuse"):
            sign(self._swap_payload("0x" + "22" * 20), _TEST_ADDRESS)

    def test_rejects_tx_with_no_to_field(self, temp_db, stub_keyring, mock_sdk_create):
        """Contract deployment / bare call — refused."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._swap_payload(_ONEINCH_V6)
        payload.pop("to")
        with pytest.raises(SigningError, match="to|guard|refuse"):
            sign(payload, _TEST_ADDRESS)

    def test_rejects_eip7702_tx_type(self, temp_db, stub_keyring, mock_sdk_create):
        """EIP-7702 set-code txs (type 4) are the 2026-04-24 attack shape. Refused categorically."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._swap_payload(_ONEINCH_V6)
        payload["type"] = 4
        with pytest.raises(SigningError, match="7702|type|guard|refuse"):
            sign(payload, _TEST_ADDRESS)

    def test_rejects_tx_with_authorization_list_field(self, temp_db, stub_keyring, mock_sdk_create):
        """Even if the type field is absent, presence of an EIP-7702 authorization list refuses."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._swap_payload(_ONEINCH_V6)
        payload["authorizationList"] = [
            {"chainId": 8453, "address": "0xd7d20E03C9793bd1b63291eeA0021c8cC096a915", "nonce": 0}
        ]
        with pytest.raises(SigningError, match="7702|authorization|guard|refuse"):
            sign(payload, _TEST_ADDRESS)

    def test_accepts_approve_when_spender_is_1inch_v6(self, temp_db, stub_keyring, mock_sdk_create):
        """ERC-20 approve() for a 1inch router is the required step before a 1inch swap."""
        from src.services.wallet_manager import sign
        self._make_wallet()
        payload = self._swap_payload(_USDC_BASE)
        payload["data"] = _approve_calldata(_ONEINCH_V6, amount=10_000_000)  # 10 USDC
        raw = sign(payload, _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_accepts_approve_when_spender_is_1inch_v5(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign
        self._make_wallet()
        payload = self._swap_payload(_USDC_BASE)
        payload["data"] = _approve_calldata(_ONEINCH_V5)
        raw = sign(payload, _TEST_ADDRESS)
        assert raw.startswith("0x")

    def test_rejects_approve_when_spender_is_not_1inch(self, temp_db, stub_keyring, mock_sdk_create):
        """Approve for a non-1inch spender = attacker draining via approve-then-transferFrom."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._swap_payload(_USDC_BASE)
        payload["data"] = _approve_calldata("0x14F9f76863b57e89f2D482557828337234DCacaD")
        with pytest.raises(SigningError, match="1inch|approve|guard|refuse"):
            sign(payload, _TEST_ADDRESS)

    def test_rejects_approve_with_malformed_data(self, temp_db, stub_keyring, mock_sdk_create):
        """An approve selector without enough bytes for the spender = refuse."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._swap_payload(_USDC_BASE)
        payload["data"] = "0x095ea7b3"  # selector only, no args
        with pytest.raises(SigningError, match="1inch|approve|guard|refuse"):
            sign(payload, _TEST_ADDRESS)


# -- x402 payment signing guard ----------------------------------------------

_USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}

# A plausible facilitator payee. Deliberately arbitrary: the guard does not
# constrain the payee, and these tests assert that it still signs for one it
# has never seen.
_PAYEE = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"


class TestX402SignGuard:
    """Second signing door: EIP-3009 USDC payment authorizations only.

    An x402 payment is EIP-712 typed data, not a transaction, so it never
    reaches TestSignGuard's checks above. And it is not inert — the signature
    IS the payment instrument, so every check must run before the key is
    decrypted. These tests pin that ordering and the narrowness of the
    allowlist.
    """

    def _make_wallet(self):
        from src.services.wallet_manager import create_wallet
        create_wallet(chain="evm", network="testnet", chain_id=84532)

    def _payload(self, **overrides) -> dict:
        """A valid Base Sepolia USDC authorization for $0.001 (1000 units)."""
        payload = {
            "domain": {
                "name": "USDC",
                "version": "2",
                "chainId": 84532,
                "verifyingContract": _USDC_BASE_SEPOLIA,
            },
            "types": {k: [dict(f) for f in v] for k, v in _AUTHORIZATION_TYPES.items()},
            "primary_type": "TransferWithAuthorization",
            "message": {
                "from": _TEST_ADDRESS,
                "to": _PAYEE,
                "value": 1000,
                "validAfter": 0,
                "validBefore": 1_900_000_000,
                "nonce": b"\x11" * 32,
            },
            "wallet_address": _TEST_ADDRESS,
        }
        payload.update(overrides)
        return payload

    # -- happy path ----------------------------------------------------------

    def test_signs_valid_testnet_authorization(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        sig = sign_x402_authorization(**self._payload())
        assert isinstance(sig, bytes)
        assert len(sig) == 65  # r, s, v

    def test_signs_valid_mainnet_authorization(self, temp_db, stub_keyring, mock_sdk_create):
        """Mainnet USDC on chain 8453 — the real-money path."""
        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {**payload["domain"], "chainId": 8453, "verifyingContract": _USDC_BASE}
        sig = sign_x402_authorization(**payload)
        assert len(sig) == 65

    def test_signature_recovers_to_the_paying_wallet(self, temp_db, stub_keyring, mock_sdk_create):
        """The produced signature must actually be valid EIP-712 over this
        payload — USDC recovers the signer on-chain and requires it to equal
        `from`, so a signature that recovers to anything else is worthless."""
        from eth_account.messages import encode_typed_data

        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        payload = self._payload()
        sig = sign_x402_authorization(**payload)

        signable = encode_typed_data(
            domain_data=payload["domain"],
            message_types=payload["types"],
            message_data=payload["message"],
        )
        assert Account.recover_message(signable, signature=sig) == _TEST_ADDRESS

    def test_payee_is_deliberately_unconstrained(self, temp_db, stub_keyring, mock_sdk_create):
        """An unknown counterparty is inherent to x402 — the receiving address
        comes from the server's 402 envelope and may rotate. The guard makes it
        auditable, not allowlisted."""
        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "to": "0x" + "ab" * 20}
        assert len(sign_x402_authorization(**payload)) == 65

    # -- the key must not be touched until the payload is approved -----------

    def test_rejects_before_decrypting_the_key(self, temp_db, stub_keyring, mock_sdk_create, monkeypatch):
        """The whole point of the guard: a bad payload is refused BEFORE the
        Fernet decrypt, so rejected payloads never touch plaintext."""
        from src.services import wallet_manager
        from src.shared.errors import SigningError
        self._make_wallet()

        def _explode(*_a, **_k):
            raise AssertionError("_load_secret called before the guard approved the payload")

        monkeypatch.setattr(wallet_manager, "_load_secret", _explode)

        payload = self._payload(primary_type="Permit")
        with pytest.raises(SigningError, match="primaryType"):
            wallet_manager.sign_x402_authorization(**payload)

    @pytest.mark.parametrize("part", ["domain", "types", "message"])
    def test_rejects_non_mapping_parts(self, temp_db, stub_keyring, mock_sdk_create, part):
        """These are parsed from a remote server's 402 envelope, so a malformed
        one must refuse cleanly rather than raise AttributeError."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        with pytest.raises(SigningError, match="must be a mapping"):
            sign_x402_authorization(**self._payload(**{part: "not-a-dict"}))

    # -- struct identity -----------------------------------------------------

    def test_rejects_non_transfer_primary_type(self, temp_db, stub_keyring, mock_sdk_create):
        """EIP-2612 Permit is a perfectly normal thing to be asked to sign, and
        the agent still refuses — one struct, nothing else."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        with pytest.raises(SigningError, match="primaryType"):
            sign_x402_authorization(**self._payload(primary_type="Permit"))

    def test_rejects_undeclared_primary_type(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        with pytest.raises(SigningError, match="no 'TransferWithAuthorization' struct"):
            sign_x402_authorization(**self._payload(types={}))

    def test_rejects_tampered_struct_definition(self, temp_db, stub_keyring, mock_sdk_create):
        """Right name, wrong definition. EIP-712 hashes the type definition, so
        this would sign a different struct than the guard believes it approved."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["types"]["TransferWithAuthorization"][2] = {"name": "value", "type": "uint128"}
        with pytest.raises(SigningError, match="canonical EIP-3009"):
            sign_x402_authorization(**payload)

    def test_rejects_reordered_struct_fields(self, temp_db, stub_keyring, mock_sdk_create):
        """Field order is part of the EIP-712 typehash."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        fields = payload["types"]["TransferWithAuthorization"]
        fields[0], fields[1] = fields[1], fields[0]
        with pytest.raises(SigningError, match="canonical EIP-3009"):
            sign_x402_authorization(**payload)

    def test_rejects_extra_nested_type(self, temp_db, stub_keyring, mock_sdk_create):
        """Extra struct definitions can smuggle nested data into the hash."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["types"]["Delegation"] = [{"name": "delegate", "type": "address"}]
        with pytest.raises(SigningError, match="unexpected type definitions"):
            sign_x402_authorization(**payload)

    def test_rejects_junk_entry_appended_to_field_list(self, temp_db, stub_keyring, mock_sdk_create):
        """The six canonical fields PLUS a non-mapping entry. Filtering
        non-mappings out instead of refusing would normalize this back to
        exactly the canonical tuple — validating a struct that is not the one
        handed to the signer."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["types"]["TransferWithAuthorization"].append("junk")
        with pytest.raises(SigningError, match="not a list of field mappings"):
            sign_x402_authorization(**payload)

    def test_rejects_non_list_type_definition(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["types"]["TransferWithAuthorization"] = {"from": "address"}
        with pytest.raises(SigningError, match="not a list of field mappings"):
            sign_x402_authorization(**payload)

    # -- token / chain binding -----------------------------------------------

    def test_rejects_mainnet_usdc_in_a_testnet_payload(self, temp_db, stub_keyring, mock_sdk_create):
        """The contract is bound TO the chain. Each half is valid alone; the
        pairing is the check — this is the mismatch that would let a mainnet
        signature be produced from a testnet-looking envelope."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {**payload["domain"], "verifyingContract": _USDC_BASE}
        with pytest.raises(SigningError, match="not USDC on chain 84532"):
            sign_x402_authorization(**payload)

    def test_rejects_unknown_token_contract(self, temp_db, stub_keyring, mock_sdk_create):
        """A forged envelope pointing at an attacker's token contract."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {**payload["domain"], "verifyingContract": "0x" + "ee" * 20}
        with pytest.raises(SigningError, match="not USDC"):
            sign_x402_authorization(**payload)

    def test_rejects_unsupported_chain(self, temp_db, stub_keyring, mock_sdk_create):
        """Ethereum mainnet is not a payment chain for this agent."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {**payload["domain"], "chainId": 1}
        with pytest.raises(SigningError, match="not a supported payment chain"):
            sign_x402_authorization(**payload)

    def test_rejects_missing_chain_id(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {k: v for k, v in payload["domain"].items() if k != "chainId"}
        with pytest.raises(SigningError, match="chainId"):
            sign_x402_authorization(**payload)

    def test_rejects_extra_domain_field(self, temp_db, stub_keyring, mock_sdk_create):
        """eth_account builds the EIP712Domain type from whichever keys the
        domain carries, so a stray `salt` changes the domain separator — the
        signature would cover a different structure than the guard checked."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {**payload["domain"], "salt": "0x" + "00" * 32}
        with pytest.raises(SigningError, match="unexpected domain fields"):
            sign_x402_authorization(**payload)

    def test_rejects_missing_verifying_contract(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["domain"] = {k: v for k, v in payload["domain"].items() if k != "verifyingContract"}
        with pytest.raises(SigningError, match="verifyingContract"):
            sign_x402_authorization(**payload)

    # -- message well-formedness ---------------------------------------------

    def test_rejects_missing_message_field(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        del payload["message"]["validBefore"]
        with pytest.raises(SigningError, match="Missing"):
            sign_x402_authorization(**payload)

    def test_rejects_extra_message_field(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"]["extra"] = 1
        with pytest.raises(SigningError, match="unexpected"):
            sign_x402_authorization(**payload)

    def test_rejects_payer_that_is_not_the_signing_wallet(self, temp_db, stub_keyring, mock_sdk_create):
        """Under EIP-3009 the payer and the signer are the same account —
        one wallet's key must never author another wallet's debit."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "from": "0x" + "cd" * 20}
        with pytest.raises(SigningError, match="not the signing wallet"):
            sign_x402_authorization(**payload)

    def test_accepts_payer_in_any_case(self, temp_db, stub_keyring, mock_sdk_create):
        """Checksummed vs lowercase is not a mismatch."""
        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "from": _TEST_ADDRESS.lower()}
        assert len(sign_x402_authorization(**payload)) == 65

    def test_rejects_malformed_payee(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "to": "not-an-address"}
        with pytest.raises(SigningError, match="message.to is not an"):
            sign_x402_authorization(**payload)

    @pytest.mark.parametrize("bad_value", ["1000", 10.5, None, True])
    def test_rejects_non_integer_value(self, temp_db, stub_keyring, mock_sdk_create, bad_value):
        """A stringified or floating amount is a malformed envelope, not
        something to coerce. `True` is included because bool is an int
        subclass in Python and would otherwise sign as 1."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "value": bad_value}
        with pytest.raises(SigningError, match="`value` must be an integer"):
            sign_x402_authorization(**payload)

    def test_rejects_value_above_uint256(self, temp_db, stub_keyring, mock_sdk_create):
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "value": 2**256}
        with pytest.raises(SigningError, match="out of uint256 range"):
            sign_x402_authorization(**payload)

    @pytest.mark.parametrize("bad_nonce", [b"\x11" * 31, "0x11", "not-hex", 12345, None])
    def test_rejects_malformed_nonce(self, temp_db, stub_keyring, mock_sdk_create, bad_nonce):
        """The nonce is what makes an authorization single-use."""
        from src.services.wallet_manager import sign_x402_authorization
        from src.shared.errors import SigningError
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "nonce": bad_nonce}
        with pytest.raises(SigningError, match="nonce is not 32 bytes"):
            sign_x402_authorization(**payload)

    def test_accepts_hex_string_nonce(self, temp_db, stub_keyring, mock_sdk_create):
        """x402 hands bytes, but a 0x-prefixed 32-byte hex string is the same
        nonce and must not be rejected on representation alone."""
        from src.services.wallet_manager import sign_x402_authorization
        self._make_wallet()
        payload = self._payload()
        payload["message"] = {**payload["message"], "nonce": "0x" + "11" * 32}
        assert len(sign_x402_authorization(**payload)) == 65

    # -- the two doors stay separate -----------------------------------------

    def test_does_not_widen_the_1inch_allowlist(self, temp_db, stub_keyring, mock_sdk_create):
        """Regression: adding the payment door must not loosen the swap door.
        A USDC transfer tx is still refused by sign(), even though USDC is now
        a known contract to the x402 guard."""
        from src.services.wallet_manager import sign
        from src.shared.errors import SigningError
        self._make_wallet()
        tx = {
            "nonce": 0, "gas": 21000,
            "maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 1_000_000_000,
            "to": _USDC_BASE, "value": 0, "data": "0x", "chainId": 8453,
        }
        with pytest.raises(SigningError, match="1inch|guard|refuse"):
            sign(tx, _TEST_ADDRESS)

    def test_sign_message_remains_disabled(self, temp_db, stub_keyring, mock_sdk_create):
        """The narrow typed helper is not a reopening of personal_sign."""
        from src.services.wallet_manager import sign_message
        from src.shared.errors import SigningError
        self._make_wallet()
        with pytest.raises(SigningError, match="disabled"):
            sign_message("gm", _TEST_ADDRESS)
