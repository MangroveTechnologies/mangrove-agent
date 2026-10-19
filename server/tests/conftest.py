"""Test configuration and fixtures."""
import os

# Default to the 'test' env for unit/integration runs; live E2E tests
# (tests/e2e/test_live_swap.py) override by setting ENVIRONMENT=local
# before pytest starts so they pick up the real MANGROVE_API_KEY +
# hosted MangroveMarkets URL from local-config.json.
os.environ.setdefault("ENVIRONMENT", "test")

import pytest
from fastapi.testclient import TestClient

from src.app import create_app


@pytest.fixture
def client():
    # Each test gets a fresh app + fresh MCP session manager. Sharing a
    # module-global app across tests breaks because FastMCP's session
    # manager's task group is closed after the first lifespan exit.
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fresh_mangrove_sessions(monkeypatch):
    """Each test opens its own MangroveAI MCP sessions: a shared session belongs to the
    event loop that opened it, and every test runs on a new loop."""
    import anyio

    from src.mcp import mangrove_proxy
    monkeypatch.setattr(mangrove_proxy, "_sessions", {})
    monkeypatch.setattr(mangrove_proxy, "_sessions_lock", anyio.Lock())
    monkeypatch.setattr(mangrove_proxy, "_open_locks", {})
    monkeypatch.setattr(mangrove_proxy, "_open_failures", {})
