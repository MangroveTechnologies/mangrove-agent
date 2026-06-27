"""CEX (Kraken) BYOK service.

Flow:
  1. `connect_from_vault(vault_token)` — reveal the {api_key, api_secret} blob
     the user stashed out-of-band (scripts/stash-kraken-secret.sh) and persist
     it encrypted (cex_credentials). The key never enters chat.
  2. Build a SDK `KrakenClient` from the stored creds and talk to Kraken
     DIRECTLY (BYOK) — balances, validate-only orders.
  3. `sync_fills()` — pull the user's Kraken fills and EMIT them to the markets
     server's telemetry via the SDK (authed by the Mangrove key). The Kraken
     key is never sent to a Mangrove server; only the trade statistics are.

`client_factory` / `telemetry` are injectable for tests + the local mock-Kraken
E2E (no real key needed until the user connects one).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from mangrove_markets import KrakenClient

from src.config import app_config
from src.services import cex_credentials
from src.services.secret_vault import vault
from src.shared.clients.mangrove import mangrove_markets_client

_VENUE = "kraken"

ClientFactory = Callable[[str, str], Any]


def _kraken_base() -> str:
    return str(getattr(app_config, "KRAKEN_API_URL", "https://api.kraken.com"))


def _client(client_factory: ClientFactory | None = None) -> Any:
    creds = cex_credentials.load(_VENUE)
    if not creds:
        raise RuntimeError(
            "No Kraken credentials connected. Run scripts/stash-kraken-secret.sh "
            "in a terminal, then connect with the returned vault_token."
        )
    api_key, api_secret = creds
    if client_factory is not None:
        return client_factory(api_key, api_secret)
    return KrakenClient(api_key, api_secret, base_url=_kraken_base())


def connect_from_vault(vault_token: str) -> dict:
    """Reveal the stashed Kraken creds blob (single-read) and persist encrypted."""
    blob = vault.reveal(vault_token)
    data = json.loads(blob)
    cex_credentials.save(_VENUE, data["api_key"], data["api_secret"])
    return {"venue": _VENUE, "connected": True}


def status() -> dict:
    return {"venue": _VENUE, "connected": cex_credentials.is_connected(_VENUE)}


def disconnect() -> dict:
    return {"venue": _VENUE, "disconnected": cex_credentials.disconnect(_VENUE)}


def get_balances(*, client_factory: ClientFactory | None = None) -> dict:
    return _client(client_factory).balance()


def validate_order(
    *,
    pair: str,
    side: str,
    volume: float,
    ordertype: str = "market",
    price: float | None = None,
    client_factory: ClientFactory | None = None,
) -> dict:
    """Dry-run an order (Kraken AddOrder validate=true). No fill."""
    return _client(client_factory).add_order(
        pair=pair, side=side, ordertype=ordertype, volume=volume,
        price=price, validate=True,
    )


def sync_fills(
    *,
    mode: str = "live",
    client_factory: ClientFactory | None = None,
    telemetry: Any | None = None,
) -> dict:
    """Pull the user's Kraken fills, map to TradeRecords, emit to telemetry.

    Sends the trade STATISTICS to the markets server (authed by the Mangrove
    key); the Kraken key stays local. Returns what was emitted.
    """
    records = _client(client_factory).trades_as_records(mode=mode)
    tel = telemetry if telemetry is not None else mangrove_markets_client().telemetry
    results = tel.report_trades(records)
    return {"emitted": len(results), "trade_ids": [r.id for r in records]}
