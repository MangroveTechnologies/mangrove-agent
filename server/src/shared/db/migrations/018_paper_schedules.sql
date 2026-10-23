CREATE TABLE paper_schedules (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    run_id TEXT,
    interval_seconds INTEGER NOT NULL,
    max_evaluations INTEGER NOT NULL,
    evaluations INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    request_id TEXT NOT NULL,
    pending_slot INTEGER,
    last_slot INTEGER,
    last_result TEXT,
    error TEXT,
    UNIQUE(scope, strategy_id)
);
