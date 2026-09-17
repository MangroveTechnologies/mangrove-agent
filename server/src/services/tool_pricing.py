"""Anonymous, non-paying price discovery with a bounded, process-local cache.

This client never uses the SDK/payment transport, credentials or a wallet.
Refreshes happen on demand in a worker thread; concurrent readers use the
previous snapshot (or unavailable), rather than queuing upstream requests.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from starlette.concurrency import run_in_threadpool

from src.mcp.pricing_bindings import TOOL_PRICING

_TTL = 300.0
_RETRY_AFTER = 30.0
_MAX_STALE = 3600.0
_DEADLINE = 8.0
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 4096
_MAX_PAGES = 8
_PRICE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
_ID = re.compile(r"(?:rest|skill|proxy):[a-z][a-z0-9_]{0,127}\Z")


@dataclass(frozen=True)
class Snapshot:
    endpoint: str
    prices: dict[str, str]
    network: str
    enabled: bool
    fetched_at: str
    fetched_monotonic: float


class PriceCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._refresh = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._attempt_endpoint: str | None = None
        self._next_attempt = 0.0

    def read(self, endpoint: str) -> tuple[Snapshot | None, str]:
        now = time.monotonic()
        with self._lock:
            snapshot = self._snapshot
            if snapshot is not None and snapshot.endpoint != endpoint:
                snapshot = None
            age = now - snapshot.fetched_monotonic if snapshot else float("inf")
            if age < _TTL:
                return snapshot, "fresh"
            throttled = self._attempt_endpoint == endpoint and now < self._next_attempt
        if throttled or not self._refresh.acquire(blocking=False):
            return (snapshot, "stale") if age < _MAX_STALE else (None, "unavailable")
        try:
            # Another refresh may have finished between our read and acquire.
            with self._lock:
                current = self._snapshot
                if current and current.endpoint == endpoint and time.monotonic() - current.fetched_monotonic < _TTL:
                    return current, "fresh"
                if self._attempt_endpoint == endpoint and time.monotonic() < self._next_attempt:
                    return (snapshot, "stale") if age < _MAX_STALE else (None, "unavailable")
                self._attempt_endpoint = endpoint
                self._next_attempt = time.monotonic() + _RETRY_AFTER
            prices, network, enabled = _fetch_prices(endpoint)
            fresh = Snapshot(endpoint, prices, network, enabled,
                             datetime.now(timezone.utc).isoformat(), time.monotonic())
            with self._lock:
                self._snapshot = fresh
            return fresh, "fresh"
        except (httpx.HTTPError, ValueError, TypeError, KeyError, RecursionError):
            # Never return/log raw URLs, headers, payloads or dependency errors.
            age = time.monotonic() - snapshot.fetched_monotonic if snapshot else float("inf")
            return (snapshot, "stale") if age < _MAX_STALE else (None, "unavailable")
        finally:
            self._refresh.release()


_cache = PriceCache()


def _catalog_endpoint() -> str:
    from src.config import app_config
    from src.shared.clients.mangrove import _payment_destination

    # These are explicitly wallet-mode prices, including for API-key users.
    # Reuse the payment destination so custom/dev hosts never inherit prod prices.
    url = httpx.URL(_payment_destination(app_config)["base_url"])
    path = url.path.rstrip("/")
    if (not url.host or url.userinfo or url.query or url.fragment
            or not path.endswith("/api/v1")
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "::1"}
            ))):
        raise ValueError("Invalid discovery destination")
    return str(url.copy_with(path=path[:-len("/api/v1")] + "/mcp/"))


def _fetch_prices(endpoint: str) -> tuple[dict[str, str], str, bool]:
    deadline = time.monotonic() + _DEADLINE
    size = 0
    prices: dict[str, str] = {}
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = None
    identity = None
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        for page in range(_MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Discovery deadline exceeded")
            params = {"_meta": {"mangrove/include_pricing": True}}
            if cursor is not None:
                params["cursor"] = cursor
            with client.stream(
                "POST", endpoint,
                json={"jsonrpc": "2.0", "id": page + 1, "method": "tools/list", "params": params},
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                timeout=min(remaining, 3.0),
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed price catalogs are not supported")
                chunks = []
                # Observe every received chunk; coalescing into fixed-size
                # chunks would let a slow trickle evade the total deadline.
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_BYTES or time.monotonic() >= deadline:
                        raise ValueError("Discovery response exceeds bounds")
                    chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0"
                    or type(payload.get("id")) is not int or payload["id"] != page + 1
                    or "error" in payload):
                raise ValueError("Invalid discovery response")
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise ValueError("Invalid tool catalog")
            meta = result.get("_meta")
            catalog = meta.get("mangrove/pricing") if isinstance(meta, dict) else None
            if (not isinstance(catalog, dict) or type(catalog.get("version")) is not int
                    or catalog["version"] != 1 or catalog.get("currency") != "USDC"
                    or type(catalog.get("enabled")) is not bool
                    or not isinstance(catalog.get("network"), str)
                    or not re.fullmatch(r"eip155:[1-9][0-9]{0,19}", catalog["network"])):
                raise ValueError("Price metadata unavailable")
            page_identity = (catalog["network"], catalog["enabled"])
            if identity is not None and identity != page_identity:
                raise ValueError("Inconsistent catalog pages")
            identity = page_identity
            entries = catalog.get("entries")
            if not isinstance(entries, list) or len(seen_ids) + len(entries) > _MAX_ENTRIES:
                raise ValueError("Invalid pricing entries")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid pricing entry")
                key = entry.get("id")
                if not isinstance(key, str) or not _ID.fullmatch(key) or key in seen_ids:
                    raise ValueError("Invalid or duplicate price identity")
                seen_ids.add(key)
                price = entry.get("mangrove/x402_price_usd")
                # Invalid/ambiguous individual prices remain unknown. Never
                # coerce booleans, floats, negative values or huge exponents.
                if isinstance(price, str) and _PRICE.fullmatch(price):
                    prices[key] = format(Decimal(price).normalize(), "f")
            cursor = result.get("nextCursor")
            if cursor is None:
                return prices, identity[0], identity[1]
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen_cursors:
                raise ValueError("Invalid catalog cursor")
            seen_cursors.add(cursor)
    raise ValueError("Too many catalog pages")


def _enrich(entries: list[dict]) -> list[dict]:
    from src.config import app_config
    from src.shared.errors import AgentError

    try:
        snapshot, freshness = _cache.read(_catalog_endpoint())
    except (AgentError, httpx.InvalidURL, ValueError, TypeError, KeyError):
        snapshot, freshness = None, "unavailable"
    output = []
    for original in entries:
        entry = dict(original)
        binding = TOOL_PRICING.get(entry["name"])
        if binding is not None:
            # Do not let old registration-time hints masquerade as live prices.
            entry.pop("price", None)
            entry.pop("network", None)
            components = []
            for meter in binding.meters:
                component = {"meter": meter, "price_usd": snapshot.prices.get(meter) if snapshot else None}
                components.append(component)
            available = bool(components) and all(c["price_usd"] is not None for c in components)
            pricing = {
                "mode": "x402", "currency": "USDC", "basis": "per_upstream_request",
                "status": freshness if available else "unavailable",
                "variable_total": binding.variable or not available,
                "components": components,
                "fetched_at": snapshot.fetched_at if snapshot else None,
                "network": snapshot.network if snapshot else None,
                "payment_enabled": snapshot.enabled if snapshot else None,
                "network_matches_configuration": snapshot.network == app_config.X402_NETWORK if snapshot else None,
            }
            entry["pricing"] = pricing
            if available and len(components) == 1:
                entry["price"] = f"${components[0]['price_usd']} USDC per upstream request"
            if snapshot:
                entry["network"] = snapshot.network
        output.append(entry)
    return output


async def enrich_tools(entries: list[dict]) -> list[dict]:
    """Shared by REST discovery, the list_tools tool, and MCP tools/list."""
    return await run_in_threadpool(_enrich, entries)


def price_hint(pricing: dict) -> str:
    """Bounded plain-text hint for clients that only show tool descriptions."""
    if pricing["status"] == "unavailable":
        return "Wallet-payment price unavailable; do not assume this tool is free."
    amounts = ", ".join(f"{c['price_usd']} USDC" for c in pricing["components"])
    hint = f"Wallet-payment prices per upstream request: {amounts} on {pricing['network']}."
    if pricing["variable_total"]:
        hint += " Total varies with requests, pagination, polling and options; these are component prices, not a total quote."
    if pricing["status"] == "stale":
        hint += " Cached prices are stale."
    if not pricing["payment_enabled"]:
        hint += " Upstream wallet payments are disabled."
    if not pricing["network_matches_configuration"]:
        hint += " The advertised network differs from the configured payment network."
    return hint + " The actual payment quote and spend limits apply. API-key calls use existing quota billing."
