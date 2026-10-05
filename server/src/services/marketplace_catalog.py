"""Discover marketplace contracts only from the configured Markets MCP service."""
from __future__ import annotations

import json
import re

import anyio
import httpx
from jsonschema import Draft202012Validator
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from mcp import ClientSession
from src.config import app_config
from src.services.marketplace import MarketplaceError, _url
from src.shared.clients.mangrove import _api_key
from src.shared.errors import upstream_access_error
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics

CONTRACT_META = 'mangrove/marketplace'
LOCAL_TOOLS = frozenset({'marketplace_prepare', 'marketplace_submit'})


def contract(tool: Tool) -> dict:
    meta = (tool.meta or {}).get(CONTRACT_META)
    if (not isinstance(meta, dict) or type(meta.get('version')) is not int
            or meta['version'] != 1 or meta.get('mode') not in {'read', 'ownership'}):
        raise MarketplaceError('Markets tool contract is unavailable or incompatible.')
    if meta['mode'] == 'ownership' and meta.get('protocol') != 'ownership-v1':
        raise MarketplaceError('Markets signing protocol is not supported.')
    return meta


def validate_tool(tool: Tool) -> None:
    meta = contract(tool)
    schema = tool.inputSchema
    if (not re.fullmatch(r'marketplace_[a-z0-9_]{1,100}', tool.name)
            or tool.name in LOCAL_TOOLS or schema.get('type') != 'object'
            or not isinstance(schema.get('properties'), dict)
            or '_agent' in schema['properties']):
        raise MarketplaceError('Markets returned an invalid tool contract.')
    if meta['mode'] == 'ownership' and (
        not isinstance(meta.get('actor_field'), str)
        or meta['actor_field'] not in schema['properties']
    ):
        raise MarketplaceError('Markets ownership contract is incomplete.')
    if meta['mode'] == 'ownership':
        defaults = meta.get('wallet_defaults', {})
        constraints = meta.get('prepare_constraints', {})
        if not isinstance(defaults, dict) or not isinstance(constraints, dict):
            raise MarketplaceError('Markets ownership defaults are invalid.')
        if any(chain not in {'base', 'xrpl'} for chain in defaults):
            raise MarketplaceError('Markets ownership network is not supported.')
        for fields in [constraints, *defaults.values()]:
            if not isinstance(fields, dict) or any(
                field not in schema['properties'] or field in {'ownership_proof', '_agent', meta['actor_field']}
                for field in fields
            ):
                raise MarketplaceError('Markets ownership defaults are invalid.')
    serialized = json.dumps(tool.model_dump(by_alias=True), allow_nan=False)
    if len(serialized.encode()) > 65536:
        raise MarketplaceError('Markets tool contract exceeds the size limit.')
    pending = [(schema, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 32 or count > 4096:
            raise MarketplaceError('Markets tool schema exceeds the complexity limit.')
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'$id', '$dynamicRef', '$recursiveRef'}:
                    raise MarketplaceError('Dynamic schema references are not supported.')
                if key == '$ref' and (
                    not isinstance(item, str) or not item.startswith('#/')
                ):
                    raise MarketplaceError('External schema references are not supported.')
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise MarketplaceError('Markets returned an invalid tool schema.') from None


async def catalog() -> list[Tool]:
    protect_mcp_diagnostics()
    endpoint = _url(app_config.MANGROVEMARKETS_BASE_URL) + '/mcp/'
    key = _api_key(app_config)
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        with anyio.fail_after(8):
            async with httpx.AsyncClient(headers=headers, timeout=5, trust_env=False,
                                         follow_redirects=False) as http:
                async with streamable_http_client(endpoint, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        cursor = None
                        seen = set()
                        names = set()
                        result = []
                        for _ in range(10):
                            page = await session.list_tools(cursor=cursor)
                            if len(page.tools) > 256:
                                raise ValueError
                            for tool in page.tools:
                                if CONTRACT_META not in (tool.meta or {}):
                                    continue
                                validate_tool(tool)
                                if tool.name in names:
                                    raise ValueError
                                names.add(tool.name)
                                result.append(tool)
                                if len(result) > 64:
                                    raise ValueError
                            cursor = page.nextCursor
                            if not cursor:
                                return result
                            if cursor in seen:
                                raise ValueError
                            seen.add(cursor)
                        raise ValueError
    except Exception as error:
        pending = [error]
        while pending:
            child = pending.pop()
            access = upstream_access_error(child)
            if access is not None:
                raise access from None
            if isinstance(child, BaseExceptionGroup):
                pending.extend(child.exceptions)
        raise MarketplaceError('Markets tool discovery is unavailable or incompatible.') from None


async def get_tool(name: str) -> Tool:
    found = next((tool for tool in await catalog() if tool.name == name), None)
    if found is None:
        raise MarketplaceError('This tool is not advertised by the configured Markets server.')
    return found


def get_tool_sync(name: str) -> Tool:
    return anyio.run(get_tool, name)
