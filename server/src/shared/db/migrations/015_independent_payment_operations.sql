DROP INDEX idx_x402_active_operation;
CREATE INDEX idx_x402_active_operation ON x402_operations(fingerprint, created_at, id) WHERE state = 'pending';
