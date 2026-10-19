-- Opt-in broker-confirmed events share one per-strategy sequence. Migration
-- 019 remains immutable; copy its close outcomes into the generalized outbox.
ALTER TABLE broker_routes ADD COLUMN external_execution_mode TEXT NOT NULL DEFAULT 'legacy'
    CHECK (external_execution_mode IN ('legacy', 'confirmed_v2'));

CREATE TABLE broker_execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    source_order_id TEXT NOT NULL REFERENCES broker_orders(id),
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    position_id TEXT NOT NULL REFERENCES positions(id),
    asset TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('opened', 'closed')),
    filled_quantity TEXT,
    realized_pnl TEXT,
    fees TEXT,
    occurred_at TEXT,
    observed_at TEXT NOT NULL,
    acknowledged_at TEXT,
    receipt_json TEXT,
    UNIQUE(strategy_id, sequence),
    UNIQUE(strategy_id, event_id),
    UNIQUE(strategy_id, position_id, kind),
    CHECK ((kind='opened' AND filled_quantity IS NOT NULL AND realized_pnl IS NULL AND fees IS NULL)
        OR (kind='closed' AND filled_quantity IS NULL AND realized_pnl IS NOT NULL AND fees IS NOT NULL))
);
INSERT INTO broker_execution_events
    (sequence,event_id,source_order_id,strategy_id,position_id,asset,kind,realized_pnl,fees,occurred_at,observed_at,acknowledged_at)
SELECT sequence,event_id,final_order_id,strategy_id,position_id,asset,'closed',realized_pnl,fees,occurred_at,observed_at,acknowledged_at
FROM broker_close_events;
DROP TABLE broker_close_events;
CREATE INDEX broker_execution_events_pending
    ON broker_execution_events(strategy_id, sequence)
    WHERE acknowledged_at IS NULL;

-- Read compatibility for existing internal diagnostics and pre-v2 tests.
CREATE VIEW broker_close_events AS
SELECT id,sequence,event_id,source_order_id AS final_order_id,strategy_id,position_id,asset,
       realized_pnl,fees,occurred_at,observed_at,acknowledged_at
FROM broker_execution_events WHERE kind='closed';
