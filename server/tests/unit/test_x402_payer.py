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


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["Authorization", "authorization", "X-API-Key", "x-api-key", "PAYMENT-SIGNATURE", "X-PAYMENT"])
async def test_async_payer_rejects_auth_or_replay_before_wallet_access(monkeypatch, header):
    from src.services import x402_payer
    from src.shared.errors import ValidationError

    monkeypatch.setattr(x402_payer, "resolve_payer_wallet", lambda *a: pytest.fail("wallet accessed"))
    with pytest.raises(ValidationError, match="credentials"):
        await x402_payer.pay("https://payments.test/resource", headers={header: ""})


@pytest.mark.asyncio
async def test_async_payer_rejects_url_credentials_before_wallet_access(monkeypatch):
    from src.services import x402_payer
    from src.shared.errors import ValidationError

    monkeypatch.setattr(x402_payer, "resolve_payer_wallet", lambda *a: pytest.fail("wallet accessed"))
    with pytest.raises(ValidationError, match="credentials"):
        await x402_payer.pay("https://user:secret@payments.test/resource")


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
    # An address, an audit label, and its budget reservations -- no key
    # material, and nothing derived from any.
    assert sorted(vars(signer)) == ["_address", "_reservations", "_resource"]
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
        # `value` and `to` are read by the spend cap before signing, so even
        # a cut-down message has to carry them.
        {"from": _TEST_ADDRESS, "to": _PAYEE, "value": 50000},
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
        {"from": _TEST_ADDRESS, "to": _PAYEE, "value": 50000},
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
    from src.services.x402_payer import build_payment_client
    from x402.schemas.errors import NoMatchingRequirementsError

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


def _settlement_header(transaction: str = "0x" + "ab" * 32, payer: str = _TEST_ADDRESS) -> str:
    return base64.b64encode(
        json.dumps({"success": True, "transaction": transaction, "network": _SEPOLIA, "payer": payer}).encode()
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
    assert result.transaction == "0x" + "ab" * 32
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
    from src.services.x402_payer import _translate_payment_error
    from src.shared.errors import SigningError
    from x402.http.clients.httpx import PaymentError

    refusal = SigningError("Refused to sign: verifyingContract is not USDC.")
    wrapped = PaymentError("Failed to handle payment: ...")
    wrapped.__cause__ = refusal

    translated = _translate_payment_error(
        wrapped, safe_url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
    )

    assert translated is refusal
    assert translated.code == "SIGNING_ERROR"


def test_unrecognised_failure_becomes_a_payment_error():
    from src.services.x402_payer import _translate_payment_error
    from x402.http.clients.httpx import PaymentError

    translated = _translate_payment_error(
        PaymentError("something odd"), safe_url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
    )

    assert translated.code == "X402_PAYMENT_ERROR"


def test_unwrapped_protocol_error_is_still_shaped():
    """Selection errors derive from a *different* PaymentError than the one
    the transport wraps with, so they must be handled on their own."""
    from src.services.x402_payer import _translate_payment_error
    from x402.schemas.errors import NoMatchingRequirementsError

    translated = _translate_payment_error(
        NoMatchingRequirementsError("nothing matched"),
        safe_url="http://agent.test/x",
        payer=_TEST_ADDRESS,
        network=_SEPOLIA,
    )

    assert translated.code == "X402_PAYMENT_ERROR"
    assert "no payment option" in translated.message


def test_translation_terminates_on_a_self_referential_cause():
    """__cause__ chains come from third-party code; a cycle must not hang."""
    from src.services.x402_payer import _translate_payment_error
    from x402.http.clients.httpx import PaymentError

    looped = PaymentError("loop")
    looped.__cause__ = looped

    translated = _translate_payment_error(
        looped, safe_url="http://agent.test/x", payer=_TEST_ADDRESS, network=_SEPOLIA
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

    with pytest.raises(X402PaymentError, match="interrupted"):
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


# -- the spend budget at the signing waist -----------------------------------


_EIP3009_FIELDS = [
    TypedDataField(name="from", type="address"),
    TypedDataField(name="to", type="address"),
    TypedDataField(name="value", type="uint256"),
    TypedDataField(name="validAfter", type="uint256"),
    TypedDataField(name="validBefore", type="uint256"),
    TypedDataField(name="nonce", type="bytes32"),
]


def _authorization(value: int = 50000) -> dict:
    return {
        "from": _TEST_ADDRESS,
        "to": _PAYEE,
        "value": value,
        "validAfter": 0,
        "validBefore": 2**32,
        "nonce": b"\x01" * 32,
    }


def test_budgeted_struct_matches_what_the_guard_will_sign():
    """A drift guard between two modules that must not disagree.

    The spend cap prices `TransferWithAuthorization` and declines to price
    anything else, on the reasoning that the guard refuses everything else
    anyway. If the guard is ever widened to a second struct, that reasoning
    stops holding and the new struct would be signed with no budget check.
    This fails first.
    """
    from src.services import wallet_manager, x402_payer

    assert x402_payer._EIP3009_PRIMARY_TYPE == wallet_manager._X402_PRIMARY_TYPE


def test_signing_writes_a_ledger_row(wallet):
    """Every signature appears on the ledger, with the chain it named."""
    from src.services import spend_service
    from src.services.x402_payer import CustodialSigner

    signer = CustodialSigner(wallet, resource="http://agent.test/api/x402/hello-mangrove?k=v")
    ExactEvmClientScheme(signer).create_payment_payload(_requirements())

    row = spend_service.list_payments()[0]
    assert row["state"] == "authorized"
    assert row["amount_usd"] == 0.05
    assert row["wallet_address"] == _TEST_ADDRESS
    assert row["payee"] == _PAYEE
    # Read off the struct being signed, not copied from X402_NETWORK.
    assert row["network"] == _SEPOLIA
    assert row["resource"] == "http://agent.test/api/x402/hello-mangrove"
    assert signer.reservations == (row["id"],)
    assert spend_service.get_status()["spent_usd"] == 0.05


def test_over_budget_payment_is_never_signed(wallet, monkeypatch):
    """The gate has to fire BEFORE the key is touched, not after."""
    from src.config import app_config
    from src.services import spend_service, x402_payer
    from src.shared.errors import X402SpendCapExceeded

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0.01, raising=False)
    calls: list = []
    monkeypatch.setattr(
        x402_payer.wallet_manager,
        "sign_x402_authorization",
        lambda **kw: calls.append(kw),
    )

    signer = x402_payer.CustodialSigner(wallet)
    with pytest.raises(X402SpendCapExceeded, match="past the"):
        ExactEvmClientScheme(signer).create_payment_payload(_requirements())

    assert calls == []
    assert signer.reservations == ()
    assert spend_service.get_status()["spent_usd"] == 0.0


def test_guard_refusal_releases_the_reservation(wallet):
    """A refused envelope must not quietly eat part of the budget.

    No signature exists, so no debit can ever be presented — the one case
    where handing budget back is provably free.
    """
    from src.services import spend_service
    from src.services.x402_payer import CustodialSigner
    from src.shared.errors import SigningError

    signer = CustodialSigner(wallet)
    with pytest.raises(SigningError, match="not USDC"):
        signer.sign_typed_data(
            TypedDataDomain(name="USDC", version="2", chain_id=84532, verifying_contract=_NOT_USDC),
            {"TransferWithAuthorization": _EIP3009_FIELDS},
            "TransferWithAuthorization",
            _authorization(),
        )

    assert signer.reservations == ()
    assert spend_service.get_status()["spent_usd"] == 0.0
    assert spend_service.list_payments()[0]["release_reason"] == "signature_refused"


def test_unpriceable_struct_is_left_to_the_guard(wallet):
    """A struct the cap cannot price must still be refused BY THE GUARD.

    Refusing it here on budget grounds would move the decision about which
    structs may be signed out of the signing guard — the security property
    that stopped the permit2 flow — and into a budget check.
    """
    from src.services import spend_service
    from src.services.x402_payer import CustodialSigner
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="primaryType"):
        CustodialSigner(wallet).sign_typed_data(
            TypedDataDomain(name="USDC", version="2", chain_id=84532, verifying_contract=_SEPOLIA_USDC),
            {"PermitWitnessTransferFrom": [TypedDataField(name="spender", type="address")]},
            "PermitWitnessTransferFrom",
            {"spender": _PAYEE},
        )

    assert spend_service.list_payments() == []


async def test_spent_budget_refuses_before_any_request(wallet, sepolia_network, mock_http):
    """Verification case 6, end to end: budget spent, no paid call goes out."""
    from src.config import app_config
    from src.services import spend_service
    from src.services.x402_payer import pay
    from src.shared.errors import X402SpendCapExceeded

    monkeypatch_cap = getattr(app_config, "X402_SPEND_CAP_USD", None)
    assert monkeypatch_cap is not None
    spend_service.reserve(value=int(monkeypatch_cap * 1_000_000), wallet_address=wallet)
    mock_http.install(httpx.Response(200, json={}))

    with pytest.raises(X402SpendCapExceeded, match="needs authorizing again"):
        await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert mock_http.requests == []

    # The user authorizes more; payment resumes.
    spend_service.reset()
    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)
    assert result.status_code == 200


async def test_settled_payment_is_recorded_with_its_transaction(
    wallet, sepolia_network, mock_http
):
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(200, json={"ok": True},
                       headers={"x-payment-response": _settlement_header()}),
    )

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    row = spend_service.list_payments()[0]
    assert row["state"] == "settled"
    assert row["transaction"] == "0x" + "ab" * 32
    assert spend_service.get_status()["spent_usd"] == 0.05


async def test_errored_resource_retains_the_budget(wallet, sepolia_network, mock_http):
    """An HTTP error cannot cancel a disclosed authorization."""
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(500, json={"error": "upstream exploded"}),
    )

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["release_reason"] is None


async def test_rejected_payment_retains_the_budget(wallet, sepolia_network, mock_http):
    """An HTTP error cannot cancel a disclosed authorization."""
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()})
    )

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert {p["state"] for p in spend_service.list_payments()} == {"authorized"}


async def test_missing_receipt_still_counts_against_the_budget(
    wallet, sepolia_network, mock_http
):
    """The ambiguous case, and the reason the ledger counts authorizations.

    The agent signed and the resource came back; the only thing missing is
    an optional header the receiver may simply not send. Releasing on that
    would let a counterparty decide how much of the budget it had used.
    """
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(200, json={"ok": True}),
    )

    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert result.paid is False
    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["state"] == "authorized"


async def test_free_resource_leaves_no_ledger_row(wallet, sepolia_network, mock_http):
    """Nothing was signed, so there is nothing to account for."""
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(httpx.Response(200, json={"message": "hello"}))

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert spend_service.list_payments() == []
    assert spend_service.get_status()["spent_usd"] == 0.0


async def test_cap_refusal_survives_the_transport_wrapper(wallet, sepolia_network, mock_http, monkeypatch):
    """The refusal happens INSIDE the transport's payment loop, which
    flattens everything it catches into a bare PaymentError. A budget
    refusal must reach the caller as X402_SPEND_CAP_EXCEEDED, not as a
    generic "payment failed" — the same defect class already fixed for the
    signing guard."""
    from src.config import app_config
    from src.services import spend_service
    from src.services.x402_payer import pay
    from src.shared.errors import X402SpendCapExceeded

    # Small enough that the $0.05 resource does not fit, but not zero — so
    # the pre-flight check in pay() passes and the refusal has to come from
    # the signer, below the transport.
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0.01, raising=False)
    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()})
    )

    with pytest.raises(X402SpendCapExceeded) as excinfo:
        await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert excinfo.value.code == "X402_SPEND_CAP_EXCEEDED"
    # The 402 was received; the paid retry never went out.
    assert len(mock_http.requests) == 1
    assert spend_service.get_status()["spent_usd"] == 0.0


def test_absurd_amounts_are_refused_before_the_ledger(wallet):
    """uint256 admits numbers SQLite cannot store. They must be refused, not
    handed to an insert that raises OverflowError."""
    from src.services import spend_service
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError, match="larger than any real USDC amount"):
        spend_service.reserve(value=2**255, wallet_address=wallet)
    assert spend_service.list_payments() == []


@pytest.mark.parametrize(
    "responses,expected_state",
    [
        pytest.param("settled", "settled", id="settled"),
        pytest.param("rejected", "authorized", id="rejected-402"),
        pytest.param("errored", "authorized", id="resource-500"),
    ],
)
async def test_pay_reconciles_without_releasing_uncertain_payments(
    wallet, sepolia_network, mock_http, responses, expected_state
):
    """Settlement is recorded; uncertain signatures remain authorized."""
    from src.services import spend_service
    from src.services.x402_payer import pay

    required = httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()})
    second = {
        "settled": httpx.Response(200, json={"ok": True},
                                  headers={"x-payment-response": _settlement_header()}),
        "rejected": required,
        "errored": httpx.Response(500, json={"error": "boom"}),
    }[responses]
    mock_http.install(required, second)

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    ledger = spend_service.list_payments()
    assert [p["state"] for p in ledger] == [expected_state]
    # Fresh uncertain authorizations have not yet expired.
    assert spend_service.get_status()["unreconciled_count"] == 0


async def test_a_settled_payment_records_its_transaction(wallet, sepolia_network, mock_http):
    """The audit trail's whole purpose: tying a ledger row to money on chain."""
    from src.services import spend_service
    from src.services.x402_payer import pay

    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header()}),
        httpx.Response(200, json={"ok": True},
                       headers={"x-payment-response": _settlement_header(transaction="0x" + "fe" * 32)}),
    )

    await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)

    assert spend_service.list_payments()[0]["transaction"] == "0x" + "fe" * 32


async def test_no_path_out_of_pay_leaks_a_query_string(wallet, sepolia_network, monkeypatch, mock_http):
    """Sanitising some paths and not others is not sanitising.

    A URL reaches the user through at least four exits: the ledger, log
    lines, the exception message, and third-party error text that
    interpolates the request URL. This walks the failure exits and asserts
    the secret survives none of them.
    """
    from src.services.x402_payer import pay
    from src.shared.errors import AgentError

    leaky = "http://agent.test/api/x402/hello-mangrove?api_key=SUPERSECRET"

    # 1. transport failure -- httpx errors embed the request URL
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {leaky}", request=request)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **k: httpx.MockTransport(_boom))
    with pytest.raises(AgentError) as transport_err:
        await pay(leaky, wallet_address=wallet)
    assert "SUPERSECRET" not in transport_err.value.message
    assert "SUPERSECRET" not in str(transport_err.value.suggestion or "")

    # 2. no payment option the agent can satisfy -- message names the resource
    mock_http.install(
        httpx.Response(402, headers={"PAYMENT-REQUIRED": _payment_required_header(asset=_NOT_USDC)})
    )
    with pytest.raises(AgentError) as protocol_err:
        await pay(leaky, wallet_address=wallet)
    assert "SUPERSECRET" not in protocol_err.value.message


def test_error_text_from_a_library_is_scrubbed(wallet):
    """The library decides how it formats the URL; we only require that the
    raw form does not survive into anything a human reads."""
    from src.services.x402_payer import _safe_error

    leaky = "https://api.test/v1/x?token=SUPERSECRET"
    scrubbed = _safe_error(RuntimeError(f"timed out calling {leaky} after 30s"), leaky)

    assert "SUPERSECRET" not in scrubbed
    assert "https://api.test/v1/x" in scrubbed
    assert "after 30s" in scrubbed


# Drive the real MCP payment SDK with a fake session and real custody/ledger.
class _McpPaymentSession:
    def __init__(self, *, receipt=None, requirements=None, failure=None, is_error=False, free=False):
        self.receipt = receipt
        self.requirements = requirements or _requirements()
        self.failure = failure
        self.is_error = is_error
        self.free = free
        self.calls = []
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    async def call_tool(self, *, name, arguments, meta=None):
        from mcp.types import CallToolResult, TextContent
        from x402.mcp import MCP_PAYMENT_RESPONSE_META_KEY

        self.calls.append((name, arguments, meta))
        if meta is None and not self.free:
            return CallToolResult(content=[], isError=True, structuredContent=PaymentRequired(
                x402_version=2, accepts=[self.requirements],
            ).model_dump(by_alias=True))
        if self.failure:
            raise self.failure
        return CallToolResult(
            content=[TextContent(type="text", text='{"message":"hello"}')], isError=self.is_error,
            _meta={MCP_PAYMENT_RESPONSE_META_KEY: self.receipt} if self.receipt is not None else None,
        )


def _mcp_receipt(**overrides):
    return {"success": True, "transaction": "0x" + "ab" * 32,
            "payer": _TEST_ADDRESS, "network": _SEPOLIA, **overrides}


@pytest.mark.parametrize("is_error", [False, True])
async def test_mcp_valid_receipt_records_payment_even_when_resource_fails(wallet, sepolia_network, is_error):
    from src.services import spend_service, x402_payer
    from x402.mcp import MCP_PAYMENT_META_KEY

    session = _McpPaymentSession(receipt=_mcp_receipt(), is_error=is_error)
    result = await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert result.paid
    assert result.status_code == (502 if is_error else 200)
    assert session.initialized and len(session.calls) == 2
    payload = session.calls[1][2][MCP_PAYMENT_META_KEY]
    assert payload["payload"]["authorization"]["from"] == wallet
    row = spend_service.list_payments()[0]
    assert row["state"] == "settled"
    assert row["transaction"] == result.transaction
    assert spend_service.get_status()["spent_usd"] == 0.05


@pytest.mark.parametrize("receipt", [None, {}, _mcp_receipt(success=False),
    _mcp_receipt(success="true"), _mcp_receipt(success=1), _mcp_receipt(transaction="bad"),
    _mcp_receipt(payer="0x" + "22" * 20), _mcp_receipt(network=_MAINNET)])
async def test_mcp_unconfirmed_receipt_keeps_signed_budget(wallet, sepolia_network, receipt):
    from src.services import spend_service, x402_payer

    session = _McpPaymentSession(receipt=receipt)
    result = await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert not result.paid
    assert result.transaction is None
    assert spend_service.list_payments()[0]["state"] == "authorized"
    assert spend_service.get_status()["spent_usd"] == 0.05
    assert len(session.calls) == 2


@pytest.mark.parametrize("failure", [TimeoutError("SYNTHETIC_SECRET"), RuntimeError("SYNTHETIC_SECRET")])
async def test_mcp_interrupted_paid_retry_retains_budget_and_sanitizes(wallet, sepolia_network, failure):
    from src.services import spend_service, x402_payer
    from src.shared.errors import X402PaymentError

    session = _McpPaymentSession(failure=failure)
    with pytest.raises(X402PaymentError) as caught:
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert spend_service.list_payments()[0]["state"] == "authorized"
    assert len(session.calls) == 2


async def test_mcp_cancellation_retains_budget(wallet, sepolia_network):
    import asyncio

    from src.services import spend_service, x402_payer

    session = _McpPaymentSession(failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert spend_service.list_payments()[0]["state"] == "authorized"


async def test_mcp_backup_gate_before_initialization(unbacked_wallet, sepolia_network):
    from src.services import spend_service, x402_payer
    from src.shared.errors import AgentError

    session = _McpPaymentSession()
    with pytest.raises(AgentError):
        await x402_payer.pay_mcp(session, wallet_address=unbacked_wallet, resource="http://localhost:9080/mcp/")
    assert not session.initialized
    assert not session.calls
    assert not spend_service.list_payments()


async def test_mcp_exhausted_cap_before_initialization(wallet, sepolia_network, monkeypatch):
    from src.config import app_config
    from src.services import spend_service, x402_payer
    from src.shared.errors import X402SpendCapExceeded

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0)
    session = _McpPaymentSession()
    with pytest.raises(X402SpendCapExceeded):
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert not session.initialized
    assert not spend_service.list_payments()


@pytest.mark.parametrize("requirements", [_requirements(network=_MAINNET, asset=_MAINNET_USDC),
    _requirements(asset=_NOT_USDC), _requirements(amount="1000001")])
async def test_mcp_unsafe_challenge_never_signs(wallet, sepolia_network, requirements, monkeypatch):
    from src.services import spend_service, wallet_manager, x402_payer
    from src.shared.errors import AgentError

    monkeypatch.setattr(wallet_manager, "sign_x402_authorization", lambda **kw: pytest.fail("must not sign"))
    session = _McpPaymentSession(requirements=requirements)
    with pytest.raises(AgentError):
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert len(session.calls) == 1
    assert not spend_service.list_payments()


async def test_mcp_free_response_cannot_claim_a_payment(wallet, sepolia_network):
    from src.services import spend_service, x402_payer

    session = _McpPaymentSession(receipt=_mcp_receipt(), free=True)
    result = await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    assert result.status_code == 200 and not result.paid
    assert not spend_service.list_payments()


async def test_mcp_each_attempt_has_fresh_nonce(wallet, sepolia_network):
    from src.services import spend_service, x402_payer
    from x402.mcp import MCP_PAYMENT_META_KEY

    sessions = [_McpPaymentSession(), _McpPaymentSession()]
    for session in sessions:
        await x402_payer.pay_mcp(session, wallet_address=wallet, resource="http://localhost:9080/mcp/")
    nonces = [s.calls[1][2][MCP_PAYMENT_META_KEY]["payload"]["authorization"]["nonce"] for s in sessions]
    assert nonces[0] != nonces[1]
    assert spend_service.get_status()["spent_usd"] == 0.1


async def test_rest_demo_payer_ignores_proxy_environment_and_redirects(wallet, sepolia_network, mock_http, monkeypatch):
    from src.services.x402_payer import pay

    monkeypatch.setenv("HTTP_PROXY", "http://unreachable.invalid:1234")
    monkeypatch.setenv("ALL_PROXY", "http://unreachable.invalid:1234")
    monkeypatch.setenv("NO_PROXY", "")
    mock_http.install(httpx.Response(307, headers={"Location": "http://outside.invalid/"}))
    result = await pay("http://agent.test/api/x402/hello-mangrove", wallet_address=wallet)
    assert result.status_code == 307 and not result.paid
    assert len(mock_http.requests) == 1


async def test_mcp_demo_over_real_http_session_with_mock_wire(wallet, sepolia_network, monkeypatch):
    """Exercise CLI connection -> MCP protocol -> x402 SDK -> custody -> ledger."""
    import importlib.util
    from pathlib import Path

    from src.services import spend_service, x402_payer
    from x402.mcp import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY

    path = Path(__file__).resolve().parents[2] / "scripts/_x402_demo.py"
    spec = importlib.util.spec_from_file_location("_demo_wire_test", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    calls = []
    original_client = httpx.AsyncClient

    def handler(request):
        assert not any(key in request.headers for key in ("authorization", "x-api-key", "payment-signature"))
        if request.method != "POST":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        if "id" not in payload:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "hello_mangrove", "inputSchema": {"type": "object"}}]}
        else:
            assert method == "tools/call"
            calls.append(payload["params"])
            if len(calls) == 1:
                assert "_meta" not in payload["params"] or MCP_PAYMENT_META_KEY not in payload["params"]["_meta"]
                result = {"isError": True, "content": [], "structuredContent": PaymentRequired(
                    x402_version=2, accepts=[_requirements()],
                ).model_dump(by_alias=True)}
            else:
                assert payload["params"]["_meta"][MCP_PAYMENT_META_KEY]["payload"]["authorization"]["from"] == wallet
                result = {"isError": False, "content": [{"type": "text", "text": '{"message":"hello"}'}],
                          "_meta": {MCP_PAYMENT_RESPONSE_META_KEY: _mcp_receipt()}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["max_redirects"] == 0
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await demo.mcp_payment("http://127.0.0.1:9080", wallet)
    assert isinstance(result, x402_payer.PaymentResult)
    assert result.paid and result.status_code == 200
    assert len(calls) == 2
    assert spend_service.list_payments()[0]["state"] == "settled"
