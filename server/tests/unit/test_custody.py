"""Synthetic custody integration: real crypto/SQLite, no funded accounts/network.

These tests verify service behavior. Separate distinct-UID E2E proves OS access
control; monkeypatched roles here do not constitute an isolation test.
"""

import importlib.util
import json
import os
import socket
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from src.custody import runtime
from src.custody.daemon import Broker
from src.custody.transport import MAX_FRAME, peer_uid, receive


@pytest.fixture
def custody(tmp_path, monkeypatch):
    from src.config import app_config
    from src.services import wallet_manager as wm
    from src.shared.crypto import fernet
    from src.shared.db import sqlite

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "wallets.db"))
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(tmp_path / "master.key"))
    monkeypatch.setattr("keyring.get_password", lambda *_: None)
    monkeypatch.setattr(runtime, "_active", True)
    sqlite.reset_connection()
    fernet.reset_master_key_cache()
    sqlite.init_db()
    wallet = wm.create_wallet(chain="evm", network="testnet", chain_id=84532)
    settings = {
        "state_dir": str(tmp_path),
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


def test_signing_exception_keeps_reservation_and_hides_secret(custody, monkeypatch):
    wm, wallet, broker, settings = custody
    wm.confirm_backup(wallet.address)
    request = payment(wallet)

    def fail(**_):
        raise RuntimeError("synthetic internal signing failure")

    monkeypatch.setattr(wm, "sign_x402_authorization", fail)
    with pytest.raises(RuntimeError):
        broker.dispatch(request)
    assert broker.policy.db.execute("SELECT amount FROM usage").fetchone()[0] == "1000"
    with pytest.raises(ValueError, match="uncertain request"):
        Broker(settings).dispatch(request)
    audit = broker.policy.db.execute("SELECT operation,digest,outcome FROM audit").fetchall()
    assert audit[-1][2] == "refused"
    assert "synthetic internal signing failure" not in json.dumps(audit)


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


def test_protected_paths_reject_writable_ancestors_and_symlinks(tmp_path):
    from src.custody.transport import check_ancestors, check_owned_path

    nested = tmp_path / "private"
    nested.mkdir(mode=0o700)
    check_ancestors(nested, allowed_owners={0, os.getuid()})
    nested.chmod(0o777)
    with pytest.raises(ValueError, match="writable"):
        check_ancestors(nested, allowed_owners={0, os.getuid()})
    nested.chmod(0o700)
    link = tmp_path / "link"
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


def test_migration_reencrypts_preserves_recovery_and_resumes(custody, tmp_path):
    import shutil

    from src.custody.admin import migrate
    from src.shared.db import sqlite

    wm, wallet, _, settings = custody
    wm.confirm_backup(wallet.address)
    original = wm._load_secret(wallet.address)
    old_db, old_key = tmp_path / "legacy.db", tmp_path / "legacy.key"
    import sqlite3

    snapshot = sqlite3.connect(old_db)
    sqlite.get_connection().backup(snapshot)
    snapshot.close()
    shutil.copyfile(tmp_path / "master.key", old_key)
    old_db.chmod(0o600)
    old_key.chmod(0o600)
    # Existing same-key wallets make migration resumable rather than overwriting.
    first = migrate(old_db, old_key, settings)
    assert migrate(old_db, old_key, settings) == first
    assert wm._load_secret(wallet.address) == original
    assert wm._get_wallet_row(wallet.address)["backup_confirmed_at"]
    assert original not in (tmp_path / "migration-receipt.json").read_text()
    # Finalization requires verified receipt and checks every original ciphertext.
    script = Path(__file__).resolve().parents[3] / "scripts/custody-retire-legacy.py"
    spec = importlib.util.spec_from_file_location("retire_custody", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Privilege decision is unit-tested with disposable paths only, not an OS test.
    from unittest.mock import patch

    with patch.object(module.os, "geteuid", return_value=0):
        module.retire(old_db, tmp_path / "migration-receipt.json", os.getuid())
    check = sqlite3.connect(old_db)
    record = check.execute("SELECT encrypted_secret,encryption_method FROM wallets").fetchone()
    assert record == (b"", "external-custody")
    check.close()
