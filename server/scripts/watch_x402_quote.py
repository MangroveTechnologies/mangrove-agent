#!/usr/bin/env python3
"""Observe an anonymous x402 quote. Never loads a wallet or signs a payment."""
import argparse
import base64
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.shared.private_files import append_private  # noqa: E402
from src.shared.redaction import redact_diagnostics  # noqa: E402
from src.shared.urls import strip_query  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def inspect(url):
    # Disable proxies, redirect following, cookie storage and ambient auth.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "url": strip_query(url), "payment_sent": False}
    try:
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error  # 402 is the response we want to inspect.
        with response:
            result["http_status"] = response.code
            correlation = response.headers.get("X-Correlation-ID", "")
            if re.fullmatch(r"[0-9a-fA-F-]{36}", correlation):
                result["correlation_id"] = correlation
            raw = response.headers.get("PAYMENT-REQUIRED")
            result["payment_required_present"] = bool(raw)
            if raw and len(raw) <= 16384:
                try:
                    envelope = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True))
                    if not isinstance(envelope, dict):
                        raise ValueError("Envelope must be an object")
                    if not isinstance(envelope.get("accepts"), list):
                        raise ValueError("Invalid offers")
                    offers = []
                    for offer in envelope.get("accepts", []):
                        if not isinstance(offer, dict):
                            continue
                        network = offer.get("network")
                        contracts = {
                            "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                            "eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                        }
                        if not isinstance(network, str) or network not in contracts:
                            continue
                        if offer.get("scheme") != "exact":
                            continue
                        payee = offer.get("payTo")
                        if not isinstance(payee, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", payee):
                            continue
                        summary = {"scheme": "exact", "network": network, "payTo": payee,
                                   "network_label": "Base Sepolia TESTNET" if network.endswith(":84532") else "Base MAINNET (real funds)"}
                        amount = str(offer.get("amount", ""))
                        if (str(offer.get("asset", "")).lower() == contracts.get(network)
                                and len(amount) <= 78 and amount.isascii() and amount.isdigit()):
                            summary["asset"] = contracts[network]
                            summary["amount_usdc"] = str(Decimal(amount) / Decimal(1_000_000))
                            offers.append(summary)
                    result["offers"] = offers
                except (ValueError, TypeError, RecursionError):
                    result["decode_error"] = "Invalid payment requirements"
            else:
                result["body_omitted"] = True
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        result["connection_error"] = type(error).__name__
    return redact_diagnostics(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5002/api/v1/signals/?limit=1")
    parser.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                        help="Repeat at this interval; Ctrl-C to stop. Default: one request.")
    parser.add_argument("--log", type=Path, help="Append timestamped evidence as JSON Lines.")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        parser.error("Use an HTTP(S) URL without embedded credentials.")
    if not math.isfinite(args.watch) or args.watch < 0 or 0 < args.watch < 1:
        parser.error("--watch must be 0 or at least 1 second.")
    try:
        while True:
            result = inspect(args.url)
            print(json.dumps(result, indent=2), flush=True)
            if args.log:
                append_private(args.log, json.dumps(result))
            if not args.watch:
                return 0 if result.get("http_status") == 402 and result.get("offers") else 1
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
