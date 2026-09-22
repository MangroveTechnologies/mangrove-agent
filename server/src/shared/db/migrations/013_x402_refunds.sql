-- Separate credits preserve the original charge and its budget period.
CREATE TABLE IF NOT EXISTS x402_refunds (
    reservation_id TEXT PRIMARY KEY REFERENCES x402_payments(id),
    operation_id TEXT NOT NULL,
    recovery_headers BLOB,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'review', 'confirmed')),
    refund_tx TEXT,
    network TEXT NOT NULL,
    amount_micro_usd INTEGER NOT NULL CHECK (amount_micro_usd > 0),
    evidence_json TEXT,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (network, refund_tx),
    CHECK (state != 'confirmed' OR (refund_tx IS NOT NULL AND evidence_json IS NOT NULL))
);
