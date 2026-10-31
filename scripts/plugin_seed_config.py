"""Seed or update the plugin's local-config.json (chmod 600) with the user's key.

Usage: plugin_seed_config.py EXAMPLE_CONFIG CONFIG_PATH PORT MARKETS_URL_DEFAULT
The API key is read from standard input, never from process arguments.
Called by plugin-start-agent.sh.
"""
import json
import os
import sys

example, cfg_path, port, markets_default = sys.argv[1:5]
key = sys.stdin.read()
if not key:
    raise SystemExit("An API key is required on standard input.")
if os.path.exists(cfg_path):
    with open(cfg_path, "r") as f:
        cfg = json.load(f)
else:
    with open(example, "r") as f:
        cfg = json.load(f)
cfg["MANGROVE_API_KEY"] = key
# The plugin's MCP entry authenticates with the MangroveAI key, and this agent
# binds 127.0.0.1 only. Never keep the example's shared default key.
cfg["API_KEYS"] = key
if cfg.get("MANGROVEMARKETS_BASE_URL") in (None, "", "http://localhost:9081"):
    cfg["MANGROVEMARKETS_BASE_URL"] = markets_default
cfg["LOCAL_AGENT_URL"] = f"http://127.0.0.1:{port}"
cfg["DB_PATH"] = "./agent-data/agent.db"
cfg["MASTER_KEY_PATH"] = "./agent-data/master.key"
tmp = cfg_path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, cfg_path)
