# shellcheck shell=bash
# _agent_home.sh — resolve where this agent's config and state live. SOURCE it.
#
# The same scripts run from a git clone (./scripts/setup.sh) and from an
# installed Claude Code plugin, whose code lives in a versioned cache dir that
# is replaced on every update. State must never live there, so plugin installs
# keep config + wallets + DB under MANGROVE_AGENT_HOME (default ~/.mangrove-agent).
#
# Resolution order:
#   1. $MANGROVE_AGENT_HOME                          explicit (the plugin exports it)
#   2. <repo>/server/src/config/local-config.json    a clone set up with setup.sh
#   3. ~/.mangrove-agent/config/local-config.json    plugin install, run from a plain terminal
#
# Exports AGENT_HOME, CONFIG_FILE, LOCAL_AGENT_URL.

_AH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_AH_ROOT="$(cd "$_AH_DIR/.." && pwd)"
_AH_REPO_CFG="$_AH_ROOT/server/src/config/local-config.json"
_AH_PLUGIN_HOME="$HOME/.mangrove-agent"

if [ -n "${MANGROVE_AGENT_HOME:-}" ]; then
  AGENT_HOME="$MANGROVE_AGENT_HOME"
  CONFIG_FILE="$AGENT_HOME/config/local-config.json"
elif [ -f "$_AH_REPO_CFG" ]; then
  AGENT_HOME="$_AH_ROOT"
  CONFIG_FILE="$_AH_REPO_CFG"
elif [ -f "$_AH_PLUGIN_HOME/config/local-config.json" ]; then
  AGENT_HOME="$_AH_PLUGIN_HOME"
  CONFIG_FILE="$AGENT_HOME/config/local-config.json"
else
  AGENT_HOME="$_AH_ROOT"
  CONFIG_FILE="$_AH_REPO_CFG"
fi

if [ -z "${LOCAL_AGENT_URL:-}" ] && [ -f "$CONFIG_FILE" ]; then
  LOCAL_AGENT_URL="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("LOCAL_AGENT_URL") or "")' "$CONFIG_FILE" 2>/dev/null || true)"
fi
LOCAL_AGENT_URL="${LOCAL_AGENT_URL:-http://localhost:9080}"

export AGENT_HOME CONFIG_FILE LOCAL_AGENT_URL
