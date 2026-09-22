-- Pending operations are independent; no wallet/network lock or time expiry.
CREATE TABLE x402_operations (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'complete', 'unsigned_failed')),
    payment_headers BLOB,
    response BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_x402_active_operation ON x402_operations(fingerprint) WHERE state = 'pending';
ALTER TABLE x402_payments ADD COLUMN operation_id TEXT REFERENCES x402_operations(id);
CREATE INDEX idx_x402_payment_operation ON x402_payments(operation_id);
CREATE TABLE x402_reconciliation_jobs (
    reservation_id TEXT PRIMARY KEY REFERENCES x402_payments(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_check REAL NOT NULL DEFAULT 0,
    outcome TEXT,
    scan_block INTEGER
);
