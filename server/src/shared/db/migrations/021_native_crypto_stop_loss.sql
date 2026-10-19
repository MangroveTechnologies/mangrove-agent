-- Native crypto protection is explicit, opt-in, and driven by hosted stop
-- templates. Existing routes remain disabled after upgrade.
ALTER TABLE broker_routes ADD COLUMN native_stop_loss_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (native_stop_loss_enabled IN (0, 1));

-- Protective orders share the existing durable submission/fill journal and
-- its one-pending-order-per-strategy serialization invariant.
ALTER TABLE broker_orders ADD COLUMN order_kind TEXT NOT NULL DEFAULT 'strategy'
    CHECK (order_kind IN ('strategy', 'protective_stop'));
ALTER TABLE broker_orders ADD COLUMN protected_position_id TEXT;
ALTER TABLE broker_orders ADD COLUMN hosted_stop_price TEXT;
ALTER TABLE broker_orders ADD COLUMN routed_stop_price TEXT;
ALTER TABLE broker_orders ADD COLUMN cancel_requested_at TEXT;

CREATE TABLE broker_protection_state (
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    position_id TEXT NOT NULL REFERENCES positions(id),
    desired_stop_price TEXT,
    routed_stop_price TEXT,
    desired_quantity TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL,
    detail TEXT,
    active_order_id TEXT REFERENCES broker_orders(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(strategy_id, position_id)
);

-- A hosted exit may arrive while a native stop cancellation is pending. Keep
-- the exact authorized exit across restart instead of consuming the signal.
CREATE TABLE broker_deferred_exits (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    position_id TEXT NOT NULL REFERENCES positions(id),
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
    engine_order_id TEXT,
    intent_json TEXT NOT NULL,
    position_template_json TEXT NOT NULL,
    decision_quantity TEXT NOT NULL,
    blocker_order_id TEXT REFERENCES broker_orders(id),
    blocker_filled_at_decision TEXT NOT NULL DEFAULT '0',
    route_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    reserved_order_id TEXT REFERENCES broker_orders(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX broker_one_waiting_exit_per_position ON broker_deferred_exits(strategy_id,position_id)
    WHERE status IN ('waiting_stop_terminal','blocked');
