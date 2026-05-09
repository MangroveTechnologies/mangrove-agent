"""Agent-native x402 payment over MCP — no human in the loop.

Mirror of agent_pay_hello_mangrove.py (REST) for the MCP transport. Uses
the stock x402.mcp.x402MCPSession, which automatically detects the
PAYMENT_REQUIRED response, signs an EIP-3009 authorization, and retries
with the payment attached via MCP _meta — the same ergonomics as
x402AsyncTransport on the REST side.

Requirements:
    - Server running (default: http://127.0.0.1:9080) with the hello_mangrove
      tool registered via x402.mcp.create_payment_wrapper, so the 402 response
      shape is what x402MCPSession expects
    - WALLET_SECRET env var with an EVM private key funded on the active
      network (~$0.05 USDC + a few cents of ETH for gas on Base mainnet)

Usage:
    export WALLET_SECRET=0x...
    ENVIRONMENT=local python scripts/agent_pay_hello_mangrove_mcp.py

    SERVER_URL=http://127.0.0.1:8081 python scripts/agent_pay_hello_mangrove_mcp.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from eth_account import Account
from mcp.client.streamable_http import streamablehttp_client
from x402 import x402Client
from x402.mcp import x402MCPSession
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.signers import EthAccountSigner

from mcp import ClientSession


def _extract_message(content) -> str:
    if not content:
        return "(no content)"
    first = content[0]
    text = getattr(first, "text", None) or (first.get("text", "") if isinstance(first, dict) else "")
    try:
        return json.loads(text).get("message", text)
    except (ValueError, TypeError):
        return text


async def main() -> int:
    secret = os.environ.get("WALLET_SECRET")
    if not secret:
        print("ERROR: WALLET_SECRET unset. Export an EVM private key.", file=sys.stderr)
        return 1

    base_url = os.environ.get("SERVER_URL", "http://127.0.0.1:9080").rstrip("/")
    mcp_url = f"{base_url}/mcp/"
    network = os.environ.get("X402_NETWORK", "eip155:8453")

    account = Account.from_key(secret)
    print(f"Payer address: {account.address}")
    print(f"MCP endpoint:  {mcp_url}")

    payment_client = x402Client()
    payment_client.register(network, ExactEvmClientScheme(EthAccountSigner(account)))

    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            x402_mcp = x402MCPSession(session, payment_client, auto_payment=True)
            await x402_mcp.initialize()

            result = await x402_mcp.call_tool("hello_mangrove", {})

            if result.is_error:
                print(f"Tool call errored: {_extract_message(result.content)}")
                return 1

            print(f"Payment made:  {result.payment_made}")
            print(f"Message:       {_extract_message(result.content)}")

            pr = result.payment_response
            if pr is not None:
                tx = getattr(pr, "transaction", None) or (pr.get("transaction") if isinstance(pr, dict) else None)
                payer = getattr(pr, "payer", None) or (pr.get("payer") if isinstance(pr, dict) else None)
                network = getattr(pr, "network", None) or (pr.get("network") if isinstance(pr, dict) else None)
                print(f"Payer:         {payer}")
                print(f"Network:       {network}")
                print(f"Transaction:   {tx}")
                if tx and str(network or "").endswith(":8453"):
                    print(f"BaseScan:      https://basescan.org/tx/{tx}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
