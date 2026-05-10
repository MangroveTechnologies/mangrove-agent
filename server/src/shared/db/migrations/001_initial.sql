-- mangrove-agent initial schema.
-- All tables from docs/specification.md SQLite Schema section.
-- APScheduler manages its own tables via SQLAlchemyJobStore.

-- Wallets: encrypted local key storage
CREATE TABLE IF NOT EXISTS wallets (
    id TEXT PRIMARY KEY,                          -- UUID
    address TEXT UNIQUE NOT NULL,
    chain TEXT NOT NULL,                          -- evm | xrpl
    network TEXT NOT NULL,                        -- mainnet | testnet
    chain_id INTEGER,
    encrypted_secret BLOB NOT NULL,               -- Fernet-encrypted seed phrase
    encryption_method TEXT NOT NULL,              -- 'fernet-v1'
    label TEXT,
    created_at TEXT NOT NULL,                     -- ISO 8601
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallets_chain ON wallets(chain, chain_id);

-- Strategies: local cache of Mangrove strategies (Mangrove is source of truth)
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,                          -- agent's UUID
    mangrove_id TEXT UNIQUE NOT NULL,             -- Mangrove's strategy ID
    name TEXT NOT NULL,
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,                         -- draft | inactive | paper | live | archived
    entry_json TEXT NOT NULL,                     -- list[StrategyRule]
    exit_json TEXT NOT NULL,
    execution_config_json TEXT NOT NULL,
    generation_report_json TEXT,                  -- null for manual strategies
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);

-- Allocations: per-strategy fund commitments (live only)
CREATE TABLE IF NOT EXISTS allocations (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    token_address TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,            -- boolean
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_strategy ON allocations(strategy_id, active);

-- Evaluations: every cron tick
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    timestamp TEXT NOT NULL,
    market_snapshot_json TEXT NOT NULL,           -- data sent to the SDK
    sdk_response_json TEXT NOT NULL,              -- verbatim response from mangroveai.execution.evaluate()
    order_intents_json TEXT NOT NULL,             -- extracted from sdk_response for querying
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,                         -- ok | error | skipped
    error_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_evaluations_strategy_ts ON evaluations(strategy_id, timestamp DESC);

-- Trades: every order intent -> execution
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    evaluation_id TEXT REFERENCES evaluations(id),
    order_intent_json TEXT NOT NULL,
    mode TEXT NOT NULL,                           -- live | paper
    tx_hash TEXT,                                 -- null for paper
    input_token TEXT NOT NULL,
    input_amount REAL NOT NULL,
    output_token TEXT NOT NULL,
    output_amount REAL NOT NULL,
    fill_price REAL NOT NULL,
    fees_json TEXT NOT NULL,
    status TEXT NOT NULL,                         -- pending | confirmed | failed | simulated
    executed_at TEXT NOT NULL,
    confirmed_at TEXT,
    p_and_l REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

-- Positions: derived from trades, cached for fast evaluator access
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    asset TEXT NOT NULL,
    entry_trade_id TEXT NOT NULL REFERENCES trades(id),
    exit_trade_id TEXT REFERENCES trades(id),
    entry_price REAL NOT NULL,
    entry_amount REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_price REAL,
    exit_amount REAL,
    exit_time TEXT,
    status TEXT NOT NULL,                         -- open | closed
    stop_loss REAL,
    take_profit REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_status ON positions(strategy_id, status);
