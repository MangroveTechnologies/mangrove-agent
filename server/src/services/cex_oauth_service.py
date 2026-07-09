"""CEX (Kraken) OAuth-connect service — the KEYLESS path.

Unlike the BYOK path (`cex_service`, which stashes a Kraken key locally and
talks to Kraken directly), this service holds NO venue credential. It calls the
MangroveMarkets MCP server's authenticated exchange-proxy routes with the
user's Mangrove API key; the platform holds the OAuth grant and executes.

Flow: connect_start -> the user opens the returned authorize URL and consents
in a browser (picking view or execute mode) -> poll status until connected ->
place/validate orders, all keyless.
"""
from __future__ import annotations

from typing import Any

import httpx

from src.config import app_config

_BASE_PATH = "/api/v1/exchanges/kraken"
_TIMEOUT = 30.0


def _base_url() -> str:
    return str(app_config.MANGROVEMARKETS_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    # The user's Mangrove API key authenticates us to the markets server, which
    # derives the user_id server-side. No Kraken credential is involved.
    return {
        "Authorization": f"Bearer {app_config.MANGROVE_API_KEY}",
        "Accept": "application/json",
    }


def _request(method: str, path: str, *, json_body: dict[str, Any] | None = None,
             http: httpx.Client | None = None) -> dict[str, Any]:
    url = f"{_base_url()}{_BASE_PATH}{path}"
    client = http or httpx.Client(timeout=_TIMEOUT)
    try:
        resp = client.request(method, url, headers=_headers(), json=json_body)
    finally:
        if http is None:
            client.close()
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise RuntimeError(f"markets exchange proxy {resp.status_code}: {detail or body}")
    return body


def connect_start(*, mode: str = "view", http: httpx.Client | None = None) -> dict[str, Any]:
    """Begin an OAuth connect. Returns {authorize_url, state}; the user opens the
    URL and consents. mode = 'view' (read-only) or 'execute' (+trading)."""
    return _request("POST", "/connect", json_body={"mode": mode}, http=http)


def connect_status(*, http: httpx.Client | None = None) -> dict[str, Any]:
    """Poll connection status: {connected, connection:{mode, alias, ...}}."""
    return _request("GET", "/status", http=http)


def get_balances(*, http: httpx.Client | None = None) -> dict[str, Any]:
    return _request("GET", "/balances", http=http)


def place_order(*, base: str, quote: str, side: str, volume: str,
                order_type: str = "market", limit_price: str | None = None,
                validate_only: bool = False, http: httpx.Client | None = None) -> dict[str, Any]:
    """Place (or validate_only=True dry-run) an order through the platform.
    Requires an execute-mode connection; a view-only connection is refused
    upstream."""
    body: dict[str, Any] = {
        "base": base, "quote": quote, "side": side,
        "order_type": order_type, "volume": volume, "validate_only": validate_only,
    }
    if limit_price is not None:
        body["limit_price"] = limit_price
    return _request("POST", "/orders", json_body=body, http=http)


def open_orders(*, http: httpx.Client | None = None) -> dict[str, Any]:
    return _request("GET", "/orders", http=http)
