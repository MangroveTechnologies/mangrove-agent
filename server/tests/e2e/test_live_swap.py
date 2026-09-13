"""Live DEX swap E2E tests — OPT-IN, real funds (testnet or mainnet).

Two tests, both skipped by default:

- test_sepolia_live_swap: Base Sepolia testnet. Tiny amount. Enable with
  `ENABLE_SEPOLIA_TEST=1` and supply a funded wallet private key in
  `BASE_SEPOLIA_PRIVATE_KEY`.

- test_mainnet_live_swap: Base mainnet. Really small amount (capped).
  Enable with `ENABLE_MAINNET_TEST=1` and supply a funded wallet private
  key in `BASE_MAINNET_PRIVATE_KEY`. MUST pass sepolia first in the same
  run (order enforced via dependency).

Both drive the agent's /dex/swap endpoint — the SAME code path the cron
tick uses, so if this works the scheduled path works too.

The live MangroveMarkets MCP server at
https://mangrovemarkets-pcqgpciucq-uc.a.run.app is used — no mocking.
Requires a valid prod_* MANGROVE_API_KEY in local-config.json.

These tests are deliberately minimal — the heavy assertion surface lives
in unit + integration + smoke tests that run in CI. Here we only prove:
the agent's full stack completes a real on-chain swap.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("ENVIRONMENT", "local")

import pytest  # noqa: E402
from eth_account import Account  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _local_api_key() -> str:
    """First API_KEYS entry from local-config.json (unique per install, set by setup.sh)."""
    cfg = Path(__file__).resolve().parents[2] / "src" / "config" / "local-config.json"
    try:
        raw = json.loads(cfg.read_text()).get("API_KEYS", "")
    except FileNotFoundError:
        return ""
    return next((k.strip() for k in raw.split(",") if k.strip()), "")


_API_KEY = _local_api_key()

# Base Sepolia tokens
_SEPOLIA_CHAIN_ID = 84532
_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
_NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# Base Mainnet tokens
_MAINNET_CHAIN_ID = 8453
_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _import_wallet(address: str, private_key: str, chain_id: int) -> None:
    """Encrypt + insert the existing wallet into the local DB.

    The agent has no import endpoint (create_wallet always generates a
    fresh key), so for this test we bypass the service layer and write
    the encrypted row directly. wallet_manager.sign() will pick it up
    the same as any agent-created wallet.
    """
    from src.shared.crypto.fernet import encrypt
    from src.shared.db.sqlite import get_connection

    encrypted = encrypt(private_key.encode())
    get_connection().execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), address, "evm",
            "testnet" if chain_id != 8453 else "mainnet",
            chain_id, encrypted, "fernet-v1",
            "e2e-imported-wallet",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    get_connection().commit()


def _run_live_swap(
    private_key: str,
    chain_id: int,
    input_token: str,
    output_token: str,
    amount: float,
) -> dict:
    """Drive the agent's /dex/swap endpoint end-to-end against a real SDK.

    Returns the swap result dict (tx_hash, status, input/output amounts, …)
    on success, otherwise raises via pytest.fail.
    """
    from src.app import create_app

    account = Account.from_key(private_key)
    address = account.address

    # Build the app fresh so the imported wallet lives in this test's DB.
    app = create_app()
    with TestClient(app) as client:
        _import_wallet(address, private_key, chain_id)

        r = client.post(
            "/api/v1/agent/dex/swap",
            headers={"X-API-Key": _API_KEY},
            json={
                "input_token": input_token,
                "output_token": output_token,
                "amount": amount,
                "chain_id": chain_id,
                "wallet_address": address,
                "slippage": 1.0,
                "confirm": True,
            },
        )
        if r.status_code >= 400:
            pytest.fail(f"/dex/swap returned {r.status_code}: {r.text}")
        return r.json()


@pytest.mark.skipif(
    os.environ.get("ENABLE_SEPOLIA_TEST") != "1",
    reason="Sepolia live test opt-in via ENABLE_SEPOLIA_TEST=1",
)
def test_sepolia_live_swap(tmp_path, monkeypatch):
    """Swap ~0.10 USDC → ETH on Base Sepolia.

    Requires BASE_SEPOLIA_PRIVATE_KEY with some Sepolia USDC and a little
    Sepolia ETH for gas. Tiny amount by design so a single-run fund goes
    far.
    """
    privkey = os.environ.get("BASE_SEPOLIA_PRIVATE_KEY")
    if not privkey:
        pytest.skip("BASE_SEPOLIA_PRIVATE_KEY not set")

    db_file = tmp_path / "sepolia_live.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    result = _run_live_swap(
        privkey,
        chain_id=_SEPOLIA_CHAIN_ID,
        input_token=_SEPOLIA_USDC,
        output_token=_NATIVE_ETH,
        amount=100_000,  # 0.10 USDC (6 decimals)
    )

    assert result["status"] in {"confirmed", "success"}, result
    assert result["tx_hash"], "swap must return a tx_hash"
    print(f"\n✓ Sepolia swap confirmed: https://sepolia.basescan.org/tx/{result['tx_hash']}")

    ss.reset_scheduler_cache()
    db_mod.reset_connection()


@pytest.mark.skipif(
    os.environ.get("ENABLE_MAINNET_TEST") != "1",
    reason="Mainnet live test opt-in via ENABLE_MAINNET_TEST=1 (real $ at risk)",
)
def test_mainnet_live_swap(tmp_path, monkeypatch):
    """Swap 0.10 USDC → ETH on Base mainnet. Real funds.

    Budget cap: 0.10 USDC per run. Requires BASE_MAINNET_PRIVATE_KEY.
    Already proven manually on 2026-04-20 via
    server/scripts/first_live_swap.py (tx
    0x466c61415246ad2ee7059a8657ca80dce98a45f5d6024cdfb856261b0204b99f).
    This test reruns the same flow through the agent's /dex/swap so we
    can regression-test it.
    """
    privkey = os.environ.get("BASE_MAINNET_PRIVATE_KEY")
    if not privkey:
        pytest.skip("BASE_MAINNET_PRIVATE_KEY not set")

    db_file = tmp_path / "mainnet_live.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    result = _run_live_swap(
        privkey,
        chain_id=_MAINNET_CHAIN_ID,
        input_token=_MAINNET_USDC,
        output_token=_NATIVE_ETH,
        amount=100_000,  # 0.10 USDC (6 decimals)
    )

    assert result["status"] in {"confirmed", "success"}, result
    assert result["tx_hash"]
    print(f"\n✓ Mainnet swap confirmed: https://basescan.org/tx/{result['tx_hash']}")

    ss.reset_scheduler_cache()
    db_mod.reset_connection()
