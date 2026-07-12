-- #151: the agent persists its own engine execution_state per strategy,
-- first-class (not only buried verbatim inside evaluations.sdk_response_json).
-- Updated by strategy_service.tick after every successful evaluation; the
-- stateless evaluation lane (MangroveAI#840) round-trips this value.
ALTER TABLE strategies ADD COLUMN execution_state_json TEXT;
