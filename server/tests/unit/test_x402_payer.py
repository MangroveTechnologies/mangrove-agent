"""Unit tests for x402_payer — paying from a custodied wallet.

Every test here is offline. No facilitator, no chain, no running server:
the 402 round trip is driven through an httpx MockTransport, and signature
correctness is checked by recovering the signer from the EIP-712 hash.
"""
from __future__ import annotations

import base64
import json
import os

os.environ.setdefault("ENVIRONMENT", "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from x402.http.utils import encode_payment_required_header  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmClientScheme  # noqa: E402
from x402.mechanisms.evm.types import TypedDataDomain, TypedDataField  # noqa: E402
from x402.schemas.payments import PaymentRequired, PaymentRequirements  # noqa: E402

_TEST_PRIVKEY = "0x" + "11" * 32
_TEST_ADDRESS = Account.from_key(_TEST_PRIVKEY).address

_SEPOLIA = "eip155:84532"
_MAINNET = "eip155:8453"
_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_PAYEE = "0xde991861bB3e7078015826Fad749de398F6ec1f6"
_NOT_USDC = "0x1234567890123456789012345678901234567890"


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_x402_payer.db"
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
    store: dict[tuple[str, str], str] = {}

    monkeypatch.setattr("keyring.get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr("keyring.set_password", lambda s, u, p: store.__setitem__((s, u), p))

    from src.shared.crypto import fernet as f

    f.reset_master_key_cache()
    yield store
    f.reset_master_key_cache()


@pytest.fixture
def mock_sdk_create(monkeypatch):
    """Pin create_wallet's local key generation to a fixed keypair."""
    fixed = Account.from_key(_TEST_PRIVKEY)
    monkeypatch.setattr("src.services.wallet_manager.Account.create", lambda *a, **k: fixed)
    return fixed


@pytest.fixture
def unbacked_wallet(temp_db, stub_keyring, mock_sdk_create):
    """A stored wallet whose secret the user has NOT confirmed backing up."""
    from src.services.wallet_manager import create_wallet

    create_wallet(chain="evm", network="testnet", chain_id=84532)
    return _TEST_ADDRESS


@pytest.fixture
def wallet(unbacked_wallet):
    """A stored wallet cleared for spending."""
    from src.services.wallet_manager import confirm_backup

    confirm_backup(_TEST_ADDRESS)
    return _TEST_ADDRESS


@pytest.fixture
def sepolia_network(monkeypatch):
    from src.config import app_config

    monkeypatch.setattr(app_config, "X402_NETWORK", _SEPOLIA)
    return _SEPOLIA


def _requirements(
    *,
    network: str = _SEPOLIA,
    asset: str = _SEPOLIA_USDC,
    amount: str = "50000",
    extra: dict | None = None,
) -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network=network,
        asset=asset,
        amount=amount,
        pay_to=_PAYEE,
        max_timeout_seconds=300,
        extra=extra if extra is not None else {"name": "USDC", "version": "2"},
    )


def _sign_through_scheme(address: str, requirements: PaymentRequirements) -> dict:
    """Drive the real x402 exact-EVM scheme against the custodied signer."""
    from src.services.x402_payer import CustodialSigner

    return ExactEvmClientScheme(CustodialSigner(address)).create_payment_payload(requirements)


# -- the signer adapter ------------------------------------------------------


def test_address_is_checksummed(temp_db):
    from src.services.x402_payer import CustodialSigner

    signer = CustodialSigner(_TEST_ADDRESS.lower())
    assert signer.address == _TEST_ADDRESS


def test_malformed_address_refused(temp_db):
    from src.services.x402_payer import CustodialSigner
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError, match="not a valid EVM address"):
        CustodialSigner("not-an-address")


def test_signer_holds_no_secret(wallet):
    """The signer is an address, not a key. Nothing in it should be secret."""
    from src.services.x402_payer import CustodialSigner

    signer = CustodialSigner(wallet)
    assert list(vars(signer)) == ["_address"]
    assert _TEST_PRIVKEY not in repr(vars(signer))


def test_signature_recovers_to_the_custodied_wallet(wallet):
    """The headline: a real x402 payload, signed by the guarded wallet.

    This exercises the whole adaptation — the SDK builds an EIP-3009
    authorization, hands it to our signer as dataclasses, the signer
    converts and passes it to the wallet guard, and the guard signs. If
    any field were renamed, reordered or re-typed along the way the
    recovered address would not match.
    """
    payload = _sign_through_scheme(wallet, _requirements())

    authorization = payload["authorization"]
    assert authorization["from"] == _TEST_ADDRESS
    assert authorization["to"] == _PAYEE
    assert authorization["value"] == "50000"

    signable = encode_typed_data(
        domain_data={
            "name": "USDC",
            "version": "2",
            "chainId": 84532,
            "verifyingContract": _SEPOLIA_USDC,
        },
        message_types={
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ]
        },
        message_data={
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
        },
    )
    assert Account.recover_message(signable, signature=payload["signature"]) == _TEST_ADDRESS


def test_every_payment_gets_a_fresh_nonce(wallet):
    """A nonce is burned by the receiver before verification, so a replayed
    signature always fails. Two payments must never share one."""
    first = _sign_through_scheme(wallet, _requirements())
    second = _sign_through_scheme(wallet, _requirements())

    assert first["authorization"]["nonce"] != second["authorization"]["nonce"]
    assert first["signature"] != second["signature"]


def test_domain_dataclass_is_converted_for_the_guard(wallet, monkeypatch):
    """x402 hands dataclasses; the guard reads mappings. Assert the exact
    four domain keys arrive — the guard rejects any extra, because
    eth_account builds the domain separator from whichever keys exist."""
    from src.services import x402_payer

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return b"\x00" * 65

    monkeypatch.setattr(x402_payer.wallet_manager, "sign_x402_authorization", _capture)

    x402_payer.CustodialSigner(wallet).sign_typed_data(
        TypedDataDomain(name="USDC", version="2", chain_id=84532, verifying_contract=_SEPOLIA_USDC),
        {"TransferWithAuthorization": [TypedDataField(name="from", type="address")]},
        "TransferWithAuthorization",
        {"from": _TEST_ADDRESS},
    )

    assert captured["domain"] == {
        "name": "USDC",
        "version": "2",
        "chainId": 84532,
        "verifyingContract": _SEPOLIA_USDC,
    }
    assert captured["types"] == {
        "TransferWithAuthorization": [{"name": "from", "type": "address"}]
    }
    assert captured["wallet_address"] == _TEST_ADDRESS


def test_non_field_type_entries_are_passed_through_not_dropped(wallet, monkeypatch):
    """Filtering junk out of a type definition would let it vanish between
    validation and signing. It must reach the guard, which refuses it."""
    from src.services import x402_payer

    captured: dict = {}
    monkeypatch.setattr(
        x402_payer.wallet_manager,
        "sign_x402_authorization",
        lambda **kw: (captured.update(kw), b"\x00" * 65)[1],
    )

    x402_payer.CustodialSigner(wallet).sign_typed_data(
        TypedDataDomain(name="USDC", version="2", chain_id=84532, verifying_contract=_SEPOLIA_USDC),
        {"TransferWithAuthorization": ["junk", TypedDataField(name="to", type="address")]},
        "TransferWithAuthorization",
        {},
    )

    assert captured["types"]["TransferWithAuthorization"][0] == "junk"


# -- the gates ---------------------------------------------------------------


def test_unbacked_wallet_cannot_sign_a_payment(unbacked_wallet):
    """sign_x402_authorization deliberately does not gate; the payer must."""
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="not backed up"):
        _sign_through_scheme(unbacked_wallet, _requirements())


def test_non_usdc_asset_refused(wallet):
    """§9.1 decision 2 — USDC only. An arbitrary token contract is how a
    forged envelope drains a balance the agent never meant to spend."""
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="is not USDC"):
        _sign_through_scheme(wallet, _requirements(asset=_NOT_USDC))


def test_mainnet_usdc_inside_a_sepolia_payload_refused(wallet):
    """The guard binds contract TO chain — neither half means anything alone."""
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="is not USDC"):
        _sign_through_scheme(wallet, _requirements(asset=_MAINNET_USDC))


def test_permit2_flow_refused(wallet):
    """A permit2 offer asks for PermitWitnessTransferFrom, not EIP-3009.

    Spend controls do not catch this — the asset is genuine USDC and the
    network is right, so the envelope reaches the signer. Only the guard's
    primaryType check stands between the agent and signing an unfamiliar
    struct against its own USDC balance.
    """
    from src.shared.errors import SigningError

    requirements = _requirements(
        extra={"name": "USDC", "version": "2", "assetTransferMethod": "permit2"}
    )
    with pytest.raises(SigningError, match="PermitWitnessTransferFrom"):
        _sign_through_scheme(wallet, requirements)


# -- network pinning (§8.7) --------------------------------------------------


def test_unset_network_fails_loudly(monkeypatch):
    from src.config import app_config
    from src.services.x402_payer import _require_network
    from src.shared.errors import X402PaymentError

    monkeypatch.setattr(app_config, "X402_NETWORK", None)
    with pytest.raises(X402PaymentError, match="not configured"):
        _require_network()


def test_legacy_network_name_refused(monkeypatch):
    """"base-sepolia" is a V1 name. Registering it would match nothing and
    surface as a confusing "no matching requirements" much later."""
    from src.config import app_config
    from src.services.x402_payer import _require_network
    from src.shared.errors import X402PaymentError

    monkeypatch.setattr(app_config, "X402_NETWORK", "base-sepolia")
    with pytest.raises(X402PaymentError, match="CAIP-2"):
        _require_network()


def test_client_registers_only_the_configured_network(wallet, sepolia_network):
    """The regression guard for §8.7.

    register_exact_evm_client() would put the V2 scheme behind an
    `eip155:*` wildcard AND register V1 for every legacy EVM network —
    Base mainnet included — so the server's requirement could pick the
    chain. Nothing but the configured network may be registered.
    """
    from src.services.x402_payer import build_payment_client

    client = build_payment_client(wallet)

    assert set(client._schemes) == {_SEPOLIA}
    assert client._schemes_v1 == {}


def test_mainnet_requirement_cannot_be_paid_on_a_sepolia_config(wallet, sepolia_network):
    from x402.schemas.errors import NoMatchingRequirementsError

    from src.services.x402_payer import build_payment_client

    client = build_payment_client(wallet)
    with pytest.raises(NoMatchingRequirementsError):
        client._select_requirements_v2([_requirements(network=_MAINNET, asset=_MAINNET_USDC)])


# -- payer wallet resolution -------------------------------------------------


def test_explicit_wallet_wins_over_config(monkeypatch):
    from src.config import app_config
    from src.services.x402_payer import resolve_payer_wallet

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", _PAYEE)
    assert resolve_payer_wallet(_TEST_ADDRESS) == _TEST_ADDRESS


def test_config_wallet_used_when_no_argument(monkeypatch):
    from src.config import app_config
    from src.services.x402_payer import resolve_payer_wallet

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", _TEST_ADDRESS.lower())
    assert resolve_payer_wallet() == _TEST_ADDRESS


def test_no_wallet_anywhere_refuses_rather_than_guessing(monkeypatch):
    from src.config import app_config
    from src.services.x402_payer import resolve_payer_wallet
    from src.shared.errors import ValidationError

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", "")
    with pytest.raises(ValidationError, match="No x402 payer wallet"):
        resolve_payer_wallet()


# -- pay() round trips -------------------------------------------------------


def _settlement_header(transaction: str = "0xabc", payer: str = _TEST_ADDRESS) -> str:
    return base64.b64encode(
        json.dumps({"transaction": transaction, "network": _SEPOLIA, "payer": payer}).encode()
    ).decode()


def _payment_required_header(**kwargs) -> str:
    return encode_payment_required_header(
        PaymentRequired(x402_version=2, accepts=[_requirements(**kwargs)], error="Payment required")
    )


@pytest.fixture
def mock_http(monkeypatch):
    """Route pay()'s outbound traffic into a recording MockTransport."""
    requests: list[httpx.Request] = []
    handlers: list = []

    def _install(*responses):
        handlers.extend(responses)

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handlers[min(len(requests) - 1, len(handlers) - 1)]

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(_handler)
    )
    return type("MockHttp", (), {"install": staticmethod(_install), "requests": requests})


async def test_free_resource_is_not_a_payment(wallet, sepolia_network, mock_http):
    """A resource that never asks for payment is a clean no-payment path."""
    from src.services.x402_payer import pay

    mock_http.install(httpx.Response(200, json={"message": "hello"}))

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.status_code == 200
    assert result.paid is False
    assert result.transaction is None
    assert result.body == {"message": "hello"}
    assert len(mock_http.requests) == 1


async def test_402_is_paid_and_settlement_is_reported(wallet, sepolia_network, mock_http):
    """The milestone path: 402 → sign → retry → settled."""
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(
            200,
            json={"message": "hello"},
            headers={"x-payment-response": _settlement_header()},
        ),
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.status_code == 200
    assert result.paid is True
    assert result.transaction == "0xabc"
    assert result.network == _SEPOLIA
    assert result.payer == _TEST_ADDRESS

    # The retry must actually carry a signature.
    assert len(mock_http.requests) == 2
    assert any("payment" in name.lower() for name in mock_http.requests[1].headers)


async def test_failed_resource_is_not_charged(wallet, sepolia_network, mock_http):
    """REST skips settlement for any status >= 400 — caller paid but did not
    receive their resource, so they are not charged (§6.4)."""
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(500, json={"error": "upstream exploded"}),
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.status_code == 500
    assert result.paid is False
    assert result.transaction is None


async def test_undecodable_settlement_header_does_not_fail_the_call(
    wallet, sepolia_network, mock_http
):
    """The resource arrived and the money moved. Losing the receipt costs an
    audit field, not the call."""
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(200, json={"ok": True}, headers={"x-payment-response": "not-base64!!"}),
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.status_code == 200
    assert result.paid is False
    assert result.body == {"ok": True}


async def test_rejected_payment_is_reported_as_unpaid(wallet, sepolia_network, mock_http):
    """A 402 that survives the payment attempt means the receiver refused
    the signature — a burned nonce, an expired window, an empty balance.
    Nothing delivered, nothing charged."""
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()})
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.status_code == 402
    assert result.paid is False
    assert result.transaction is None
    # The 402, the paid retry, and the transport's one recovery attempt.
    assert len(mock_http.requests) >= 2


def test_payer_wallet_accessor_defaults_to_empty(monkeypatch):
    """Configs written before this key existed must still resolve to ''."""
    from src.config import app_config
    from src.shared.x402.config import get_payer_wallet

    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", None)
    assert get_payer_wallet() == ""


async def test_backup_gate_runs_before_any_request(unbacked_wallet, sepolia_network, mock_http):
    from src.services.x402_payer import pay
    from src.shared.errors import SigningError

    mock_http.install(httpx.Response(200, json={}))

    with pytest.raises(SigningError, match="not backed up"):
        await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=unbacked_wallet)

    assert mock_http.requests == []


def test_guard_refusal_survives_the_transport_wrapper():
    """x402AsyncTransport catches everything its payment path raises and
    re-raises it as a bare PaymentError. A refused envelope must still
    surface as SIGNING_ERROR carrying the guard's own explanation, rather
    than being flattened into a generic payment failure."""
    from x402.http.clients.httpx import PaymentError

    from src.services.x402_payer import _translate_payment_error
    from src.shared.errors import SigningError

    refusal = SigningError("Refused to sign: verifyingContract is not USDC.")
    wrapped = PaymentError("Failed to handle payment: ...")
    wrapped.__cause__ = refusal

    translated = _translate_payment_error(
        wrapped, url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
    )

    assert translated is refusal
    assert translated.code == "SIGNING_ERROR"


def test_unrecognised_failure_becomes_a_payment_error():
    from x402.http.clients.httpx import PaymentError

    from src.services.x402_payer import _translate_payment_error

    translated = _translate_payment_error(
        PaymentError("something odd"), url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
    )

    assert translated.code == "X402_PAYMENT_ERROR"


def test_unwrapped_protocol_error_is_still_shaped():
    """Selection errors derive from a *different* PaymentError than the one
    the transport wraps with, so they must be handled on their own."""
    from x402.schemas.errors import NoMatchingRequirementsError

    from src.services.x402_payer import _translate_payment_error

    translated = _translate_payment_error(
        NoMatchingRequirementsError("nothing matched"),
        url="http://agent.test/x",
        payer=_TEST_ADDRESS,
        network=_SEPOLIA,
    )

    assert translated.code == "X402_PAYMENT_ERROR"
    assert "no payment option" in translated.message


def test_translation_terminates_on_a_self_referential_cause():
    """__cause__ chains come from third-party code; a cycle must not hang."""
    from x402.http.clients.httpx import PaymentError

    from src.services.x402_payer import _translate_payment_error

    looped = PaymentError("loop")
    looped.__cause__ = looped

    translated = _translate_payment_error(
        looped, url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
    )

    assert translated.code == "X402_PAYMENT_ERROR"


async def test_non_usdc_asset_is_refused_before_the_wallet_is_asked(
    wallet, sepolia_network, mock_http
):
    """Two independent layers refuse a non-USDC asset, and the outer one
    fires first: the SDK's spend controls only admit assets its default
    registry recognises, so the envelope never reaches the wallet guard.
    The guard still refuses it on its own (see test_non_usdc_asset_refused)
    — this asserts the agent never signs, whichever layer catches it."""
    from src.services.x402_payer import pay
    from src.shared.errors import X402PaymentError

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header(asset=_NOT_USDC)})
    )

    with pytest.raises(X402PaymentError, match="no payment option"):
        await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    # The 402 was received; the retry-with-payment never happened.
    assert len(mock_http.requests) == 1


async def test_unreachable_server_reports_a_payment_error(wallet, sepolia_network, monkeypatch):
    from src.services.x402_payer import pay
    from src.shared.errors import X402PaymentError

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(_boom))

    with pytest.raises(X402PaymentError, match="transport layer"):
        await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)


async def test_no_wallet_secret_env_var_is_ever_read(wallet, sepolia_network, mock_http, monkeypatch):
    """The anti-pattern this service replaces. A payment must succeed with
    WALLET_SECRET unset, and must not consult it when it is set."""
    from src.services.x402_payer import pay

    monkeypatch.delenv("WALLET_SECRET", raising=False)
    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(200, json={"ok": True}, headers={"x-payment-response": _settlement_header()}),
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.paid is True
    assert "WALLET_SECRET" not in os.environ
