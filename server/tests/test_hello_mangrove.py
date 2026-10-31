"""Easter egg endpoint tests (x402-gated).

The official x402 middleware handles payment flow:
- No credentials -> 402 with payment-required header
- API key -> 200 (bypasses middleware)
- Invalid payment signature -> 402 (correctly rejected by facilitator)
"""
import os

os.environ.setdefault("ENVIRONMENT", "test")

import base64
import json

from src.shared.x402.config import get_pay_to


def test_hello_mangrove_without_credentials_returns_402(offline_x402_client):
    resp = offline_x402_client.get("/api/x402/hello-mangrove")
    assert resp.status_code == 402

    payment_header = resp.headers.get("payment-required")
    assert payment_header is not None, "Missing payment-required header"

    padded = payment_header + "=" * (4 - len(payment_header) % 4)
    requirements = json.loads(base64.b64decode(padded))
    assert requirements["x402Version"] == 2
    assert len(requirements["accepts"]) >= 1
    accept = requirements["accepts"][0]
    assert accept["payTo"] == get_pay_to()


def test_hello_mangrove_with_api_key_returns_message(offline_x402_client):
    resp = offline_x402_client.get("/api/x402/hello-mangrove", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 200
    data = resp.json()
    assert "Thank you for supporting the project" in data["message"]
    assert "timestamp" in data


def test_hello_mangrove_invalid_payment_returns_402(offline_x402_client):
    """Mock/invalid payment signatures are correctly rejected by the real middleware."""
    resp = offline_x402_client.get(
        "/api/x402/hello-mangrove",
        headers={"Payment-Signature": "invalid-signature"},
    )
    # Real middleware rejects invalid signatures -- this is correct behavior
    assert resp.status_code == 402
