-- mangrove-agent#146: portfolio kill switch (agent-side circuit breaker).
--
-- The agent owns the whole live book locally, so aggregate drawdown across
-- every live strategy is measurable here (the MangroveAI engine only ever sees
-- one strategy's account per eval). This single-row table tracks the live-book
-- high-water mark and the LATCHED trip state.
--
-- Basis is realized P&L:
--   book_value = SUM(active live allocations.amount)
--              + SUM(realized p_and_l over live strategies' trades)
-- When drawdown from the high-water mark reaches PORTFOLIO_MAX_DRAWDOWN_PCT the
-- switch trips: ALL live strategies are set inactive and the latch is set. It
-- does NOT auto-resume (unlike the per-strategy engine breaker) -- a human must
-- explicitly reset it. This is the go-trader portfolio-kill-switch model.
CREATE TABLE IF NOT EXISTS portfolio_risk (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- enforce a single row
    high_water_mark REAL    NOT NULL DEFAULT 0,
    tripped         INTEGER NOT NULL DEFAULT 0,          -- 0/1 latch
    tripped_at      TEXT,
    tripped_reason  TEXT,
    updated_at      TEXT    NOT NULL
);

INSERT OR IGNORE INTO portfolio_risk (id, high_water_mark, tripped, updated_at)
VALUES (1, 0, 0, '1970-01-01T00:00:00+00:00');
