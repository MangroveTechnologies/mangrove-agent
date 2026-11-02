"""Adopt server-owned definitions into the existing local broker lifecycle."""

import json
from uuid import UUID, uuid4

from src.services import broker_store as store
from src.services import scheduler_service, strategy_service
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import SdkError, ValidationError


def _scope():
    # Same credential/destination boundary as the existing server paper client.
    # In particular, rejected API credentials never fall back to wallet payment.
    from src.services.paper_schedule_service import _scope as upstream_scope
    return upstream_scope()


def binding(strategy_id):
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM local_deployments WHERE strategy_id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None


def _fetch(server_strategy_id, asset_class):
    service = mangrove_ai_client().strategies
    if not hasattr(service, "local_deployment"):
        raise ValidationError("Install the MangroveAI SDK release with local_deployment support")
    result = service.local_deployment(server_strategy_id, asset_class=asset_class)
    if (result.get("contract_version") != 1 or result.get("strategy_id") != server_strategy_id
            or result.get("asset_class") != asset_class):
        raise ValidationError("Unsupported or mismatched server deployment contract")
    snapshot = result.get("snapshot") or {}
    owner = result.get("owner") or {}
    if (not owner.get("user_id") or not owner.get("org_id")
            or not isinstance(result.get("fingerprint"), str) or len(result["fingerprint"]) != 64
            or snapshot.get("quote_token") != "USD" or not snapshot.get("market_data_venue")
            or snapshot.get("position_size_calc") not in {"v1", "v2"}
            or result.get("timeframe") not in scheduler_service._TIMEFRAME_TO_CRON
            or not snapshot.get("entry") or not isinstance(snapshot.get("execution_config"), dict)):
        raise ValidationError("Incomplete or unsupported server deployment snapshot")
    # Validate against the existing local representation before any durable
    # write; a server market the runner cannot represent must not half-adopt.
    strategy_service.StrategyMarketContext.model_validate({
        key: snapshot.get(key) for key in ("base_token", "quote_token", "market_data_venue")
    })
    if snapshot.get("base_token") != snapshot.get("asset"):
        raise ValidationError("Server snapshot base token does not match its asset")
    return result


def inspect(strategy_id):
    bound = binding(strategy_id)
    if not bound or bound["scope"] != _scope():
        raise ValidationError("Local deployment not found for the current upstream identity")
    return _inspect(strategy_id, bound)


def _inspect(strategy_id, bound):
    row = strategy_service._get_row(strategy_id)
    return {
        "deployment_id": strategy_id, "server_strategy_id": bound["server_strategy_id"],
        "fingerprint": bound["fingerprint"], "asset_class": bound["asset_class"],
        "status": row["status"], "pending_orders": len(store.orders(strategy_id, pending=True)),
        "strategy": strategy_service.get_strategy(strategy_id).model_dump(mode="json"),
    }


def validate(strategy_id, *, require_ready=False):
    bound = binding(strategy_id)
    if not bound or bound["scope"] != _scope():
        raise ValidationError("Adopt the saved server strategy for the current upstream identity first")
    row = strategy_service._get_row(strategy_id)
    if row["mangrove_id"] != bound["server_strategy_id"]:
        raise ValidationError("Local and server strategy identities do not match")
    current = _fetch(bound["server_strategy_id"], bound["asset_class"])
    if (current["owner"] != json.loads(bound["owner_json"])
            or current["fingerprint"] != bound["fingerprint"]
            or current["snapshot"] != json.loads(bound["snapshot_json"])
            or current["timeframe"] != row["timeframe"]):
        raise ValidationError("Server strategy changed; stop and explicitly rebind before execution")
    if require_ready and current.get("activation_ready") is not True:
        raise ValidationError(current.get("activation_blocker") or "Server strategy is not ready for activation")
    return current


def adopt(server_strategy_id, asset_class, rebind=False):
    from src.services.broker_execution import _lock

    server_strategy_id = str(UUID(server_strategy_id))
    if asset_class not in {"crypto", "equity"}:
        raise ValidationError("asset_class must be crypto or equity")
    with _lock:
        scope = _scope()
        remote = _fetch(server_strategy_id, asset_class)
        snapshot = remote["snapshot"]
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM strategies WHERE mangrove_id=?", (server_strategy_id,)).fetchone()
            strategy_id = existing["id"] if existing else str(uuid4())
            bound = conn.execute("SELECT * FROM local_deployments WHERE strategy_id=?", (strategy_id,)).fetchone()
            if existing:
                if not bound or bound["scope"] != scope or json.loads(bound["owner_json"]) != remote["owner"]:
                    raise ValidationError("A conflicting local execution record already exists")
                if bound["fingerprint"] == remote["fingerprint"] and bound["asset_class"] == asset_class:
                    return {**inspect(strategy_id), "activation_ready": remote.get("activation_ready") is True,
                            "activation_blocker": remote.get("activation_blocker")}
                if not rebind:
                    raise ValidationError("Server strategy changed; explicitly rebind the inactive deployment")
                if existing["status"] != "inactive":
                    raise ValidationError("Stop the deployment before rebinding")
                if (conn.execute("SELECT 1 FROM broker_orders WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
                        or conn.execute("SELECT 1 FROM trades WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()):
                    raise ValidationError("Execution history cannot be rebound; use a new saved server strategy")
                # A changed definition needs a new allocation review as well.
                conn.execute("DELETE FROM broker_routes WHERE strategy_id=?", (strategy_id,))
            now = store.now()
            conn.execute(
                """INSERT INTO strategies(id,mangrove_id,name,asset,timeframe,status,entry_json,exit_json,
                execution_config_json,created_at,updated_at,evaluation_lane,market_context_json)
                VALUES (?,?,?,?,?,'inactive',?,?,?,?,?,'stateless',?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,asset=excluded.asset,timeframe=excluded.timeframe,
                entry_json=excluded.entry_json,exit_json=excluded.exit_json,execution_config_json=excluded.execution_config_json,
                market_context_json=excluded.market_context_json,updated_at=excluded.updated_at""",
                (strategy_id, server_strategy_id, remote["name"], snapshot["asset"], remote["timeframe"],
                 json.dumps(snapshot["entry"]), json.dumps(snapshot.get("exit") or []),
                 json.dumps(snapshot["execution_config"]), now, now,
                 json.dumps({key: snapshot[key] for key in ("base_token", "quote_token", "market_data_venue")})),
            )
            conn.execute(
                """INSERT INTO local_deployments VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(strategy_id)
                DO UPDATE SET fingerprint=excluded.fingerprint,asset_class=excluded.asset_class,snapshot_json=excluded.snapshot_json""",
                (strategy_id, server_strategy_id, scope, json.dumps(remote["owner"]), remote["fingerprint"],
                 asset_class, json.dumps(snapshot), now),
            )
        return {**inspect(strategy_id), "activation_ready": remote.get("activation_ready") is True,
                "activation_blocker": remote.get("activation_blocker")}


def activate(strategy_id, confirm=False):
    from src.services import broker_execution, confirmed_execution

    with broker_execution._lock:
        with confirmed_execution.tick_lock(strategy_id, purpose="status") as acquired:
            if not acquired:
                raise SdkError("Strategy evaluation is in progress; retry activation after it completes.")
            return _activate(strategy_id, confirm)


def _activate(strategy_id, confirm=False):
    from src.services import broker_execution

    if confirm is not True:
        raise ValidationError("Explicit authorization for live broker execution and recurring API/x402 costs requires confirm=true")
    with broker_execution._lock:
        validate(strategy_id, require_ready=True)
        row = strategy_service._get_row(strategy_id)
        if row["status"] not in {"inactive", "live"}:
            raise ValidationError("Only an inactive deployment can be activated")
        if row["status"] == "live":
            return inspect(strategy_id)
        route = store.route(strategy_id)
        if not route:
            raise ValidationError("Configure the Robinhood allocation before activation")
        if store.orders(strategy_id, pending=True):
            raise ValidationError("Reconcile pending broker orders before activation")
        broker_execution.run(broker_execution.preflight, route)
        # Persist before scheduling. A crash between these steps is repaired by
        # scheduler startup; ticks always check the durable lifecycle state.
        with store.connection() as conn:
            conn.execute("UPDATE strategies SET status='live',updated_at=? WHERE id=?", (store.now(), strategy_id))
        try:
            strategy_service._apply_scheduler_effects(strategy_id, row, "live", row["status"])
        except Exception:
            with store.connection() as conn:
                conn.execute("UPDATE strategies SET status='inactive' WHERE id=?", (strategy_id,))
            raise
        if row["status"] != "live":
            from src.services import portfolio_risk_service
            portfolio_risk_service.rebaseline(reason="local_deployment_live")
        return inspect(strategy_id)


def stop(strategy_id, confirm=False):
    from src.services import broker_execution, confirmed_execution

    with broker_execution._lock:
        with confirmed_execution.tick_lock(strategy_id, purpose="status") as acquired:
            if not acquired:
                raise SdkError("Strategy evaluation is in progress; retry stopping after it completes.")
            return _stop(strategy_id, confirm)


def _stop(strategy_id, confirm=False):
    from src.services import broker_execution

    if confirm is not True:
        raise ValidationError("Stopping live execution requires confirm=true")
    with broker_execution._lock:
        # Stopping never depends on upstream availability or a billable read.
        bound = binding(strategy_id)
        if not bound:
            raise ValidationError("Local deployment not found")
        row = strategy_service._get_row(strategy_id)
        exit_only_mode = strategy_service._exit_monitor_mode(strategy_id, row, row["status"])
        # Commit the stop and retained exit monitor together. Recovery must not
        # mistake an interrupted stop for an inactive, unmonitored deployment.
        with store.connection() as conn:
            conn.execute(
                "UPDATE strategies SET status='inactive',exit_only_mode=?,updated_at=? WHERE id=?",
                (exit_only_mode, store.now(), strategy_id),
            )
        strategy_service._apply_scheduler_effects(strategy_id, row, "inactive", row["status"])
        if row["status"] == "live":
            from src.services import portfolio_risk_service
            portfolio_risk_service.rebaseline(reason="local_deployment_stopped")
        return {**_inspect(strategy_id, bound), "pending_orders_canceled": False}


def restore_jobs():
    """Restore committed activation and stop state using the existing scheduler."""
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT s.id,s.timeframe,s.status,s.exit_only_mode FROM strategies s JOIN local_deployments d ON d.strategy_id=s.id"
        ).fetchall()
    for row in rows:
        if row["status"] == "live" or row["exit_only_mode"] == "live":
            scheduler_service.register_job(row["id"], row["timeframe"], "src.services.strategy_service:tick")
        else:
            scheduler_service.cancel_job(row["id"])
