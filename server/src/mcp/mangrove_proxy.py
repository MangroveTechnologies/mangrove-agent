"""Discover and invoke MangroveAI tools through its authoritative MCP server."""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager

import anyio
import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool

from src.config import app_config
from src.shared.clients.mangrove import _api_key, _api_key_base_url, _payment_destination
from src.shared.errors import AgentError, SdkError, upstream_access_error
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics


def endpoint() -> str:
    key = _api_key(app_config)
    base = _api_key_base_url(app_config) if key else _payment_destination(app_config)['base_url']
    if base is None:
        base = _payment_destination(app_config)['base_url']
    url = httpx.URL(base)
    if (not url.host or url.userinfo or url.query or url.fragment
            or url.path.rstrip('/')[-7:] != '/api/v1'
            or not (url.scheme == 'https' or (url.scheme == 'http' and url.host in {'localhost', '127.0.0.1', '::1'}))):
        raise SdkError('Invalid configured MangroveAI MCP destination.')
    return str(url.copy_with(path=url.path.rstrip('/')[:-7] + '/mcp/'))


@asynccontextmanager
async def session(endpoint_url: str, key: str | None = None):
    protect_mcp_diagnostics()
    timeout = float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS)
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout, trust_env=False, follow_redirects=False) as http:
        async with streamable_http_client(endpoint_url, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client


def validate_tool(tool: Tool) -> None:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', tool.name):
        raise ValueError('Invalid tool name')
    if len(json.dumps(tool.model_dump(by_alias=True), allow_nan=False).encode()) > 256 * 1024:
        raise ValueError('Tool contract too large')
    pending = [(tool.inputSchema, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 16384 or depth > 48:
            raise ValueError('Tool schema too complex')
        if isinstance(item, dict):
            for key, value in item.items():
                if key in {'$id', '$dynamicRef', '$recursiveRef'} or (key == '$ref' and (not isinstance(value, str) or not value.startswith('#/'))):
                    raise ValueError('Only local schema references are supported')
                pending.append((value, depth + 1))
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
    Draft202012Validator.check_schema(tool.inputSchema)


async def catalog() -> list[Tool]:
    """Read current definitions; no local schemas or stale fallback catalogue."""
    try:
        with anyio.fail_after(15):
            async with session(endpoint()) as client:
                cursor = None
                cursors, names, result = set(), set(), []
                size = 0
                for _ in range(16):
                    page = await client.list_tools(cursor=cursor)
                    for tool in page.tools:
                        validate_tool(tool)
                        size += len(tool.model_dump_json())
                        if tool.name in names or len(result) >= 2048 or size > 8 * 1024 * 1024:
                            raise ValueError('Invalid catalogue bounds or duplicate tools')
                        names.add(tool.name)
                        result.append(tool)
                    cursor = page.nextCursor
                    if cursor is None:
                        return result
                    if cursor in cursors:
                        raise ValueError('Repeated catalogue cursor')
                    cursors.add(cursor)
                raise ValueError('Catalogue page limit exceeded')
    except AgentError:
        raise
    except Exception:
        raise SdkError('MangroveAI MCP tool discovery is unavailable. No local tool fallback was used.') from None


def failure(code: str, message: str) -> CallToolResult:
    body = {'error': True, 'code': code, 'message': message}
    return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])


async def call_tool(name: str, arguments: dict) -> CallToolResult:
    from src.mcp.tools import _require
    from src.services import x402_payer

    if not _require(''):
        return failure('AUTH_REQUIRED', 'Authenticate to the local agent using its X-API-Key header.')
    try:
        tools = await catalog()
        tool = next((item for item in tools if item.name == name), None)
        if tool is None:
            return failure('UNKNOWN_TOOL', 'Tool is not advertised by MangroveAI MCP.')
        if not isinstance(arguments, dict) or not Draft202012Validator(tool.inputSchema).is_valid(arguments):
            return failure('VALIDATION_ERROR', 'Arguments do not match the current MangroveAI MCP inputSchema. Refresh tools/list.')
        target, key = endpoint(), _api_key(app_config)
        if key is not None:
            with anyio.fail_after(float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS)):
                async with session(target, key) as client:
                    return await client.call_tool(name, arguments=arguments)
        result = await x402_payer.pay_remote_mcp(
            target, name=name, arguments=arguments, operation_id=str(uuid.uuid4()),
            timeout=float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS),
        )
        if result.mcp_result is None:
            return failure('UPSTREAM_ERROR', 'MangroveAI MCP returned no tool result. Check payment records before retrying.')
        return CallToolResult.model_validate(result.mcp_result)
    except AgentError as error:
        body = error.to_dict()
        return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])
    except Exception as error:
        pending = [error]
        while pending:
            nested = pending.pop()
            if isinstance(nested, BaseExceptionGroup):
                pending.extend(nested.exceptions)
                continue
            denied = upstream_access_error(nested)
            if denied is not None:
                body = denied.to_dict()
                return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])
        return failure('UPSTREAM_ERROR', 'MangroveAI MCP call could not be completed. No alternate transport was attempted.')
