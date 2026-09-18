"""Decryption and payment preflight cannot initialize encryption-key state."""
import pytest
from cryptography.fernet import Fernet
from src.config import app_config
from src.shared.crypto import fernet
from src.shared.errors import SigningError


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "new-state" / "master.key"
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(path))
    monkeypatch.setattr(fernet, "_read_keychain", lambda: None)
    fernet.reset_master_key_cache()
    yield path
    fernet.reset_master_key_cache()


@pytest.mark.parametrize("operation", ["preflight", "decrypt"])
def test_missing_key_refuses_without_creating_files(key_path, operation):
    with pytest.raises(SigningError, match="encryption key is unavailable"):
        if operation == "preflight":
            fernet.require_existing_master_key()
        else:
            fernet.decrypt(Fernet(Fernet.generate_key()).encrypt(b"synthetic"))
    assert not key_path.parent.exists()


def test_keychain_still_supports_decryption_without_keyfile(key_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setattr(fernet, "_read_keychain", lambda: (key, "keychain"))
    assert fernet.require_existing_master_key() is None
    assert fernet.decrypt(Fernet(key).encrypt(b"synthetic")) == b"synthetic"
    assert not key_path.parent.exists()


def test_wallet_creation_can_still_initialize_key_and_decrypt(key_path):
    ciphertext = fernet.encrypt(b"synthetic")
    assert key_path.is_file()
    assert fernet.get_master_key_source() == "generated_keyfile"
    original = key_path.read_bytes()
    fernet.require_existing_master_key()
    assert fernet.decrypt(ciphertext) == b"synthetic"
    assert key_path.read_bytes() == original


def test_wrong_existing_key_is_not_replaced(key_path):
    key_path.parent.mkdir()
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o600)
    original = key_path.read_bytes()
    with pytest.raises(SigningError, match="master key mismatch"):
        fernet.decrypt(Fernet(Fernet.generate_key()).encrypt(b"synthetic"))
    assert key_path.read_bytes() == original


@pytest.mark.parametrize("first_operation", ["decrypt", "preflight", "encrypt"])
def test_one_cached_key_survives_keychain_lock_for_all_wallets(key_path, monkeypatch, first_operation):
    original_key = Fernet.generate_key()
    original_ciphertext = Fernet(original_key).encrypt(b"wallet-A-private-key")
    monkeypatch.setattr(fernet, "_read_keychain", lambda: (original_key, "keychain"))
    if first_operation == "decrypt":
        assert fernet.decrypt(original_ciphertext) == b"wallet-A-private-key"
    elif first_operation == "preflight":
        fernet.require_existing_master_key()
    else:
        fernet.encrypt(b"initial-wallet")

    # Keychain becomes unavailable only after this process loaded its key.
    monkeypatch.setattr(fernet, "_read_keychain", lambda: None)
    new_ciphertext = fernet.encrypt(b"wallet-B-private-key")
    assert fernet.decrypt(original_ciphertext) == b"wallet-A-private-key"
    assert fernet.decrypt(new_ciphertext) == b"wallet-B-private-key"
    assert Fernet(original_key).decrypt(new_ciphertext) == b"wallet-B-private-key"
    assert fernet.get_master_key() == original_key
    assert fernet.get_master_key_source() == "keychain"
    assert not key_path.parent.exists()


def test_failed_read_does_not_cache_missing_key(key_path, monkeypatch):
    with pytest.raises(SigningError):
        fernet.require_existing_master_key()
    original_key = Fernet.generate_key()
    monkeypatch.setattr(fernet, "_read_keychain", lambda: (original_key, "keychain"))
    fernet.require_existing_master_key()
    assert fernet.get_master_key() == original_key
    assert not key_path.parent.exists()


def test_concurrent_initial_encryption_generates_one_key(key_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    original_generate = fernet._generate_and_persist_keyfile
    calls = []
    barrier = Barrier(6)

    def generate():
        calls.append(1)
        time.sleep(0.02)  # Expose unsynchronized first-load races.
        return original_generate()

    def encrypt(index):
        barrier.wait(timeout=5)
        return fernet.encrypt(f"wallet-{index}".encode())

    monkeypatch.setattr(fernet, "_generate_and_persist_keyfile", generate)
    with ThreadPoolExecutor(max_workers=6) as pool:
        ciphertexts = list(pool.map(encrypt, range(6)))
    assert len(calls) == 1
    assert fernet.get_master_key_source() == "generated_keyfile"
    persisted = Fernet(key_path.read_bytes())
    for index, ciphertext in enumerate(ciphertexts):
        assert fernet.decrypt(ciphertext) == f"wallet-{index}".encode()
        assert persisted.decrypt(ciphertext) == f"wallet-{index}".encode()
