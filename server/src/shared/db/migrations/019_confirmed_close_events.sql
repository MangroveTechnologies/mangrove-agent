-- Durable outbox of fully closed positions for future Mangrove risk
-- acknowledgement. A position contributes one event regardless of how many
-- cumulative snapshots or partial sell fills closed it. Outcomes aggregate
-- from cumulative broker_orders so pre-migration partial fills are retained.
CREATE TABLE broker_close_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    final_order_id TEXT NOT NULL REFERENCES broker_orders(id),
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    position_id TEXT NOT NULL REFERENCES positions(id),
    asset TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    fees TEXT NOT NULL,
    occurred_at TEXT,
    observed_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE(strategy_id,sequence),
    UNIQUE(strategy_id,event_id),
    UNIQUE(strategy_id,position_id)
);
CREATE INDEX broker_close_events_pending
    ON broker_close_events(strategy_id, sequence)
    WHERE acknowledged_at IS NULL;
