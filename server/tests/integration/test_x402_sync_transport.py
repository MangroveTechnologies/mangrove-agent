"""Offline integration through real x402 signing, custody, SQLite and the SDK."""
from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from x402.http.utils import encode_payment_required_header
from x402.schemas.payments import PaymentRequired, PaymentRequirements

from src.config import app_config
from src.services import spend_service, wallet_manager, x402_payer
from src.shared.clients.mangrove import create_x402_mangrove_client
from src.shared.crypto import fernet
from src.shared.db import sqlite
from src.shared.errors import AgentError, ValidationError, X402PaymentError, X402SpendCapExceeded
from src.shared.x402.sync_transport import X402SyncTransport

ORIGIN = "https://payments.test"
URL = ORIGIN + "/api/v1/signals/"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAYEE = "0xde991861bB3e7078015826Fad749de398F6ec1f6"


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setattr(app_config, "MASTER_KEY_PATH", str(tmp_path / "master.key"))
    monkeypatch.setattr(app_config, "X402_NETWORK", "eip155:84532")
    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", "")
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 25)
    monkeypatch.setattr("keyring.get_password", lambda *args: None)
    monkeypatch.delenv("MANGROVE_API_KEY", raising=False)
    sqlite.reset_connection()
    fernet.reset_master_key_cache()
    sqlite.init_db()
    yield
    sqlite.reset_connection()
    fernet.reset_master_key_cache()


@pytest.fixture
def wallet(database):
    result = wallet_manager.create_wallet(chain="evm", network="testnet", chain_id=84532)
    address = result.address
    wallet_manager.confirm_backup(address)
    return address


def challenge(amount="1000", network="eip155:84532", asset=USDC, extra=None):
    required = PaymentRequired(x402_version=2, accepts=[PaymentRequirements(
        scheme="exact", network=network, asset=asset, amount=amount,
        pay_to=PAYEE, max_timeout_seconds=300,
        extra=extra or {"name": "USDC", "version": "2"},
    )])
    return httpx.Response(402, headers={"PAYMENT-REQUIRED": encode_payment_required_header(required)})


def receipt(wallet):
    return base64.b64encode(json.dumps({
        "success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:84532", "payer": wallet,
    }).encode()).decode()


def payment(request):
    return json.loads(base64.b64decode(request.headers["PAYMENT-SIGNATURE"]))["payload"]


def client(handler, wallet=None, **kwargs):
    return httpx.Client(transport=X402SyncTransport(
        allowed_origins=[ORIGIN], wallet_address=wallet,
        transport=httpx.MockTransport(handler),
    ), **kwargs)


@pytest.fixture
def automatic_client(monkeypatch):
    """Keep real factory selection, SDK, transport and ledger; replace only HTTP."""
    from src.mcp.server import reset_mcp_server
    from src.shared.clients import mangrove

    mangrove.reset_clients()
    reset_mcp_server()
    # The inbound donation demo is unrelated to outbound SDK payments.
    # Avoid its external facilitator handshake while registering normal tools.
    def unavailable_demo():
        raise ConnectionError("offline demo")

    monkeypatch.setattr("src.shared.x402.server._ensure_initialized", unavailable_demo)
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "")
    monkeypatch.setattr(app_config, "X402_MANGROVE_ENVIRONMENT", "dev")
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", ORIGIN + "/api/v1")
    monkeypatch.setattr(app_config, "X402_MANGROVE_KB_BASE_URL", ORIGIN + "/kb")

    def install(handler):
        monkeypatch.setattr(httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(handler))
        return mangrove.mangrove_ai_client

    yield install
    mangrove.reset_clients()
    reset_mcp_server()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,search,expected_payments", [(1, None, 1), (101, None, 2), (1, "trend", 1)])
async def test_normal_mcp_tool_automatically_pays_only_needed_pages(
    wallet, automatic_client, monkeypatch, limit, search, expected_payments,
):
    from src.mcp.server import create_mcp_server
    from src.shared.auth.middleware import reset_request_api_key, set_request_api_key

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    # Config URLs/environment must take precedence over every ambient SDK default.
    monkeypatch.setenv("MANGROVE_ENVIRONMENT", "prod")
    monkeypatch.setenv("MANGROVE_BASE_URL", "https://wrong.test/api/v1")
    monkeypatch.setenv("MANGROVE_KB_BASE_URL", "https://wrong.test/kb")
    monkeypatch.setattr("mangrove_ai._config._maybe_load_dotenv", lambda: pytest.fail("dotenv loaded"))
    seen, nonces = [], []

    def handle(request):
        seen.append(request)
        assert request.url.host == "payments.test"
        assert "Authorization" not in request.headers
        assert "X-API-Key" not in request.headers  # local key must not be forwarded
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        signed = payment(request)
        assert signed["authorization"]["from"] == wallet
        nonces.append(signed["authorization"]["nonce"])
        if search:
            body = json.loads(request.content)
            assert body["query"] == search
            size, offset = body["limit"], 0
        else:
            size, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
        return httpx.Response(200, json={
            "signals": [{"name": f"signal_{i}", "category": "trend"} for i in range(offset, offset + size)],
            "total": 10000, "limit": size, "offset": offset,
        }, headers={"payment-response": receipt(wallet)})

    automatic_client(handle)
    server = create_mcp_server()
    token = set_request_api_key("test-key-1")
    try:
        result = json.loads(await server._tool_manager._tools["list_signals"].run({"limit": limit, "search": search}))
    finally:
        reset_request_api_key(token)
    assert len(result["items"]) == limit
    assert len(seen) == 2 * expected_payments
    assert len(set(nonces)) == expected_payments
    rows = spend_service.list_payments()
    assert len(rows) == expected_payments
    assert all(row["state"] == "settled" for row in rows)
    assert spend_service.check_before_payment()["spent_usd"] == expected_payments * 0.001


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [("missing_wallet", "VALIDATION_ERROR"), ("cap", "X402_SPEND_CAP_EXCEEDED")])
async def test_normal_tool_payment_failures_are_structured_without_signed_retry(
    wallet, automatic_client, monkeypatch, failure, code,
):
    from src.mcp.server import create_mcp_server

    if failure == "cap":
        monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
        monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0.0001)
    seen = []

    def handle(request):
        seen.append(request)
        assert "PAYMENT-SIGNATURE" not in request.headers
        return challenge()

    automatic_client(handle)
    tool = create_mcp_server()._tool_manager._tools["list_signals"]
    result = json.loads(await tool.run({"api_key": "test-key-1", "limit": 1}))
    assert result["error"] is True
    assert result["code"] == code
    assert len(seen) == 1
    assert spend_service.list_payments() == []


@pytest.mark.asyncio
async def test_normal_tool_still_requires_local_auth(database, automatic_client):
    from src.mcp.server import create_mcp_server

    automatic_client(lambda request: pytest.fail("unauthorized caller reached upstream"))
    tool = create_mcp_server()._tool_manager._tools["list_signals"]
    for key in ("", "wrong-key"):
        result = json.loads(await tool.run({"api_key": key}))
        assert result["code"] == "AUTH_INVALID_API_KEY"
    assert spend_service.list_payments() == []


def test_automatic_free_call_needs_no_wallet_or_budget(database, automatic_client):
    get_client = automatic_client(lambda request: httpx.Response(200, json={"signals": [], "total": 0}))
    assert get_client().signals.list().total == 0
    assert spend_service.list_payments() == []


@pytest.mark.parametrize("wrong_network", [False, True])
def test_real_sdk_uses_desktop_defaults_without_changing_payment_network(
    wallet, automatic_client, monkeypatch, wrong_network,
):
    monkeypatch.setattr(app_config, "ENVIRONMENT", "local")
    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    for key in ("X402_MANGROVE_ENVIRONMENT", "X402_MANGROVE_BASE_URL", "X402_MANGROVE_KB_BASE_URL"):
        monkeypatch.setattr(app_config, key, None)
    monkeypatch.setenv("MANGROVE_ENVIRONMENT", "dev")
    monkeypatch.setenv("MANGROVE_BASE_URL", "https://wrong.test/api/v1")
    monkeypatch.setenv("MANGROVE_KB_BASE_URL", "https://wrong.test/api")
    seen = []

    def handle(request):
        seen.append(request)
        assert str(request.url).startswith("https://api.mangrovedeveloper.ai/api/v1/signals/")
        assert "Authorization" not in request.headers
        assert "X-API-Key" not in request.headers
        if wrong_network:
            assert "PAYMENT-SIGNATURE" not in request.headers
            return challenge(network="eip155:8453", asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                             extra={"name": "USD Coin", "version": "2"})
        return httpx.Response(200, json={"signals": [], "total": 0})

    get_client = automatic_client(handle)
    if wrong_network:
        with pytest.raises(X402PaymentError):
            get_client().signals.list()
    else:
        assert get_client().signals.list().total == 0
    assert len(seen) == 1
    assert app_config.X402_NETWORK == "eip155:84532"
    assert spend_service.list_payments() == []


def test_normal_mcp_http_request_pays_with_local_header_auth(wallet, automatic_client, monkeypatch):
    from fastapi.testclient import TestClient

    from src.app import create_app

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    monkeypatch.setattr("src.services.scheduler_service.start", lambda: None)
    monkeypatch.setattr("src.services.scheduler_service.shutdown", lambda: None)
    seen = []

    def handle(request):
        seen.append(request)
        assert "Authorization" not in request.headers
        assert "X-API-Key" not in request.headers
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        return httpx.Response(200, json={"signals": [{"name": "trend", "category": "trend"}], "total": 1},
                              headers={"payment-response": receipt(wallet)})

    automatic_client(handle)
    with TestClient(create_app(), base_url="http://localhost:9080") as local:
        response = local.post("/mcp/", headers={
            "X-API-Key": "test-key-1", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "list_signals", "arguments": {"limit": 1}}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError")
    content = json.loads(result["content"][0]["text"])
    assert content["items"][0]["name"] == "trend"
    assert len(seen) == 2
    assert spend_service.list_payments()[0]["state"] == "settled"


def test_automatic_mode_refuses_ambient_key_without_mutating_environment(database, automatic_client, monkeypatch):
    import os

    monkeypatch.setenv("MANGROVE_API_KEY", "prod_ambient")
    get_client = automatic_client(lambda request: pytest.fail("ambient key escaped"))
    with pytest.raises(ValidationError, match="credentials"):
        get_client().signals.list()
    assert os.environ["MANGROVE_API_KEY"] == "prod_ambient"
    assert spend_service.list_payments() == []


@pytest.mark.asyncio
async def test_normal_rest_route_keeps_payment_error_code(wallet, automatic_client, monkeypatch):
    from src.api.routes.signals import list_signals

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0.0001)
    automatic_client(lambda request: challenge())
    with pytest.raises(X402SpendCapExceeded):
        await list_signals(limit=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_invalid_signal_limit_does_not_call_upstream(database, automatic_client, limit):
    from src.mcp.server import create_mcp_server

    automatic_client(lambda request: pytest.fail("invalid limit reached upstream"))
    tool = create_mcp_server()._tool_manager._tools["list_signals"]
    result = json.loads(await tool.run({"api_key": "test-key-1", "limit": limit}))
    assert result["code"] == "VALIDATION_ERROR"
    assert spend_service.list_payments() == []


@pytest.mark.parametrize("amount", ["1000", "50000"])
def test_real_signature_roundtrip_and_ledger(wallet, amount):
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return challenge(amount)
        signed = payment(request)
        auth = signed["authorization"]
        assert auth["from"] == wallet
        assert auth["to"] == PAYEE
        assert auth["value"] == amount
        assert int(auth["validBefore"]) >= int(time.time()) + 295
        message = dict(auth)
        for key in ("value", "validAfter", "validBefore"):
            message[key] = int(message[key])
        message["nonce"] = bytes.fromhex(message["nonce"][2:])
        signable = encode_typed_data(
            domain_data={"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": USDC},
            message_types={"TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
            ]}, message_data=message,
        )
        assert Account.recover_message(signable, signature=signed["signature"]) == wallet
        return httpx.Response(200, json={"ok": True}, headers={"payment-response": receipt(wallet)})

    with client(handle, wallet) as http:
        response = http.post(URL + "?token=private", content=iter([b'{"x":', b'1}']), timeout=80)
    assert response.json() == {"ok": True}
    assert len(seen) == 2
    assert seen[0].content == seen[1].content == b'{"x":1}'
    assert seen[0].url == seen[1].url
    assert seen[0].method == seen[1].method == "POST"
    assert seen[0].extensions["timeout"] == seen[1].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 80)
    assert seen[0].headers["X-Wallet-Address"] == wallet
    assert seen[0].headers["X-Mcp-Session-Id"] == seen[1].headers["X-Mcp-Session-Id"]
    assert "PAYMENT-SIGNATURE" not in seen[0].headers
    row, = spend_service.list_payments()
    assert row["state"] == "settled"
    assert row["resource"] == URL
    assert row["transaction"] == "0x" + "ab" * 32
    assert spend_service.get_status()["spent_usd"] == int(amount) / 1_000_000


@pytest.mark.parametrize("status", [402, 400, 500])
def test_rejected_or_errored_call_reconciles_without_retry_loop(wallet, status):
    seen = []

    def handle(request):
        seen.append(request)
        return challenge() if len(seen) == 1 else httpx.Response(status, json={"error": "rejected"})

    with client(handle, wallet) as http:
        assert http.get(URL).status_code == status
    assert len(seen) == 2
    assert spend_service.list_payments()[0]["state"] == "authorized"
    assert spend_service.get_status()["spent_usd"] == .001


@pytest.mark.parametrize("outcome", ["missing", "malformed", "disconnect"])
def test_uncertain_outcome_keeps_authorized_budget(wallet, outcome):
    def handle(request):
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        if outcome == "disconnect":
            raise httpx.ReadTimeout("connection lost: token=private", request=request)
        headers = {"payment-response": "not-json"} if outcome == "malformed" else {}
        return httpx.Response(200, json={"ok": True}, headers=headers)

    with client(handle, wallet) as http:
        if outcome == "disconnect":
            with pytest.raises(X402PaymentError) as error:
                http.get(URL)
            assert "token=private" not in str(error.value)
        else:
            assert http.get(URL).status_code == 200
    assert spend_service.list_payments()[0]["state"] == "authorized"
    assert spend_service.get_status()["spent_usd"] == .001


def test_next_attempt_has_fresh_nonce_and_separate_reservation(wallet):
    nonces = []

    def handle(request):
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        nonces.append(payment(request)["authorization"]["nonce"])
        if len(nonces) == 1:
            return httpx.Response(402)
        return httpx.Response(200, headers={"x-payment-response": receipt(wallet)})

    with client(handle, wallet) as http:
        assert http.get(URL).status_code == 402
        assert http.get(URL).status_code == 200
    assert len(set(nonces)) == 2
    assert sorted(row["state"] for row in spend_service.list_payments()) == ["authorized", "settled"]
    assert spend_service.get_status()["spent_usd"] == .002


def test_free_resource_needs_neither_wallet_nor_budget(database, monkeypatch):
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0)
    with client(lambda request: httpx.Response(200, json={"free": True})) as http:
        assert http.get(URL).json() == {"free": True}
    assert spend_service.list_payments() == []


def test_over_budget_never_signs(wallet, monkeypatch):
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", .0005)
    monkeypatch.setattr(wallet_manager, "sign_x402_authorization", lambda **kwargs: pytest.fail("signed"))
    with client(lambda request: challenge(), wallet) as http:
        with pytest.raises(X402SpendCapExceeded):
            http.get(URL)
    assert spend_service.list_payments() == []


@pytest.mark.parametrize("kwargs", [
    {"network": "eip155:8453"}, {"asset": PAYEE}, {"amount": "1000001"},
    {"extra": {"assetTransferMethod": "permit2"}},
])
def test_wrong_network_asset_excess_price_or_struct_cannot_pay(wallet, kwargs):
    with client(lambda request: challenge(**kwargs), wallet) as http:
        with pytest.raises(AgentError):
            http.get(URL)
    assert spend_service.get_status()["spent_usd"] == 0


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key", "payment-signature", "X-PAYMENT"])
def test_auth_and_replayed_signatures_refused_before_network(database, header):
    with client(lambda request: pytest.fail("request escaped")) as http:
        with pytest.raises(ValidationError):
            http.get(URL, headers={header: "secret"})


def test_sdk_environment_key_cannot_escape(wallet, monkeypatch):
    monkeypatch.setenv("MANGROVE_API_KEY", "prod_ambient")
    with create_x402_mangrove_client(
        environment="dev", base_url=ORIGIN + "/api/v1", kb_base_url=ORIGIN + "/kb",
        wallet_address=wallet, transport=httpx.MockTransport(lambda request: pytest.fail("key escaped")),
    ) as sdk:
        with pytest.raises(ValidationError, match="credentials"):
            sdk.signals.list()


def test_real_sdk_injection_timeout_and_identity(wallet, monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_SDK_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr("mangrove_ai._config._maybe_load_dotenv", lambda: pytest.fail("loaded dotenv"))
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.path == "/api/v1/signals/"
        assert request.extensions["timeout"]["read"] == 120
        assert "Authorization" not in request.headers
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        return httpx.Response(200, json={"signals": [], "total": 0},
                              headers={"payment-response": receipt(wallet)})

    with create_x402_mangrove_client(
        environment="dev", base_url=ORIGIN + "/api/v1", kb_base_url=ORIGIN + "/kb",
        wallet_address=wallet, transport=httpx.MockTransport(handle),
    ) as sdk:
        assert sdk.signals.list().total == 0
    assert len(seen) == 2
    assert spend_service.list_payments()[0]["state"] == "settled"


def test_redirect_cannot_spend_at_another_origin(database):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://untrusted.test/pay"})

    with client(handle, follow_redirects=True) as http:
        with pytest.raises(ValidationError, match="origins"):
            http.get(URL)
    assert len(seen) == 1


class RecordingStream(httpx.SyncByteStream):
    closed = False

    def __iter__(self):
        yield b"malformed"

    def close(self):
        self.closed = True


def test_bad_challenge_closes_connection_and_sanitizes_errors(wallet):
    stream = RecordingStream()
    with client(lambda request: httpx.Response(402, stream=stream), wallet) as http:
        with pytest.raises(X402PaymentError) as error:
            http.get(URL + "?secret=do-not-log")
    assert stream.closed
    assert "do-not-log" not in str(error.value)


def test_sync_builder_keeps_network_pin_and_ceiling(wallet):
    sync = x402_payer.build_sync_payment_client(wallet)
    assert set(sync._schemes) == {"eip155:84532"}
    assert not sync._schemes_v1


@pytest.mark.parametrize("origin", ["http://remote.test", "https://user:pass@remote.test", "", "file:///tmp/pay"])
def test_unsafe_origin_is_rejected(origin):
    with pytest.raises(ValidationError):
        X402SyncTransport(allowed_origins=[origin])


def test_close_releases_underlying_transport():
    class Pool(httpx.BaseTransport):
        closed = False

        def close(self):
            self.closed = True

    pool = Pool()
    with httpx.Client(transport=X402SyncTransport(allowed_origins=[ORIGIN], transport=pool)):
        pass
    assert pool.closed


def test_overlapping_requests_do_not_share_reservations(wallet):
    # Re-enter while the first signed request is in flight. A shared signer
    # would reconcile the inner call's reservation with the outer response.
    def handle(request):
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        if request.url.path == "/outer":
            assert http.get(ORIGIN + "/inner").status_code == 200
            return httpx.Response(402)
        return httpx.Response(200, headers={"payment-response": receipt(wallet)})

    with client(handle, wallet) as http:
        assert http.get(ORIGIN + "/outer").status_code == 402
    rows = {row["resource"]: row for row in spend_service.list_payments()}
    assert rows[ORIGIN + "/outer"]["state"] == "authorized"
    assert rows[ORIGIN + "/inner"]["state"] == "settled"
    assert spend_service.get_status()["spent_usd"] == .002


def test_missing_backup_refuses_before_signing_and_closes_challenge(wallet, monkeypatch):
    stream = RecordingStream()

    def refuse(address):
        raise ValidationError("backup required")

    monkeypatch.setattr(wallet_manager, "require_backup_confirmed", refuse)
    monkeypatch.setattr(wallet_manager, "sign_x402_authorization", lambda **kwargs: pytest.fail("signed"))
    with client(lambda request: httpx.Response(402, stream=stream), wallet) as http:
        with pytest.raises(ValidationError, match="backup required"):
            http.get(URL)
    assert stream.closed
    assert spend_service.list_payments() == []


def test_sdk_does_not_automatically_repeat_a_failed_paid_call(wallet):
    from mangrove_ai.exceptions import ServiceUnavailableError

    seen = []

    def handle(request):
        seen.append(request)
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        return httpx.Response(503, json={"error": "unavailable"})

    with create_x402_mangrove_client(
        environment="dev", base_url=ORIGIN + "/api/v1", kb_base_url=ORIGIN + "/kb",
        wallet_address=wallet, transport=httpx.MockTransport(handle),
    ) as sdk:
        with pytest.raises(ServiceUnavailableError):
            sdk.signals.list()
    assert len(seen) == 2
    assert len(spend_service.list_payments()) == 1


@pytest.mark.parametrize("outcome", ["signed_402", "balance_rpc_error", "balance_timeout", "wrong_transfer", "success"])
def test_manual_checker_payment_outcomes(wallet, monkeypatch, capsys, tmp_path, outcome):
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("check_x402_e2e", root / "server/scripts/check_x402_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(root)
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.delenv("MANGROVE_AGENT_HOME", raising=False)
    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    # Keep this CLI test from binding global loggers to pytest's temporary stderr.
    monkeypatch.setattr("src.shared.logging.configure", lambda environment: None)
    monkeypatch.setattr(sys, "argv", ["check", "--case", "hello", "--receiver", PAYEE,
                                     "--pay", "--log", str(tmp_path / "evidence.jsonl")])
    calls = []
    def handle(request):
        if request.url.host == "sepolia.base.org":
            body = json.loads(request.content)
            method = body["method"]
            assert method in {"eth_chainId", "eth_call", "eth_getTransactionReceipt"}
            if method == "eth_getTransactionReceipt":
                return httpx.Response(200, json={"result": {
                    "status": "0x1", "blockNumber": "0x100", "logs": [{
                        "address": USDC, "topics": [module.TRANSFER,
                            "0x" + wallet[2:].lower().rjust(64, "0"),
                            "0x" + PAYEE[2:].lower().rjust(64, "0")],
                        "data": hex(1 if outcome == "wrong_transfer" else 50000),
                    }],
                }})
            if method == "eth_call" and body["params"][1] != "latest":
                if outcome == "balance_rpc_error":
                    return httpx.Response(200, json={"error": {"message": "SYNTHETIC_SECRET"}})
                if outcome == "balance_timeout":
                    raise httpx.ReadTimeout("SYNTHETIC_SECRET", request=request)
            return httpx.Response(200, json={"result": hex(84532) if method == "eth_chainId" else hex(1000000)})
        calls.append(request)
        if "payment-signature" not in request.headers:
            return challenge(amount="50000")
        if outcome != "signed_402":
            return httpx.Response(200, headers={"payment-response": receipt(wallet)}, json={})
        return httpx.Response(402, headers={"payment-response": base64.b64encode(json.dumps({
            "success": False, "errorReason": "insufficient_funds",
        }).encode()).decode()}, json={})
    monkeypatch.setattr(httpx, "HTTPTransport", lambda *a, **kw: httpx.MockTransport(handle))
    real_client = httpx.Client
    def offline_client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handle))
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "Client", offline_client)
    assert module.main() == (1 if outcome in {"signed_402", "wrong_transfer"} else 0)
    output = capsys.readouterr().out
    assert "JSONDecodeError" not in output
    assert "SYNTHETIC_SECRET" not in output
    assert len(calls) == 2
    if outcome == "signed_402":
        assert '"error_code": "insufficient_funds"' in output
        assert '"error_type": "HTTPStatusError"' in output
        assert spend_service.list_payments()[0]["state"] == "authorized"
    elif outcome == "wrong_transfer":
        assert "Receipt does not contain the expected USDC transfer" in output
        assert '"event": "PASS"' not in output
    else:
        assert '"event": "onchain_verified"' in output
        assert '"event": "PASS"' in output
        assert spend_service.list_payments()[0]["state"] == "settled"
        assert ('"event": "balances_after_unavailable"' in output) == (outcome != "success")
    assert spend_service.get_status()["spent_usd"] == .05



def test_signer_persists_public_authorization_identity(wallet):
    seen = []
    def handle(request):
        if "PAYMENT-SIGNATURE" not in request.headers:
            return challenge()
        seen.append(payment(request)["authorization"])
        return httpx.Response(503)
    with client(handle, wallet) as http:
        assert http.get(URL).status_code == 503
    row = spend_service.list_payments()[0]
    signed = seen[0]
    assert row["authorization_nonce"] == signed["nonce"].lower()
    assert row["valid_after"] == int(signed["validAfter"])
    assert row["valid_before"] == int(signed["validBefore"])
    assert row["asset"] == USDC.lower()
    assert row["state"] == "authorized"
    assert "signature" not in row
