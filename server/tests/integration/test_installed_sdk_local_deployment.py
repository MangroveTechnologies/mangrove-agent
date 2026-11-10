"""Exercise the installed SDK and Agent adapter against a loopback HTTP API."""

import copy
import ipaddress
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from mangrove_ai import MangroveAI
from mangrove_ai._services.strategies import StrategiesService
from src.services import local_deployment as deployments
from src.shared.errors import ValidationError

HAS_HELPER = callable(getattr(StrategiesService, "local_deployment", None))
FIXTURE = Path(__file__).parents[1] / "fixtures" / "local-deployment-not-ready.json"


@pytest.fixture
def installed_api(monkeypatch):
    """Only the API response is synthetic; SDK serialization and HTTP are real."""
    connect = socket.socket.connect
    resolve = socket.getaddrinfo

    def local_address(host):
        assert ipaddress.ip_address(host).is_loopback, "External SDK connection denied"

    def local_connect(connection, address):
        if isinstance(address, tuple):
            local_address(address[0])
        return connect(connection, address)

    def local_resolve(host, *args, **kwargs):
        local_address(host)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "getaddrinfo", local_resolve)
    payload = json.loads(FIXTURE.read_text())
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), self.headers.get("X-API-Key")))
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with MangroveAI(
        api_key="synthetic-local-api-key",
        base_url=f"http://127.0.0.1:{server.server_port}/api/v1",
        load_dotenv=False,
        auto_auth=False,
        auto_retry=False,
        max_retries=0,
        httpx_client=httpx.Client(trust_env=False),
    ) as sdk:
        monkeypatch.setattr(deployments, "mangrove_ai_client", lambda: sdk)
        try:
            yield payload, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_installed_package_capability_matches_ci_selection(installed_api):
    expected = os.environ.get("MANGROVE_LOCAL_DEPLOYMENT_EXPECTED")
    assert expected in {None, "absent", "present"}
    if expected is not None:
        assert HAS_HELPER is (expected == "present")
    if not HAS_HELPER:
        payload, requests = installed_api
        with pytest.raises(ValidationError, match="SDK release with local_deployment support"):
            deployments._fetch(payload["strategy_id"], "crypto")
        assert requests == []


@pytest.mark.skipif(not HAS_HELPER, reason="Installed SDK has no local-deployment helper")
def test_installed_helper_preserves_server_snapshot_and_readiness(installed_api):
    payload, requests = installed_api
    original = copy.deepcopy(payload)
    result = deployments._fetch(payload["strategy_id"], "crypto")
    assert result == original
    assert result["activation_ready"] is False
    assert result["activation_blocker"] == original["activation_blocker"]
    assert result["snapshot"]["position_size_calc"] == "v2"
    assert result["snapshot"]["execution_config"]["circuit_breaker"]["consecutive_loss_limit"] == 4
    assert requests == [(
        f"/api/v1/strategies/{payload['strategy_id']}/local-deployment?asset_class=crypto",
        "Bearer synthetic-local-api-key",
        None,
    )]


@pytest.mark.skipif(not HAS_HELPER, reason="Installed SDK has no local-deployment helper")
@pytest.mark.parametrize("field,value", [("contract_version", 2), ("strategy_id", "wrong-id"), ("asset_class", "equity")])
def test_installed_helper_mismatched_contract_is_rejected(installed_api, field, value):
    payload, requests = installed_api
    requested = payload["strategy_id"]
    payload[field] = value
    with pytest.raises(ValidationError, match="mismatched server deployment contract"):
        deployments._fetch(requested, "crypto")
    assert len(requests) == 1
