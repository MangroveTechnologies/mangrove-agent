"""Sign-In-With-X proofs: the guarded signer, the header, and when the proxy sends one.

MangroveAI verifies these with the same EIP-4361 text (pinned there in
tests/v402/test_siwx.py against the reference implementation); the golden string below is
that same text, so the two sides cannot drift apart unnoticed.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from src.config import app_config
from src.services import wallet_manager
from src.shared.errors import SigningError
from src.shared.x402 import siwx
from tests.integration import test_x402_sync_transport as payment_fixtures

database = payment_fixtures.database
wallet = payment_fixtures.wallet
ORIGIN = "http://127.0.0.1:5002"


@pytest.fixture
def mangrove(monkeypatch, wallet):
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", ORIGIN + "/api/v1")
    monkeypatch.setattr(app_config, "X402_PAYER_WALLET", wallet)
    return wallet


def _times(start_offset=0, minutes=5):
    issued = datetime.now(timezone.utc) + timedelta(seconds=start_offset)
    return (siwx._stamp(issued), siwx._stamp(issued + timedelta(minutes=minutes)))


def _sign(wallet_address, **override):
    issued, expires = _times()
    fields = dict(wallet_address=wallet_address, domain="127.0.0.1:5002", uri=ORIGIN + "/mcp/",
                  chain="eip155:84532", nonce="a1b2c3d4e5f60718", issued_at=issued,
                  expiration_time=expires)
    fields.update(override)
    return wallet_manager.sign_siwx_message(**fields), fields


def test_the_message_text_is_the_one_mangroveai_verifies():
    assert siwx.format_eip4361(
        domain="127.0.0.1:5002", address="0x5A0b54D5dc17e0AadC383d2db43B0a0D3E029c4c",
        uri="http://127.0.0.1:5002/mcp/", version="1", chain_id=84532,
        nonce="a1b2c3d4e5f60718", issued_at="2026-10-17T02:30:00.123Z",
        statement="Sign in to MangroveAI", expiration_time="2026-10-17T02:35:00.123Z",
    ) == ("127.0.0.1:5002 wants you to sign in with your Ethereum account:\n"
          "0x5A0b54D5dc17e0AadC383d2db43B0a0D3E029c4c\n\nSign in to MangroveAI\n\n"
          "URI: http://127.0.0.1:5002/mcp/\nVersion: 1\nChain ID: 84532\n"
          "Nonce: a1b2c3d4e5f60718\nIssued At: 2026-10-17T02:30:00.123Z\n"
          "Expiration Time: 2026-10-17T02:35:00.123Z")


def test_a_valid_proof_is_signed_by_the_wallet_over_the_fixed_text(mangrove):
    signature, fields = _sign(mangrove)
    message = siwx.format_eip4361(
        domain=fields["domain"], address=mangrove, uri=fields["uri"], version="1",
        chain_id=84532, nonce=fields["nonce"], issued_at=fields["issued_at"],
        statement=siwx.STATEMENT, expiration_time=fields["expiration_time"])
    assert Account.recover_message(encode_defunct(text=message), signature=signature) == mangrove


@pytest.mark.parametrize("override", [
    {"domain": "evil.example"},                         # another site's login
    {"uri": "https://evil.example/mcp/"},
    {"chain": "eip155:1"},                              # a chain the wallet is not on
    {"chain": "eip155:8453"},                           # mainnet, for a Sepolia wallet
    {"nonce": "short"},
    {"nonce": "has spaces in it"},
])
def test_the_guard_refuses_anything_but_a_proof_for_the_configured_service(mangrove, override):
    with pytest.raises(SigningError, match="Refused to sign"):
        _sign(mangrove, **override)


@pytest.mark.parametrize("start,minutes", [(-300, 5), (300, 5), (0, 30), (0, 0)])
def test_the_guard_refuses_a_window_that_is_stale_future_long_or_empty(mangrove, start, minutes):
    issued, expires = _times(start, minutes)
    with pytest.raises(SigningError, match="Refused to sign"):
        _sign(mangrove, issued_at=issued, expiration_time=expires)


def test_general_message_signing_stays_refused(mangrove):
    with pytest.raises(SigningError, match="sign_message is disabled"):
        wallet_manager.sign_message("Sign in to MangroveAI", mangrove)


def test_the_header_carries_a_fresh_verifiable_proof(mangrove):
    first = json.loads(base64.b64decode(siwx.build_siwx_header(ORIGIN + "/mcp/")))
    second = json.loads(base64.b64decode(siwx.build_siwx_header(ORIGIN + "/mcp/")))
    assert first["nonce"] != second["nonce"], "one proof per call: MangroveAI spends each nonce"
    assert (first["domain"], first["chainId"], first["address"]) == ("127.0.0.1:5002", "eip155:84532", mangrove)
    message = siwx.format_eip4361(
        domain=first["domain"], address=first["address"], uri=first["uri"], version="1",
        chain_id=84532, nonce=first["nonce"], issued_at=first["issuedAt"],
        statement=first["statement"], expiration_time=first["expirationTime"])
    assert Account.recover_message(encode_defunct(text=message), signature=first["signature"]) == mangrove


@pytest.mark.asyncio
async def test_the_proxy_signs_tool_calls_only(mangrove):
    from src.mcp import mangrove_proxy
    hook = mangrove_proxy._sign_in_hook(ORIGIN + "/mcp/")
    call = httpx.Request("POST", ORIGIN + "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                        "params": {"name": "list_strategies"}})
    housekeeping = httpx.Request("POST", ORIGIN + "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    await hook(call)
    await hook(housekeeping)
    assert siwx.SIWX_HEADER in call.headers
    assert siwx.SIWX_HEADER not in housekeeping.headers


@pytest.mark.asyncio
async def test_no_payer_wallet_sends_no_proof_and_never_blocks_the_call(database, monkeypatch):
    from src.mcp import mangrove_proxy
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", ORIGIN + "/api/v1")
    hook = mangrove_proxy._sign_in_hook(ORIGIN + "/mcp/")
    call = httpx.Request("POST", ORIGIN + "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
    await hook(call)
    assert siwx.SIWX_HEADER not in call.headers


def test_the_guard_refuses_a_time_with_no_zone(mangrove):
    issued, expires = _times()
    with pytest.raises(SigningError, match="time zone"):
        _sign(mangrove, issued_at=issued.replace("Z", ""), expiration_time=expires.replace("Z", ""))


def test_a_proof_is_stamped_slightly_in_the_past_for_clock_skew(mangrove):
    issued = json.loads(base64.b64decode(siwx.build_siwx_header(ORIGIN + "/mcp/")))["issuedAt"]
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(issued.replace("Z", "+00:00"))).total_seconds()
    assert siwx.CLOCK_SKEW_SECONDS - 5 <= age <= siwx.CLOCK_SKEW_SECONDS + 5


def test_existing_testnet_wallet_signs_only_active_mainnet(mangrove, monkeypatch):
    monkeypatch.setattr(app_config, 'X402_NETWORK', 'eip155:8453')
    signature, fields = _sign(mangrove, chain='eip155:8453')
    message = siwx.format_eip4361(
        domain=fields['domain'], address=mangrove, uri=fields['uri'], version='1',
        chain_id=8453, nonce=fields['nonce'], issued_at=fields['issued_at'],
        statement=siwx.STATEMENT, expiration_time=fields['expiration_time'])
    assert Account.recover_message(encode_defunct(text=message), signature=signature) == mangrove
    with pytest.raises(SigningError, match='configured network'):
        _sign(mangrove, chain='eip155:84532')
