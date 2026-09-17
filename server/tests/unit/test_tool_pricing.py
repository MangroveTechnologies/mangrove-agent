"""Price discovery must stay bounded, anonymous, non-paying and honest."""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from src.config import app_config
from src.services import tool_pricing as pricing

_real_catalog_endpoint = pricing._catalog_endpoint


def envelope(entries=None, *, network="eip155:84532", enabled=True, cursor=None, page=1):
    result = {
        "tools": [],
        "_meta": {"mangrove/pricing": {
            "version": 1, "currency": "USDC", "network": network, "enabled": enabled,
            "entries": entries if entries is not None else [
                {"id": "rest:signals_list", "mangrove/x402_price_usd": "0.001"},
                {"id": "rest:signals_get", "mangrove/x402_price_usd": "0.001"},
            ],
        }},
    }
    if cursor is not None:
        result["nextCursor"] = cursor
    return {"jsonrpc": "2.0", "id": page, "result": result}


@pytest.fixture
def mock_http(monkeypatch):
    original = httpx.Client

    def install(handler):
        def factory(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            assert "auth" not in kwargs
            return original(transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(pricing.httpx, "Client", factory)
    return install


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(pricing, "_cache", pricing.PriceCache())
    monkeypatch.setattr(pricing, "_catalog_endpoint", lambda: "https://upstream.example/mcp/")


def test_anonymous_discovery_does_not_use_sdk_or_wallet(mock_http, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Discovery touched the SDK or wallet")
    monkeypatch.setattr("src.shared.clients.mangrove.mangrove_ai_client", forbidden)
    monkeypatch.setattr("src.services.x402_payer.resolve_payer_wallet", forbidden)
    monkeypatch.setenv("MANGROVE_API_KEY", "ambient-secret")
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "configured-secret")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://upstream.example/mcp/"
        assert request.method == "POST"
        assert not any(h in request.headers for h in ("authorization", "x-api-key", "payment-signature", "x-wallet-address"))
        assert json.loads(request.content)["method"] == "tools/list"
        return httpx.Response(200, json=envelope())

    mock_http(handler)
    rows = [{"name": "list_signals"}, {"name": "get_signal"}, {"name": "list_wallets"}]
    result = pricing._enrich(rows)
    assert result[0]["price"] == "$0.001 USDC per upstream request"
    assert result[0]["pricing"]["variable_total"] is True
    assert result[1]["pricing"]["variable_total"] is False
    assert result[2] == rows[2]
    assert "pricing" not in rows[0]
    pricing._enrich(rows)
    assert len(requests) == 1


@pytest.mark.parametrize("price", [None, True, 0.01, "NaN", "Infinity", "-1", "1e100000", "0.0000001", "<script>", pytest.param("1" * 10000, id="oversized-price")])
def test_untrusted_prices_never_become_zero_or_descriptions(mock_http, price):
    mock_http(lambda r: httpx.Response(200, json=envelope([
        {"id": "rest:signals_get", "mangrove/x402_price_usd": price},
    ])))
    result = pricing._enrich([{"name": "get_signal", "price": "old"}])[0]
    assert "price" not in result
    assert result["pricing"]["status"] == "unavailable"
    assert "do not assume" in pricing.price_hint(result["pricing"])


def test_explicit_zero_is_preserved_but_missing_price_is_unknown(mock_http):
    mock_http(lambda r: httpx.Response(200, json=envelope([
        {"id": "proxy:api_calls", "mangrove/x402_price_usd": "0.00"},
    ])))
    result = pricing._enrich([{"name": "oracle_get_experiment"}, {"name": "kb_search"}])
    assert result[0]["pricing"]["components"][0]["price_usd"] == "0"
    assert result[1]["pricing"]["status"] == "unavailable"
    assert "price" not in result[1]


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500])
def test_failures_never_retry_pay_or_forward_errors(mock_http, status):
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(status, headers={"Location": "https://evil.example/"}, text="secret-upstream-error")
    mock_http(handler)
    for _ in range(3):
        result = pricing._enrich([{"name": "get_signal"}])
        assert result[0]["pricing"]["status"] == "unavailable"
        assert "secret" not in json.dumps(result)
    assert len(calls) == 1


def test_old_server_catalog_degrades_without_guessing(mock_http):
    mock_http(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}))
    assert pricing._enrich([{"name": "get_signal"}])[0]["pricing"]["status"] == "unavailable"


def test_cache_refresh_price_change_stale_expiry_and_recovery(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pricing.time, "monotonic", lambda: now[0])
    calls = []
    def fetch(endpoint):
        calls.append(endpoint)
        return {"rest:signals_get": "0.123" if len(calls) > 1 else "0.001"}, "eip155:84532", True
    monkeypatch.setattr(pricing, "_fetch_prices", fetch)
    cache = pricing.PriceCache()
    first, state = cache.read("a")
    assert state == "fresh"
    now[0] += pricing._TTL - 1
    assert cache.read("a")[0] is first
    now[0] += 2
    updated, state = cache.read("a")
    assert updated.prices["rest:signals_get"] == "0.123"
    assert state == "fresh" and len(calls) == 2
    def fail(endpoint):
        raise httpx.ConnectError("secret")
    monkeypatch.setattr(pricing, "_fetch_prices", fail)
    now[0] += pricing._TTL + 1
    assert cache.read("a") == (updated, "stale")
    assert cache.read("b") == (None, "unavailable")  # no cross-origin stale data
    now[0] += pricing._MAX_STALE
    assert cache.read("a") == (None, "unavailable")
    monkeypatch.setattr(pricing, "_fetch_prices", fetch)
    now[0] += pricing._RETRY_AFTER + 1
    assert cache.read("a")[1] == "fresh"


def test_concurrent_cold_read_refreshes_once_and_does_not_queue(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(endpoint):
        calls.append(endpoint)
        entered.set()
        assert release.wait(5)
        return {}, "eip155:84532", True
    monkeypatch.setattr(pricing, "_fetch_prices", fetch)
    cache = pricing.PriceCache()
    with ThreadPoolExecutor(max_workers=8) as pool:
        winner = pool.submit(cache.read, "a")
        assert entered.wait(5)
        try:
            others = list(pool.map(cache.read, ["a"] * 20))
            assert all(item == (None, "unavailable") for item in others)
        finally:
            release.set()
        assert winner.result()[1] == "fresh"
    assert len(calls) == 1


def test_pagination_collects_pages_and_bounds_cursor_cycles(mock_http):
    def handler(request):
        data = json.loads(request.content)
        if data["id"] == 1:
            return httpx.Response(200, json=envelope(cursor="next"))
        assert data["params"]["cursor"] == "next"
        return httpx.Response(200, json=envelope([
            {"id": "skill:crypto_ohlcv", "mangrove/x402_price_usd": "0.05"},
        ], page=2))
    mock_http(handler)
    prices, _, _ = pricing._fetch_prices("https://upstream.example/mcp/")
    assert prices["skill:crypto_ohlcv"] == "0.05"
    mock_http(lambda r: httpx.Response(200, json=envelope([], cursor="loop", page=json.loads(r.content)["id"])))
    with pytest.raises(ValueError, match="cursor"):
        pricing._fetch_prices("https://upstream.example/mcp/")


@pytest.mark.parametrize("case", ["duplicate", "oversized", "rpc_error", "bad_id", "bad_network", "compressed", "invalid_json", "deadline"])
def test_malformed_or_unbounded_response_is_unavailable(mock_http, monkeypatch, case):
    body = envelope()
    headers = {}
    if case == "duplicate":
        body["result"]["_meta"]["mangrove/pricing"]["entries"] *= 2
    elif case == "oversized":
        monkeypatch.setattr(pricing, "_MAX_BYTES", 10)
    elif case == "rpc_error":
        body["error"] = {"message": "secret"}
    elif case == "bad_id":
        body["id"] = True
    elif case == "bad_network":
        body = envelope(network="<script>")
    elif case == "compressed":
        # Unsupported encoding avoids even attempting decompression.
        headers["Content-Encoding"] = "unknown"
    elif case == "deadline":
        monkeypatch.setattr(pricing, "_DEADLINE", -1)
    mock_http(lambda r: httpx.Response(200, content=b"{" if case == "invalid_json" else json.dumps(body).encode(), headers=headers))
    assert pricing._enrich([{"name": "get_signal"}])[0]["pricing"]["status"] == "unavailable"


def test_network_and_disabled_payment_are_reported_without_config_changes(mock_http):
    configured = app_config.X402_NETWORK
    mock_http(lambda r: httpx.Response(200, json=envelope(network="eip155:1", enabled=False)))
    item = pricing._enrich([{"name": "get_signal"}])[0]
    assert item["network"] == "eip155:1"
    assert not item["pricing"]["network_matches_configuration"]
    assert "disabled" in pricing.price_hint(item["pricing"])
    assert app_config.X402_NETWORK == configured


def test_composite_does_not_advertise_a_total_and_stale_hint_is_explicit(monkeypatch):
    snap = pricing.Snapshot("x", {"rest:backtest_get": "0.001", "skill:crypto_ohlcv": "0.02"},
                            "eip155:84532", True, "2026-09-17T00:00:00Z", 0)
    monkeypatch.setattr(pricing._cache, "read", lambda _: (snap, "stale"))
    item = pricing._enrich([{"name": "get_backtest"}])[0]
    assert "price" not in item
    assert item["pricing"]["variable_total"]
    assert "not a total quote" in pricing.price_hint(item["pricing"])
    assert "stale" in pricing.price_hint(item["pricing"])
    monkeypatch.setattr(pricing._cache, "read", lambda _: (replace(snap, prices={}), "fresh"))
    assert pricing._enrich([{"name": "get_backtest"}])[0]["pricing"]["status"] == "unavailable"


async def test_fetch_does_not_block_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(endpoint):
        entered.set()
        assert release.wait(5)
        return {}, "eip155:84532", True
    monkeypatch.setattr(pricing, "_fetch_prices", fetch)
    task = asyncio.create_task(pricing.enrich_tools([{"name": "get_signal"}]))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        assert not task.done()
    finally:
        release.set()
    await task


@pytest.mark.parametrize(("base", "expected"), [
    ("https://custom.example/api/v1", "https://custom.example/mcp/"),
    ("https://custom.example/prefix/api/v1/", "https://custom.example/prefix/mcp/"),
    ("http://localhost:5002/api/v1", "http://localhost:5002/mcp/"),
    ("http://[::1]:5002/api/v1", "http://[::1]:5002/mcp/"),
])
def test_destination_matches_explicit_payment_origin(monkeypatch, base, expected):
    monkeypatch.setattr("src.shared.clients.mangrove._payment_destination", lambda config: {"base_url": base})
    assert _real_catalog_endpoint() == expected


@pytest.mark.parametrize("base", [
    "http://remote.example/api/v1", "https://user:secret@host/api/v1",
    "https://host/api/v1?token=secret", "https://host/api/v1#fragment",
    "https://host/not-the-api", "file:///api/v1",
])
def test_invalid_destinations_cannot_send_discovery_requests(monkeypatch, base):
    monkeypatch.setattr("src.shared.clients.mangrove._payment_destination", lambda config: {"base_url": base})
    with pytest.raises(ValueError):
        _real_catalog_endpoint()


def test_slow_trickle_cannot_evade_deadline(mock_http, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pricing.time, "monotonic", lambda: now[0])

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                now[0] += 1
                yield b" "

    mock_http(lambda r: httpx.Response(200, stream=Trickle()))
    with pytest.raises(ValueError, match="bounds"):
        pricing._fetch_prices("https://upstream.example/mcp/")
    assert now[0] == 108.0


def test_too_many_pages_and_entries_are_bounded(mock_http, monkeypatch):
    def handler(request):
        page = json.loads(request.content)["id"]
        return httpx.Response(200, json=envelope([], cursor=str(page), page=page))
    mock_http(handler)
    with pytest.raises(ValueError, match="Too many"):
        pricing._fetch_prices("https://upstream.example/mcp/")
    monkeypatch.setattr(pricing, "_MAX_ENTRIES", 1)
    mock_http(lambda r: httpx.Response(200, json=envelope()))
    with pytest.raises(ValueError, match="entries"):
        pricing._fetch_prices("https://upstream.example/mcp/")
