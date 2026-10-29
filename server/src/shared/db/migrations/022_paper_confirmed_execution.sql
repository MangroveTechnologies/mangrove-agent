-- Stateless paper owns simulated fills, independently of the live journal.
ALTER TABLE strategies ADD COLUMN paper_execution_state_json TEXT;
UPDATE strategies SET paper_execution_state_json=execution_state_json,execution_state_json=NULL
 WHERE status='paper' AND evaluation_lane='stateless'
 AND NOT EXISTS (SELECT 1 FROM trades WHERE trades.strategy_id=strategies.id AND trades.mode='live')
 AND NOT EXISTS (SELECT 1 FROM allocations WHERE allocations.strategy_id=strategies.id)
 AND NOT EXISTS (SELECT 1 FROM broker_routes WHERE broker_routes.strategy_id=strategies.id);
CREATE TABLE paper_capital (
 strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
 amount TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TABLE paper_fills (
 strategy_id TEXT NOT NULL REFERENCES strategies(id),
 intent_key TEXT NOT NULL,
 trade_id TEXT NOT NULL UNIQUE REFERENCES trades(id),
 template_json TEXT NOT NULL,
 PRIMARY KEY(strategy_id,intent_key)
);
