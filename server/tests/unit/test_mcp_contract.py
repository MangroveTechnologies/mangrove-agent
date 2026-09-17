"""Contract guards must reject real drift, not merely match a golden file."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("mcp_contract_checks", ROOT / "scripts/mcp_contract.py")
checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checks)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    from src.mcp import registry
    monkeypatch.setattr(registry, "_tools", [])


def example():
    return ([{"name": "read", "inputSchema": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["symbol"]}}],
        [{"name": "read", "access": "auth", "parameters": [
            {"name": "symbol", "type": "string", "required": True},
            {"name": "limit", "type": "integer", "required": False}]}], ["read"])


@pytest.mark.parametrize("mutation", ["runtime_missing", "catalog_missing", "runtime_duplicate",
    "catalog_duplicate", "registration_duplicate", "parameter_missing", "parameter_duplicate",
    "required", "type", "access"])
def test_registry_rejects_injected_drift(mutation):
    runtime, catalog, attempts = example()
    if mutation == "runtime_missing":
        runtime.clear()
    elif mutation == "catalog_missing":
        catalog.clear()
    elif mutation == "runtime_duplicate":
        runtime.append(copy.deepcopy(runtime[0]))
    elif mutation == "catalog_duplicate":
        catalog.append(copy.deepcopy(catalog[0]))
    elif mutation == "registration_duplicate":
        attempts.append("read")  # final runtime dictionary has already deduplicated it
    elif mutation == "parameter_missing":
        catalog[0]["parameters"].pop()
    elif mutation == "parameter_duplicate":
        catalog[0]["parameters"].append(copy.deepcopy(catalog[0]["parameters"][0]))
    elif mutation == "required":
        catalog[0]["parameters"][1]["required"] = True
    elif mutation == "type":
        catalog[0]["parameters"][1]["type"] = "string"
    elif mutation == "access":
        catalog[0]["access"] = "typo"
    assert checks.check_registry(runtime, catalog, attempts)


def test_schema_preserves_nested_validation_defaults_and_named_properties():
    schema = {"type": "object", "title": "Presentation", "properties": {
        "description": {"type": "string", "default": "original"},
        "rows": {"type": "array", "items": {"type": "object", "properties": {
            "value": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]}},
            "required": ["value"]}}}}
    normalized = checks.schema_contract(schema)
    assert "title" not in normalized
    assert "description" in normalized["properties"]
    old = {"version": 1, "tools": {"read": {"inputSchema": normalized}}, "bindings": {}}
    changed_schema = copy.deepcopy(schema)
    changed_schema["properties"]["rows"]["items"]["properties"]["value"]["anyOf"][0]["minimum"] = 2
    current = copy.deepcopy(old)
    current["tools"]["read"]["inputSchema"] = checks.schema_contract(changed_schema)
    assert checks.compare_local(current, old)
    changed = copy.deepcopy(old)
    changed["tools"]["read"]["inputSchema"]["properties"]["description"]["default"] = "new"
    assert checks.compare_local(changed, old)


def test_optional_nullable_parameters_are_not_required():
    runtime, catalog, attempts = example()
    runtime[0]["inputSchema"]["properties"]["limit"] = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
    assert not checks.check_registry(runtime, catalog, attempts)


def test_binding_classifications_and_hidden_rest_meter():
    bindings = {"read": SimpleNamespace(meters=("rest:hidden_read",)), "kb": SimpleNamespace(meters=())}
    policy = {"without_upstream_pricing": {"wallet": "Local custody"}, "unknown_pricing": {"kb": "Separate KB origin"}}
    upstream = {"tools": {}, "billing_ids": ["rest:hidden_read"]}
    assert not checks.check_bindings(bindings, {"read", "kb", "wallet"}, policy, upstream)
    assert checks.check_bindings(bindings, {"read", "kb", "wallet", "new"}, policy, upstream)
    assert checks.check_bindings(bindings, {"read", "kb", "wallet"}, policy, {"billing_ids": ["skill:hidden_read"]})
    assert checks.check_bindings(bindings, {"kb", "wallet"}, policy, upstream)
    policy["unknown_pricing"] = {}
    assert checks.check_bindings(bindings, {"read", "kb", "wallet"}, policy, upstream)


SOURCE = '''
from src.shared.clients.mangrove import mangrove_ai_client as backend
@server.tool()
async def read(symbol: str):
    return backend().crypto_assets.get_market_data(symbol)
'''


def test_direct_sdk_exception_ratchet_detects_changes_additions_and_removal():
    actual = checks.direct_sdk_tools(SOURCE)
    exceptions = {"read": {"sha256": actual["read"], "reason": "Existing adapter pending shared service"}}
    assert not checks.check_delegation(SOURCE, exceptions)
    assert checks.check_delegation(SOURCE.replace('symbol)', '"ETH")'), exceptions)
    assert checks.check_delegation(SOURCE.replace("def read", "def added"), exceptions)
    assert checks.check_delegation("", exceptions)
    assert checks.check_delegation(SOURCE, {})


def test_shared_service_and_route_delegation_need_no_exception():
    source = '''
@server.tool()
async def read(symbol):
    from src.services.market import snapshot
    return snapshot(symbol)
@server.tool()
async def other(symbol):
    from src.api.routes.market import market_data as route
    return await route(symbol)
'''
    assert checks.direct_sdk_tools(source) == {}


def response_payload(entries=None, tools=None):
    return {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools if tools is not None else [
        {"name": "read", "description": "not copied", "inputSchema": {"type": "object", "properties": {}}}],
        "_meta": {"mangrove/pricing": {"version": 1, "currency": "USDC", "entries": entries if entries is not None else [
            {"id": "rest:read", "mangrove/x402_price_usd": "0.005"}]}}}}


def test_snapshot_has_no_prices_or_descriptions_and_namespaces_are_preserved():
    snap = checks.normalize_upstream(response_payload()["result"])
    assert snap == {"version": 1, "tools": {"read": {"type": "object", "properties": {}}}, "billing_ids": ["rest:read"]}
    assert "0.005" not in json.dumps(snap)
    assert "not copied" not in json.dumps(snap)


@pytest.mark.parametrize("kind", ["duplicate_tool", "duplicate_id", "bad_id", "missing_metadata", "bad_schema"])
def test_malformed_upstream_fails(kind):
    result = response_payload()["result"]
    if kind == "duplicate_tool":
        result["tools"] *= 2
    elif kind == "duplicate_id":
        result["_meta"]["mangrove/pricing"]["entries"] *= 2
    elif kind == "bad_id":
        result["_meta"]["mangrove/pricing"]["entries"][0]["id"] = "https://private.invalid"
    elif kind == "missing_metadata":
        result.pop("_meta")
    elif kind == "bad_schema":
        result["tools"][0]["inputSchema"] = "bad"
    with pytest.raises(ValueError):
        checks.normalize_upstream(result)


def test_upstream_compares_its_own_schema_not_wrapper_schema():
    old = checks.normalize_upstream(response_payload()["result"])
    bindings = {"read": SimpleNamespace(meters=("rest:read",))}
    current = copy.deepcopy(old)
    current["tools"]["backend_only"] = {"type": "object"}
    assert not checks.compare_upstream(current, old, {"read", "wallet"}, bindings)
    current["tools"]["read"] = {"type": "object", "required": ["new_argument"]}
    assert checks.compare_upstream(current, old, {"read", "wallet"}, bindings)
    current = copy.deepcopy(old)
    current["billing_ids"] = []
    assert checks.compare_upstream(current, old, {"read", "wallet"}, bindings)


def install_http(monkeypatch, handler):
    original = httpx.Client
    def factory(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False}
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", factory)


def test_live_fetch_is_anonymous_and_does_not_retry_payment(monkeypatch):
    seen = []
    def handler(req):
        seen.append(req)
        assert not any(h in req.headers for h in ("authorization", "x-api-key", "payment-signature", "x-wallet-address"))
        assert json.loads(req.content)["method"] == "tools/list"
        return httpx.Response(402, json={"private": "do not print"})
    install_http(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        checks.fetch_upstream("https://upstream.example/mcp/")
    assert len(seen) == 1


@pytest.mark.parametrize("url", ["http://remote.example/mcp/", "https://user:password@example.com/mcp/",
    "https://example.com/mcp/?key=value", "file:///etc/passwd", "https://example.com/#fragment"])
def test_unsafe_catalog_destination_rejected_before_request(monkeypatch, url):
    client = Mock(side_effect=AssertionError("Must not construct HTTP client"))
    monkeypatch.setattr(httpx, "Client", client)
    with pytest.raises(ValueError):
        checks.fetch_upstream(url)
    client.assert_not_called()


@pytest.mark.parametrize("mode", ["redirect", "compressed", "oversize", "bad_rpc_id", "cursor_cycle", "duplicate_page"])
def test_live_fetch_rejects_unbounded_or_ambiguous_catalogs(monkeypatch, mode):
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        body = response_payload()
        body["id"] = count
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example"})
        if mode == "compressed":
            return httpx.Response(200, headers={"Content-Encoding": "unsupported"}, content=b"x")
        if mode == "oversize":
            return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))
        if mode == "bad_rpc_id":
            body["id"] = True
        if mode in {"cursor_cycle", "duplicate_page"}:
            body["result"]["nextCursor"] = "same"
            if mode == "cursor_cycle":
                body["result"]["tools"][0]["name"] = f"read_{count}"
        return httpx.Response(200, json=body)
    install_http(monkeypatch, handler)
    with pytest.raises((ValueError, httpx.HTTPError)):
        checks.fetch_upstream("https://upstream.example/mcp/")
    assert count <= 2


def test_offline_cli_covers_real_registration_and_committed_baselines():
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/mcp_contract.py")],
                            cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(checks.read_json('local.json')['tools'])} tools" in result.stdout
    assert "Offline check only" in result.stdout


@pytest.mark.parametrize("event,args", [
    ("socket.connect", ()), ("sqlite3.connect", ()), ("subprocess.Popen", ()),
    ("open", ("ignored", "w", 0)), ("os.mkdir", ()), ("os.remove", ()),
])
def test_collection_guard_rejects_io(event, args):
    with pytest.raises(RuntimeError):
        checks.deny_runtime_io(event, args)


@pytest.mark.parametrize("tool_name,route_name,method,extra", [
    ("get_market_data", "market_data", "get_market_data", {}),
    ("get_ohlcv", "ohlcv", "get_ohlcv", {"lookback_days": 12}),
])
async def test_market_tools_share_safe_errors_auth_and_arguments(monkeypatch, tool_name, route_name, method, extra):
    from mcp.server.fastmcp import FastMCP
    from src.api.routes import market
    from src.mcp import tools
    from src.shared.errors import SdkError, X402PaymentError
    server = FastMCP("market-contract")
    tools._register_market(server)
    fn = server._tool_manager._tools[tool_name]
    sdk_method = Mock(return_value={"symbol": "BTC"})
    client = SimpleNamespace(crypto_assets=SimpleNamespace(**{method: sdk_method}))
    monkeypatch.setattr(market, "mangrove_ai_client", lambda: client)
    denied = json.loads(await fn.run({"symbol": "BTC"}))
    assert denied["code"] == "AUTH_INVALID_API_KEY"
    sdk_method.assert_not_called()
    args = {"symbol": "BTC", "provider": "example", "api_key": "test-key-1", **extra}
    assert json.loads(await fn.run(args)) == {"symbol": "BTC"}
    expected = {"symbol": "BTC", "provider": "example"}
    if extra:
        expected["days"] = 12
    sdk_method.assert_called_once_with(**expected)
    sdk_method.side_effect = RuntimeError("SYNTHETIC_PRIVATE_DETAIL")
    result = json.loads(await fn.run(args))
    assert result["code"] == SdkError.code
    assert "SYNTHETIC_PRIVATE_DETAIL" not in json.dumps(result)
    sdk_method.side_effect = X402PaymentError("Safe payment error", correlation_id="correlation-test")
    result = json.loads(await fn.run(args))
    assert result["code"] == X402PaymentError.code
    assert result["correlation_id"] == "correlation-test"


def test_demo_catalog_follows_configured_network_in_both_registration_paths(monkeypatch):
    from src.config import app_config
    monkeypatch.setattr(app_config, "X402_NETWORK", "eip155:8453")
    for healthy in (False, True):
        runtime, catalog, attempts = checks.collect_runtime(healthy)
        assert not checks.check_registry(runtime, catalog, attempts)
        demo = next(row for row in catalog if row["name"] == "hello_mangrove")
        assert demo["network"] == "eip155:8453"
        assert "eip155:8453" in demo["description"]


@pytest.mark.parametrize("name", ["execute_swap", "x402_spend_reset"])
@pytest.mark.parametrize("confirm", [None, False])
async def test_optional_confirmation_still_refuses_execution(name, confirm):
    from mcp.server.fastmcp import FastMCP
    from src.mcp import tools
    server = FastMCP("confirmation-contract")
    tools._register_dex(server)
    tools._register_x402_spend(server)
    args = {"api_key": "test-key-1"}
    if name == "execute_swap":
        args.update(input_token="ETH", output_token="USDC", amount=1,
                    chain_id=84532, wallet_address="unused", slippage_pct=0.001)
    if confirm is not None:
        args["confirm"] = confirm
    result = json.loads(await server._tool_manager._tools[name].run(args))
    assert result["code"] == "CONFIRMATION_REQUIRED"


async def test_all_three_discovery_surfaces_preserve_registry_and_pricing(monkeypatch):
    import time

    from src.api.routes.discovery import tools as rest_tools
    from src.mcp import registry, tools
    from src.mcp.pricing_bindings import TOOL_PRICING
    from src.mcp.server import PricedFastMCP
    from src.services import tool_pricing

    monkeypatch.setattr("src.shared.x402.server._ensure_initialized", Mock(side_effect=ConnectionError("offline")))
    server = PricedFastMCP("discovery-contract")
    tools.register(server)
    before = copy.deepcopy(registry.list_tools())
    snapshot = tool_pricing.Snapshot("https://example.invalid/mcp/",
        {meter: "0.001" for b in TOOL_PRICING.values() for meter in b.meters},
        "eip155:84532", True, "synthetic-test", time.monotonic())
    monkeypatch.setattr(tool_pricing, "_catalog_endpoint", lambda: snapshot.endpoint)
    monkeypatch.setattr(tool_pricing._cache, "read", lambda _: (snapshot, "fresh"))
    # Any accidental real refresh is forbidden rather than depending on networking.
    monkeypatch.setattr(tool_pricing, "_fetch_prices", Mock(side_effect=AssertionError("No network")))
    rest = (await rest_tools())["tools"]
    tool_catalog = json.loads(await server._tool_manager._tools["list_tools"].run({}))["tools"]
    protocol = await server.list_tools()
    assert rest == tool_catalog
    assert {t.name for t in protocol} == {t["name"] for t in rest}
    rest_by_name = {row["name"]: row for row in rest}
    for tool in protocol:
        assert (tool.meta or {}).get("mangrove/pricing") == rest_by_name[tool.name].get("pricing")
    assert registry.list_tools() == before
    assert rest_by_name["kb_search"]["pricing"]["status"] == "unavailable"
    assert "pricing" not in rest_by_name["create_wallet"]


def test_fetch_pages_unions_global_billing_extension(monkeypatch):
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        body = response_payload()
        body["id"] = count
        body["result"]["tools"][0]["name"] = f"read_{count}"
        if count == 1:
            body["result"]["nextCursor"] = "second"
        else:
            assert json.loads(req.content)["params"]["cursor"] == "second"
        return httpx.Response(200, json=body)
    install_http(monkeypatch, handler)
    result = checks.fetch_upstream("https://upstream.example/mcp/")
    assert set(result["tools"]) == {"read_1", "read_2"}
    assert result["billing_ids"] == ["rest:read"]


def test_cli_failure_withholds_remote_exception_details():
    # Test the user-facing failure boundary in a child, without any HTTP access.
    code = f'''
import runpy, sys
sys.argv = [{str(ROOT / 'scripts/mcp_contract.py')!r}, '--upstream-url', 'https://private:secret@example.invalid/mcp/']
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "ValueError" in result.stderr
    assert "secret" not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr
