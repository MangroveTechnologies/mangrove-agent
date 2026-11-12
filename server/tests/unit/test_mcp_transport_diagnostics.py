"""Passive HTTP diagnostics never change transport or expose payloads."""
import asyncio
import json
from unittest.mock import Mock

import pytest
import structlog

from src.shared import mcp_transport_diagnostics as diagnostics
from src.shared.logging import with_correlation_id


def envelope(code=-32600, message="Bad Request: Missing session ID"):
    return json.dumps({"error": {"code": code, "message": message}, "id": "secret-id"}).encode()


async def exercise(monkeypatch, body=b'{"method":"initialize"}', result=None, status=400,
                   chunks=False, consume=True, json_type=True, logger=None):
    logger = logger or Mock()
    monkeypatch.setattr(diagnostics, "_log", logger)
    result = envelope() if result is None else result
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    if chunks:
        incoming = [{"type": "http.request", "body": body[:7], "more_body": True},
                    {"type": "http.request", "body": body[7:], "more_body": False}]
    outgoing = [{"type": "http.response.start", "status": status,
                 "headers": [(b"content-type", b"application/json" if json_type else b"text/plain")]}]
    outgoing += [{"type": "http.response.body", "body": result[:9], "more_body": True},
                 {"type": "http.response.body", "body": result[9:], "more_body": False}]
    seen, received = [], []
    queue = list(incoming)

    async def receive():
        return queue.pop(0)

    async def send(message):
        seen.append(message)

    async def app(scope, recv, emit):
        assert scope is original_scope
        if consume:
            while True:
                msg = await recv()
                received.append(msg)
                if not msg.get("more_body"):
                    break
        await asyncio.sleep(0)
        for msg in outgoing:
            await emit(msg)

    original_scope = {"type": "http", "headers": [(b"authorization", b"secret")], "path": "/mcp/"}
    await diagnostics.MCPTransportDiagnostics(app)(original_scope, receive, send)
    assert all(a is b for a, b in zip(seen, outgoing))
    assert seen == outgoing
    assert received == (incoming if consume else [])
    assert all(a is b for a, b in zip(received, incoming))
    return logger


@pytest.mark.parametrize("code,message,reason", [
    (-32700, "Parse error: secret", "parse"),
    (-32602, "Validation error: secret", "schema"),
    (-32600, "Bad Request: Missing session ID", "session"),
    (-32600, "Bad Request: Unsupported protocol version: secret", "protocol"),
    (-32600, "secret", "unknown"),
    (-32603, "secret", "unknown"),
])
async def test_rejection_categories_and_chunk_passthrough(monkeypatch, code, message, reason):
    logger = await exercise(monkeypatch, result=envelope(code, message), chunks=True)
    logger.warning.assert_called_once_with("mcp.transport.rejected", status=400,
                                          rpc_method="initialize", rpc_code=code, reason=reason)


@pytest.mark.parametrize("result", [b"malformed secret", b"[]", b'{"error": "secret"}',
                                    b"[" * 2000, b" " * 8193 + envelope()])
async def test_bad_or_oversized_response_is_unknown(monkeypatch, result):
    logger = await exercise(monkeypatch, result=result)
    assert logger.warning.call_args.kwargs["reason"] == "unknown"
    assert logger.warning.call_args.kwargs["rpc_code"] == "unknown"


@pytest.mark.parametrize("body", [b"{bad secret", b'{"method":"secret"}', b'{"method":[]}',
                                  b" " * 8193 + b'{"method":"initialize"}'])
async def test_unknown_or_oversized_request_method(monkeypatch, body):
    logger = await exercise(monkeypatch, body=body)
    assert logger.warning.call_args.kwargs["rpc_method"] == "unknown"


@pytest.mark.parametrize("code", [12345, True, "secret", [-32600]])
async def test_unrecognized_code_never_logged(monkeypatch, code):
    logger = await exercise(monkeypatch, result=envelope(code, "secret"))
    assert logger.warning.call_args.kwargs["rpc_code"] == "unknown"


async def test_success_does_not_parse_or_log(monkeypatch):
    monkeypatch.setattr(diagnostics._Body, "document", Mock(side_effect=AssertionError("must not parse")))
    logger = await exercise(monkeypatch, status=200)
    logger.warning.assert_not_called()


async def test_no_preconsumption_or_non_json_classification(monkeypatch):
    logger = await exercise(monkeypatch, consume=False, json_type=False)
    assert logger.warning.call_args.kwargs == dict(status=400, rpc_method="unknown",
                                                  rpc_code="unknown", reason="unknown")


async def test_logger_failure_is_invisible_to_transport(monkeypatch):
    logger = Mock()
    logger.warning.side_effect = RuntimeError("secret")
    await exercise(monkeypatch, logger=logger)


async def test_concurrent_requests_are_isolated(monkeypatch):
    logger = Mock()
    await asyncio.gather(
        exercise(monkeypatch, body=b'{"method":"initialize"}', result=envelope(-32700), logger=logger),
        exercise(monkeypatch, body=b'{"method":"tools/call"}', result=envelope(-32602), logger=logger),
    )
    assert {(call.kwargs["rpc_method"], call.kwargs["reason"]) for call in logger.warning.call_args_list} == {
        ("initialize", "parse"), ("tools/call", "schema")}


async def test_secrets_and_untrusted_log_context_omitted(monkeypatch):
    from src.shared.logging import _correlation_id_var
    captured = []

    class Logger:
        def warning(self, event, **kwargs):
            captured.append((event, kwargs, _correlation_id_var.get(), structlog.contextvars.get_contextvars()))

    with with_correlation_id("secret-correlation"), structlog.contextvars.bound_contextvars(secret="secret-context"):
        await exercise(monkeypatch, body=b'{"method":"tools/call","params":{"key":"secret-payload"}}',
                       result=envelope(-32602, "secret-error"), logger=Logger())
    assert "secret" not in repr(captured)
    assert captured[0][2:] == (None, {})


async def test_non_http_passthrough():
    from unittest.mock import AsyncMock
    app = AsyncMock()
    scope = {"type": "lifespan"}
    await diagnostics.MCPTransportDiagnostics(app)(scope, None, None)
    app.assert_awaited_once_with(scope, None, None)


async def test_installed_sdk_missing_session_is_classified(monkeypatch):
    """Exercise actual SDK validation/response construction, without a network."""
    from mcp.server.streamable_http import StreamableHTTPServerTransport

    logger = Mock()
    monkeypatch.setattr(diagnostics, "_log", logger)
    transport = StreamableHTTPServerTransport(mcp_session_id="test-session")
    messages = []

    async def receive():
        raise AssertionError("GET rejection must not consume a body")

    async def send(message):
        messages.append(message)

    async with transport.connect():
        await diagnostics.MCPTransportDiagnostics(transport.handle_request)(
            {"type": "http", "method": "GET", "path": "/mcp/",
             "headers": [(b"accept", b"text/event-stream")]}, receive, send,
        )
    assert messages[0]["status"] == 400
    assert json.loads(messages[1]["body"])["error"]["message"] == "Bad Request: Missing session ID"
    logger.warning.assert_called_once_with("mcp.transport.rejected", status=400,
                                          rpc_method="unknown", rpc_code=-32600, reason="session")
