-- #151 Phase 3: the evaluation lane is a per-strategy OPTION.
--   evaluation_lane: 'server' (default, by-id, engine-DB-authoritative) or
--   'stateless' (object lane; the agent supplies and receives all state --
--   MangroveAI#840/#848). NULL falls back to the config default
--   (EVALUATION_LANE, default 'server').
--   open_positions_json: the engine-shaped open_positions blob the stateless
--   lane round-trips verbatim (persist response -> echo next tick), exactly
--   like execution_state_json (migration 005). The human-readable positions
--   table (agent shape) remains the audit trail; this is protocol state.
ALTER TABLE strategies ADD COLUMN evaluation_lane TEXT;
ALTER TABLE strategies ADD COLUMN open_positions_json TEXT;
