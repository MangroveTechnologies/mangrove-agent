from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.services import paper_schedule_service as paper
from src.shared.db import sqlite


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(sqlite.app_config, "DB_PATH", str(tmp_path / "test.db"))
    sqlite.reset_connection()
    sqlite.init_db()
    monkeypatch.setattr(paper, "_scope", lambda: "owner-a")
    remote = Mock()
    remote.execution.paper_start.return_value = {"run_id": str(uuid4())}
    remote.execution.paper_tick.return_value = {"success": True, "execution_state": {"cash_balance": 990}}
    remote.execution.paper_pause.return_value = {"state": "paused"}
    monkeypatch.setattr(paper, "mangrove_ai_client", lambda: remote)
    yield remote
    sqlite.reset_connection()


def start():
    return paper.start(str(uuid4()), 60, 2, True)


def test_confirm_required_without_calls(setup):
    with pytest.raises(Exception, match="Confirm"):
        paper.start(str(uuid4()), 60, 2)
    setup.execution.paper_start.assert_not_called()


def test_same_slot_only_evaluates_once(setup):
    schedule = start()
    paper.tick(schedule["id"])
    paper.tick(schedule["id"])
    assert setup.execution.paper_tick.call_count == 1
    assert paper.inspect(schedule["id"])["evaluations"] == 1


def test_failure_halts_and_reconcile_reuses_slot(setup):
    schedule = start()
    setup.execution.paper_tick.side_effect = TimeoutError()
    paper.tick(schedule["id"])
    args = setup.execution.paper_tick.call_args
    assert paper.inspect(schedule["id"])["state"] == "attention"
    paper.tick(schedule["id"])
    assert setup.execution.paper_tick.call_count == 1
    setup.execution.paper_tick.side_effect = None
    paper.reconcile(schedule["id"])
    assert setup.execution.paper_tick.call_args == args
    assert paper.inspect(schedule["id"])["evaluations"] == 1


def test_pause_stops_locally_before_failed_server_call(setup):
    schedule = start()
    setup.execution.paper_pause.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        paper.pause(schedule["id"])
    assert paper.inspect(schedule["id"])["state"] == "pausing"
    paper.tick(schedule["id"])
    setup.execution.paper_tick.assert_not_called()


def test_pause_retries_keep_same_request_identity(setup):
    schedule = start()
    setup.execution.paper_pause.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        paper.pause(schedule['id'])
    original = setup.execution.paper_pause.call_args
    sqlite.reset_connection()
    setup.execution.paper_pause.side_effect = None
    assert paper.pause(schedule['id'])['state'] == 'paused'
    assert setup.execution.paper_pause.call_args == original


def test_identity_change_hides_schedule(setup, monkeypatch):
    schedule = start()
    monkeypatch.setattr(paper, "_scope", lambda: "owner-b")
    with pytest.raises(Exception, match="not found"):
        paper.inspect(schedule["id"])


def test_budget_stops_new_evaluations(setup, monkeypatch):
    schedule = start()
    for slot in [100, 101, 102]:
        monkeypatch.setattr(paper.time, "time", lambda: slot * 60)
        paper.tick(schedule["id"])
    assert setup.execution.paper_tick.call_count == 2
    assert paper.inspect(schedule["id"])["state"] == "exhausted"


def test_restart_recovers_same_pending_slot(setup):
    schedule = start()
    paper._update(schedule["id"], pending_slot=123, evaluations=1)
    sqlite.reset_connection()
    paper.tick(schedule["id"])
    assert setup.execution.paper_tick.call_args.kwargs["slot"] == 123
    assert paper.inspect(schedule["id"])["evaluations"] == 1


def test_pause_during_inflight_tick_stays_paused(setup):
    schedule = start()

    def response(*args, **kwargs):
        paper.pause(schedule["id"])
        return {"success": True}

    setup.execution.paper_tick.side_effect = response
    paper.tick(schedule["id"])
    assert paper.inspect(schedule["id"])["state"] == "paused"
    paper.tick(schedule["id"])
    assert setup.execution.paper_tick.call_count == 1


def test_dispatch_caps_workers_and_skips_inflight(setup, monkeypatch):
    for _ in range(6):
        start()
    executor = Mock()
    monkeypatch.setattr(paper, "_workers", executor)
    paper._inflight.clear()
    try:
        paper.dispatch()
        paper.dispatch()
        assert executor.submit.call_count == 4
        assert len(paper._inflight) == 4
    finally:
        paper._inflight.clear()


def test_rejected_start_preserves_reason_and_renews_request_on_retry(setup):
    import json

    from mangrove_ai.exceptions import APIError

    from src.mcp.tools import _handle_agent_error
    from src.shared.errors import SdkError

    correlation = str(uuid4())
    reason = "A passing backtest of this exact strategy configuration and market is required"
    setup.execution.paper_start.side_effect = APIError(409, "VALIDATION_ERROR", reason, "PAPER_CONFLICT", correlation)
    strategy = str(uuid4())
    with pytest.raises(SdkError) as caught:
        paper.start(strategy, 60, 2, True)
    response = json.loads(_handle_agent_error(caught.value))
    assert response["message"] == reason
    assert response["correlation_id"] == correlation
    assert response["code"] == "PAPER_START_REJECTED"
    row = paper.list_schedules()["schedules"][0]
    assert row["state"] == "start_rejected"
    assert row["error"] == reason
    paper.tick(row["id"])
    setup.execution.paper_tick.assert_not_called()
    old_request = setup.execution.paper_start.call_args.kwargs["request_id"]
    setup.execution.paper_start.side_effect = None
    result = paper.start(strategy, 60, 2, True)
    assert result["state"] == "active"
    assert result["error"] is None
    assert setup.execution.paper_start.call_args.kwargs["request_id"] != old_request


@pytest.mark.parametrize("failure", [TimeoutError("private upstream text"), KeyError("run_id")])
def test_uncertain_start_keeps_request_on_retry(setup, failure):
    from src.shared.errors import SdkError

    strategy = str(uuid4())
    setup.execution.paper_start.side_effect = failure
    with pytest.raises(SdkError, match="unconfirmed"):
        paper.start(strategy, 60, 2, True)
    row = paper.list_schedules()["schedules"][0]
    assert row["state"] == "start_uncertain"
    args = setup.execution.paper_start.call_args
    setup.execution.paper_start.side_effect = None
    assert paper.start(strategy, 60, 2, True)["state"] == "active"
    assert setup.execution.paper_start.call_args == args


def test_untrusted_rejection_text_is_not_exposed(setup):
    from mangrove_ai.exceptions import APIError

    from src.shared.errors import SdkError

    setup.execution.paper_start.side_effect = APIError(409, "error", "secret payload", "PAPER_CONFLICT", "secret")
    with pytest.raises(SdkError) as caught:
        start()
    assert "secret" not in str(caught.value.to_dict())


def test_pause_during_failed_start_wins(setup):
    from src.shared.errors import SdkError

    def response(*args, **kwargs):
        schedule = paper.list_schedules()["schedules"][0]
        paper.pause(schedule["id"])
        raise TimeoutError()

    setup.execution.paper_start.side_effect = response
    with pytest.raises(SdkError):
        start()
    assert paper.list_schedules()["schedules"][0]["state"] == "paused"


def test_malformed_success_is_uncertain(setup):
    from src.shared.errors import SdkError

    setup.execution.paper_start.return_value = {"run_id": None}
    with pytest.raises(SdkError):
        start()
    assert paper.list_schedules()["schedules"][0]["state"] == "start_uncertain"


@pytest.mark.parametrize("status,code", [(409, "PAPER_BUSY"), (503, "SERVICE_UNAVAILABLE")])
def test_busy_or_unavailable_start_is_uncertain(setup, status, code):
    from mangrove_ai.exceptions import APIError

    from src.shared.errors import SdkError

    setup.execution.paper_start.side_effect = APIError(status, "error", "details", code)
    with pytest.raises(SdkError):
        start()
    assert paper.list_schedules()["schedules"][0]["state"] == "start_uncertain"


def test_access_rejection_preserves_access_error(setup):
    from mangrove_ai.exceptions import APIError

    from src.shared.errors import UpstreamAccessError

    setup.execution.paper_start.side_effect = APIError(403, "error", "private", "FORBIDDEN")
    with pytest.raises(UpstreamAccessError):
        start()
    assert paper.list_schedules()["schedules"][0]["state"] == "start_rejected"


@pytest.mark.parametrize('pause_fails', [False, True])
def test_pending_tick_survives_pause_and_reconciles_without_resuming(setup, pause_fails):
    schedule = start()
    setup.execution.paper_tick.side_effect = TimeoutError()
    paper.tick(schedule['id'])
    original = setup.execution.paper_tick.call_args
    if pause_fails:
        setup.execution.paper_pause.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            paper.pause(schedule['id'])
    else:
        paper.pause(schedule['id'])
    state = paper.inspect(schedule['id'])['state']
    sqlite.reset_connection()
    with pytest.raises(Exception, match='Reconcile'):
        paper.start(schedule['strategy_id'], 60, 2, True)
    setup.execution.paper_tick.side_effect = None
    result = paper.reconcile(schedule['id'])
    assert setup.execution.paper_tick.call_args == original
    assert result['state'] == state
    assert result['pending_slot'] is None
    assert result['evaluations'] == 1
    paper.tick(schedule['id'])
    assert setup.execution.paper_tick.call_count == 2
    if pause_fails:
        assert result['error'] is not None


@pytest.mark.parametrize('late_failure', [False, True])
def test_late_pause_cannot_overwrite_new_start(setup, late_failure):
    schedule = start()
    def delayed(*args, **kwargs):
        setup.execution.paper_pause.side_effect = None
        paper.pause(schedule['id'])
        paper.start(schedule['strategy_id'], 60, 2, True)
        if late_failure:
            raise TimeoutError()
        return {'state': 'paused'}
    setup.execution.paper_pause.side_effect = delayed
    if late_failure:
        with pytest.raises(TimeoutError):
            paper.pause(schedule['id'])
    else:
        result = paper.pause(schedule['id'])
        assert result['pause_superseded'] is True
        assert 'server' not in result
    assert paper.inspect(schedule['id'])['state'] == 'active'
    assert paper.inspect(schedule['id'])['error'] is None


def test_failed_reconciliation_preserves_paused_pending_tick(setup):
    schedule = start()
    setup.execution.paper_tick.side_effect = TimeoutError()
    paper.tick(schedule['id'])
    paper.pause(schedule['id'])
    before = paper.inspect(schedule['id'])
    after = paper.reconcile(schedule['id'])
    assert after['state'] == 'paused'
    assert after['pending_slot'] == before['pending_slot']
    assert after['evaluations'] == before['evaluations']
