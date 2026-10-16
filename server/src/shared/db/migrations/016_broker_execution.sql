-- Broker orders are durable intents; trades below represent actual fills only.
CREATE TABLE broker_routes (
    strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
    venue TEXT NOT NULL CHECK (venue = 'robinhood'),
    account_number TEXT NOT NULL,
    rhs_account_number TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK (asset_class IN ('equity', 'crypto')),
    capital TEXT NOT NULL,
    max_order_usd TEXT NOT NULL,
    slippage_pct TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE broker_orders (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
    intent_key TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    position_template_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    broker_order_id TEXT,
    approval_id TEXT,
    status TEXT NOT NULL,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    filled_notional TEXT NOT NULL DEFAULT '0',
    fees TEXT NOT NULL DEFAULT '0',
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(strategy_id, intent_key)
);
CREATE UNIQUE INDEX broker_one_pending_per_strategy ON broker_orders(strategy_id)
    WHERE status IN ('submitting', 'uncertain', 'open', 'partial', 'awaiting_approval', 'cancel_pending');
CREATE TABLE broker_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES broker_orders(id),
    status TEXT NOT NULL,
    filled_quantity TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE trades ADD COLUMN venue TEXT;
ALTER TABLE trades ADD COLUMN venue_order_ref TEXT;
ALTER TABLE trades ADD COLUMN venue_trade_ref TEXT;
-- Preserve exact broker quantities/cost alongside legacy REAL-valued UI rows.
CREATE TABLE broker_holdings (
    position_id TEXT PRIMARY KEY REFERENCES positions(id),
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    quantity TEXT NOT NULL,
    unit_cost TEXT NOT NULL
);
