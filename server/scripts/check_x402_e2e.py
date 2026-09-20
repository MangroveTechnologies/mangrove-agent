#!/usr/bin/env python3
"""Manual Base/Sepolia test. Quote-only unless --pay is explicitly supplied."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

NETWORK = "eip155:84532"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RPC = "https://sepolia.base.org"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("hello", "signals"), required=True)
    parser.add_argument("--receiver", required=True, help="Expected receiver PUBLIC address")
    parser.add_argument("--pay", action="store_true", help="Authorize ONE payment on the selected network")
    parser.add_argument("--network", choices=("sepolia", "mainnet"), default="sepolia")
    parser.add_argument("--allow-mainnet", action="store_true",
                        help="Explicitly permit real USDC spending with --network mainnet --pay")
    parser.add_argument("--log", type=Path, default=Path("agent-data/x402-e2e.jsonl"))
    args = parser.parse_args()
    if args.pay and args.network == "mainnet" and not args.allow_mainnet:
        parser.error("Mainnet payment requires --allow-mainnet; omit --pay to inspect a quote only.")
    if args.network == "mainnet" and args.case != "signals":
        parser.error("The mainnet smoke test supports only signals (maximum 0.001 USDC).")
    network = "eip155:8453" if args.network == "mainnet" else NETWORK
    chain_id = 8453 if args.network == "mainnet" else 84532
    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" if args.network == "mainnet" else USDC
    rpc_url = "https://mainnet.base.org" if args.network == "mainnet" else RPC
    domain_name = "USD Coin" if args.network == "mainnet" else "USDC"
    explorer = "https://basescan.org/tx/" if args.network == "mainnet" else "https://sepolia.basescan.org/tx/"
    root = Path(__file__).resolve().parents[2]
    if Path.cwd().resolve() != root:
        parser.error(f"Run from the repository root: {root}")
    if os.environ.get("ENVIRONMENT") != "local" or os.environ.get("MANGROVE_AGENT_HOME"):
        parser.error("Use ENVIRONMENT=local and unset MANGROVE_AGENT_HOME for this checkout test.")
    if os.environ.get("MANGROVE_API_KEY"):
        parser.error("Unset MANGROVE_API_KEY in this shell; this test uses wallet payment.")
    sys.path.insert(0, str(root / "server"))

    import httpx
    from eth_utils import is_address, to_checksum_address

    from src.shared.logging import configure
    configure("local")
    from src.config import app_config
    from src.services import spend_service, wallet_manager, x402_payer
    from src.shared.clients.mangrove import create_x402_mangrove_client
    from src.shared.private_files import append_private
    from src.shared.redaction import redact_diagnostics
    from src.shared.x402.diagnostics import payment_response_metadata
    from src.shared.x402.sync_transport import X402SyncTransport

    if not is_address(args.receiver):
        parser.error("--receiver must be an EVM public address.")
    receiver = to_checksum_address(args.receiver)
    run_id = str(uuid.uuid4())
    args.log.parent.mkdir(parents=True, exist_ok=True)

    def event(name, **fields):
        item = {"time": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                "event": name, **fields}
        line = json.dumps(redact_diagnostics(item))
        print(line, flush=True)
        append_private(args.log, line)

    limit = 50000 if args.case == "hello" else 1000
    url = ("http://127.0.0.1:9082/api/x402/hello-mangrove" if args.case == "hello"
           else "http://127.0.0.1:5002/api/v1/signals/?limit=1")
    quote_amount = None
    signed_requests = 0
    last_status = None

    class ObservedTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            nonlocal quote_amount, signed_requests, last_status
            signed = "payment-signature" in request.headers
            if signed:
                signed_requests += 1
                if signed_requests > 1:
                    raise RuntimeError("Refusing a second signed request in this test")
            event("request", method=request.method, url=str(request.url), signed=signed)
            response = self.inner.handle_request(request)
            last_status = response.status_code
            event("response", status=response.status_code,
                  correlation_id=response.headers.get("X-Correlation-ID"))
            # A signed 402 may contain a failure receipt rather than a new
            # PAYMENT-REQUIRED quote. Preserve that outcome for reconciliation.
            if signed:
                event("payment_response", **payment_response_metadata(response))
                return response
            if response.status_code == 402:
                try:
                    raw = response.headers.get("payment-required", "")
                    envelope = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True))
                    offers = envelope.get("accepts", [])
                    if envelope.get("x402Version") != 2 or len(offers) != 1:
                        raise RuntimeError("Expected exactly one V2 offer")
                    offer = offers[0]
                    amount = str(offer.get("amount", ""))
                    if not amount.isascii() or not amount.isdigit():
                        raise RuntimeError("Invalid amount")
                    amount = int(amount)
                    event("quote", network=offer.get("network"), asset=offer.get("asset"),
                          receiver=offer.get("payTo"), amount_usdc=str(Decimal(amount) / 1000000),
                          domain_valid=offer.get("extra", {}).get("name") == domain_name and offer.get("extra", {}).get("version") == "2")
                    if (offer.get("scheme") != "exact" or offer.get("network") != network
                            or str(offer.get("asset", "")).lower() != usdc.lower()
                            or str(offer.get("payTo", "")).lower() != receiver.lower()
                            or offer.get("extra", {}).get("name") != domain_name
                            or offer.get("extra", {}).get("version") != "2"
                            or not 0 < amount <= limit):
                        raise RuntimeError("Quote differs from allowed network, USDC, receiver, domain or price")
                    quote_amount = amount
                except Exception:
                    response.close()
                    raise
            return response

        def close(self):
            self.inner.close()

    def rpc(method, params):
        with httpx.Client(timeout=20, trust_env=False) as http:
            response = http.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            raise RuntimeError("Public RPC returned an error")
        return data["result"]

    def balance(address, block="latest"):
        data = "0x70a08231" + address[2:].lower().rjust(64, "0")
        return int(rpc("eth_call", [{"to": usdc, "data": data}, block]), 16)

    try:
        event("start", case=args.case, pay=args.pay, network=network,
              maximum_usdc=str(Decimal(limit) / 1000000))
        if str(app_config.X402_NETWORK) != network:
            raise RuntimeError("Agent configuration must match the selected test network")
        if not args.pay:
            with httpx.Client(transport=ObservedTransport(), timeout=20, trust_env=False) as http:
                response = http.get(url)
            if response.status_code != 402 or quote_amount is None:
                raise RuntimeError("Expected an anonymous 402 quote")
            event("quote_only_pass", payment_sent=False)
            return 0

        if not Path(str(app_config.DB_PATH)).is_file():
            raise RuntimeError("Existing wallet database not found; import the wallet first")
        payer = x402_payer.resolve_payer_wallet()
        wallet_manager.require_backup_confirmed(payer)
        x402_payer.check_payment_budget(url)
        if int(rpc("eth_chainId", []), 16) != chain_id:
            raise RuntimeError("RPC does not match the selected network")
        before = {"payer": balance(payer), "receiver": balance(receiver)}
        event("balances_before", payer=payer, receiver=receiver, micro_usdc=before)
        if before["payer"] < limit:
            raise RuntimeError("Payer has insufficient USDC on the selected network for this test limit")
        prior_ids = {row["id"] for row in spend_service.list_payments(limit=500)}
        try:
            if args.case == "signals":
                with create_x402_mangrove_client(
                    environment="local", base_url="http://127.0.0.1:5002/api/v1",
                    kb_base_url="http://127.0.0.1:8080/api", wallet_address=payer,
                    transport=ObservedTransport(),
                ) as sdk:
                    result = sdk.signals.list(limit=1)
                    event("resource_received", signal_count=len(result.items), total=result.total)
            else:
                with httpx.Client(transport=X402SyncTransport(
                    allowed_origins=["http://127.0.0.1:9082"], wallet_address=payer,
                    transport=ObservedTransport(),
                ), timeout=120, trust_env=False) as http:
                    response = http.get(url)
                    response.raise_for_status()
                    event("resource_received", status=response.status_code)
        finally:
            rows = [row for row in spend_service.list_payments(limit=500) if row["id"] not in prior_ids]
            event("ledger", payments=rows, budget=spend_service.get_status())
        if not (last_status is not None and 200 <= last_status < 300 and signed_requests == 1):
            raise RuntimeError("Did not complete exactly one signed successful request")
        if len(rows) != 1 or rows[0]["state"] != "settled" or not rows[0]["transaction"]:
            raise RuntimeError("Expected exactly one settled ledger entry with a transaction")
        tx = rows[0]["transaction"]
        receipt = None
        for _ in range(10):
            receipt = rpc("eth_getTransactionReceipt", [tx])
            if receipt:
                break
            time.sleep(2)
        if not receipt or int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError("No successful on-chain receipt yet; do not repeat payment automatically")
        def matches(log):
            topics = log.get("topics", [])
            return (log.get("address", "").lower() == usdc.lower() and len(topics) == 3
                    and topics[0].lower() == TRANSFER
                    and topics[1][-40:].lower() == payer[2:].lower()
                    and topics[2][-40:].lower() == receiver[2:].lower()
                    and int(log.get("data", "0x0"), 16) == quote_amount)
        if not any(matches(log) for log in receipt.get("logs", [])):
            raise RuntimeError("Receipt does not contain the expected USDC transfer")
        event("onchain_verified", transaction=tx, block=int(receipt["blockNumber"], 16),
              explorer=explorer + tx)
        # The successful receipt and exact Transfer event above are required
        # proof. Historical balance snapshots are supplementary: public RPCs
        # may not serve state for a just-mined block or may rate-limit reads.
        balances_available = True
        try:
            after = {"payer": balance(payer, receipt["blockNumber"]),
                     "receiver": balance(receiver, receipt["blockNumber"])}
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as error:
            balances_available = False
            event("balances_after_unavailable", block=int(receipt["blockNumber"], 16),
                  error_type=type(error).__name__,
                  reason="Supplementary RPC balance snapshot unavailable; transfer already verified",
                  payment_retry_needed=False)
        else:
            event("balances_after", block=int(receipt["blockNumber"], 16), micro_usdc=after)
        event("PASS", case=args.case, onchain_verified=True,
              balances_after_available=balances_available)
        return 0
    except Exception as error:
        # Don't dump remote exception bodies, signatures, or credential-bearing tracebacks.
        event("FAIL", error_type=type(error).__name__, code=getattr(error, "code", None),
              reason=str(error) if type(error) is RuntimeError else "See response status and ledger above",
              signed_requests=signed_requests,
              next_step="Inspect the failure and any transaction before repeating --pay; do not reset the budget automatically.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
