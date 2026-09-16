-- merge_plan B3 / item 5: the outbound x402 spend budget.
--
-- The agent is the only place an AGGREGATE outbound spend is visible. A
-- signing guard sees one payload and a payer service sees one request;
-- neither can see a running total. This is the same argument that puts the
-- portfolio kill switch here rather than in the engine (007), and the shape
-- borrows from it: a single-row state table plus -- new here -- a per-payment
-- ledger the total is derived from.
--
-- It is a BUDGET, not a circuit breaker, and the vocabulary follows. A
-- breaker fires on an anomaly and demands review: a 30% book drawdown means
-- something went wrong. A budget runs out through entirely normal use, and
-- the fix is a top-up, not an investigation. Spending is therefore
-- `exhausted`, never `tripped`, and the remedy is a human authorizing more
-- -- which they can do in one sentence through the MCP tools, without
-- leaving the conversation.
--
-- Two tables, not one, because a money control needs an audit trail. A bare
-- accumulator column can drift from reality with no way to tell; a ledger
-- summed on read cannot.
--
-- Amounts are INTEGER micro-USD, never REAL. USDC is a 6-decimal token, so
-- the on-chain `value` of an EIP-3009 authorization IS the micro-USD amount:
-- storing it verbatim means the number the agent budgets against is the exact
-- number it signed, with no float rounding anywhere in between.

CREATE TABLE IF NOT EXISTS x402_spend_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),  -- enforce a single row
    -- Incremented by a human top-up. Every ledger row is stamped with the
    -- period it belongs to, so starting a fresh budget does NOT delete or
    -- rewrite history.
    period_id            INTEGER NOT NULL DEFAULT 1,
    period_started_at    TEXT    NOT NULL,
    -- The budget a human explicitly authorized for THIS period, in
    -- micro-USD. NULL means "whatever X402_SPEND_CAP_USD says".
    --
    -- Config is the default for a fresh install; this column is what a
    -- person actually agreed to spend, recorded with the moment they agreed
    -- to it. For the question "how much did the user authorize?", a row
    -- written by an explicit act is better provenance than a file that may
    -- have been edited at any time for any reason.
    period_cap_micro_usd INTEGER CHECK (period_cap_micro_usd IS NULL OR period_cap_micro_usd >= 0),
    -- Budget spent. Payments stop until a human authorizes more; it never
    -- clears itself, and topping up is their call, not the agent's.
    exhausted            INTEGER NOT NULL DEFAULT 0,          -- 0/1
    exhausted_at         TEXT,
    exhausted_reason     TEXT,
    updated_at           TEXT    NOT NULL
);

INSERT OR IGNORE INTO x402_spend_state
    (id, period_id, period_started_at, period_cap_micro_usd, exhausted, updated_at)
VALUES (1, 1, '1970-01-01T00:00:00+00:00', NULL, 0, '1970-01-01T00:00:00+00:00');

-- One row per payment AUTHORIZATION -- not per settlement.
--
-- The agent controls what it signs; it does not control what a receiver
-- settles, and settlement receipts are an optional header servers disagree
-- about. Budgeting on settlement would therefore let a receiver decide how
-- much of the budget it consumed. So a row is written the moment a signature
-- is authorized, and it counts against the cap until there is positive
-- evidence it can never be settled:
--
--   authorized -> settled   receipt seen; `transaction_hash` recorded
--   authorized -> released  provably dead (the guard refused, or the
--                           receiver rejected the payment and burned the
--                           nonce, so the authorization can never be used)
--
-- Anything else stays `authorized` and keeps counting. Erring toward
-- over-counting spends less money than erring the other way.
CREATE TABLE IF NOT EXISTS x402_payments (
    id               TEXT    PRIMARY KEY,
    period_id        INTEGER NOT NULL,
    state            TEXT    NOT NULL CHECK (state IN ('authorized', 'settled', 'released')),
    amount_micro_usd INTEGER NOT NULL CHECK (amount_micro_usd >= 0),
    wallet_address   TEXT    NOT NULL,
    payee            TEXT,
    network          TEXT,
    resource         TEXT,                                 -- URL, query string stripped
    transaction_hash TEXT,
    -- `validBefore` from the signed authorization: the unix second after
    -- which USDC will reject it, so it can never be settled by anyone.
    --
    -- Recorded even though nothing reads it yet. An `authorized` row past
    -- this instant is PROVABLY dead money, which is the only sound basis
    -- for handing its budget back automatically -- and that reclaim cannot
    -- be added later without this column, i.e. without a migration against
    -- live ledgers. Cheap to write now, expensive to retrofit.
    valid_before     INTEGER,
    release_reason   TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

-- The hot path: SUM(amount) for the current period over the two counting
-- states, read before every single payment.
CREATE INDEX IF NOT EXISTS idx_x402_payments_period_state
    ON x402_payments (period_id, state);

-- The audit path: most recent payments first.
CREATE INDEX IF NOT EXISTS idx_x402_payments_created_at
    ON x402_payments (created_at DESC);
