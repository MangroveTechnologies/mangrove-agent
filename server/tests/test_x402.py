"""Tests for x402 config system + middleware bypass guard.

Verifies that all x402 config values are loaded from the JSON config file
via the app_config singleton (no hardcoded values), and that the HTTP
middleware does not bypass payment for invalid API keys (regression
test for the audit finding where any non-empty X-API-Key skipped x402).
"""
import os

os.environ.setdefault("ENVIRONMENT", "test")

from src.shared.x402.config import (
    get_cdp_api_key_id,
    get_cdp_api_key_secret,
    get_facilitator_url,
    get_hello_mangrove_price,
    get_network,
    get_pay_to,
    get_usdc_contract,
)


def test_config_values_loaded_from_json():
    """All x402 config values come from test-config.json."""
    assert get_hello_mangrove_price() == "50000"
    assert get_pay_to().startswith("0x")
    assert len(get_pay_to()) == 42
    assert get_network().startswith("eip155:")
    assert get_facilitator_url().startswith("https://")
    assert get_usdc_contract().startswith("0x")


def test_facilitator_url_is_valid():
    url = get_facilitator_url()
    assert url in [
        "https://x402.org/facilitator",
        "https://api.cdp.coinbase.com/platform/v2/x402",
    ]


def test_network_is_caip2_format():
    network = get_network()
    assert network.startswith("eip155:")
    chain_id = network.split(":")[1]
    assert chain_id.isdigit()


def test_cdp_keys_are_strings():
    assert isinstance(get_cdp_api_key_id(), str)
    assert isinstance(get_cdp_api_key_secret(), str)


# ---------------------------------------------------------------------------
# x402 middleware bypass regression
# ---------------------------------------------------------------------------
#
# Previously, the middleware in src/app.py treated *any* non-empty X-API-Key
# header as a bypass signal, without validating it. A request with
# `X-API-Key: anything` skipped the x402 payment path entirely. The fix
# validates the key against has_valid_api_key before bypassing.

def test_invalid_api_key_does_not_bypass_x402(client):
    """An invalid X-API-Key must NOT skip x402; expect 402 Payment Required."""
    r = client.get(
        "/api/x402/hello-mangrove",
        headers={"X-API-Key": "this-is-not-a-real-key"},
    )
    assert r.status_code == 402, (
        f"Expected 402 (payment required) for invalid API key — got {r.status_code}. "
        "Middleware is letting any X-API-Key value bypass x402 without validation."
    )


def test_valid_api_key_bypasses_x402(client):
    """A valid X-API-Key skips x402 and returns 200."""
    r = client.get(
        "/api/x402/hello-mangrove",
        headers={"X-API-Key": "test-key-1"},
    )
    assert r.status_code == 200


def test_no_api_key_returns_402(client):
    """No X-API-Key, no payment header → 402."""
    r = client.get("/api/x402/hello-mangrove")
    assert r.status_code == 402
