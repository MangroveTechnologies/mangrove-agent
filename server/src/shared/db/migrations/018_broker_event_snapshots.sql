-- Keep Robinhood's per-execution timestamps separate from local observation time.
ALTER TABLE broker_events ADD COLUMN observed_at TEXT;
ALTER TABLE broker_events ADD COLUMN snapshot_json TEXT;
ALTER TABLE broker_events ADD COLUMN snapshot_key TEXT;

-- Identical broker snapshots are one receipt per order. Null keys preserve any
-- historical broker_events rows written before this migration.
CREATE UNIQUE INDEX broker_event_snapshot_once
    ON broker_events(order_id, snapshot_key)
    WHERE snapshot_key IS NOT NULL;
