"""Discover and invoke MangroveAI tools through its authoritative MCP server."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from contextlib import asynccontextmanager

import anyio
import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ClientRequest, TextContent, Tool
from referencing import Registry

from src.config import app_config
from src.services import mangrove_oauth as oauth
from src.shared.clients.mangrove import _api_key, _api_key_base_url, _payment_destination
from src.shared.errors import AgentError, SdkError, upstream_access_error
from src.shared.logging import get_logger
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics

_CATALOG_TTL_SECONDS = 60
_catalog_cache = None
_catalog_lock = anyio.Lock()
#: How long opening a shared session may take before the call fails.
_OPEN_TIMEOUT_SECONDS = 15
#: How long a failed open answers for its destination. Callers queued behind an open that
#: just failed get that failure at once instead of each waiting out a fresh open timeout.
_OPEN_FAILURE_HOLD_SECONDS = 5
_sessions: dict = {}
#: Guards only the dictionaries below -- never held across network I/O.
_sessions_lock = anyio.Lock()
#: One lock per (endpoint, credential): opening one destination never queues another.
_open_locks: dict = {}
#: (endpoint, credential) -> (monotonic time, error) of the last failed open.
_open_failures: dict = {}
_log = get_logger(__name__)


def endpoint() -> str:
    if oauth.enabled():
        return oauth.endpoint()
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


def _http_client(endpoint_url: str, key: str | None) -> httpx.AsyncClient:
    timeout = float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS)
    # Send the key BOTH ways. The receiver reads X-API-Key first and accepts it
    # whatever its shape; its Bearer branch only recognises a key by format, and
    # until MangroveAI#1362 ships it recognised a prefix nothing issues -- so a
    # Bearer-only client had every tools/call rejected with upstream 401 while
    # the same key worked as X-API-Key. Keeping Bearer too means this works
    # against receivers that read either header.
    if oauth.enabled() and endpoint_url == oauth.endpoint():
        async def authorize(request):
            if str(request.url) != oauth.endpoint():
                raise oauth.OAuthError('OAuth token destination mismatch.')
            token = await anyio.to_thread.run_sync(oauth.access_token)
            request.headers['Authorization'] = f'Bearer {token}'
        return httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False,
                                 event_hooks={'request': [authorize]})
    headers = {'X-API-Key': key, 'Authorization': f'Bearer {key}'} if key else {}
    hooks = {'request': [_sign_in_hook(endpoint_url)]} if key is None else {}
    return httpx.AsyncClient(headers=headers, timeout=timeout, trust_env=False,
                             follow_redirects=False, event_hooks=hooks)


def _sign_in_hook(endpoint_url: str):
    """Attach a fresh Sign-In-With-X proof to each keyless tool call.

    In wallet mode a free call otherwise runs as MangroveAI's shared anonymous identity,
    and the rate limit counts the caller by IP. With a proof, MangroveAI runs the call as
    this agent's payer wallet and counts it per wallet. Only ``tools/call`` carries one:
    the session's housekeeping messages need no identity, and each proof costs a
    signature and spends a nonce. No payer wallet configured means no proof: the call
    goes out as before, and a per-caller free tool answers that it needs one.
    """
    from src.shared.x402.siwx import SIWX_HEADER, build_siwx_header

    warned = False

    async def hook(request: httpx.Request) -> None:
        nonlocal warned
        if request.method != 'POST' or b'"tools/call"' not in request.content:
            return
        try:
            body = json.loads(request.content)
        except ValueError:
            return
        if not isinstance(body, dict) or body.get('method') != 'tools/call':
            return
        try:
            request.headers[SIWX_HEADER] = await anyio.to_thread.run_sync(build_siwx_header, endpoint_url)
        except Exception as error:  # noqa: BLE001 -- identity is optional; never block the call
            if not warned:
                warned = True
                _log.warning('mangrove.siwx.unavailable', error_type=type(error).__name__)
    return hook


class _SharedSession:
    """One MCP session per destination and credential, opened once and reused.

    The MCP specification has a client initialize a session once and send every later
    request on it. Opening a session per call cost three extra requests per call and,
    against MangroveAI's per-caller rate limit, ended builds early. A background task
    owns the transport -- its task group must outlive any one request -- and callers
    share the session it opened. A session that fails is dropped; the next call opens a
    fresh one. Nothing is retried here: a paid call's outcome stays with the payment
    ledger, which knows whether a signed payment left.
    """

    def __init__(self, endpoint_url: str, key: str | None):
        self.endpoint_url, self.key = endpoint_url, key
        self.client = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._lost = asyncio.Event()
        self._error: BaseException | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def race(self, call):
        """Await one call on the session, failing it at once if the session dies.

        A transport error -- an HTTP 401, 403 or 429 from MangroveAI -- is raised in the
        task that owns the session, not in the caller waiting for its response, which
        would otherwise wait out its whole timeout. The caller gets that same error, at
        the point it awaited the call: inside a payment, where a signed authorization is
        then reported as uncertain rather than lost.
        """
        pending = asyncio.ensure_future(call)
        lost = asyncio.ensure_future(self._lost.wait())
        try:
            done, _ = await asyncio.wait({pending, lost}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            pending.cancel()
            lost.cancel()
            raise
        if pending in done:
            lost.cancel()
            return pending.result()
        pending.cancel()
        try:
            await pending
        except BaseException:  # noqa: BLE001 -- the session's own error is the one to report
            pass
        raise self._error or SdkError('MangroveAI MCP session closed during the call.')

    @property
    def alive(self) -> bool:
        # A session belongs to the event loop that opened it; from any other loop it is
        # unusable however healthy its task looks.
        return (self.client is not None and self._task is not None and not self._task.done()
                and self._loop is asyncio.get_running_loop())

    async def open(self):
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), _OPEN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self.close()
            raise SdkError('MangroveAI MCP did not open a session in time.') from None
        except BaseException:
            # The opener was cancelled (its own deadline, a client disconnect): nobody
            # will ever hold this session, so its transport task must not outlive it.
            self._stop.set()
            self._task.cancel()
            raise
        if self.client is None:
            raise self._error or SdkError('MangroveAI MCP session could not be opened.')
        return self.client

    async def _run(self) -> None:
        protect_mcp_diagnostics()
        try:
            async with _http_client(self.endpoint_url, self.key) as http:
                async with streamable_http_client(self.endpoint_url, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        self.client = client
                        self._ready.set()
                        await self._stop.wait()
        except Exception as error:  # noqa: BLE001 -- recorded for the opener and callers
            self._error = error
        except BaseException:
            # Cancellation (close() gave up waiting) or interpreter shutdown. Callers still
            # waiting on this session get an ordinary error -- never a CancelledError that
            # is not theirs, which would skip their payment-uncertainty handling.
            self._error = SdkError('MangroveAI MCP session was closed.')
            raise
        finally:
            self.client = None
            self._ready.set()
            self._lost.set()

    async def close(self) -> None:
        if self._loop is not asyncio.get_running_loop():
            return  # its loop is gone, and its task with it
        self._stop.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()


class _GuardedClient:
    """The shared ClientSession, with every awaited call raced against the session dying."""

    def __init__(self, shared: _SharedSession):
        self._shared = shared

    def __getattr__(self, name):
        client = self._shared.client
        if client is None:
            raise self._shared._error or SdkError('MangroveAI MCP session is closed.')
        attribute = getattr(client, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute

        async def guarded(*args, **kwargs):
            return await self._shared.race(attribute(*args, **kwargs))
        return guarded


async def shared_session(endpoint_url: str, key: str | None = None):
    """The open session for this destination and credential, opening it if needed.

    Concurrent callers for one destination wait for a single open and share its session.
    The lock they wait on is that destination's own, so a slow or unreachable MangroveAI
    never delays another destination; and callers queued behind an open that just failed
    get that failure immediately rather than each spending a fresh open timeout.
    """
    slot = (endpoint_url, key)
    async with _sessions_lock:
        current = _sessions.get(slot)
        if current is not None and current.alive:
            return _GuardedClient(current)
        lock = _open_locks.setdefault(slot, anyio.Lock())
    async with lock:
        async with _sessions_lock:
            current = _sessions.get(slot)
            if current is not None and current.alive:
                return _GuardedClient(current)
            stale = _sessions.pop(slot, None)
            failed = _open_failures.get(slot)
        if stale is not None:
            await stale.close()
        if failed is not None and time.monotonic() - failed[0] < _OPEN_FAILURE_HOLD_SECONDS:
            raise failed[1].with_traceback(None)
        opened = _SharedSession(endpoint_url, key)
        try:
            await opened.open()
        except Exception as error:
            async with _sessions_lock:
                _open_failures[slot] = (time.monotonic(), error)
            raise
        async with _sessions_lock:
            _sessions[slot] = opened
            _open_failures.pop(slot, None)
        return _GuardedClient(opened)


async def drop_session(endpoint_url: str, key: str | None = None,
                       session: _SharedSession | None = None) -> None:
    """Forget a session that failed, so the next call opens a fresh one.

    With ``session``, only that session is dropped: a caller reporting the failure of the
    session it used must not close a newer one another caller has since opened.
    """
    async with _sessions_lock:
        current = _sessions.get((endpoint_url, key))
        if current is None or (session is not None and current is not session):
            current = None
        else:
            _sessions.pop((endpoint_url, key), None)
    if current is not None:
        await current.close()


async def close_sessions() -> None:
    """Close every shared session (application shutdown)."""
    async with _sessions_lock:
        current = list(_sessions.values())
        _sessions.clear()
        _open_failures.clear()
    for item in current:
        await item.close()


@asynccontextmanager
async def session(endpoint_url: str, key: str | None = None):
    """The shared session for one use.

    The session is shared by every concurrent caller, so one caller's failure says
    nothing about it unless the session itself died: a malformed catalogue entry, a tool
    output that fails its schema, a refusal or a caller's own deadline leave it serving
    everyone else's calls -- paid ones included. Only a dead session (its transport task
    ended) is dropped, so the next call opens a fresh one. A request abandoned mid-flight
    does not poison the session: responses are matched by request id, and an
    unclaimed one is discarded.
    """
    client = await shared_session(endpoint_url, key)
    try:
        yield client
    except BaseException:
        if not client._shared.alive:
            with anyio.CancelScope(shield=True):
                await drop_session(endpoint_url, key, client._shared)
        raise


def validate_tool(tool: Tool) -> None:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', tool.name):
        raise ValueError('Invalid tool name')
    if len(json.dumps(tool.model_dump(by_alias=True), allow_nan=False).encode()) > 256 * 1024:
        raise ValueError('Tool contract too large')
    pending = [(schema, 0) for schema in (tool.inputSchema, tool.outputSchema) if schema is not None]
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
    if tool.outputSchema is not None:
        Draft202012Validator.check_schema(tool.outputSchema)


async def _read_catalog(target: str) -> list[Tool]:
    try:
        with anyio.fail_after(15):
            async with session(target) as client:
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
    except Exception as error:
        known = transport_error(error)
        if known is not None:
            raise known from None
        raise SdkError('MangroveAI MCP tool discovery is unavailable. No local tool fallback was used.') from None



async def catalog(*, refresh: bool = True) -> list[Tool]:
    global _catalog_cache
    target = endpoint()
    async with _catalog_lock:
        cached = _catalog_cache
        if not refresh and cached and cached[0] == target and time.monotonic() < cached[1]:
            return [tool.model_copy(deep=True) for tool in cached[2]]
        _catalog_cache = None
        result = await _read_catalog(target)
        _catalog_cache = (target, time.monotonic() + _CATALOG_TTL_SECONDS, result)
        return [tool.model_copy(deep=True) for tool in result]


class UpstreamRateLimitError(SdkError):
    code = 'UPSTREAM_RATE_LIMITED'
    http_status = 429

    def __init__(self):
        super().__init__('MangroveAI rejected the request because its rate limit was reached (HTTP 429).',
                         suggestion='Stop this lookup sequence and wait before trying again. No automatic retry or payment fallback was attempted.')

    def to_dict(self):
        return {**super().to_dict(), 'upstream_status': 429, 'retry_payment': False}


def transport_error(error):
    pending = [error]
    while pending:
        nested = pending.pop()
        if isinstance(nested, BaseExceptionGroup):
            pending.extend(nested.exceptions)
            continue
        if (oauth.enabled() and isinstance(nested, httpx.HTTPStatusError)
                and nested.response.status_code in {401, 403}
                and str(nested.request.url) == oauth.endpoint()):
            challenge = nested.response.headers.get('www-authenticate', '')
            match = re.search(r'\bscope="([^"]*)"', challenge)
            scopes = set(match.group(1).split()) if match else set()
            extra = sorted(scopes & {'signals:evaluate', 'backtests:run'})
            command = './scripts/setup.sh --auth oauth --oauth login'
            command += ''.join(' --oauth-scope ' + scope for scope in extra)
            return oauth.OAuthError('Mangrove sign-in or additional permission is required. No tool was retried and no wallet payment was attempted.',
                                    suggestion='Run ' + command + ' and approve access in your browser.')
        denied = upstream_access_error(nested)
        if denied is not None:
            return denied
        if isinstance(nested, httpx.HTTPStatusError) and nested.response.status_code == 429:
            return UpstreamRateLimitError()
    return None


async def invoke(client, tool: Tool, arguments: dict) -> CallToolResult:
    result = await client.send_request(
        ClientRequest(CallToolRequest(params=CallToolRequestParams(name=tool.name, arguments=arguments))),
        CallToolResult,
    )
    if not result.isError and tool.outputSchema is not None:
        if result.structuredContent is None:
            raise ValueError('Missing structured tool output')
        Draft202012Validator(tool.outputSchema, registry=Registry()).validate(result.structuredContent)
    return result


def failure(code: str, message: str) -> CallToolResult:
    body = {'error': True, 'code': code, 'message': message}
    return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])


async def call_tool(name: str, arguments: dict) -> CallToolResult:
    from src.mcp.tools import _require
    from src.services import x402_payer

    if not _require(''):
        return failure('AUTH_REQUIRED', 'Authenticate to the local agent using its X-API-Key header.')
    try:
        tools = await catalog(refresh=False)
        tool = next((item for item in tools if item.name == name), None)
        if tool is None:
            return failure('UNKNOWN_TOOL', 'Tool is not advertised by MangroveAI MCP.')
        if not isinstance(arguments, dict) or not Draft202012Validator(tool.inputSchema).is_valid(arguments):
            return failure('VALIDATION_ERROR', 'Arguments do not match the current MangroveAI MCP inputSchema. Refresh tools/list.')
        target, key = endpoint(), None if oauth.enabled() else _api_key(app_config)
        annotations = tool.annotations.model_dump() if tool.annotations is not None else {}
        public_free_read = (
            annotations.get('mangrove/access_level') == 'public'
            and annotations.get('mangrove/side_effect') == 'read'
            and annotations.get('mangrove/x402_price_usd') is None
        )
        if oauth.enabled() or key is not None or public_free_read:
            with anyio.fail_after(float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS)):
                async with session(target, key) as client:
                    return await invoke(client, tool, arguments)
        async with session(target) as client:
            result = await x402_payer.pay_on_open_mcp_session(
                client, target, name=name, arguments=arguments, operation_id=str(uuid.uuid4()),
                timeout=float(app_config.MANGROVE_SDK_TIMEOUT_SECONDS),
            )
        if result.mcp_result is None:
            return failure('UPSTREAM_ERROR', 'MangroveAI MCP returned no tool result. Check payment records before retrying.')
        return CallToolResult.model_validate(result.mcp_result)
    except AgentError as error:
        body = error.to_dict()
        return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])
    except Exception as error:
        known = transport_error(error)
        if known is not None:
            body = known.to_dict()
            return CallToolResult(isError=True, structuredContent=body, content=[TextContent(type='text', text=json.dumps(body))])
        return failure('UPSTREAM_ERROR', 'MangroveAI MCP call could not be completed. No alternate transport was attempted.')
