"""Unit tests for dex_service — human <-> base-unit conversion.

Covers the root cause of the INSUFFICIENT_LIQUIDITY blocker: the agent
must convert a human swap amount (e.g. 0.001 ETH) to the input token's
base units (wei) before the backend call, and convert returned amounts
back to human units.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.services import dex_service  # noqa: E402
from src.shared.errors import SdkError, ValidationError  # noqa: E402

_WETH = "0x4200000000000000000000000000000000000006"
_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def _client_with_decimals(mapping):
    """A mock SDK client whose token_info returns the mapped decimals."""
    client = MagicMock()

    def _token_info(chain_id, address):
        ti = MagicMock()
        ti.decimals = mapping[address.lower()]
        return ti

    client.dex.token_info.side_effect = _token_info
    return client


# -- pure conversion -------------------------------------------------------

@pytest.mark.parametrize("amount,decimals,expected", [
    (0.001, 18, 1_000_000_000_000_000),
    (1.0, 18, 1_000_000_000_000_000_000),
    (1.0, 6, 1_000_000),
    (25, 6, 25_000_000),
    (0.0001, 18, 100_000_000_000_000),
])
def test_to_base_units(amount, decimals, expected):
    assert dex_service.to_base_units(amount, decimals) == expected


def test_from_base_units_roundtrip():
    assert dex_service.from_base_units(1_000_000_000_000_000, 18) == 0.001
    assert dex_service.from_base_units(2_500_000, 6) == 2.5


def test_to_base_units_rejects_negative():
    with pytest.raises(ValidationError):
        dex_service.to_base_units(-1.0, 18)


# -- decimals resolution ---------------------------------------------------

def test_resolve_decimals_native_sentinel_is_offline():
    client = MagicMock()
    assert dex_service.resolve_decimals(client, 8453, _NATIVE_ETH) == 18
    client.dex.token_info.assert_not_called()  # no network call for native


def test_resolve_decimals_address_uses_token_info():
    client = _client_with_decimals({_USDC.lower(): 6})
    assert dex_service.resolve_decimals(client, 8453, _USDC) == 6


def test_resolve_decimals_raises_on_lookup_failure():
    client = MagicMock()
    client.dex.token_info.side_effect = RuntimeError("upstream 500")
    # The raw-payload fallback can't recover decimals from a bare mock
    # (non-dict), so the lookup still surfaces a clear SdkError.
    with pytest.raises(SdkError):
        dex_service.resolve_decimals(client, 8453, _USDC)


def test_resolve_decimals_falls_back_to_raw_when_model_rejects():
    """If the SDK's typed TokenInfo rejects the response over an unrelated
    field (e.g. live server returns `tags` as objects vs the SDK's
    `list[str]`), decimals must still resolve from the raw tool payload —
    a swap can't fail on a metadata schema mismatch.
    """
    client = MagicMock()
    client.dex.token_info.side_effect = ValueError(
        "8 validation errors for TokenInfo: tags.0 Input should be a valid string"
    )
    client.dex._call_tool.return_value = {
        "token": {"address": _USDC, "symbol": "USDC", "decimals": 6,
                  "tags": [{"provider": "1inch", "value": "bluechip"}]}
    }
    assert dex_service.resolve_decimals(client, 8453, _USDC) == 6
    client.dex._call_tool.assert_called_once()


def test_resolve_decimals_fallback_handles_unwrapped_payload():
    """Fallback also reads decimals when the server doesn't wrap in `token`."""
    client = MagicMock()
    client.dex.token_info.side_effect = ValueError("validation error")
    client.dex._call_tool.return_value = {"decimals": 18, "symbol": "WETH"}
    assert dex_service.resolve_decimals(client, 8453, _WETH) == 18


# -- get_quote end-to-end (mocked client) ----------------------------------

def test_get_quote_sends_base_units_and_returns_human(monkeypatch):
    client = _client_with_decimals({_WETH.lower(): 18, _USDC.lower(): 6})
    quote = MagicMock()
    quote.model_dump.return_value = {
        "quote_id": "q1",
        "input_amount": 1_000_000_000_000_000,  # 0.001 WETH in wei
        "output_amount": 2_500_000,             # 2.5 USDC base units
    }
    client.dex.get_quote.return_value = quote
    monkeypatch.setattr(dex_service, "mangrove_markets_client", lambda: client)

    out = dex_service.get_quote(
        input_token=_WETH, output_token=_USDC, amount=0.001, chain_id=8453,
    )

    _, kwargs = client.dex.get_quote.call_args
    assert kwargs["amount"] == 1_000_000_000_000_000  # base units, not 0.001
    assert out["input_amount"] == 0.001
    assert out["output_amount"] == 2.5
    assert out["output_amount_base_units"] == 2_500_000


def test_get_quote_rejects_dust(monkeypatch):
    client = _client_with_decimals({_USDC.lower(): 6})
    monkeypatch.setattr(dex_service, "mangrove_markets_client", lambda: client)
    with pytest.raises(ValidationError):
        # 1e-7 USDC * 1e6 -> 0 base units
        dex_service.get_quote(
            input_token=_USDC, output_token=_WETH, amount=0.0000001, chain_id=8453,
        )
    client.dex.get_quote.assert_not_called()
