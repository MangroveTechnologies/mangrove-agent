"""Local clock and bounded requests for server-owned paper execution."""

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

from src.config import app_config
from src.shared.clients.mangrove import _api_key, _api_key_base_url, _payment_destination, mangrove_ai_client
from src.shared.db.sqlite import get_connection
from src.shared.errors import AgentError, SdkError, ValidationError, upstream_access_error

_lock = threading.RLock()
_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="paper-tick")
_inflight = set()
INTERVALS = {60, 300, 900, 3600, 14400, 86400}


@contextmanager
def _database():
    if str(app_config.DB_PATH) == ":memory:":
        yield get_connection()
        return
    conn = sqlite3.connect(str(app_config.DB_PATH), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _scope():
    key = _api_key(app_config)
    if key:
        identity = ["api-key", _api_key_base_url(app_config), key]
    else:
        from src.services.x402_payer import resolve_payer_wallet

        identity = ["x402", _payment_destination(app_config), resolve_payer_wallet().lower()]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _get(schedule_id):
    with _database() as conn:
        row = conn.execute("SELECT * FROM paper_schedules WHERE id=? AND scope=?", (schedule_id, _scope())).fetchone()
    if not row:
        raise ValidationError("Paper schedule not found for the current upstream identity")
    return dict(row)


def _update(schedule_id, **values):
    with _database() as conn:
        conn.execute(
            "UPDATE paper_schedules SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), schedule_id),
        )
        conn.commit()



def _start_error(exc):
    from mangrove_ai.exceptions import APIError

    access = upstream_access_error(exc)
    if access is not None:
        return "start_rejected", access
    if isinstance(exc, AgentError):
        return "start_uncertain", exc
    rejected = isinstance(exc, APIError) and exc.status_code in {400, 404, 409, 422} and exc.code != "PAPER_BUSY"
    state = "start_rejected" if rejected else "start_uncertain"
    message = (
        "MangroveAI rejected the paper start. No evaluations will be scheduled."
        if rejected else
        "The paper start outcome is unconfirmed. No evaluations will be scheduled; recover using the same start request."
    )
    if isinstance(exc, APIError) and exc.code == "PAPER_CONFLICT":
        reasons = {
            "A passing backtest of this exact strategy configuration and market is required",
            "Server-owned paper execution does not support v3 yet",
            "Paper execution requires a backtested, unarchived strategy",
            "Strategy already belongs to another execution flow",
            "Strategy requires valid server-owned paper balances",
        }
        if exc.message in reasons:
            message = exc.message
    correlation_id = None
    if isinstance(exc, APIError) and exc.correlation_id:
        try:
            correlation_id = str(UUID(str(exc.correlation_id)))
        except ValueError:
            pass
    error = SdkError(message, correlation_id=correlation_id)
    error.code = "PAPER_START_REJECTED" if rejected else "PAPER_START_UNCONFIRMED"
    error.http_status = 409 if rejected else 502
    return state, error

def start(strategy_id, interval_seconds, max_evaluations, confirm=False):
    if confirm is not True:
        raise ValidationError("Confirm recurring evaluations and their API/x402 costs with confirm=true")
    strategy_id = str(UUID(str(strategy_id)))
    if type(interval_seconds) is not int or interval_seconds not in INTERVALS:
        raise ValidationError("Unsupported evaluation interval")
    if type(max_evaluations) is not int or not 1 <= max_evaluations <= 10000:
        raise ValidationError("max_evaluations must be between 1 and 10000")
    client = mangrove_ai_client().execution
    if not hasattr(client, "paper_start"):
        raise ValidationError("Install the MangroveAI SDK release with paper execution support")
    with _lock, _database() as conn:
        row = conn.execute(
            "SELECT * FROM paper_schedules WHERE scope=? AND strategy_id=?", (_scope(), strategy_id)
        ).fetchone()
        if row:
            row = dict(row)
            if row["id"] in _inflight:
                raise ValidationError("Wait for the current evaluation before starting this schedule")
            if row["interval_seconds"] != interval_seconds or row["max_evaluations"] != max_evaluations:
                raise ValidationError("Existing schedule settings differ; use its current limits")
            if row["state"] == "active":
                return inspect(row["id"])
            if row["pending_slot"] is not None:
                raise ValidationError("Reconcile the unresolved evaluation before starting again")
            if row["state"] not in {"starting", "start_uncertain", "start_rejected", "paused", "exhausted"}:
                raise ValidationError("Reconcile the existing schedule before starting it again")
            if row["state"] not in {"starting", "start_uncertain"}:
                _update(
                    row["id"],
                    state="starting",
                    request_id=str(uuid4()),
                    evaluations=0,
                    pending_slot=None,
                    last_slot=None,
                    last_result=None,
                    error=None,
                )
            schedule_id = row["id"]
        else:
            schedule_id = str(uuid4())
            conn.execute(
                "INSERT INTO paper_schedules (id,scope,strategy_id,controller_id,interval_seconds,max_evaluations,state,request_id) VALUES (?,?,?,?,?,?,'starting',?)",
                (schedule_id, _scope(), strategy_id, str(uuid4()), interval_seconds, max_evaluations, str(uuid4())),
            )
            conn.commit()
        row = _get(schedule_id)
        _update(schedule_id, state="starting")
    try:
        result = client.paper_start(
            strategy_id, controller_id=row["controller_id"], request_id=row["request_id"], interval_seconds=interval_seconds
        )
        run_id = str(UUID(str(result["run_id"])))
    except Exception as exc:
        state, error = _start_error(exc)
        with _lock:
            current = _get(schedule_id)
            if current["state"] == "starting" and current["request_id"] == row["request_id"]:
                _update(schedule_id, state=state, error=error.message)
        raise error from exc
    with _lock:
        # A concurrent pause wins; never reactivate a locally stopped schedule.
        current = _get(schedule_id)
        if current["state"] == "starting" and current["request_id"] == row["request_id"]:
            _update(schedule_id, run_id=run_id, state="active", error=None)
    return inspect(schedule_id)


def pause(schedule_id):
    with _lock:
        row = _get(schedule_id)
        request_id = row["request_id"] if row["state"] == "pausing" else str(uuid4())
        _update(schedule_id, state="pausing", request_id=request_id)
    try:
        result = mangrove_ai_client().execution.paper_pause(
            row["strategy_id"], controller_id=row["controller_id"], request_id=request_id,
        )
    except Exception:
        with _lock:
            current = _get(schedule_id)
            if current["state"] == "pausing" and current["request_id"] == request_id:
                _update(schedule_id, error="Local scheduling stopped; server pause is unconfirmed. Review the returned error before retrying.")
        raise
    with _lock:
        current = _get(schedule_id)
        if current["state"] != "pausing" or current["request_id"] != request_id:
            return {**inspect(schedule_id), "pause_superseded": True}
        _update(schedule_id, state="paused", error=None)
        return {**inspect(schedule_id), "server": result, "positions_closed": False}


def list_schedules():
    with _lock, _database() as conn:
        rows = conn.execute("SELECT id FROM paper_schedules WHERE scope=? ORDER BY id", (_scope(),)).fetchall()
        return {"schedules": [inspect(row["id"]) for row in rows]}


def inspect(schedule_id, refresh=False):
    with _lock:
        row = _get(schedule_id)
        result = {
            key: row[key]
            for key in (
                "id",
                "strategy_id",
                "run_id",
                "state",
                "interval_seconds",
                "max_evaluations",
                "evaluations",
                "error",
                "pending_slot",
            )
        }
        result["last_result"] = json.loads(row["last_result"]) if row["last_result"] else None
    if refresh:
        result["server"] = mangrove_ai_client().execution.paper_status(row["strategy_id"])
    return result


def tick(schedule_id, *, recovery=False):
    try:
        with _lock:
            row = _get(schedule_id)
            if recovery:
                if row["pending_slot"] is None or row["state"] not in {"attention", "paused", "pausing"}:
                    return
            elif row["state"] != "active":
                return
            slot = row["pending_slot"]
            if slot is None:
                slot = int(time.time()) // row["interval_seconds"]
                if row["last_slot"] == slot:
                    return
                if row["evaluations"] >= row["max_evaluations"]:
                    _update(schedule_id, state="exhausted")
                    return
                _update(schedule_id, pending_slot=slot, evaluations=row["evaluations"] + 1)
        result = mangrove_ai_client().execution.paper_tick(
            row["strategy_id"],
            controller_id=row["controller_id"],
            run_id=row["run_id"],
            slot=slot,
        )
        if result.get("success") is not True:
            raise ValidationError("Server evaluation did not succeed")
        with _lock:
            current = _get(schedule_id)
            if current["run_id"] == row["run_id"] and current["pending_slot"] == slot:
                values = dict(pending_slot=None, last_slot=slot, last_result=json.dumps(result))
                if current["request_id"] == row["request_id"] and current["state"] == row["state"]:
                    if current['state'] != 'pausing':
                        values['error'] = None
                    if recovery and current['state'] == 'attention':
                        values['state'] = 'active'
                _update(schedule_id, **values)
    except Exception:
        with _lock, _database() as conn:
            row = conn.execute("SELECT state FROM paper_schedules WHERE id=?", (schedule_id,)).fetchone()
            if row and row[0] == "active":
                _update(
                    schedule_id,
                    state="attention",
                    error="Evaluation outcome needs reconciliation; no further calls will be scheduled",
                )
    finally:
        with _lock:
            _inflight.discard(schedule_id)


def reconcile(schedule_id):
    with _lock:
        row = _get(schedule_id)
        if row["state"] not in {"attention", "paused", "pausing"} or row["pending_slot"] is None:
            raise ValidationError("No unresolved evaluation to reconcile")
        if schedule_id in _inflight:
            raise ValidationError("Evaluation already in progress")
        _inflight.add(schedule_id)
    tick(schedule_id, recovery=True)
    return inspect(schedule_id)


def dispatch():
    try:
        scope = _scope()
    except Exception:
        return
    with _lock, _database() as conn:
        if len(_inflight) >= 4:
            return
        rows = conn.execute(
            "SELECT id,interval_seconds,last_slot,pending_slot FROM paper_schedules WHERE scope=? AND state='active' ORDER BY COALESCE(last_slot,-1)",
            (scope,),
        ).fetchall()
        for row in rows:
            if len(_inflight) >= 4:
                break
            if row["id"] in _inflight or (
                row["pending_slot"] is None and row["last_slot"] == int(time.time()) // row["interval_seconds"]
            ):
                continue
            _inflight.add(row["id"])
            _workers.submit(tick, row["id"])
