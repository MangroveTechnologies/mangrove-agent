-- One user-chosen name per installation, shared by every AI client.
-- No default name: first run asks the user.
CREATE TABLE IF NOT EXISTS agent_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64)
);
