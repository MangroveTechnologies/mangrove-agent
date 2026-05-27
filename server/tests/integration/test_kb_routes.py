"""Integration tests for KB routes (search, glossary, documents, indicators, tags)."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_API_KEY = "test-key-1"


@pytest.fixture
def client(monkeypatch):
    # Stub the mangroveai KB surface. Each accessor (search/glossary/
    # documents/indicators/tags) is a MagicMock that returns Pydantic-
    # style objects with model_dump().
    sdk = MagicMock()

    search_resp = MagicMock()
    search_resp.model_dump.return_value = {
        "query": "momentum", "hits": [{"slug": "macd", "score": 0.9}],
    }
    sdk.kb.search.query.return_value = search_resp

    glossary_entry = MagicMock()
    glossary_entry.model_dump.return_value = {
        "term": "RSI", "definition": "Relative Strength Index",
        "backlinks": ["rsi_overbought", "rsi_oversold"],
    }
    sdk.kb.glossary.get.return_value = glossary_entry

    doc_summary = MagicMock()
    doc_summary.model_dump.return_value = {"slug": "macd", "title": "MACD guide"}
    sdk.kb.documents.list.return_value = [doc_summary]

    doc_full = MagicMock()
    doc_full.model_dump.return_value = {
        "slug": "macd", "title": "MACD guide", "body": "MACD is…",
    }
    sdk.kb.documents.get.return_value = doc_full

    indicator = MagicMock()
    indicator.model_dump.return_value = {
        "name": "rsi", "category": "momentum", "description": "Relative Strength Index",
    }
    sdk.kb.indicators.list.return_value = [indicator]

    tag = MagicMock()
    tag.model_dump.return_value = {"name": "momentum", "count": 12}
    sdk.kb.tags.list.return_value = [tag]

    monkeypatch.setattr("src.api.routes.kb.mangrove_ai_client", lambda: sdk)

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def _auth() -> dict:
    return {"X-API-Key": _API_KEY}


def test_search(client):
    r = client.get("/api/v1/agent/kb/search?q=momentum", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "momentum"
    assert body["hits"][0]["slug"] == "macd"


def test_glossary(client):
    r = client.get("/api/v1/agent/kb/glossary/RSI", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["term"] == "RSI"
    assert "rsi_overbought" in body["backlinks"]


def test_documents_list(client):
    r = client.get("/api/v1/agent/kb/documents", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["slug"] == "macd"


def test_document_get(client):
    r = client.get("/api/v1/agent/kb/documents/macd", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "MACD guide"
    assert "body" in body


def test_indicators_list(client):
    r = client.get("/api/v1/agent/kb/indicators", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "rsi"
    assert body[0]["category"] == "momentum"


def test_indicators_list_with_category_filter(client):
    r = client.get("/api/v1/agent/kb/indicators?category=momentum", headers=_auth())
    assert r.status_code == 200
    # Verify the filter was passed through to the SDK.
    from src.api.routes.kb import mangrove_ai_client
    sdk = mangrove_ai_client()
    sdk.kb.indicators.list.assert_called_with(category="momentum")


def test_tags_list(client):
    r = client.get("/api/v1/agent/kb/tags", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "momentum"


def test_auth_required_on_kb_endpoints(client):
    assert client.get("/api/v1/agent/kb/search?q=x").status_code == 401
    assert client.get("/api/v1/agent/kb/glossary/RSI").status_code == 401
    assert client.get("/api/v1/agent/kb/documents").status_code == 401
    assert client.get("/api/v1/agent/kb/documents/macd").status_code == 401
    assert client.get("/api/v1/agent/kb/indicators").status_code == 401
    assert client.get("/api/v1/agent/kb/tags").status_code == 401
