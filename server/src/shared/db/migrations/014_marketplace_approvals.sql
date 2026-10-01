CREATE TABLE marketplace_approvals (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'submitted', 'completed')),
    result TEXT
);
CREATE INDEX marketplace_approvals_expiry ON marketplace_approvals (state, expires_at);
