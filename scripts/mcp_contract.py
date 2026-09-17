"""B7 contract checks. Default execution is offline; no app lifespan is started.

Snapshots are review artifacts, not alternate tool implementations or price tables.
The receiver's own schemas are tracked separately from intentionally different
agent schemas. See docs/mcp-contracts.md for scope and refresh instructions.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "scripts" / "contracts"
METER = re.compile(r"(?:rest|skill|proxy):[a-z][a-z0-9_]{0,127}\Z")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}\Z")


def schema_contract(schema):
    """Ignore presentation text, preserving property names and validation rules."""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in {"title", "description", "examples", "$comment"}:
            continue
        if key in {"properties", "$defs", "definitions", "patternProperties"}:
            result[key] = {name: schema_contract(child) for name, child in value.items()}
        elif key in {"items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames"}:
            result[key] = schema_contract(value)
        elif key in {"anyOf", "allOf", "oneOf", "prefixItems"}:
            result[key] = [schema_contract(child) for child in value]
        elif key == "required":
            result[key] = sorted(value)
        else:
            result[key] = value
    return result


def unique(rows, label):
    names = [row["name"] for row in rows]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate {label}: {', '.join(duplicates)}")
    return {row["name"]: row for row in rows}


def parameter_types(schema):
    if "anyOf" in schema:
        return set().union(*(parameter_types(item) for item in schema["anyOf"]))
    value = schema.get("type")
    return set(value) if isinstance(value, list) else {value}


def check_registry(runtime, catalog, attempts):
    """Validate independent catalogs, before and after FastMCP deduplication."""
    errors = []
    try:
        live = unique(runtime, "runtime tools")
        manual = unique(catalog, "catalog tools")
        unique([{"name": name} for name in attempts], "registration attempts")
    except ValueError as exc:
        return [str(exc)]
    for name in sorted(live.keys() ^ manual.keys()):
        errors.append(f"{name}: missing from {'catalog' if name in live else 'runtime'}")
    if set(attempts) != set(live):
        errors.append("Registration attempts and runtime tool names disagree")
    for name in sorted(live.keys() & manual.keys()):
        entry = manual[name]
        if entry["access"] not in {"free", "auth", "x402"}:
            errors.append(f"{name}: invalid access tier")
        schema = live[name]["inputSchema"]
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        try:
            params = unique(entry["parameters"], f"{name} parameters")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for param in sorted(properties.keys() ^ params.keys()):
            errors.append(f"{name}.{param}: parameter missing from one surface")
        for param in sorted(properties.keys() & params.keys()):
            meta = params[param]
            if type(meta["required"]) is not bool or meta["required"] != (param in required):
                errors.append(f"{name}.{param}: required flag differs")
            if meta["type"] not in parameter_types(properties[param]):
                errors.append(f"{name}.{param}: type differs")
    return errors


def collect_runtime(healthy=False):
    """Register real tools with only facilitator discovery substituted.

    CLI calls this inside a process-wide I/O guard. Tests can use it with fixtures.
    We never use create_app(), DB initialization, scheduling or pricing discovery.
    """
    from types import SimpleNamespace

    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.tools.tool_manager import ToolManager
    from src.mcp import registry, tools
    from x402.schemas import PaymentRequirements

    attempts = []
    original = ToolManager.add_tool

    def record(manager, fn, *args, **kwargs):
        attempts.append(kwargs.get("name") or (args[0] if args else None) or fn.__name__)
        return original(manager, fn, *args, **kwargs)

    def requirements(config):
        return [PaymentRequirements(
            scheme="exact", network=config.network,
            asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e", amount="50000",
            pay_to="0x0000000000000000000000000000000000000001", max_timeout_seconds=300,
        )]

    init = {"return_value": SimpleNamespace(build_payment_requirements=requirements)} if healthy else {
        "side_effect": ConnectionError("offline contract check")
    }
    server = FastMCP("contract-check")
    with patch.object(ToolManager, "add_tool", record), patch("src.shared.x402.server._ensure_initialized", **init):
        tools.register(server)
    degraded = "facilitator was unreachable" in server._tool_manager._tools["hello_mangrove"].description
    if degraded == healthy:
        raise ValueError("Requested payment-wrapper registration path was not exercised")
    runtime = [tool.model_dump(by_alias=True) for tool in asyncio.run(server.list_tools())]
    return runtime, registry.list_tools(), attempts


def local_contract(runtime, catalog, bindings):
    return {
        "version": 1,
        "tools": {
            tool["name"]: {"inputSchema": schema_contract(tool["inputSchema"]),
                           "access": next(row["access"] for row in catalog if row["name"] == tool["name"])}
            for tool in runtime
        },
        "bindings": {name: {"meters": list(binding.meters), "variable": binding.variable}
                     for name, binding in sorted(bindings.items())},
    }


def compare_local(actual, expected):
    errors = []
    if expected.get("version") != 1:
        return ["Unsupported local contract version"]
    for group in ("tools", "bindings"):
        current, old = actual[group], expected[group]
        for name in sorted(current.keys() | old.keys()):
            if current.get(name) != old.get(name):
                errors.append(f"{group}.{name}: reviewed contract changed (review before refreshing)")
    return errors


def check_bindings(bindings, names, policy, upstream):
    errors = []
    non_upstream = policy["without_upstream_pricing"]
    unknown = policy["unknown_pricing"]
    if set(bindings) - names:
        errors.append("Pricing bindings name missing local tools: " + ", ".join(sorted(set(bindings) - names)))
    if set(non_upstream) & set(bindings):
        errors.append("Tools classified both upstream and outside upstream pricing")
    if names != set(bindings) | set(non_upstream):
        errors.append("Every tool needs an explicit upstream/non-upstream pricing classification")
    if set(unknown) != {name for name, binding in bindings.items() if not binding.meters}:
        errors.append("Unknown-price exceptions differ from empty bindings")
    if not all(isinstance(reason, str) and reason.strip() for reason in [*non_upstream.values(), *unknown.values()]):
        errors.append("Pricing exceptions require a reason")
    ids = set(upstream["billing_ids"])
    for name, binding in sorted(bindings.items()):
        if len(binding.meters) != len(set(binding.meters)):
            errors.append(f"{name}: repeated billing identifier")
        for meter in binding.meters:
            if not METER.fullmatch(meter) or meter not in ids:
                errors.append(f"{name}: unknown billing identifier {meter}")
    return errors


def direct_sdk_tools(source):
    """Conservative AST ratchet for existing MCP->SDK adapters.

    No regex/count-only check: record normalized function bodies, including
    validation, pagination and error handling, so changing an exception requires
    review. New direct SDK adapters fail even without a matching REST route.
    This is not a proof of arbitrary interprocedural business-logic equivalence.
    """
    tree = ast.parse(source)
    factories = {"mangrove_ai_client", "mangrove_markets_client"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.shared.clients.mangrove":
            factories.update(alias.asname or alias.name for alias in node.names)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [d for d in node.decorator_list if isinstance(d, ast.Call)
                      and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"]
        if not decorators:
            continue
        calls = [call for call in ast.walk(node) if isinstance(call, ast.Call)]
        direct = any((isinstance(call.func, ast.Name) and call.func.id in factories)
                     or (isinstance(call.func, ast.Attribute) and call.func.attr in factories)
                     for call in calls)
        # Catch newly introduced raw clients/transports or inline SDK imports too.
        direct |= any(isinstance(n, ast.ImportFrom) and n.module and n.module.startswith(
            ("mangrove_ai", "mangrove_markets", "httpx", "requests")) for n in ast.walk(node))
        direct |= any(isinstance(c.func, ast.Attribute) and ast.unparse(c.func).startswith(
            ("httpx.", "requests.", "mangrove_ai.", "mangrove_markets.")) for c in calls)
        if direct:
            name = next((kw.value.value for d in decorators for kw in d.keywords
                         if kw.arg == "name" and isinstance(kw.value, ast.Constant)), node.name)
            found[name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    return found


def check_delegation(source, exceptions):
    actual = direct_sdk_tools(source)
    errors = []
    for name in sorted(actual.keys() | exceptions.keys()):
        entry = exceptions.get(name, {})
        if name not in actual:
            errors.append(f"{name}: remove obsolete direct-SDK exception")
        elif not entry.get("reason") or entry.get("sha256") != actual[name]:
            errors.append(f"{name}: direct SDK adapter added/changed; share logic or review the scoped exception")
    return errors


def normalize_upstream(result):
    """Retain public schemas and billing IDs only. Never copy amounts or prompts."""
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        raise ValueError("Invalid upstream catalog")
    tools = result["tools"]
    if len(tools) > 4096:
        raise ValueError("Too many upstream tools")
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not NAME.fullmatch(tool["name"]) or not isinstance(tool.get("inputSchema"), dict)
                or tool["inputSchema"].get("type") != "object"):
            raise ValueError("Malformed upstream tool")
    by_name = unique(tools, "upstream tools")
    catalog = result.get("_meta", {}).get("mangrove/pricing", {})
    if (type(catalog.get("version")) is not int or catalog["version"] != 1
            or catalog.get("currency") != "USDC" or not isinstance(catalog.get("entries"), list)):
        raise ValueError("Upstream pricing identifiers unavailable")
    ids = []
    for entry in catalog["entries"]:
        key = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not METER.fullmatch(key):
            raise ValueError("Invalid upstream billing identifier")
        ids.append(key)
    if len(ids) > 4096 or len(ids) != len(set(ids)):
        raise ValueError("Duplicate or excessive upstream billing identifiers")
    return {"version": 1, "tools": {name: schema_contract(t["inputSchema"]) for name, t in sorted(by_name.items())},
            "billing_ids": sorted(ids)}


def fetch_upstream(url):
    """One bounded anonymous tools/list sequence; never use the payment SDK."""
    import time

    import httpx
    endpoint = httpx.URL(url)
    if (not endpoint.host or endpoint.userinfo or endpoint.query or endpoint.fragment
            or endpoint.scheme not in {"http", "https"}
            or (endpoint.scheme == "http" and endpoint.host not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use HTTPS or loopback HTTP, without credentials/query/fragment")
    deadline = time.monotonic() + 12
    size = 0
    cursor = None
    seen = set()
    merged = None
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        for page in range(8):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Catalog deadline exceeded")
            params = {"_meta": {"mangrove/include_pricing": True}}
            if cursor is not None:
                params["cursor"] = cursor
            with client.stream("POST", endpoint, json={"jsonrpc": "2.0", "id": page + 1,
                               "method": "tools/list", "params": params},
                               headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                               timeout=min(remaining, 3)) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed catalogs unsupported")
                chunks = []
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2 * 1024 * 1024 or time.monotonic() >= deadline:
                        raise ValueError("Catalog exceeds byte/time bounds")
                    chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            if (not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "error" in payload
                    or type(payload.get("id")) is not int or payload["id"] != page + 1):
                raise ValueError("Invalid JSON-RPC catalog response")
            result = payload.get("result")
            current = normalize_upstream(result)
            if merged is None:
                merged = current
            else:
                if merged["tools"].keys() & current["tools"].keys():
                    raise ValueError("Duplicate tool across catalog pages")
                merged["tools"].update(current["tools"])
                # The receiver may repeat the same complete pricing extension on
                # each page. Schema IDs, unlike amounts, are safe to union.
                merged["billing_ids"] = sorted(set(merged["billing_ids"]) | set(current["billing_ids"]))
            if len(merged["tools"]) > 4096 or len(merged["billing_ids"]) > 4096:
                raise ValueError("Catalog exceeds entry bounds")
            cursor = result.get("nextCursor")
            if cursor is None:
                merged["source"] = {"kind": "anonymous-tools-list", "endpoint": str(endpoint),
                                    "retrieved_at": datetime.now(timezone.utc).isoformat()}
                return merged
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen:
                raise ValueError("Invalid catalog cursor")
            seen.add(cursor)
    raise ValueError("Too many catalog pages")


def compare_upstream(current, previous, local_names, bindings):
    """Compare receiver schemas with receiver schemas, never with local wrappers."""
    errors = []
    for name in sorted(local_names & previous["tools"].keys()):
        if current["tools"].get(name) != previous["tools"][name]:
            errors.append(f"Upstream {name}: callable removed or schema changed; review adapter compatibility")
    for name, binding in bindings.items():
        missing = set(binding.meters) - set(current["billing_ids"])
        if missing:
            errors.append(f"Upstream {name}: missing billing IDs {', '.join(sorted(missing))}")
    return errors


def deny_runtime_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "sqlite3.connect", "subprocess.Popen", "os.system"}:
        raise RuntimeError("Contract collection prohibits network, databases and subprocesses")
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.chown",
                 "os.link", "os.symlink", "os.truncate", "os.utime"}:
        raise RuntimeError("Contract collection prohibits filesystem mutations")
    if event == "open":
        mode, flags = args[1:3]
        if ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))):
            raise RuntimeError("Contract collection prohibits file writes")


def read_json(name):
    return json.loads((CONTRACTS / name).read_text())


def write_json(name, value):
    (CONTRACTS / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-url", help="Opt-in anonymous live comparison; no payment calls")
    parser.add_argument("--refresh-upstream", action="store_true", help="Write a new receiver snapshot for review")
    parser.add_argument("--refresh-local", action="store_true", help="Write generated local contract for review")
    parser.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.collect:
        # Set test config BEFORE importing application modules. This only changes
        # this child process; no user configuration is read or rewritten.
        os.environ["ENVIRONMENT"] = "test"
        os.environ.pop("MANGROVE_AGENT_HOME", None)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(ROOT / "server"))
        sys.addaudithook(deny_runtime_io)
        # Registration's expected degraded warning must not corrupt JSON output.
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            degraded = collect_runtime()
            healthy = collect_runtime(True)
        from src.config import app_config
        from src.mcp.pricing_bindings import TOOL_PRICING
        errors = check_registry(*degraded) + check_registry(*healthy)
        actual = local_contract(*healthy[:2], TOOL_PRICING)
        if actual != local_contract(*degraded[:2], TOOL_PRICING):
            errors.append("Healthy and degraded registration schemas/access/bindings differ")
        for catalog in (healthy[1], degraded[1]):
            demo = next(row for row in catalog if row["name"] == "hello_mangrove")
            if demo.get("network") != app_config.X402_NETWORK:
                errors.append("hello_mangrove: advertised network differs from config")
        print(json.dumps({"contract": actual, "errors": errors}))
        return 0
    if args.refresh_upstream and not args.upstream_url:
        parser.error("--refresh-upstream requires --upstream-url")
    if args.refresh_local and args.refresh_upstream:
        parser.error("Refresh local and receiver snapshots separately for review")
    import subprocess
    child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--collect"],
                           capture_output=True, text=True, timeout=30)
    if child.returncode:
        print("FAIL: isolated registration failed; run --collect for local diagnostics", file=sys.stderr)
        return 1
    collected = json.loads(child.stdout)
    actual, errors = collected["contract"], collected["errors"]
    sys.path.insert(0, str(ROOT / "server"))
    from src.mcp.pricing_bindings import TOOL_PRICING
    policy = read_json("policy.json")
    upstream = read_json("upstream.json") if not args.refresh_upstream else fetch_upstream(args.upstream_url)
    if policy.get("version") != 1 or upstream.get("version") != 1:
        raise ValueError("Unsupported policy or receiver snapshot version")
    errors += check_bindings(TOOL_PRICING, set(actual["tools"]), policy, upstream)
    errors += check_delegation((ROOT / "server/src/mcp/tools.py").read_text(), policy["direct_sdk_adapters"])
    if not args.refresh_local:
        errors += compare_local(actual, read_json("local.json"))
    if args.upstream_url and not args.refresh_upstream:
        live = fetch_upstream(args.upstream_url)
        errors += compare_upstream(live, upstream, set(actual["tools"]), TOOL_PRICING)
    if errors:
        for error in sorted(set(errors)):
            print("FAIL:", error)
        return 1
    if args.refresh_local:
        write_json("local.json", actual)
        print("Wrote local contract; review the diff before committing.")
    elif args.refresh_upstream:
        write_json("upstream.json", upstream)
        print("Wrote price-free receiver contract; review the diff before committing.")
    else:
        print(f"PASS: {len(actual['tools'])} tools; registration, schemas, pricing bindings and adapter exceptions agree.")
        print("Live receiver compatibility checked." if args.upstream_url else "Offline check only; receiver availability/current deployment not asserted.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # URLs and upstream payloads can carry sensitive data. Report type only.
        print(f"FAIL: contract check unavailable ({type(exc).__name__}); no compatibility result", file=sys.stderr)
        raise SystemExit(1) from None
