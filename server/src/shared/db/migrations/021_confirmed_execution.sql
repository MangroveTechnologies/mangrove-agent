-- Execution proposals never become the confirmed local book. Revisions fence
-- risk responses against another evaluation or an intervening actual fill.
ALTER TABLE strategies ADD COLUMN execution_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE strategies ADD COLUMN book_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE strategies ADD COLUMN exit_only_mode TEXT CHECK (exit_only_mode IN ('live', 'paper'));
ALTER TABLE trades ADD COLUMN execution_context_json TEXT NOT NULL DEFAULT '{}';

DROP INDEX broker_one_pending_per_strategy;
CREATE UNIQUE INDEX broker_one_pending_per_position ON broker_orders(
    strategy_id, COALESCE(json_extract(intent_json, '$.engine_position_id'), '__unknown__')
) WHERE status IN ('submitting','uncertain','open','partial','awaiting_approval','cancel_pending');
CREATE UNIQUE INDEX broker_one_pending_entry_per_strategy ON broker_orders(strategy_id)
WHERE status IN ('submitting','uncertain','open','partial','awaiting_approval','cancel_pending')
AND json_extract(intent_json, '$.side') = 'buy';

-- Unresolved broadcasts carry no execution amounts or estimated fills.
CREATE TABLE execution_capital (
 strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
 allocation_id TEXT NOT NULL REFERENCES allocations(id),
 quote_token_address TEXT NOT NULL,
 quote_token_symbol TEXT NOT NULL,
 amount TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE dex_orders (
 id TEXT PRIMARY KEY,
 strategy_id TEXT NOT NULL REFERENCES strategies(id),
 evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
 intent_key TEXT NOT NULL,
 intent_json TEXT NOT NULL,
 template_json TEXT NOT NULL,
 context_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL,
 tx_hash TEXT,
 error TEXT,
 created_at TEXT NOT NULL,
 UNIQUE(strategy_id,intent_key)
);
CREATE UNIQUE INDEX dex_pending_position ON dex_orders(strategy_id,COALESCE(json_extract(intent_json,'$.engine_position_id'),'__unknown__'))
 WHERE status IN ('submitting','pending','uncertain');
CREATE UNIQUE INDEX dex_pending_entry ON dex_orders(strategy_id)
 WHERE status IN ('submitting','pending','uncertain') AND json_extract(intent_json,'$.action')='enter';
CREATE TABLE dex_holdings (
 position_id TEXT PRIMARY KEY REFERENCES positions(id),
 strategy_id TEXT NOT NULL REFERENCES strategies(id),
 quantity TEXT NOT NULL,
 unit_cost TEXT NOT NULL,
 financial_complete INTEGER NOT NULL
);
