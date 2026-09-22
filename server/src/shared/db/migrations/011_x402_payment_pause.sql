-- Preserve every existing authorization. Unresolved rows now block further
-- signatures for that payer/network across ALL budget periods.
CREATE INDEX IF NOT EXISTS idx_x402_pending_payer_network
    ON x402_payments (lower(wallet_address), network) WHERE state = 'authorized';

-- Evidence is appended atomically with an explicit reconciliation transition.
CREATE TABLE IF NOT EXISTS x402_reconciliation_evidence (
    reservation_id TEXT PRIMARY KEY REFERENCES x402_payments(id),
    outcome TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
