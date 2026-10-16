-- Explicit market selection for caller-owned (stateless) evaluation.
ALTER TABLE strategies ADD COLUMN market_context_json TEXT;
