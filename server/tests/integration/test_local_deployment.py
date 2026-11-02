"""Saved-strategy handoff with isolated, reopenable SQLite and no live orders."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.config import app_config
from src.services import broker_execution, broker_store, scheduler_service, strategy_service
from src.services import local_deployment as deployments
from src.shared.db import sqlite as db
from src.shared.errors import SdkError, StrategyInvalidStatusTransition, ValidationError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db.reset_connection()
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "adoption.db"))
    db.init_db()
    remote = {
        "contract_version": 1, "strategy_id": str(uuid4()), "owner": {"user_id": "u", "org_id": "o"},
        "name": "BTC", "fingerprint": "a" * 64, "timeframe": "1h", "asset_class": "crypto",
        "activation_ready": True, "activation_blocker": None,
        "snapshot": {"asset": "BTC", "base_token": "BTC", "quote_token": "USD", "market_data_venue": "COINBASE",
                     "entry": [{"name": "rsi", "timeframe": "1h"}], "exit": [],
                     "position_size_calc": "v2", "execution_config": {"position_size_calc": "v2"}},
    }
    fetch = Mock(side_effect=lambda *a, **kw: copy.deepcopy(remote))
    monkeypatch.setattr(deployments, "mangrove_ai_client", lambda: SimpleNamespace(strategies=SimpleNamespace(local_deployment=fetch)))
    monkeypatch.setattr(deployments, "_scope", lambda: "scope-one")
    real_register = scheduler_service.register_job
    register, cancel = Mock(), Mock()
    monkeypatch.setattr(scheduler_service, "register_job", register)
    monkeypatch.setattr(scheduler_service, "cancel_job", cancel)
    monkeypatch.setattr(broker_execution, "run", Mock())
    yield SimpleNamespace(remote=remote, fetch=fetch, register=register, cancel=cancel, real_register=real_register)
    db.reset_connection()


def adopt(setup):
    return deployments.adopt(setup.remote["strategy_id"], "crypto")["deployment_id"]


def route(sid):
    with broker_store.connection() as conn:
        conn.execute("INSERT INTO broker_routes VALUES (?,'robinhood','eligible','numeric','crypto','100','25','0.0025',?)", (sid, broker_store.now()))


def test_adoption_is_durable_idempotent_and_never_schedules(setup):
    sid = adopt(setup)
    db.reset_connection()
    db.init_db()
    assert adopt(setup) == sid
    result = deployments.inspect(sid)
    assert result["server_strategy_id"] == setup.remote["strategy_id"]
    assert result["fingerprint"] == "a" * 64
    assert result["status"] == "inactive"
    assert result["pending_orders"] == 0
    setup.register.assert_not_called()
    broker_execution.run.assert_not_called()


def test_concurrent_adoption_creates_one_deployment(setup):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adopt(setup), range(2)))
    assert results[0] == results[1]
    with broker_store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM local_deployments").fetchone()[0] == 1


def pending_order(sid):
    with broker_store.connection() as conn:
        conn.execute("""INSERT INTO evaluations (id,strategy_id,timestamp,market_snapshot_json,sdk_response_json,
            order_intents_json,duration_ms,status) VALUES ('eval',?,?,'{}','{}','[]',0,'ok')""", (sid, broker_store.now()))
        conn.execute("""INSERT INTO broker_orders (id,strategy_id,evaluation_id,intent_key,intent_json,
            position_template_json,request_json,status,created_at,updated_at)
            VALUES ('order',?,'eval','key','{}','{}','{}','uncertain',?,?)""", (sid, broker_store.now(), broker_store.now()))


@pytest.mark.asyncio
async def test_pending_order_survives_stop_restart_retries_and_rebinding_refusal(setup):
    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    pending_order(sid)
    # A repeated activation is an acknowledgement, not a new schedule or order.
    assert deployments.activate(sid, True)["pending_orders"] == 1
    deployments.stop(sid, True)
    db.reset_connection()
    db.init_db()
    assert broker_store.orders(sid, pending=True)[0]["status"] == "uncertain"
    with pytest.raises(ValidationError, match="pending"):
        deployments.activate(sid, True)
    venue = SimpleNamespace(account=AsyncMock(return_value={"account_number": "eligible", "rhs_account_number": "numeric", "rhc_account_number": "crypto"}), quote=AsyncMock())
    config = {"asset_class": "crypto", "capital": "100", "max_order_usd": "25", "slippage_pct": "0.0025"}
    await broker_execution.configure(venue, sid, config)
    with pytest.raises(SdkError, match="history"):
        await broker_execution.configure(venue, sid, {**config, "capital": "200"})
    setup.remote["fingerprint"] = "b" * 64
    with pytest.raises(ValidationError, match="history"):
        deployments.adopt(setup.remote["strategy_id"], "crypto", rebind=True)
    assert len(broker_store.orders(sid)) == 1
    assert deployments.inspect(sid)["fingerprint"] == "a" * 64


@pytest.mark.parametrize("field,value", [("contract_version", 2), ("strategy_id", str(uuid4())), ("asset_class", "equity")])
def test_bad_server_contract_leaves_no_local_record(setup, field, value):
    requested = setup.remote["strategy_id"]
    setup.remote[field] = value
    with pytest.raises(ValidationError):
        deployments.adopt(requested, "crypto")
    with broker_store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM local_deployments").fetchone()[0] == 0


def test_access_failure_does_not_fall_back_or_write(setup):
    setup.fetch.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        adopt(setup)
    assert setup.fetch.call_count == 1
    with broker_store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM local_deployments").fetchone()[0] == 0


def test_changed_identity_cannot_reuse_binding_but_can_stop_locally(setup, monkeypatch):
    sid = adopt(setup)
    monkeypatch.setattr(deployments, "_scope", lambda: "different")
    with pytest.raises(ValidationError):
        adopt(setup)
    with pytest.raises(ValidationError):
        deployments.activate(sid, True)
    assert deployments.stop(sid, True)["status"] == "inactive"


def test_changed_revision_requires_explicit_inactive_rebind_and_new_allocation(setup):
    sid = adopt(setup)
    route(sid)
    setup.remote["fingerprint"] = "b" * 64
    setup.remote["snapshot"]["entry"][0]["timeframe"] = "4h"
    with pytest.raises(ValidationError, match="explicitly rebind"):
        adopt(setup)
    with pytest.raises(ValidationError, match="changed"):
        deployments.activate(sid, True)
    deployments.adopt(setup.remote["strategy_id"], "crypto", rebind=True)
    assert broker_store.route(sid) is None
    assert deployments.inspect(sid)["fingerprint"] == "b" * 64
    setup.register.assert_not_called()


def test_server_id_cannot_replace_local_id_and_local_rules_are_immutable(setup):
    sid = adopt(setup)
    with pytest.raises(ValidationError):
        deployments.activate(setup.remote["strategy_id"], True)
    with broker_store.connection() as conn:
        conn.execute("UPDATE strategies SET mangrove_id=? WHERE id=?", (str(uuid4()), sid))
    with pytest.raises(ValidationError, match="identities"):
        deployments.activate(sid, True)
    with pytest.raises(StrategyInvalidStatusTransition):
        strategy_service.update_status(sid, strategy_service.StrategyStatusUpdate(status="live", confirm=True))
    with pytest.raises(StrategyInvalidStatusTransition):
        strategy_service.configure_market(sid, strategy_service.StrategyMarketContext(base_token="BTC", quote_token="USD", market_data_venue="COINBASE"))


def test_activation_requires_approval_allocation_and_exact_backtest(setup):
    sid = adopt(setup)
    with pytest.raises(ValidationError, match="authorization"):
        deployments.activate(sid)
    with pytest.raises(ValidationError, match="allocation"):
        deployments.activate(sid, True)
    route(sid)
    setup.remote["activation_ready"] = False
    with pytest.raises(ValidationError, match="ready"):
        deployments.activate(sid, True)
    setup.register.assert_not_called()
    setup.remote["activation_ready"] = True
    assert deployments.activate(sid, True)["status"] == "live"
    deployments.activate(sid, True)
    assert setup.register.call_count == 1
    assert deployments.stop(sid, True)["pending_orders_canceled"] is False
    assert deployments.inspect(sid)["status"] == "inactive"


def test_scheduler_failure_leaves_inactive_and_restart_repairs_committed_activation(setup):
    sid = adopt(setup)
    route(sid)
    setup.register.side_effect = RuntimeError("scheduler unavailable")
    with pytest.raises(RuntimeError):
        deployments.activate(sid, True)
    assert deployments.inspect(sid)["status"] == "inactive"
    # Simulate process loss after committing live but before registering a job.
    with broker_store.connection() as conn:
        conn.execute("UPDATE strategies SET status='live' WHERE id=?", (sid,))
    db.reset_connection()
    setup.register.side_effect = None
    deployments.restore_jobs()
    setup.register.assert_called_with(sid, "1h", "src.services.strategy_service:tick")


def test_real_scheduler_recovery_retains_one_durable_job(setup, monkeypatch):
    sid = adopt(setup)
    route(sid)
    with broker_store.connection() as conn:
        conn.execute("UPDATE strategies SET status='live' WHERE id=?", (sid,))
    scheduler_service.reset_scheduler_cache()
    monkeypatch.setattr(scheduler_service, "register_job", setup.real_register)
    try:
        scheduler = scheduler_service.get_scheduler()
        scheduler.start(paused=True)  # Persist jobs, never execute a trading tick.
        deployments.restore_jobs()
        deployments.restore_jobs()
        assert [job.id for job in scheduler.get_jobs()] == [f"eval-{sid}"]
        scheduler.shutdown(wait=True)
        scheduler_service.get_scheduler.cache_clear()
        scheduler = scheduler_service.get_scheduler()
        scheduler.start(paused=True)
        deployments.restore_jobs()
        assert [job.id for job in scheduler.get_jobs()] == [f"eval-{sid}"]
    finally:
        scheduler_service.reset_scheduler_cache()


@pytest.mark.asyncio
async def test_configuration_retry_preserves_state_and_sends_no_orders(setup):
    sid = adopt(setup)
    venue = SimpleNamespace(account=AsyncMock(return_value={"account_number": "eligible", "rhs_account_number": "numeric", "rhc_account_number": "crypto"}), quote=AsyncMock())
    allocation = {"asset_class": "crypto", "capital": "100", "max_order_usd": "25", "slippage_pct": "0.0025"}
    result = await broker_execution.configure(venue, sid, allocation)
    assert result["active"] is False
    with broker_store.connection() as conn:
        conn.execute("UPDATE strategies SET execution_state_json=? WHERE id=?", (json.dumps({"keep": 1}), sid))
    await broker_execution.configure(venue, sid, allocation)
    assert json.loads(strategy_service._get_row(sid)["execution_state_json"]) == {"keep": 1}
    setup.register.assert_not_called()
    assert broker_store.orders(sid) == []
    with pytest.raises(SdkError, match="Adopt"):
        await broker_execution.configure(venue, setup.remote["strategy_id"], allocation)


@pytest.mark.asyncio
async def test_stale_tick_stops_before_evaluation_or_order_submission(setup):
    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    setup.remote["fingerprint"] = "b" * 64
    venue = SimpleNamespace(quote=AsyncMock())
    with pytest.raises(ValidationError, match="changed"):
        await broker_execution._tick(venue, sid)
    venue.quote.assert_not_called()
    assert deployments.inspect(sid)["status"] == "inactive"
    assert broker_store.orders(sid) == []


@pytest.mark.asyncio
async def test_tick_evaluates_the_pinned_server_snapshot_with_local_capital(setup, monkeypatch):
    from src.services import portfolio_risk_service
    from src.shared.clients import mangrove
    setup.remote["snapshot"]["reward_factor"] = 3
    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    evaluate = Mock(return_value=SimpleNamespace(new_orders=[], model_dump=lambda: {"success": True, "new_orders": []}))
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate)))
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": True})
    venue = SimpleNamespace(quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}))
    await broker_execution._tick(venue, sid)
    payload = evaluate.call_args.args[0]
    assert payload["reward_factor"] == 3
    assert payload["entry"] == setup.remote["snapshot"]["entry"]
    assert payload["execution_config"]["initial_balance"] == 100
    assert evaluate.call_args.kwargs["persist"] is False
    assert broker_store.orders(sid) == []


@pytest.mark.asyncio
async def test_mcp_deployment_tools_require_local_auth(setup, monkeypatch):
    from src.mcp import broker_tools, registry, tools
    functions = {}

    class Server:
        def tool(self, **kwargs):
            def register(function):
                functions[function.__name__] = function
                return function
            return register

    monkeypatch.setattr(registry, "_tools", [])
    monkeypatch.setattr(tools, "_require", lambda key: key == "authorized")
    broker_tools.register(Server())
    arguments = {
        "agent_adopt_strategy": {"server_strategy_id": setup.remote["strategy_id"], "asset_class": "crypto"},
        "agent_get_deployment": {"deployment_id": str(uuid4())},
        "agent_activate_deployment": {"deployment_id": str(uuid4()), "confirm": True},
        "agent_stop_deployment": {"deployment_id": str(uuid4()), "confirm": True},
    }
    for name, args in arguments.items():
        response = json.loads(await functions[name](**args))
        assert response["code"] == "AUTH_INVALID_API_KEY"
    setup.fetch.assert_not_called()
    setup.register.assert_not_called()
    response = json.loads(await functions["agent_adopt_strategy"](**arguments["agent_adopt_strategy"], api_key="authorized"))
    assert response["server_strategy_id"] == setup.remote["strategy_id"]
    assert response["status"] == "inactive"


def test_rest_deployment_routes_require_local_auth(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.router import api_router
    from src.shared.auth import middleware
    from src.shared.errors import AgentError, agent_error_handler

    monkeypatch.setattr(middleware, "_get_config", lambda: SimpleNamespace(AUTH_ENABLED=True, API_KEYS="private-test-key"))
    app = FastAPI()
    app.add_exception_handler(AgentError, agent_error_handler)
    app.include_router(api_router)
    client = TestClient(app)
    body = {"server_strategy_id": setup.remote["strategy_id"], "asset_class": "crypto"}
    assert client.post("/api/v1/agent/strategies/deployments", json=body).status_code == 401
    setup.fetch.assert_not_called()
    result = client.post("/api/v1/agent/strategies/deployments", json=body, headers={"X-API-Key": "private-test-key"})
    assert result.status_code == 200
    sid = result.json()["deployment_id"]
    assert client.post(f"/api/v1/agent/strategies/deployments/{sid}/activate", json={"confirm": True}).status_code == 401
    assert client.post(f"/api/v1/agent/strategies/deployments/{sid}/stop", json={"confirm": True}).status_code == 401


def test_start_and_stop_refuse_an_in_progress_evaluation(setup):
    from src.services import confirmed_execution

    sid = adopt(setup)
    route(sid)
    with confirmed_execution.tick_lock(sid) as acquired:
        assert acquired
        with pytest.raises(SdkError, match="in progress"):
            deployments.activate(sid, True)
        with pytest.raises(SdkError, match="in progress"):
            deployments.stop(sid, True)
    assert deployments.inspect(sid)["status"] == "inactive"
    setup.register.assert_not_called()


def test_portfolio_pause_uses_local_lifecycle_and_retains_exit_monitor_on_restart(setup):
    from src.services import confirmed_execution

    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    pending_order(sid)
    setup.fetch.reset_mock()
    with confirmed_execution.tick_lock(sid) as acquired:
        assert acquired
        strategy_service.pause_for_portfolio(sid)
    assert deployments.inspect(sid)["status"] == "inactive"
    assert strategy_service._get_row(sid)["exit_only_mode"] == "live"
    setup.fetch.assert_not_called()
    setup.cancel.assert_not_called()
    db.reset_connection()
    setup.register.reset_mock()
    deployments.restore_jobs()
    setup.register.assert_called_once_with(sid, "1h", "src.services.strategy_service:tick")
    assert broker_store.orders(sid, pending=True)[0]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_adopted_exit_monitor_keeps_snapshot_and_confirmed_history_with_entries_disabled(setup, monkeypatch):
    from src.services import confirmed_execution, portfolio_risk_service
    from src.shared.clients import mangrove

    setup.remote["snapshot"]["reward_factor"] = 3
    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    with broker_store.connection() as conn:
        conn.execute("UPDATE strategies SET status='inactive',exit_only_mode='live' WHERE id=?", (sid,))
    closes = [{"id": "confirmed-close"}]
    monkeypatch.setattr(confirmed_execution, "broker_history", lambda _: (closes, True))
    monkeypatch.setattr(confirmed_execution, "close_delivery", lambda *a, **kw: (closes, True))
    evaluate = Mock(return_value=SimpleNamespace(new_orders=[], model_dump=lambda: {"success": True, "new_orders": []}))
    monkeypatch.setattr(mangrove, "mangrove_ai_client", lambda: SimpleNamespace(execution=SimpleNamespace(evaluate_by_object=evaluate)))
    monkeypatch.setattr(portfolio_risk_service, "check_before_live_execution", lambda: {"allowed": False})
    venue = SimpleNamespace(quote=AsyncMock(return_value={"ask_price": "100", "bid_price": "100"}))
    await broker_execution._tick(venue, sid)
    payload = evaluate.call_args.args[0]
    assert payload["reward_factor"] == 3
    assert payload["closed_positions"] == closes
    assert payload["closed_positions_bootstrap_complete"] is True
    assert payload["execution_state"]["allow_entries"] is False
    assert evaluate.call_args.kwargs["persist"] is False
    assert broker_store.orders(sid) == []


@pytest.mark.parametrize("with_pending_order", [True, False])
def test_interrupted_stop_restores_only_required_exit_monitor(setup, monkeypatch, with_pending_order):
    sid = adopt(setup)
    route(sid)
    deployments.activate(sid, True)
    if with_pending_order:
        pending_order(sid)
    setup.register.reset_mock()
    setup.fetch.reset_mock()

    class ProcessLost(BaseException):
        pass

    with monkeypatch.context() as interrupted:
        interrupted.setattr(strategy_service, "_apply_scheduler_effects", Mock(side_effect=ProcessLost))
        with pytest.raises(ProcessLost):
            deployments.stop(sid, True)

    db.reset_connection()
    row = strategy_service._get_row(sid)
    assert row["status"] == "inactive"
    assert row["exit_only_mode"] == ("live" if with_pending_order else None)
    deployments.restore_jobs()
    if with_pending_order:
        setup.register.assert_called_once_with(sid, "1h", "src.services.strategy_service:tick")
        setup.cancel.assert_not_called()
        assert broker_store.orders(sid, pending=True)[0]["status"] == "uncertain"
    else:
        setup.register.assert_not_called()
        setup.cancel.assert_called_once_with(sid)
    # A retry stays stopped and never needs server availability.
    assert deployments.stop(sid, True)["status"] == "inactive"
    assert strategy_service._get_row(sid)["exit_only_mode"] == row["exit_only_mode"]
    setup.fetch.assert_not_called()
