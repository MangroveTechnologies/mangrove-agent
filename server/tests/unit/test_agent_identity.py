"""Naming works through either interface and survives client/server changes."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from src.api.routes import discovery
from src.config import app_config
from src.mcp import registry
from src.mcp.tools import _register_discovery
from src.services import agent_identity
from src.shared.db import sqlite as db
from src.shared.errors import AgentError, agent_error_handler


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_tools", [])
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "agent.db"))
    db.reset_connection()
    db.init_db()
    # A naming/status check never needs an upstream connection or scheduler.
    monkeypatch.setattr(discovery, "_catalog_counts", lambda: {})
    monkeypatch.setattr(discovery, "active_job_count", lambda: 0)
    app = FastAPI()
    app.include_router(discovery.router, prefix="/api/v1/agent")
    app.add_exception_handler(AgentError, agent_error_handler)
    with TestClient(app) as client:
        yield client
    db.reset_connection()


def mcp_client():
    server = FastMCP("naming-test")
    _register_discovery(server)
    return server


async def call(server, tool_name, **arguments):
    return json.loads(await server._tool_manager._tools[tool_name].run(arguments))


@pytest.mark.asyncio
async def test_fresh_status_has_no_name_and_does_not_assign_one(installation):
    assert installation.get("/api/v1/agent/status").json()["agent_name"] is None
    assert (await call(mcp_client(), "status"))["agent_name"] is None
    assert db.get_connection().execute("SELECT COUNT(*) FROM agent_identity").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_name_is_shared_across_interfaces_and_survives_restart(installation):
    response = installation.put("/api/v1/agent/identity", json={"name": "  Orbit  "},
                                headers={"X-API-Key": "test-key-1"})
    assert response.status_code == 200
    assert response.json() == {"agent_name": "Orbit"}
    assert (await call(mcp_client(), "status"))["agent_name"] == "Orbit"

    db.reset_connection()
    db.init_db()  # An installer rerun/restart must preserve the choice.
    second_client = mcp_client()
    assert (await call(second_client, "status"))["agent_name"] == "Orbit"
    assert await call(second_client, "set_agent_name", name="Nova", api_key="test-key-1") == {
        "agent_name": "Nova",
    }
    assert installation.get("/api/v1/agent/status").json()["agent_name"] == "Nova"
    assert db.get_connection().execute("SELECT COUNT(*) FROM agent_identity").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "invalid"])
async def test_unauthorized_clients_cannot_choose_or_replace_name(installation, key):
    agent_identity.set_name("Keep me")
    headers = {"X-API-Key": key} if key else {}
    assert installation.put("/api/v1/agent/identity", json={"name": "Replace me"},
                            headers=headers).status_code == 401
    args = {"api_key": key} if key else {}
    result = await call(mcp_client(), "set_agent_name", name="Replace me", **args)
    assert result["code"] == "AUTH_INVALID_API_KEY"
    assert agent_identity.get_name() == "Keep me"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   ", "a" * 65, "bad\nname", "bad\x00name", "bad\x1bname"])
async def test_invalid_name_does_not_replace_saved_choice(installation, name):
    agent_identity.set_name("Keep me")
    assert installation.put("/api/v1/agent/identity", json={"name": name},
                            headers={"X-API-Key": "test-key-1"}).status_code == 422
    result = await call(mcp_client(), "set_agent_name", name=name, api_key="test-key-1")
    assert result["code"] == "VALIDATION_ERROR"
    assert agent_identity.get_name() == "Keep me"


@pytest.mark.parametrize("name", ["Sage", "User's agent", "東京", "x" * 64])
def test_user_can_choose_their_own_name(installation, name):
    assert agent_identity.set_name(name) == {"agent_name": name}
    assert agent_identity.get_name() == name
