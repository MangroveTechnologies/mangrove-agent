"""Opt-in funded acceptance against a preprovisioned isolated-custody agent.

The ordinary suite runs an offline unsupported-Sepolia regression and skips
funded Base mainnet acceptance. This harness never imports a secret, opens a
wallet database, reads installed configuration, or changes spending policy.

An administrator must first install custody, back up the public wallet, approve
its exact USDC/native pair/executor/rate/fee/spend bounds, and fund 0.10 USDC
plus gas. Set ENABLE_MAINNET_TEST=1, LIVE_AGENT_URL (loopback HTTP(S) origin),
LIVE_AGENT_API_KEY (local incoming credential), and LIVE_WALLET_ADDRESS.
One run can broadcast one finite approval and one 0.10 USDC swap. Never retry
an uncertain result automatically; inspect its durable journal first.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import pytest

_MAINNET_CHAIN_ID = 8453
_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
_INPUT_USDC = 0.1  # Human units, exactly 100,000 USDC base units.
_SLIPPAGE = 0.002  # REST decimal: 0.2%, separate from routing fee policy.


@dataclass(frozen=True)
class LiveSettings:
    origin: str
    wallet: str
    api_key: str = field(repr=False)


def _live_settings(environ: dict[str, str] | None = None) -> LiveSettings:
    """Validate explicit public-wallet settings without reading local files."""
    environ = os.environ if environ is None else environ
    if environ.get("ENABLE_MAINNET_TEST") != "1":
        raise ValueError("Funded Base mainnet acceptance requires explicit ENABLE_MAINNET_TEST=1.")
    names = ("LIVE_AGENT_URL", "LIVE_AGENT_API_KEY", "LIVE_WALLET_ADDRESS")
    if any(not environ.get(name) for name in names):
        raise ValueError("Funded acceptance needs LIVE_AGENT_URL, LIVE_AGENT_API_KEY and LIVE_WALLET_ADDRESS.")
    origin = environ["LIVE_AGENT_URL"]
    try:
        target = urlsplit(origin)
        host = ipaddress.ip_address(target.hostname or "")
        if (target.scheme not in {"http", "https"} or not host.is_loopback
                or target.username is not None or target.password is not None
                or target.query or target.fragment or target.path not in {"", "/"}
                or target.port is None or any(ord(char) < 32 for char in origin)):
            raise ValueError
    except ValueError:
        raise ValueError("LIVE_AGENT_URL must be an explicit numeric loopback HTTP(S) origin with a port.") from None
    wallet = environ["LIVE_WALLET_ADDRESS"]
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise ValueError("LIVE_WALLET_ADDRESS must be the preprovisioned public EVM address.")
    return LiveSettings(origin.rstrip("/"), wallet, environ["LIVE_AGENT_API_KEY"])


def _run_live_swap(settings: LiveSettings, *, transport=None) -> dict:
    """Use actual HTTP, without redirects/proxies or automatic POST retries."""
    with httpx.Client(
        base_url=settings.origin, headers={"X-API-Key": settings.api_key},
        follow_redirects=False, trust_env=False, timeout=180, transport=transport,
    ) as client:
        wallets = client.get("/api/v1/agent/wallet/list")
        if wallets.status_code != 200:
            raise AssertionError("Agent wallet metadata was unavailable; no swap requested.")
        matching = [item for item in wallets.json()
                    if item.get("address", "").lower() == settings.wallet.lower()]
        if (len(matching) != 1 or matching[0].get("custody_status") != "isolated"
                or not matching[0].get("backup_confirmed_at")
                or matching[0].get("chain") != "evm" or matching[0].get("chain_id") != _MAINNET_CHAIN_ID):
            raise AssertionError("The exact Base wallet needs isolated custody and human-confirmed backup; no swap requested.")
        response = client.post("/api/v1/agent/dex/swap", json={
            "input_token": _MAINNET_USDC, "output_token": _NATIVE_ETH,
            "amount": _INPUT_USDC, "chain_id": _MAINNET_CHAIN_ID,
            "wallet_address": settings.wallet, "slippage_pct": _SLIPPAGE,
            "venue_id": "1inch", "confirm": True,
        })
        if response.status_code != 200:
            # Never print bodies, credentials or provider exceptions. A failed
            # swap may still leave a finite approval or uncertain broadcast.
            raise AssertionError(
                "Funded swap failed or has an uncertain outcome. Inspect the agent journal and custody audit; "
                "a finite approval may remain. Do not rerun automatically."
            )
        return response.json()


def test_sepolia_swap_signing_is_unsupported():
    """No provider call, credential, signature or broadcast is needed."""
    from src.services.transaction_intent import router_for_chain
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="Unsupported signing chain"):
        router_for_chain(84532)


@pytest.mark.skipif(
    os.environ.get("ENABLE_MAINNET_TEST") != "1",
    reason="Funded Base acceptance needs explicit authorization and a funded, policy-approved isolated custody service",
)
def test_mainnet_live_swap():
    """Spend exactly 0.10 USDC; routing fee and native gas need policy approval."""
    completed = False
    try:
        settings = _live_settings()
        result = _run_live_swap(settings)
        completed = (
            result["status"] in {"confirmed", "success"}
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", result.get("tx_hash", "")) is not None
            and result["input_amount"] == pytest.approx(_INPUT_USDC)
            and bool(result.get("trade_log_id"))
        )
    except Exception:
        # Test tracebacks can render HTTP arguments/credentials. Leave the
        # exception context before issuing a fixed, traceback-free failure.
        pass
    if not completed:
        pytest.fail(
            "Funded acceptance failed or is uncertain. Check LIVE_AGENT_URL/API_KEY/WALLET_ADDRESS "
            "and independent custody configuration in the administrator terminal, then inspect the "
            "agent journal/custody audit before any retry. A finite approval may remain.",
            pytrace=False,
        )
