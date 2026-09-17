"""Unit tests for shared/clients/mangrove.py — SDK singletons."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("ENVIRONMENT", "test")

import httpx
import pytest
from mangrove_ai import MangroveAI
from mangrove_markets import MangroveMarkets

from src.config import app_config
from src.shared.clients import mangrove
from src.shared.clients.mangrove import (
    mangrove_ai_client,
    mangrove_markets_client,
    reset_clients,
)
from src.shared.errors import ValidationError


@pytest.fixture(autouse=True)
def clean_clients(monkeypatch):
    reset_clients()
    monkeypatch.delenv("MANGROVE_API_KEY", raising=False)
    yield
    reset_clients()


def test_mangroveai_singleton_returns_same_instance():
    reset_clients()
    a = mangrove_ai_client()
    b = mangrove_ai_client()
    assert a is b
    assert isinstance(a, MangroveAI)


def test_mangrovemarkets_singleton_returns_same_instance():
    reset_clients()
    a = mangrove_markets_client()
    b = mangrove_markets_client()
    assert a is b
    assert isinstance(a, MangroveMarkets)


def test_reset_clients_creates_new_instance():
    reset_clients()
    a = mangrove_ai_client()
    reset_clients()
    b = mangrove_ai_client()
    assert a is not b  # reset caused re-instantiation


@pytest.mark.parametrize("key", [None, "", "  ", "null", "None"])
def test_no_key_selects_explicit_payment_factory(monkeypatch, key):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", key)
    monkeypatch.setattr(app_config, "X402_MANGROVE_ENVIRONMENT", "local")
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", "http://localhost:5001/api/v1")
    monkeypatch.setattr(app_config, "X402_MANGROVE_KB_BASE_URL", "http://localhost:8080/api")
    factory = Mock()
    monkeypatch.setattr(mangrove, "create_x402_mangrove_client", factory)
    assert mangrove_ai_client() is factory.return_value
    factory.assert_called_once_with(
        environment="local", base_url="http://localhost:5001/api/v1",
        kb_base_url="http://localhost:8080/api",
    )


def test_missing_key_attribute_selects_payment_mode(monkeypatch):
    config = SimpleNamespace(MANGROVE_ENDPOINTS=app_config.MANGROVE_ENDPOINTS,
                             X402_MANGROVE_ENVIRONMENT="local",
                             X402_MANGROVE_BASE_URL="http://localhost:5001/api/v1",
                             X402_MANGROVE_KB_BASE_URL="http://localhost:8080/api")
    monkeypatch.setattr(mangrove, "_get_config", lambda: config)
    factory = Mock()
    monkeypatch.setattr(mangrove, "create_x402_mangrove_client", factory)
    assert mangrove_ai_client() is factory.return_value


@pytest.mark.parametrize("agent_env,sdk_env,base_url,kb_url", [
    ("local", "prod", "https://api.mangrovedeveloper.ai/api/v1", "https://kb.mangrovedeveloper.ai/api"),
    ("dev", "dev", "https://devapi.mangrove.trade/api/v1", "https://kb.mangrovedeveloper.ai/api"),
    ("test", "local", "http://localhost:5001/api/v1", "http://localhost:8080/api"),
    ("prod", "prod", "https://api.mangrovedeveloper.ai/api/v1", "https://kb.mangrovedeveloper.ai/api"),
])
@pytest.mark.parametrize("unset", [None, "", "  ", "null"])
def test_bundled_destinations_need_no_user_urls(monkeypatch, agent_env, sdk_env, base_url, kb_url, unset):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "")
    monkeypatch.setattr(app_config, "ENVIRONMENT", agent_env)
    for name in ("X402_MANGROVE_ENVIRONMENT", "X402_MANGROVE_BASE_URL", "X402_MANGROVE_KB_BASE_URL"):
        monkeypatch.setattr(app_config, name, unset)
    monkeypatch.setenv("MANGROVE_ENVIRONMENT", "unexpected")
    monkeypatch.setenv("MANGROVE_BASE_URL", "https://wrong.test")
    network, wallet = app_config.X402_NETWORK, app_config.X402_PAYER_WALLET
    factory = Mock()
    monkeypatch.setattr(mangrove, "create_x402_mangrove_client", factory)
    mangrove_ai_client()
    factory.assert_called_once_with(environment=sdk_env, base_url=base_url, kb_base_url=kb_url)
    assert (app_config.X402_NETWORK, app_config.X402_PAYER_WALLET) == (network, wallet)


def test_developer_selects_local_backend_without_knowing_urls(monkeypatch):
    monkeypatch.setattr(app_config, "ENVIRONMENT", "local")
    monkeypatch.setattr(app_config, "X402_MANGROVE_ENVIRONMENT", "local")
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", None)
    monkeypatch.setattr(app_config, "X402_MANGROVE_KB_BASE_URL", None)
    assert mangrove._payment_destination(app_config) == {
        "environment": "local", "base_url": "http://localhost:5001/api/v1",
        "kb_base_url": "http://localhost:8080/api",
    }


def test_single_url_override_keeps_other_default(monkeypatch):
    monkeypatch.setattr(app_config, "ENVIRONMENT", "local")
    monkeypatch.setattr(app_config, "X402_MANGROVE_ENVIRONMENT", None)
    monkeypatch.setattr(app_config, "X402_MANGROVE_BASE_URL", "http://localhost:5002/api/v1")
    monkeypatch.setattr(app_config, "X402_MANGROVE_KB_BASE_URL", None)
    assert mangrove._payment_destination(app_config) == {
        "environment": "prod", "base_url": "http://localhost:5002/api/v1",
        "kb_base_url": "https://kb.mangrovedeveloper.ai/api",
    }


@pytest.mark.parametrize("name", ["X402_MANGROVE_ENVIRONMENT", "X402_MANGROVE_BASE_URL", "X402_MANGROVE_KB_BASE_URL"])
@pytest.mark.parametrize("invalid", [False, 42, {}, []])
def test_invalid_override_does_not_silently_default(monkeypatch, name, invalid):
    monkeypatch.setattr(app_config, name, invalid)
    with pytest.raises(ValidationError, match=name):
        mangrove._payment_destination(app_config)


@pytest.mark.parametrize("agent_env,override", [("unexpected", None), ("local", "unexpected")])
def test_unknown_environment_never_guesses_production(monkeypatch, agent_env, override):
    monkeypatch.setattr(app_config, "ENVIRONMENT", agent_env)
    monkeypatch.setattr(app_config, "X402_MANGROVE_ENVIRONMENT", override)
    with pytest.raises(ValidationError, match="environment"):
        mangrove._payment_destination(app_config)


@pytest.mark.parametrize("key", [42, False, [], {}])
def test_invalid_key_type_does_not_enable_payments(monkeypatch, key):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", key)
    with pytest.raises(ValidationError, match="string or null"):
        mangrove_ai_client()


@pytest.mark.parametrize("status", [200, 401, 402, 403])
def test_key_mode_real_sdk_headers_and_no_payment_fallback(monkeypatch, status):
    from mangrove_ai.exceptions import APIError
    seen = []

    def handle(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer dev_configured"
        assert "PAYMENT-SIGNATURE" not in request.headers
        assert "X-Wallet-Address" not in request.headers
        return httpx.Response(status, json={"signals": [], "total": 0})

    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", "dev_configured")
    monkeypatch.setenv("MANGROVE_API_KEY", "prod_ambient")
    monkeypatch.setattr("mangrove_ai._config._maybe_load_dotenv", lambda: pytest.fail("dotenv loaded"))
    monkeypatch.setattr(mangrove, "create_x402_mangrove_client", lambda **kw: pytest.fail("payment fallback"))
    sdk_class = MangroveAI
    monkeypatch.setattr(mangrove, "MangroveAI", lambda **kw: sdk_class(
        **kw, httpx_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ))
    if status == 200:
        assert mangrove_ai_client().signals.list().total == 0
    else:
        with pytest.raises(APIError):
            mangrove_ai_client().signals.list()
    assert len(seen) == 1


def test_concurrent_first_access_builds_one_pool_and_reset_closes_it(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(mangrove, "MangroveAI", factory)
    with ThreadPoolExecutor(max_workers=12) as pool:
        clients = list(pool.map(lambda _: mangrove_ai_client(), range(40)))
    assert all(client is clients[0] for client in clients)
    factory.assert_called_once()
    reset_clients()
    clients[0].close.assert_called_once()
    reset_clients()
    clients[0].close.assert_called_once()


def test_keyless_markets_does_not_use_payment_transport(monkeypatch):
    monkeypatch.setattr(app_config, "MANGROVE_API_KEY", None)
    factory = Mock()
    monkeypatch.setattr(mangrove, "MangroveMarkets", factory)
    monkeypatch.setattr(mangrove, "create_x402_mangrove_client", lambda **kw: pytest.fail("markets payment"))
    mangrove_markets_client()
    assert factory.call_args.kwargs["api_key"] is None
