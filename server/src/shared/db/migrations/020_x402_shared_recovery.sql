CREATE TABLE x402_recovery_state (
    operation_id TEXT PRIMARY KEY REFERENCES x402_operations(id),
    action_id TEXT NOT NULL,
    token BLOB,
    replacement_allowed INTEGER NOT NULL DEFAULT 0,
    next_id TEXT UNIQUE REFERENCES x402_operations(id)
);
INSERT INTO x402_recovery_state(operation_id, action_id)
SELECT id, id FROM x402_operations;
UPDATE x402_recovery_state
SET next_id = (SELECT payment_request_id FROM paper_schedules WHERE request_id=operation_id)
WHERE operation_id IN (
    SELECT request_id FROM paper_schedules
    WHERE payment_request_id IS NOT NULL AND payment_request_id != request_id
      AND payment_request_id IN (SELECT id FROM x402_operations)
);
