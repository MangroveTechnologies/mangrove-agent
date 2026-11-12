"""Synthetic custody integration: real crypto/SQLite, no funded accounts/network.

These tests verify service behavior. Separate distinct-UID E2E proves OS access
control; monkeypatched roles here do not constitute an isolation test.
"""

import importlib.util
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from src.custody import runtime
from src.custody.daemon import Broker
from src.custody.transport import MAX_FRAME, peer_uid, receive


@pytest.fixture
def protected_directory(tmp_path):
    """Use a real protected ancestor; Linux pytest's /tmp (1777) is unsafe.

    macOS home directories may themselves carry ACLs, so accept a candidate
    only after the unchanged production ancestor check succeeds.
    """
    from src.custody.transport import check_ancestors, check_owned_path

    configured = os.environ.get("MANGROVE_CUSTODY_TEST_ROOT")
    candidates = (Path(configured),) if configured else (tmp_path, Path.home())
    failures = []
    for candidate in candidates:
        if not candidate.is_absolute():
            pytest.fail("MANGROVE_CUSTODY_TEST_ROOT must be an absolute protected directory")
        base = candidate.resolve()
        try:
            check_ancestors(base, allowed_owners={0, os.getuid()})
            if configured:
                check_owned_path(base, owner=os.getuid(), private=True)
        except (OSError, ValueError) as error:
            if configured:
                pytest.fail(f"Unsafe MANGROVE_CUSTODY_TEST_ROOT {base}: {error}")
            failures.append(f"{base}: {error}")
            continue
        with tempfile.TemporaryDirectory(prefix="mangrove-custody-test-", dir=base) as directory:
            protected = Path(directory)
            check_owned_path(protected, owner=os.getuid(), private=True)
            check_ancestors(protected, allowed_owners={0, os.getuid()})
            yield protected
        return
    pytest.fail("Custody tests require a protected user-owned ancestor without extended ACLs: " + "; ".join(failures))


@pytest.fixture
def custody(protected_directory, monkeypatch):
    from src.config import app_config
    from src.services import wallet_manager as wm
    from src.shared.crypto import fernet
    from src.shared.db import sqlite

    monkeypatch.setattr(app_config, "DB_PATH", str(protected_directory / "wallets.db"))
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(protected_directory / "master.key"))
    monkeypatch.setattr("keyring.get_password", lambda *_: None)
    monkeypatch.setattr(runtime, "_active", True)
    sqlite.reset_connection()
    fernet.reset_master_key_cache()
    sqlite.init_db()
    wallet = wm.create_wallet(chain="evm", network="testnet", chain_id=84532)
    settings = {
        "state_dir": str(protected_directory),
        "policy": {
            "x402": {
                "wallet_address": wallet.address,
                "chain_id": 84532,
                "payees": ["0x" + "22" * 20],
                "max_payment_micro_usd": 2000,
                "lifetime_micro_usd": 3000,
                "max_validity_seconds": 600,
            },
            "siwx_origins": [],
            "identity_chains": [],
        },
    }
    yield wm, wallet, Broker(settings), settings
    sqlite.reset_connection()
    fernet.reset_master_key_cache()


def payment(wallet, nonce=1, value=1000, **message_fields):
    from src.services.wallet_manager import _X402_AUTHORIZATION_FIELDS

    message = {
        "from": wallet.address,
        "to": "0x" + "22" * 20,
        "value": value,
        "validAfter": 0,
        "validBefore": int(time.time()) + 300,
        "nonce": "0x" + nonce.to_bytes(32, "big").hex(),
    }
    message.update(message_fields)
    return {
        "operation": "sign_x402_authorization",
        "arguments": {
            "wallet_address": wallet.address,
            "domain": {
                "name": "USDC",
                "version": "2",
                "chainId": 84532,
                "verifyingContract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            },
            "types": {
                "TransferWithAuthorization": [{"name": name, "type": kind} for name, kind in _X402_AUTHORIZATION_FIELDS]
            },
            "primary_type": "TransferWithAuthorization",
            "message": message,
        },
    }


def test_created_wallet_has_no_export_capability(custody):
    wm, wallet, broker, _ = custody
    assert wallet.vault_token == "" and wallet.secret_ttl_seconds == 0
    assert "custody-service.py admin" in wallet.reveal_cmd
    metadata = broker.dispatch({"operation": "list_wallets", "arguments": {}})[0]
    assert not {"secret", "encrypted_secret", "vault_token"}.intersection(metadata)
    assert wm.reveal_wallet_secret(wallet.address).secret  # human role only


@pytest.mark.parametrize(
    "operation",
    [
        "reveal_wallet_secret",
        "reveal-secret",
        "_load_secret",
        "stash_external_secret",
        "confirm_backup",
        "import_wallet",
        "reset_budget",
    ],
)
def test_agent_rpc_never_exposes_administration(custody, operation):
    _, wallet, broker, _ = custody
    with pytest.raises(ValueError, match="not available"):
        broker.dispatch({"operation": operation, "arguments": {"wallet_address": wallet.address}})


def test_backup_precondition_does_not_burn_payment_nonce(custody):
    wm, wallet, broker, _ = custody
    request = payment(wallet)
    with pytest.raises(Exception, match="not backed up"):
        broker.dispatch(request)
    assert broker.policy.db.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0
    assert broker.policy.db.execute("SELECT count(*) FROM usage").fetchone()[0] == 0
    wm.confirm_backup(wallet.address)
    signature = broker.dispatch(request)
    args = request["arguments"]
    signable = encode_typed_data(domain_data=args["domain"], message_types=args["types"], message_data=args["message"])
    assert Account.recover_message(signable, signature=signature) == wallet.address


def test_payment_budget_and_nonce_replay_are_durable(custody):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet, value=2000)
    signature = broker.dispatch(request)
    assert broker.dispatch(request) == signature
    broker = Broker(settings)  # restart does not reset counters or replay records
    assert broker.dispatch(request) == signature
    changed = payment(wallet, value=1000)
    with pytest.raises(ValueError, match="nonce"):
        broker.dispatch(changed)
    assert broker.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "2000"
    assert broker.dispatch(payment(wallet, nonce=2, value=1000)).startswith("0x")
    with pytest.raises(ValueError, match="budget"):
        broker.dispatch(payment(wallet, nonce=3, value=1))


def test_changed_policy_cannot_release_cached_signature(custody):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)
    broker.dispatch(request)
    settings["policy"]["x402"]["payees"] = []
    replacement = Broker(settings)
    with pytest.raises(ValueError, match="independent custody policy"):
        replacement.dispatch(request)


def test_cached_payment_is_not_released_after_expiry(custody, monkeypatch):
    wm, wallet, broker, _ = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)
    broker.dispatch(request)
    expires = request["arguments"]["message"]["validBefore"]
    monkeypatch.setattr("src.custody.policy.time.time", lambda: expires + 1)
    with pytest.raises(ValueError, match="independent custody policy"):
        broker.dispatch(request)
    assert broker.policy.db.execute("SELECT amount FROM usage").fetchone()[0] == "1000"


def swap_request(wallet, nonce=0, amount=1000, minimum=990):
    from eth_abi import encode
    from src.services import transaction_intent as ti

    token, output, executor = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20
    intent = ti.SwapIntent(8453, wallet.address, ti.ROUTER_V6, token, output, amount, minimum)
    desc = (token, output, executor, wallet.address, amount, minimum, 0)
    data = (
        "0x"
        + (ti._SWAP_SELECTORS[ti.ROUTER_V6] + encode(ti._SWAP_TYPES[ti.ROUTER_V6], [executor, desc, b"route"])).hex()
    )
    transaction = dict(
        to=ti.ROUTER_V6,
        data=data,
        value=0,
        nonce=nonce,
        chainId=8453,
        gas=100000,
        maxFeePerGas=10000000,
        maxPriorityFeePerGas=1000000,
    )
    return {
        "operation": "sign",
        "arguments": {
            "unsigned_tx": transaction,
            "intent": ti.intent_to_dict(intent),
            "wallet_address": wallet.address,
            "chain_id": 8453,
        },
    }


def swap_policy(wallet):
    from src.services.transaction_intent import ROUTER_V6

    return {
        "wallet_address": wallet.address,
        "chain_id": 8453,
        "input_token": "0x" + "22" * 20,
        "output_token": "0x" + "33" * 20,
        "router": ROUTER_V6,
        "allowed_executors": ["0x" + "44" * 20],
        "max_amount": 1000,
        "lifetime_amount": 2000,
        "min_rate_numerator": 99,
        "min_rate_denominator": 100,
        "max_gas": 100000,
        "max_fee_per_gas": 10000000,
        "max_total_fee_wei": 1000000000000,
        "lifetime_total_fee_wei": 2000000000000,
        "max_native_value": 0,
        "max_routing_fee_bps": 0,
    }


def test_swap_signatures_use_independent_price_input_and_fee_budgets(custody):
    wm, wallet, _, settings = custody
    wm.confirm_backup(wallet.address)
    settings["policy"]["swap_rules"] = [swap_policy(wallet)]
    broker = Broker(settings)
    request = swap_request(wallet)
    signature = broker.dispatch(request)
    assert Account.recover_transaction(signature) == wallet.address
    assert broker.dispatch(request) == signature
    assert Account.recover_transaction(broker.dispatch(swap_request(wallet, nonce=1))) == wallet.address
    with pytest.raises(ValueError, match="budget"):
        broker.dispatch(swap_request(wallet, nonce=2, amount=1, minimum=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("wallet_address", "0x" + "55" * 20),
        ("max_amount", 999),
        ("min_rate_numerator", 100),
        ("allowed_executors", []),
        ("max_gas", 99999),
        ("max_fee_per_gas", 9999999),
        ("max_total_fee_wei", 999999999999),
        ("lifetime_total_fee_wei", 999999999999),
    ],
)
def test_swap_agent_intent_cannot_widen_independent_policy(custody, field, value):
    wm, wallet, _, settings = custody
    wm.confirm_backup(wallet.address)
    policy = swap_policy(wallet)
    policy[field] = value
    settings["policy"]["swap_rules"] = [policy]
    broker = Broker(settings)
    with pytest.raises(ValueError):
        broker.dispatch(swap_request(wallet))
    assert broker.policy.db.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0


def test_extra_signing_arguments_do_not_consume_budget(custody):
    wm, wallet, broker, _ = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)
    request["arguments"]["unknown"] = True
    with pytest.raises(TypeError):
        broker.dispatch(request)
    assert broker.policy.db.execute("SELECT count(*) FROM usage").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["payee", "chain", "cap", "validity", "future", "expired"])
def test_caller_cannot_widen_payment_policy(custody, mutation):
    wm, wallet, broker, _ = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)
    args = request["arguments"]
    if mutation == "payee":
        args["message"]["to"] = Account.create().address
    if mutation == "chain":
        args["domain"]["chainId"] = 8453
    if mutation == "cap":
        args["message"]["value"] = 2001
    if mutation == "validity":
        args["message"]["validBefore"] = int(time.time()) + 601
    if mutation == "future":
        args["message"]["validAfter"] = int(time.time()) + 1
    if mutation == "expired":
        args["message"]["validBefore"] = int(time.time()) - 1
    with pytest.raises(Exception):
        broker.dispatch(request)
    assert broker.policy.db.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0


def test_signing_exception_releases_reservation_and_hides_secret(custody, monkeypatch):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)
    original = wm.sign_x402_authorization

    def fail(**_):
        raise RuntimeError("synthetic internal signing failure")

    monkeypatch.setattr(wm, "sign_x402_authorization", fail)
    with pytest.raises(RuntimeError):
        broker.dispatch(request)
    # No signature left custody, so the budget is refunded and the nonce is free.
    assert broker.policy.db.execute("SELECT amount FROM usage").fetchone()[0] == "0"
    assert broker.policy.db.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0
    audit = broker.policy.db.execute("SELECT operation,digest,outcome FROM audit").fetchall()
    assert audit[-1][2] == "released_unsigned"
    assert "synthetic internal signing failure" not in json.dumps(audit)
    monkeypatch.setattr(wm, "sign_x402_authorization", original)
    assert Broker(settings).dispatch(request).startswith("0x")
    assert broker.policy.db.execute("SELECT amount FROM usage").fetchone()[0] == "1000"


def _swap_broker(custody, **overrides):
    wm, wallet, _, settings = custody
    wm.confirm_backup(wallet.address)
    rule = swap_policy(wallet)
    rule.update(overrides)
    settings["policy"]["swap_rules"] = [rule]
    return wm, wallet, Broker(settings), settings


def _usage(broker):
    rows = broker.policy.db.execute("SELECT scope, amount FROM usage").fetchall()
    return {scope.split(":")[0]: int(amount) for scope, amount in rows}


def test_unbroadcast_swap_nonce_accepts_requote_and_gas_bump(custody):
    """Review probe HIGH-1: a signed but never mined swap must not strand its nonce."""
    _, wallet, broker, _ = _swap_broker(custody, lifetime_amount=10_000, lifetime_total_fee_wei=10**15)
    first = broker.dispatch(swap_request(wallet, nonce=7, amount=1000, minimum=990))
    requote = broker.dispatch(swap_request(wallet, nonce=7, amount=900, minimum=891))
    assert Account.recover_transaction(requote) == wallet.address and requote != first
    bumped = swap_request(wallet, nonce=7, amount=1000, minimum=990)
    bumped["arguments"]["unsigned_tx"]["maxPriorityFeePerGas"] = 2_000_000
    assert Account.recover_transaction(broker.dispatch(bumped)) == wallet.address
    # One nonce can spend once: the slot is charged at its largest candidate.
    assert _usage(broker) == {"swap": 1000, "gas": 100000 * 10000000}
    # The original request still replays its stored signature without a new charge.
    assert broker.dispatch(swap_request(wallet, nonce=7, amount=1000, minimum=990)) == first
    assert _usage(broker) == {"swap": 1000, "gas": 100000 * 10000000}


def test_signing_failure_frees_swap_nonce_for_identical_retry(custody, monkeypatch):
    """Review probe HIGH-1: a failed signing attempt must not strand its nonce."""
    wm, wallet, broker, _ = _swap_broker(custody)
    calls = {"n": 0}
    original = wm._load_secret

    def flaky(address):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient keyfile read failure")
        return original(address)

    monkeypatch.setattr(wm, "_load_secret", flaky)
    request = swap_request(wallet, nonce=3)
    with pytest.raises(RuntimeError):
        broker.dispatch(request)
    assert _usage(broker) == {"swap": 0, "gas": 0}
    assert Account.recover_transaction(broker.dispatch(request)) == wallet.address
    assert _usage(broker) == {"swap": 1000, "gas": 100000 * 10000000}


def test_crashed_reservation_is_refunded_on_next_request(custody):
    """A reservation left by a crash between commit and signing never released a signature."""
    _, wallet, broker, settings = _swap_broker(custody)
    request = swap_request(wallet, nonce=4)
    with pytest.raises(SystemExit):
        broker.policy.sign("sign", request["arguments"], lambda: (_ for _ in ()).throw(SystemExit()))
    # SystemExit is not an Exception: the reservation survives as if the process died.
    assert _usage(broker) == {"swap": 1000, "gas": 100000 * 10000000}
    restarted = Broker(settings)
    restarted.dispatch(request)
    assert _usage(restarted) == {"swap": 1000, "gas": 100000 * 10000000}


def test_nonce_replacement_cannot_double_spend_or_exceed_budgets(custody):
    """Replacements are charged their excess over the slot; caps still bound the total."""
    _, wallet, broker, _ = _swap_broker(custody, lifetime_amount=1500)
    broker.dispatch(swap_request(wallet, nonce=0, amount=600, minimum=594))
    # Repeated replacements at the slot never charge more than the slot's maximum.
    for amount in (500, 600, 550):
        broker.dispatch(swap_request(wallet, nonce=0, amount=amount, minimum=amount * 99 // 100 + 1))
    assert _usage(broker)["swap"] == 600
    # A larger replacement pays the difference and is checked against the cap.
    broker.dispatch(swap_request(wallet, nonce=0, amount=1000, minimum=990))
    assert _usage(broker)["swap"] == 1000
    broker.dispatch(swap_request(wallet, nonce=1, amount=500, minimum=495))
    assert _usage(broker)["swap"] == 1500
    with pytest.raises(ValueError, match="budget"):
        broker.dispatch(swap_request(wallet, nonce=1, amount=501, minimum=496))
    with pytest.raises(ValueError, match="budget"):
        broker.dispatch(swap_request(wallet, nonce=2, amount=1, minimum=1))
    assert _usage(broker)["swap"] == 1500


@pytest.mark.parametrize("field,value", [("max_amount", 999), ("allowed_executors", []),
                                         ("min_rate_numerator", 100), ("max_fee_per_gas", 9_999_999)])
def test_nonce_replacement_must_pass_policy_on_its_own(custody, field, value):
    wm, wallet, broker, settings = _swap_broker(custody)
    broker.dispatch(swap_request(wallet, nonce=5))
    settings["policy"]["swap_rules"][0][field] = value
    with pytest.raises(ValueError):
        Broker(settings).dispatch(swap_request(wallet, nonce=5, amount=1000, minimum=991))
    assert _usage(broker) == {"swap": 1000, "gas": 100000 * 10000000}


def test_different_payment_at_signed_x402_nonce_is_still_refused(custody):
    wm, wallet, broker, _ = custody
    wm.confirm_backup(wallet.address)
    broker.dispatch(payment(wallet, value=1000))
    with pytest.raises(ValueError, match="already used this signing nonce"):
        broker.dispatch(payment(wallet, value=500))
    assert broker.policy.db.execute("SELECT amount FROM usage").fetchone()[0] == "1000"


def test_no_configurable_local_secret_fallback(custody, monkeypatch):
    wm, wallet, _, _ = custody
    from src.config import app_config

    monkeypatch.setattr(runtime, "_active", False)
    monkeypatch.setattr(app_config, "CUSTODY_SOCKET", None)
    for operation in [
        lambda: wm._load_secret(wallet.address),
        lambda: wm.reveal_wallet_secret(wallet.address),
        lambda: wm.stash_external_secret("no secret"),
        lambda: wm.confirm_backup(wallet.address),
    ]:
        with pytest.raises(Exception, match="isolated custody service"):
            operation()
    with pytest.raises(Exception, match="not configured"):
        wm.create_wallet("evm", "testnet", 84532)
    assert wm.list_wallets()[0].custody_status == "migration_required"


def test_real_unix_peer_credentials():
    left, right = socket.socketpair()
    try:
        assert peer_uid(left) == os.getuid()
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("payload", [b'{"operation":"x","operation":"y"}\n', b"[]\n", b"{}\n{}\n"])
def test_transport_rejects_ambiguous_frames(payload):
    left, right = socket.socketpair()
    try:
        right.sendall(payload)
        with pytest.raises(ValueError):
            receive(left)
    finally:
        left.close()
        right.close()


def test_transport_rejects_frame_one_byte_over_limit(monkeypatch):
    class Connection:
        def __init__(self):
            self.sent = False

        def gettimeout(self):
            return None

        def settimeout(self, value):
            pass

        def recv(self, maximum):
            if self.sent:
                return b"\n"
            self.sent = True
            return b" " * MAX_FRAME

    with pytest.raises(ValueError, match="too large"):
        receive(Connection())


def test_transport_deadline_stops_a_slow_drip_and_restores_timeout(monkeypatch):
    moments = iter([0, 0, 11])
    monkeypatch.setattr("src.custody.transport.time.monotonic", lambda: next(moments))

    class Connection:
        timeout = 20
        reads = 0

        def gettimeout(self):
            return self.timeout

        def settimeout(self, value):
            self.timeout = value

        def recv(self, maximum):
            self.reads += 1
            return b" "

    connection = Connection()
    with pytest.raises(TimeoutError, match="deadline"):
        receive(connection)
    assert connection.reads == 1 and connection.timeout == 20


def test_bootstrap_refuses_nonisolated_interpreter_before_loading_configuration():
    from src.custody.bootstrap import initialize

    with pytest.raises(ValueError, match="Python -I"):
        initialize("/no/configuration/should/be/read.json")


def test_protected_paths_reject_writable_ancestors_and_symlinks(protected_directory):
    from src.custody.transport import check_ancestors, check_owned_path

    nested = protected_directory / "private"
    nested.mkdir(mode=0o700)
    check_ancestors(nested, allowed_owners={0, os.getuid()})
    nested.chmod(0o777)
    with pytest.raises(ValueError, match="writable"):
        check_ancestors(nested, allowed_owners={0, os.getuid()})
    nested.chmod(0o700)
    link = protected_directory / "link"
    link.symlink_to(nested)
    with pytest.raises(ValueError, match="unsafe ownership"):
        check_owned_path(link, owner=os.getuid())


def test_generated_service_definitions_preserve_platform_boundary():
    import plistlib
    import shlex

    script = Path(__file__).resolve().parents[3] / "scripts/install-custody.py"
    spec = importlib.util.spec_from_file_location("install_custody", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = [
        "/opt/space dir/.venv/bin/python",
        "-I",
        "/opt/space dir/scripts/custody-service.py",
        "daemon",
        "--config",
        "/etc/custody/policy.json",
    ]
    name, raw = module.service_definition(
        command, "custody-user", 10002, Path("/var/lib/custody"), Path("/run/custody"), "linux"
    )
    service = raw.decode()
    assert name.endswith(".service")
    assert "NoNewPrivileges=yes" in service and "CapabilityBoundingSet=\n" in service
    assert "RuntimeDirectory=custody\n" in service and "Restart=on-failure" in service
    start = next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(start) == command
    name, raw = module.service_definition(
        command,
        "custody-user",
        10002,
        Path("/Library/MangroveCustody/state"),
        Path("/Library/MangroveCustody/run"),
        "darwin",
    )
    plist = plistlib.loads(raw)
    assert name.endswith(".plist") and plist["UserName"] == "custody-user"
    assert plist["ProgramArguments"] == command
    defaults = module.argument_parser("darwin").parse_args(["--custody-user", "custody", "--agent-user", "agent"])
    assert defaults.destination == "/Library/MangroveCustody/app"
    assert defaults.socket_dir == "/Library/MangroveCustody/run"


def test_migration_reencrypts_preserves_recovery_and_resumes(custody, protected_directory):
    import shutil

    from src.custody.admin import migrate
    from src.shared.db import sqlite

    wm, wallet, _, settings = custody
    wm.confirm_backup(wallet.address)
    original = wm._load_secret(wallet.address)
    old_db, old_key = protected_directory / "legacy.db", protected_directory / "legacy.key"
    import sqlite3

    snapshot = sqlite3.connect(old_db)
    sqlite.get_connection().backup(snapshot)
    snapshot.close()
    shutil.copyfile(protected_directory / "master.key", old_key)
    old_db.chmod(0o600)
    old_key.chmod(0o600)
    # Existing same-key wallets make migration resumable rather than overwriting.
    first = migrate(old_db, old_key, settings)
    assert migrate(old_db, old_key, settings) == first
    assert wm._load_secret(wallet.address) == original
    assert wm._get_wallet_row(wallet.address)["backup_confirmed_at"]
    assert original not in (protected_directory / "migration-receipt.json").read_text()
    # Finalization requires verified receipt and checks every original ciphertext.
    script = Path(__file__).resolve().parents[3] / "scripts/custody-retire-legacy.py"
    spec = importlib.util.spec_from_file_location("retire_custody", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Privilege decision is unit-tested with disposable paths only, not an OS test.
    from unittest.mock import patch

    with patch.object(module.os, "geteuid", return_value=0):
        module.retire(old_db, protected_directory / "migration-receipt.json", os.getuid())
    check = sqlite3.connect(old_db)
    record = check.execute("SELECT encrypted_secret,encryption_method FROM wallets").fetchone()
    assert record == (b"", "external-custody")
    check.close()


def test_migration_requires_new_backup_confirmation(custody, protected_directory):
    """Review probe MEDIUM-3: a legacy backup flag, settable by the agent, is not migrated."""
    import sqlite3

    from cryptography.fernet import Fernet
    from src.custody.admin import migrate
    from src.shared.errors import SigningError

    wm, _, _, settings = custody
    legacy_key = Fernet.generate_key()
    account = Account.create()
    legacy_dir = protected_directory / "legacy"
    legacy_dir.mkdir(mode=0o700)
    database = legacy_dir / "agent.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE wallets (id TEXT, address TEXT, chain TEXT, network TEXT, chain_id INTEGER, "
                 "encrypted_secret BLOB, encryption_method TEXT, label TEXT, created_at TEXT, "
                 "metadata_json TEXT, backup_confirmed_at TEXT)")
    conn.execute("INSERT INTO wallets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("id1", account.address, "evm", "mainnet", 8453,
                  Fernet(legacy_key).encrypt(("0x" + account.key.hex().removeprefix("0x")).encode()),
                  "fernet-v1", None, "2026-01-01T00:00:00+00:00", None, "2026-01-02T00:00:00+00:00"))
    conn.commit()
    conn.close()
    os.chmod(database, 0o600)
    keyfile = legacy_dir / "master.key"
    keyfile.write_bytes(legacy_key)
    os.chmod(keyfile, 0o600)
    migrate(database, keyfile, settings)
    assert wm._get_wallet_row(account.address)["backup_confirmed_at"] is None
    with pytest.raises(SigningError, match="not backed up"):
        wm.require_backup_confirmed(account.address)
    wm.confirm_backup(account.address)
    wm.require_backup_confirmed(account.address)


def _legacy_snapshot(directory, rows):
    """Write a stopped legacy agent database and master key as private files."""
    import sqlite3

    from cryptography.fernet import Fernet

    legacy_key = Fernet.generate_key()
    directory.mkdir(mode=0o700)
    database = directory / "agent.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE wallets (id TEXT, address TEXT, chain TEXT, network TEXT, chain_id INTEGER, "
                 "encrypted_secret BLOB, encryption_method TEXT, label TEXT, created_at TEXT, "
                 "metadata_json TEXT, backup_confirmed_at TEXT)")
    for index, (address, secret, method) in enumerate(rows):
        ciphertext = Fernet(legacy_key).encrypt(secret.encode()) if secret else b""
        conn.execute("INSERT INTO wallets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (f"id{index}", address, "evm", "testnet", 84532, ciphertext, method, None,
                      "2026-01-01T00:00:00+00:00", None, None))
    conn.commit()
    conn.close()
    os.chmod(database, 0o600)
    keyfile = directory / "master.key"
    keyfile.write_bytes(legacy_key)
    os.chmod(keyfile, 0o600)
    return database, keyfile


def test_migration_skips_external_custody_rows_and_migrates_the_rest(custody, protected_directory):
    """A legacy row already held by external custody has no ciphertext to migrate."""
    import sqlite3

    from src.custody.admin import migrate

    wm, _, _, settings = custody
    legacy, external = Account.create(), Account.create()
    database, keyfile = _legacy_snapshot(protected_directory / "legacy", [
        (legacy.address, "0x" + legacy.key.hex().removeprefix("0x"), "fernet-v1"),
        (external.address, None, "external-custody"),
    ])
    result = migrate(database, keyfile, settings)
    assert [item["address"] for item in result["wallets"]] == [legacy.address]
    assert result["skipped"] == [{"address": external.address, "reason": "already_external_custody"}]
    assert wm._load_secret(legacy.address) == "0x" + legacy.key.hex().removeprefix("0x")
    assert wm._get_wallet_row(external.address) is None
    assert migrate(database, keyfile, settings) == result

    script = Path(__file__).resolve().parents[3] / "scripts/custody-retire-legacy.py"
    spec = importlib.util.spec_from_file_location("retire_custody", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from unittest.mock import patch

    with patch.object(module.os, "geteuid", return_value=0):
        module.retire(database, protected_directory / "migration-receipt.json", os.getuid())
    check = sqlite3.connect(database)
    methods = [method for (method,) in check.execute("SELECT encryption_method FROM wallets")]
    check.close()
    # After retirement no row keeps the migration_required gate closed.
    assert "migration_required" not in {wm._custody_status(method) for method in methods}


def test_migration_still_aborts_on_unknown_encryption(custody, protected_directory):
    from src.custody.admin import migrate

    wm, _, _, settings = custody
    legacy = Account.create()
    database, keyfile = _legacy_snapshot(protected_directory / "legacy", [
        (legacy.address, "0x" + legacy.key.hex().removeprefix("0x"), "fernet-v1"),
        (Account.create().address, "unknown", "rot13"),
    ])
    with pytest.raises(ValueError, match="no migration was committed"):
        migrate(database, keyfile, settings)
    assert wm._get_wallet_row(legacy.address) is None


def test_startup_sweep_refunds_crashed_payment_reservation(custody):
    """A crash between reservation and stored result left no signature outside custody."""
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet, nonce=9, value=2000)
    with pytest.raises(SystemExit):
        broker.policy.sign("sign_x402_authorization", request["arguments"],
                           lambda: (_ for _ in ()).throw(SystemExit()))
    assert broker.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "2000"
    restarted = Broker(settings)
    assert restarted.policy.recover_unfinished() == 1
    assert restarted.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "0"
    assert restarted.policy.db.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0
    assert restarted.policy.db.execute("SELECT outcome FROM audit ORDER BY id DESC").fetchone()[0] == "released_at_startup"
    # The freed budget pays a different payment; the never-signed nonce is not burned.
    assert restarted.dispatch(payment(wallet, nonce=10, value=2000)).startswith("0x")


def test_startup_sweep_keeps_released_signatures_counted(custody):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    signature = broker.dispatch(payment(wallet, nonce=11, value=2000))
    restarted = Broker(settings)
    assert restarted.policy.recover_unfinished() == 0
    assert restarted.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "2000"
    assert restarted.dispatch(payment(wallet, nonce=11, value=2000)) == signature
    with pytest.raises(ValueError, match="budget"):
        restarted.dispatch(payment(wallet, nonce=12, value=1001))


def test_identical_payment_replay_signs_once_even_after_policy_change(custody, monkeypatch):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    calls = []
    original = wm.sign_x402_authorization

    def counting(**arguments):
        calls.append(arguments["message"]["nonce"])
        return original(**arguments)

    monkeypatch.setattr(wm, "sign_x402_authorization", counting)
    request = payment(wallet, nonce=21, value=1000)
    signature = broker.dispatch(request)
    assert broker.dispatch(copy_request(request)) == signature
    # A benign policy change (higher lifetime cap) must not re-sign the cached payment.
    settings["policy"]["x402"]["lifetime_micro_usd"] = 4000
    changed = Broker(settings)

    def fail(**_):
        raise RuntimeError("synthetic signer failure")

    monkeypatch.setattr(wm, "sign_x402_authorization", fail)
    assert changed.dispatch(copy_request(request)) == signature
    assert len(calls) == 1
    assert changed.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "1000"
    assert changed.policy.db.execute("SELECT count(*) FROM signatures WHERE result IS NOT NULL").fetchone()[0] == 1
    # The signed nonce stays bound to its first payment.
    with pytest.raises(ValueError, match="already used this signing nonce"):
        changed.dispatch(payment(wallet, nonce=21, value=500))


def copy_request(request):
    return json.loads(json.dumps(request))


class _EchoBroker:
    class _Ledger:
        def recover_unfinished(self):
            return 0

    def __init__(self, settings):
        self.policy = self._Ledger()

    def dispatch(self, request):
        return {"echo": request["operation"]}


def _serve_in_thread(monkeypatch, settings, broker_class=_EchoBroker):
    import threading

    from src.custody import daemon

    monkeypatch.setattr(daemon.signal, "signal", lambda *_: None)
    monkeypatch.setattr(daemon, "Broker", broker_class)
    errors = []

    def run():
        try:
            daemon.serve(settings)
        except Exception as error:  # noqa: BLE001 - reported to the test thread below
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not Path(settings["socket_path"]).exists() and not errors and time.monotonic() < deadline:
        time.sleep(0.01)
    return thread, errors


def _rpc(path, request, timeout=5):
    from src.custody.transport import send

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        send(client, request)
        return receive(client, timeout_seconds=timeout)


@pytest.fixture
def short_socket_dir():
    """Unix socket paths are limited to about 104 bytes, so use a short scratch directory."""
    with tempfile.TemporaryDirectory(prefix="mcs-", dir="/tmp") as directory:
        yield Path(directory)


def test_silent_client_does_not_block_other_custody_clients(protected_directory, short_socket_dir, monkeypatch):
    settings = {"state_dir": str(protected_directory), "socket_path": str(short_socket_dir / "s.sock"),
                "agent_uid": os.getuid()}
    _, errors = _serve_in_thread(monkeypatch, settings)
    assert not errors and Path(settings["socket_path"]).exists()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        silent.connect(settings["socket_path"])
        time.sleep(0.2)  # the daemon has accepted the silent client and is waiting on it
        started = time.monotonic()
        response = _rpc(settings["socket_path"], {"operation": "list_wallets", "arguments": {}})
        assert response == {"ok": True, "result": {"echo": "list_wallets"}}
        assert time.monotonic() - started < 2
    finally:
        silent.close()


def test_daemon_refuses_new_connections_beyond_the_concurrency_bound(protected_directory, short_socket_dir, monkeypatch):
    from src.custody import daemon

    settings = {"state_dir": str(protected_directory), "socket_path": str(short_socket_dir / "s.sock"),
                "agent_uid": os.getuid()}
    _serve_in_thread(monkeypatch, settings)
    silent = []
    try:
        for _ in range(daemon.MAX_CONNECTIONS):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(settings["socket_path"])
            silent.append(connection)
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as extra:
            extra.settimeout(2)
            extra.connect(settings["socket_path"])
            assert receive(extra, timeout_seconds=2) == {"ok": False, "error": "Custody service is busy"}
    finally:
        for connection in silent:
            connection.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if _rpc(settings["socket_path"], {"operation": "list_wallets", "arguments": {}})["ok"]:
                break
        except (OSError, ValueError):
            pass  # slots free as the closed silent clients are reaped
        time.sleep(0.05)
    else:
        pytest.fail("daemon did not recover after silent clients disconnected")


def test_serve_sweeps_crashed_reservations_and_refuses_a_second_daemon(custody, short_socket_dir, monkeypatch):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet, nonce=31, value=2000)
    with pytest.raises(SystemExit):
        broker.policy.sign("sign_x402_authorization", request["arguments"],
                           lambda: (_ for _ in ()).throw(SystemExit()))
    settings.update(socket_path=str(short_socket_dir / "s.sock"), agent_uid=os.getuid())
    _, errors = _serve_in_thread(monkeypatch, settings, broker_class=Broker)
    assert not errors and Path(settings["socket_path"]).exists()
    assert broker.policy.db.execute("SELECT amount FROM usage WHERE scope='x402'").fetchone()[0] == "0"
    # A second daemon on the same state directory refuses before touching the ledger.
    second = dict(settings, socket_path=str(short_socket_dir / "t.sock"))
    from src.custody import daemon
    with pytest.raises(ValueError, match="already running"):
        daemon.serve(second)
    assert not Path(second["socket_path"]).exists()


def test_state_lock_outlives_serve_until_process_exit(custody, short_socket_dir, monkeypatch):
    """Detached worker threads may still be signing after serve() returns, so it keeps the lock."""
    from types import SimpleNamespace

    from src.custody import daemon

    _, _, _, settings = custody
    settings.update(socket_path=str(short_socket_dir / "s.sock"), agent_uid=os.getuid())

    class StoppingListener(socket.socket):
        def accept(self):
            raise SystemExit(0)

    acquired = []
    acquire = daemon._acquire_state_lock
    monkeypatch.setattr(daemon, "_acquire_state_lock", lambda state_dir: acquired.append(acquire(state_dir)) or acquired[-1])
    monkeypatch.setattr(daemon, "socket", SimpleNamespace(
        socket=StoppingListener, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM))
    monkeypatch.setattr(daemon.signal, "signal", lambda *_: None)
    try:
        with pytest.raises(SystemExit):
            daemon.serve(settings)
        assert not Path(settings["socket_path"]).exists()
        with pytest.raises(ValueError, match="already running"):
            acquire(Path(settings["state_dir"]))
    finally:
        for descriptor in acquired:
            os.close(descriptor)


def test_migration_never_prints_a_crafted_skipped_address(custody, protected_directory, capsys):
    """A legacy row's address is untrusted DB text: it must not reach the terminal raw."""
    from src.custody.admin import migrate, print_migration_result

    _, _, _, settings = custody
    legacy, external = Account.create(), Account.create()
    crafted = "0x" + "ab" * 20 + "\x1b[2J\nMigrated 999 wallets"
    database, keyfile = _legacy_snapshot(protected_directory / "legacy", [
        (legacy.address, "0x" + legacy.key.hex().removeprefix("0x"), "fernet-v1"),
        (crafted, None, "external-custody"),
        (external.address, None, "external-custody"),
    ])
    result = migrate(database, keyfile, settings)
    assert [item["address"] for item in result["wallets"]] == [legacy.address]
    reasons = {item["reason"]: item["address"] for item in result["skipped"]}
    assert reasons["already_external_custody"] == external.address
    assert reasons["invalid_address"] == repr(crafted)
    print_migration_result(result)
    printed = capsys.readouterr().out
    assert "\x1b" not in printed
    assert "Skipped " + external.address + ": already_external_custody\n" in printed
    assert "\\x1b[2J\\nMigrated 999 wallets" in printed
    assert all(line.startswith(("Skipped ", "Migrated 1 wallets", "Backup confirmations"))
               for line in printed.splitlines())
