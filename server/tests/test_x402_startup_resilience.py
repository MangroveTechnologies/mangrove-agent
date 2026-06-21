"""Regression tests for cold-start resilience to an unreachable x402 facilitator.

Bug (issue #106): registering the ``hello_mangrove`` MCP demo tool eagerly
initialized the EXTERNAL x402 facilitator (a blocking ``/supported`` fetch) at
import time, inside ``create_app()``. If the facilitator was unreachable, slow,
or firewalled, that call crashed or stalled app import *before uvicorn bound its
port*, so ``/health`` never came up and the setup/verify scripts failed with
"/health did not respond within 30s".

The free + auth tiers must come up regardless of payment-facilitator reach. These
tests force the facilitator init to fail and assert the app still starts, ``/health``
answers, and the demo tool is still catalogued (degraded), never taking the agent
down with it.
"""
import os

os.environ.setdefault("ENVIRONMENT", "test")

import concurrent.futures

import pytest
from fastapi.testclient import TestClient

import src.shared.x402.server as x402_server
from src.app import create_app
from src.mcp.tools import _run_bounded


def _boom():
    """Stand in for an unreachable facilitator handshake."""
    raise ConnectionError("simulated: x402 facilitator unreachable")


def test_unreachable_facilitator_does_not_block_startup(monkeypatch):
    """create_app() must succeed and /health must answer even if the x402
    facilitator handshake fails — the core of issue #106."""
    monkeypatch.setattr(x402_server, "_ensure_initialized", _boom)

    app = create_app()  # before the fix this raised during MCP tool registration
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


def test_hello_mangrove_still_catalogued_when_facilitator_down(monkeypatch):
    """The demo tool stays in the discovery catalog (degraded), so discovery is
    stable rather than the tool silently vanishing or 500-ing startup."""
    monkeypatch.setattr(x402_server, "_ensure_initialized", _boom)

    app = create_app()
    with TestClient(app) as client:
        tools = client.get("/api/v1/agent/tools").json()["tools"]
        names = {t["name"] for t in tools}
        assert "hello_mangrove" in names


def test_run_bounded_returns_value_quickly():
    assert _run_bounded(lambda: 21 + 21, 5.0) == 42


def test_run_bounded_gives_up_on_a_hanging_call():
    """A call that outlasts the bound raises TimeoutError instead of blocking
    forever — this is what keeps a hanging facilitator from stalling startup."""
    import time

    with pytest.raises(concurrent.futures.TimeoutError):
        _run_bounded(lambda: time.sleep(10), 0.2)
