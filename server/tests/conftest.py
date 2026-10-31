"""Test configuration and fixtures."""
import os
from contextlib import nullcontext
from unittest.mock import patch

# Default to the 'test' env for unit/integration runs; live E2E tests
# (tests/e2e/test_live_swap.py) override by setting ENVIRONMENT=local
# before pytest starts so they pick up the real MANGROVE_API_KEY +
# hosted MangroveMarkets URL from local-config.json.
os.environ.setdefault("ENVIRONMENT", "test")

import pytest
from fastapi.testclient import TestClient
from x402.http import HTTPFacilitatorClient


def _unavailable_facilitator_metadata(*_args, **_kwargs):
    raise ConnectionError("offline test facilitator metadata")


# src.app constructs its module-level app during import, before fixtures run.
# Keep ordinary test collection offline; live runs opt into ENVIRONMENT=local.
with (patch.object(HTTPFacilitatorClient, "get_supported", _unavailable_facilitator_metadata)
      if os.environ.get("ENVIRONMENT") == "test" else nullcontext()):
    import src.app as app_module


@pytest.fixture(autouse=True)
def _offline_facilitator_metadata(monkeypatch):
    """Generic test app construction must not depend on a public facilitator."""
    if os.environ.get("ENVIRONMENT") == "test":
        monkeypatch.setattr(HTTPFacilitatorClient, "get_supported", _unavailable_facilitator_metadata)


@pytest.fixture
def client():
    # Each test gets a fresh app + fresh MCP session manager. Sharing a
    # module-global app across tests breaks because FastMCP's session
    # manager's task group is closed after the first lifespan exit.
    app = app_module.create_app()
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


@pytest.fixture
def offline_x402_facilitator(monkeypatch, _offline_facilitator_metadata):
    """Real inbound payment gates with only the facilitator handshake simulated."""
    from x402.http import HTTPFacilitatorClient
    from x402.schemas import SupportedResponse

    from src.mcp import server as mcp_server
    from src.shared.x402 import server as receiver
    from src.shared.x402.config import get_network

    calls = {"supported": 0, "verify": 0, "settle": 0}

    def supported(_client):
        calls["supported"] += 1
        return SupportedResponse.model_validate({
            "kinds": [{"x402Version": 2, "scheme": "exact", "network": get_network()}],
            "extensions": [],
            "signers": {},
        })

    async def verify(*_args, **_kwargs):
        calls["verify"] += 1
        pytest.fail("Inbound no-payment tests must not contact facilitator verification")

    async def settle(*_args, **_kwargs):
        calls["settle"] += 1
        pytest.fail("Inbound no-payment tests must not contact facilitator settlement")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(HTTPFacilitatorClient, "get_supported", supported)
            patch.setattr(HTTPFacilitatorClient, "verify", verify)
            patch.setattr(HTTPFacilitatorClient, "settle", settle)
            patch.setattr(receiver, "_server", None)
            patch.setattr(receiver, "_initialized", False)
            patch.setattr(mcp_server, "_mcp_server", None)
            patch.setattr(app_module, "x402_handler", app_module._setup_x402())
            yield calls
            assert calls["supported"] > 0, "Real server initialization did not fetch supported kinds"
            assert calls["verify"] == calls["settle"] == 0
    finally:
        receiver._server = None
        receiver._initialized = False
        mcp_server.reset_mcp_server()


@pytest.fixture
def offline_x402_client(offline_x402_facilitator):
    # The explicit dependency installs the handshake before app construction.
    app = app_module.create_app()
    with TestClient(app) as client:
        yield client
