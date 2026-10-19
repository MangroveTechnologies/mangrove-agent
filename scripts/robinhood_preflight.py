#!/usr/bin/env python3
"""Read-only local checks before activating a configured Robinhood strategy."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


class PreflightError(RuntimeError):
    """A read-only readiness check failed."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        if (
            parsed.scheme != "http"
            or not loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        port = parsed.port or 80
    except (ValueError, TypeError):
        raise PreflightError("Agent URL must be an HTTP loopback origin with no path or credentials.") from None
    host_text = f"[{host}]" if ":" in host else host
    return f"http://{host_text}:{port}"


def _get_json(origin: str, api_key: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        origin + path,
        headers={"Accept": "application/json", "X-API-Key": api_key},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            raise PreflightError("Agent response exceeded the 1 MiB safety limit.")
        value = json.loads(payload)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise PreflightError(f"Agent returned HTTP {status} for {path}.") from None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        raise PreflightError(f"No valid response from the local agent for {path}.") from None
    if not isinstance(value, dict):
        raise PreflightError(f"Agent returned an invalid response for {path}.")
    return value


def check_strategy(
    strategy_id: str,
    origin: str,
    api_key: str,
    request_json: Callable[[str, str, str], dict[str, Any]] = _get_json,
) -> dict[str, Any]:
    """Read broker readiness and strategy detail; never mutate local or broker state."""
    base = _local_origin(origin)
    if not strategy_id:
        raise PreflightError("Provide a valid local strategy ID.")
    if not api_key.strip():
        raise PreflightError("Set MANGROVE_AGENT_API_KEY to a local agent API key.")

    status = request_json(base, api_key, "/api/v1/agent/brokers/robinhood/status")
    strategy_path = "/api/v1/agent/strategies/" + urllib.parse.quote(strategy_id, safe="")
    strategy = request_json(base, api_key, strategy_path)
    if status.get("connected") is not True:
        raise PreflightError("Robinhood did not report a connected account.")
    if strategy.get("id") != strategy_id:
        raise PreflightError("Agent returned a different strategy than requested.")
    if strategy.get("status") != "inactive":
        raise PreflightError("Strategy must be inactive before this preflight can pass.")
    venue = strategy.get("execution_venue")
    if not isinstance(venue, dict) or venue.get("venue") != "robinhood":
        raise PreflightError("Strategy is not configured for Robinhood execution.")

    return {
        "ready_for_human_review": True,
        "strategy_id": strategy_id,
        "strategy_status": strategy["status"],
        "execution_venue": venue,
        "market_context": strategy.get("market_context"),
        "robinhood": {
            key: status.get(key)
            for key in (
                "connected", "account", "account_state", "crypto_linked",
                "trade_approvals", "buying_power", "crypto_buying_power", "tool_count",
            )
        },
        "checks_performed": [
            "GET /api/v1/agent/brokers/robinhood/status",
            f"GET {strategy_path}",
        ],
        "state_changed": False,
        "evaluation_run": False,
        "order_submitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy_id", help="Local Mangrove Agent strategy UUID")
    parser.add_argument(
        "--url", default=os.environ.get("MANGROVE_AGENT_URL", "http://127.0.0.1:9080"),
        help="Local agent origin (default: MANGROVE_AGENT_URL or loopback port 9080)",
    )
    args = parser.parse_args()
    api_key = os.environ.get("MANGROVE_AGENT_API_KEY", "")
    try:
        result = check_strategy(args.strategy_id, args.url, api_key)
    except PreflightError as exc:
        parser.exit(1, f"Preflight failed: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    print("Read-only checks passed. Review results before any separate activation action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
