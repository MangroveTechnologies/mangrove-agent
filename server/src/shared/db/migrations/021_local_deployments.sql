-- A binding to an authoritative server definition, never local authoring.
CREATE TABLE local_deployments (
    strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
    server_strategy_id TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    owner_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    asset_class TEXT NOT NULL CHECK(asset_class IN ('crypto','equity')),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
