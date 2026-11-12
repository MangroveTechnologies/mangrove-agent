"""Bounded, passive diagnostics for MCP HTTP 400 responses.

Only observe bytes consumed/emitted by the transport. Never pre-read requests,
change ASGI messages, or retain bodies after the request. Classifications are
hints from the installed SDK's error envelope, not authentication decisions.
"""
from __future__ import annotations

import contextvars
import json

from src.shared.logging import get_logger

_log = get_logger(__name__)
_LIMIT = 8192
_METHODS = frozenset({
    "initialize", "ping", "notifications/initialized", "notifications/cancelled",
    "notifications/progress", "tools/list", "tools/call", "resources/list",
    "resources/templates/list", "resources/read", "resources/subscribe",
    "resources/unsubscribe", "prompts/list", "prompts/get", "completion/complete",
    "logging/setLevel",
})
_CODES = frozenset({-32700, -32600, -32601, -32602, -32603})


class _Body:
    def __init__(self):
        self.data = bytearray()
        self.complete = False
        self.overflow = False

    def observe(self, message):
        body = message.get("body", b"")
        if not self.overflow:
            if len(self.data) + len(body) > _LIMIT:
                self.data.clear()
                self.overflow = True
            else:
                self.data.extend(body)
        self.complete = not message.get("more_body", False)

    def document(self):
        if self.overflow or not self.complete:
            return {}
        value = json.loads(self.data)
        return value if isinstance(value, dict) else {}


def _emit(request, response, is_json):
    """Fail-open diagnostics: malformed bodies/logging cannot affect transport."""
    try:
        method = "unknown"
        try:
            candidate = request.document().get("method")
            if isinstance(candidate, str) and candidate in _METHODS:
                method = candidate
        except (ValueError, UnicodeError, RecursionError):
            pass
        code, reason = "unknown", "unknown"
        try:
            error = response.document().get("error") if is_json else None
            if isinstance(error, dict):
                candidate = error.get("code")
                if type(candidate) is int and candidate in _CODES:
                    code = candidate
                message = error.get("message")
                if code == -32700:
                    reason = "parse"
                elif code == -32602:
                    reason = "schema"
                elif code == -32600 and isinstance(message, str):
                    if message == "Bad Request: Missing session ID":
                        reason = "session"
                    elif message.startswith("Bad Request: Unsupported protocol version:"):
                        reason = "protocol"
        except (ValueError, UnicodeError, RecursionError):
            pass
        # Do not inherit client-supplied correlation IDs or arbitrary bound
        # logging context: this event has a strictly finite, non-secret schema.
        contextvars.Context().run(
            _log.warning, "mcp.transport.rejected", status=400,
            rpc_method=method, rpc_code=code, reason=reason,
        )
    except Exception:
        pass  # Diagnostics must never change a delivered response.


class MCPTransportDiagnostics:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        request, response = _Body(), _Body()
        rejected, is_json, emitted = False, False, False

        async def observed_receive():
            message = await receive()
            if message.get("type") == "http.request":
                request.observe(message)
            return message

        async def observed_send(message):
            nonlocal rejected, is_json, emitted
            if message.get("type") == "http.response.start":
                rejected = message.get("status") == 400
                is_json = any(
                    name.lower() == b"content-type"
                    and value.split(b";", 1)[0].strip().lower() == b"application/json"
                    for name, value in message.get("headers", [])
                ) if rejected else False
            elif rejected and message.get("type") == "http.response.body":
                response.observe(message)
            await send(message)
            if rejected and response.complete and not emitted:
                emitted = True
                _emit(request, response, is_json)
                request.data.clear()
                response.data.clear()

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            request.data.clear()
            response.data.clear()
