"""Integration tests for discovery routes — /status and /tools."""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "disc.db"
    from src.api.routes import discovery
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()
    discovery.reset_catalog_cache()

    from src.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c
    ss.reset_scheduler_cache()
    db_mod.reset_connection()
    discovery.reset_catalog_cache()


def test_status_returns_expected_shape(client):
    r = client.get("/api/v1/agent/status")
    assert r.status_code == 200
    body = r.json()
    # Backwards-compat: original `version` field still present.
    assert "version" in body
    assert isinstance(body["version"], str)
    # New: explicit server_version mirrors `version`, plus sdk_versions + catalog.
    assert body["server_version"] == body["version"]
    assert set(body["sdk_versions"].keys()) == {"mangroveai", "mangrovemarkets"}
    assert set(body["catalog"].keys()) == {
        "signals_total",
        "kb_indicators_total",
        "kb_tags_total",
        "as_of",
        "error",
    }
    # Unchanged fields.
    assert body["wallets_count"] == 0
    assert set(body["strategies"].keys()) == {"draft", "inactive", "paper", "live", "archived"}
    assert body["active_cron_jobs"] == 0
    assert "db_path" in body
    assert body["uptime_seconds"] >= 0


def test_status_counts_wallets_and_strategies(client):
    from src.shared.db.sqlite import get_connection

    conn = get_connection()
    conn.execute(
        """INSERT INTO wallets (id, address, chain, network, chain_id,
           encrypted_secret, encryption_method, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("w1", "0xabc", "evm", "testnet", 84532, b"ct", "fernet-v1",
         "2026-04-20T00:00:00+00:00"),
    )
    for sid, status in [("s1", "paper"), ("s2", "live"), ("s3", "paper"), ("s4", "archived")]:
        conn.execute(
            """INSERT INTO strategies (id, mangrove_id, name, asset, timeframe, status,
               entry_json, exit_json, execution_config_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, f"mg-{sid}", sid, "ETH", "1h", status, "[]", "[]", "{}",
             "2026-04-20T00:00:00+00:00", "2026-04-20T00:00:00+00:00"),
        )
    conn.commit()

    body = client.get("/api/v1/agent/status").json()
    assert body["wallets_count"] == 1
    assert body["strategies"]["paper"] == 2
    assert body["strategies"]["live"] == 1
    assert body["strategies"]["archived"] == 1


def test_status_reports_catalog_counts_when_sdk_reachable(client, monkeypatch):
    """With a stubbed SDK, catalog counts flow through end-to-end."""
    from types import SimpleNamespace

    from src.api.routes import discovery

    class _StubSignals:
        def list_iter(self, *, limit_per_page):
            yield from (SimpleNamespace(name=f"sig_{i}") for i in range(228))

    class _StubIndicators:
        def list(self, **_):
            return [SimpleNamespace(name=f"ind_{i}") for i in range(70)]

    class _StubTags:
        def list(self):
            return [SimpleNamespace(tag=f"t_{i}") for i in range(42)]

    class _StubKB:
        indicators = _StubIndicators()
        tags = _StubTags()

    class _StubClient:
        signals = _StubSignals()
        kb = _StubKB()

    discovery.reset_catalog_cache()
    monkeypatch.setattr(
        "src.shared.clients.mangrove.mangrove_ai_client",
        lambda: _StubClient(),
    )

    body = client.get("/api/v1/agent/status").json()
    assert body["catalog"]["signals_total"] == 228
    assert body["catalog"]["kb_indicators_total"] == 70
    assert body["catalog"]["kb_tags_total"] == 42
    assert body["catalog"]["error"] is None
    assert body["catalog"]["as_of"] is not None


def test_status_catalog_is_resilient_when_sdk_unreachable(client, monkeypatch):
    """If the SDK client itself raises, catalog fields are null with a truncated error note."""
    from src.api.routes import discovery

    def _raise():
        raise RuntimeError("no API key configured")

    discovery.reset_catalog_cache()
    monkeypatch.setattr(
        "src.shared.clients.mangrove.mangrove_ai_client",
        _raise,
    )

    body = client.get("/api/v1/agent/status").json()
    assert body["catalog"]["signals_total"] is None
    assert body["catalog"]["kb_indicators_total"] is None
    assert body["catalog"]["kb_tags_total"] is None
    assert body["catalog"]["error"] is not None
    assert "client_init_failed" in body["catalog"]["error"]


def test_sdk_versions_are_strings_or_null(client):
    body = client.get("/api/v1/agent/status").json()
    for sdk in ("mangroveai", "mangrovemarkets"):
        v = body["sdk_versions"][sdk]
        assert v is None or (isinstance(v, str) and len(v) > 0)


def test_tools_returns_registered_catalog(client):
    r = client.get("/api/v1/agent/tools")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    assert isinstance(body["tools"], list)
    # hello_mangrove is registered from Phase 1; should be here.
    names = {t["name"] for t in body["tools"]}
    assert "hello_mangrove" in names


def test_discovery_endpoints_do_not_require_api_key(client):
    # No X-API-Key header.
    assert client.get("/api/v1/agent/status").status_code == 200
    assert client.get("/api/v1/agent/tools").status_code == 200
    assert client.get("/health").status_code == 200
