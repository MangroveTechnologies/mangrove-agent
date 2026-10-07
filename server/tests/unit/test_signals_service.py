"""Offline HTTP integration with the installed candidate SDK."""
from __future__ import annotations

import json
from math import ceil

import httpx
import pytest
from mangrove_ai import MangroveAI

from src.services.signals import list_signals
from src.shared.errors import SdkError, ValidationError


@pytest.fixture
def sdk_factory():
    clients = []

    def make(handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        sdk = MangroveAI(api_key="test-upstream-key", load_dotenv=False, auto_retry=False,
                        base_url="https://signals.test/api/v1", httpx_client=http)
        clients.append(sdk)
        return sdk

    yield make
    for sdk in clients:
        sdk.close()


def page(offset, size, total=300):
    end = min(total, offset + size)
    return {"signals": [{"name": f"signal_{i}", "category": "trend"} for i in range(offset, end)],
            "offset": offset, "limit": size, "total": total,
            "has_more": end < total, "next_offset": end if end < total else None}


@pytest.mark.parametrize("count", [1, 30, 31, 100, 101, 250])
@pytest.mark.parametrize("ceiling", [7, 30, 50])
def test_collection_uses_effective_pages_without_skipping_or_extra_fetches(sdk_factory, count, ceiling):
    calls = []

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/signals/"
        params = request.url.params
        assert params["category"] == "trend"
        assert params["regime_direction"] == "bull"
        assert params["role"] == "TRIGGER"
        assert set(params) == {"limit", "offset", "category", "regime_direction", "role"}
        offset, requested = int(params["offset"]), int(params["limit"])
        calls.append((offset, requested))
        return httpx.Response(200, json=page(offset, min(ceiling, requested)))

    result = list_signals(client=sdk_factory(handler), limit=count, category=" Trend ",
                          regime_direction="bull", role="TRIGGER", collect=True)
    assert result["total"] == count
    assert [s["name"] for s in result["items"]] == [f"signal_{i}" for i in range(count)]
    assert len(calls) == ceil(count / ceiling)
    expected_last = (count % ceiling or ceiling) if len(calls) > 1 else count
    assert calls[-1][1] == expected_last


@pytest.mark.parametrize("total", [0, 1, 30, 65, 101])
def test_catalogue_end_never_fetches_an_extra_page(sdk_factory, total):
    calls = []

    def handler(request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        return httpx.Response(200, json=page(offset, 30, total))

    result = list_signals(client=sdk_factory(handler), limit=250, collect=True)
    assert result["total"] == total
    assert len(calls) == max(1, ceil(total / 30))


def test_local_rest_page_preserves_server_metadata(sdk_factory):
    result = list_signals(client=sdk_factory(lambda r: httpx.Response(200, json=page(30, 30, 120))), offset=30)
    assert result["total"] == 120
    assert result["limit"] == 30
    assert result["offset"] == 30
    assert result["next_offset"] == 60
    assert result["has_more"] is True


def test_search_forwards_filters_in_one_distinct_request(sdk_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/signals/search"
        assert json.loads(request.content)["query"] == "momentum"
        assert json.loads(request.content)["category"] == "trend"
        assert json.loads(request.content)["role"] == "FILTER"
        body = page(0, 1, 1)
        return httpx.Response(200, json=body)

    result = list_signals(client=sdk_factory(handler), search="momentum", category="trend", role="FILTER", collect=True)
    assert result["total"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1001}, {"limit": True},
                                    {"offset": -1}])
def test_invalid_workflow_inputs_never_reach_upstream(sdk_factory, kwargs):
    with pytest.raises(ValidationError):
        list_signals(client=sdk_factory(lambda r: pytest.fail("request sent")), **kwargs)


@pytest.mark.parametrize("change", [
    {"offset": 50}, {"next_offset": 0}, {"signals": [], "has_more": True},
    {"signals": "private-response-sentinel"},
])
def test_malformed_page_stops_without_another_request(sdk_factory, change):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={**page(0, 30), **change})

    with pytest.raises(SdkError) as error:
        list_signals(client=sdk_factory(handler), limit=100, collect=True)
    assert "private-response-sentinel" not in str(error.value)
    assert len(calls) == 1


def test_shrinking_pages_cannot_expand_the_workflow_budget(sdk_factory):
    calls = []

    def handler(request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        return httpx.Response(200, json=page(offset, 30 if offset == 0 else 1))

    with pytest.raises(SdkError, match="page budget"):
        list_signals(client=sdk_factory(handler), limit=100, collect=True)
    assert calls == [0, 30, 31, 32]


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_upstream_errors_do_not_become_empty_success_or_payment_fallback(sdk_factory, status):
    calls = []

    def handler(request):
        calls.append(request)
        assert "payment-signature" not in request.headers
        return httpx.Response(status, json={"message": "private-error-sentinel"})

    with pytest.raises(SdkError) as error:
        list_signals(client=sdk_factory(handler), collect=True)
    assert "private-error-sentinel" not in str(error.value)
    assert len(calls) == 1


def test_older_sdk_is_rejected_before_any_billable_request(sdk_factory, monkeypatch):
    from mangrove_ai import models

    monkeypatch.delattr(models, "SignalListPage")
    with pytest.raises(SdkError, match="updated MangroveAI SDK"):
        list_signals(client=sdk_factory(lambda r: pytest.fail("outdated SDK reached upstream")))


@pytest.mark.parametrize("status,code", [
    (401, "UPSTREAM_AUTHENTICATION_FAILED"), (403, "UPSTREAM_ACCESS_DENIED"),
])
@pytest.mark.parametrize("search", [None, "momentum"])
def test_access_denial_preserves_safe_status_and_stops_collection(sdk_factory, status, code, search):
    from src.shared.errors import UpstreamAccessError

    calls = []

    def handler(request):
        calls.append(request)
        assert "payment-signature" not in request.headers
        return httpx.Response(status, json={"message": "private-key-sentinel", "code": "untrusted-code"})

    with pytest.raises(UpstreamAccessError) as error:
        list_signals(client=sdk_factory(handler), limit=100, collect=True, search=search)
    payload = error.value.to_dict()
    assert payload["code"] == code
    assert payload["upstream_status"] == error.value.http_status == status
    assert payload["retryable"] is False and payload["retry_payment"] is False
    assert "private-key-sentinel" not in json.dumps(payload)
    assert "untrusted-code" not in json.dumps(payload)
    assert "Do not inspect local files" in payload["suggestion"]
    assert error.value.__suppress_context__ is True
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_rest_preserves_sdk_access_denial(sdk_factory, monkeypatch, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.routes.signals import router
    from src.services import signals
    from src.shared.auth.dependency import require_api_key
    from src.shared.errors import AgentError, agent_error_handler

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "secret-upstream-body"})

    sdk = sdk_factory(handler)
    monkeypatch.setattr(signals, "mangrove_ai_client", lambda: sdk)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_key] = lambda: "local-test-identity"
    app.add_exception_handler(AgentError, agent_error_handler)
    with TestClient(app) as client:
        response = client.get("/signals?limit=10")
    assert response.status_code == status
    result = response.json()
    assert result["error"] is True
    assert result["code"] == ("UPSTREAM_AUTHENTICATION_FAILED" if status == 401 else "UPSTREAM_ACCESS_DENIED")
    assert result["upstream_status"] == status
    assert result["retryable"] is False
    assert result["retry_payment"] is False
    assert "secret-upstream-body" not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_http_status_error_is_not_flattened_to_generic_failure(status):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.shared.errors import UpstreamAccessError

    request = httpx.Request("GET", "https://signals.test")
    response = httpx.Response(status, request=request)
    upstream = Mock(side_effect=httpx.HTTPStatusError("secret-body", request=request, response=response))
    client = SimpleNamespace(signals=SimpleNamespace(list=upstream))
    with pytest.raises(UpstreamAccessError) as error:
        list_signals(client=client, limit=10, collect=True)
    assert error.value.upstream_status == status
    assert "secret-body" not in json.dumps(error.value.to_dict())
    upstream.assert_called_once()


def test_unknown_signal_failure_does_not_claim_access_is_valid():
    from types import SimpleNamespace
    from unittest.mock import Mock

    upstream = Mock(side_effect=RuntimeError("secret-body"))
    client = SimpleNamespace(signals=SimpleNamespace(list=upstream))
    with pytest.raises(SdkError) as error:
        list_signals(client=client, limit=10, collect=True)
    payload = error.value.to_dict()
    assert payload["code"] == "SDK_ERROR"
    assert "no specific reason was returned" in payload["suggestion"]
    assert "Do not speculate" in payload["suggestion"]
    assert "secret-body" not in json.dumps(payload)
    upstream.assert_called_once()
