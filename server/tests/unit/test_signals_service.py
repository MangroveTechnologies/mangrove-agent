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


def test_search_remains_one_distinct_request_with_category_refinement(sdk_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/signals/search"
        assert json.loads(request.content)["query"] == "momentum"
        body = page(0, 2, 2)
        body["signals"][1]["category"] = "volume"
        return httpx.Response(200, json=body)

    result = list_signals(client=sdk_factory(handler), search="momentum", category="trend", collect=True)
    assert result["total"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1001}, {"limit": True},
                                    {"offset": -1}, {"search": "trend", "role": "FILTER"}])
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
