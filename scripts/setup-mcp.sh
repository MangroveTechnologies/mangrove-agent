#!/usr/bin/env bash
# setup-mcp.sh — register the mangrove-agent MCP server with Claude Code.
#
# Why this exists:
#   Claude Code's project-scope .mcp.json approval is unreliable
#   (upstream issue #9189 — the enable prompt on session start often
#   doesn't persist, so `cp .mcp.json.example ./.mcp.json` alone
#   leaves attendees stuck in an approval loop). The documented
#   workaround is `claude mcp add -s local ...`, which writes
#   user-scope config keyed to the project directory and is honored
#   reliably across restarts.
#
# Checks (in order):
#   1. `claude` CLI is on PATH
#   2. Container is up and /health returns 200
#   3. server/src/config/local-config.json exists
#   4. Extract the first value of API_KEYS for the X-API-Key header
#   5. Remove any existing mangrove-agent registration (idempotent)
#   6. Register mangrove-agent at http://localhost:9080/mcp/ in local scope
#
# After this: restart Claude Code in the repo directory. Tools load
# automatically. No approval prompt.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BASE_URL="${BASE_URL:-http://localhost:9080}"
CONFIG_FILE="server/src/config/local-config.json"
SERVER_NAME="mangrove-agent"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

# -- 1. claude CLI on PATH --------------------------------------------------

step "1. Claude Code CLI available"
if ! command -v claude >/dev/null 2>&1; then
  fail "'claude' not on PATH. Install: npm install -g @anthropic-ai/claude-code"
fi
ok "claude found: $(command -v claude)"

# -- 2. Container reachable -------------------------------------------------

step "2. mangrove-agent container healthy at $BASE_URL"
if ! curl -fsS -m 5 "$BASE_URL/health" >/dev/null 2>&1; then
  fail "$BASE_URL/health did not respond. Run './scripts/setup.sh --yes' first (bare-metal) or 'docker compose up -d --build' (docker)."
fi
ok "/health returned 200"

# -- 3. Local config present ------------------------------------------------

step "3. Local config present"
if [ ! -f "$CONFIG_FILE" ]; then
  fail "$CONFIG_FILE not found. Run 'cp server/src/config/local-example-config.json $CONFIG_FILE' first."
fi
ok "$CONFIG_FILE found"

# -- 4. Extract first API key -----------------------------------------------

step "4. Extract X-API-Key from $CONFIG_FILE"
API_KEY="$(
  python3 -c "
import json, sys
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
raw = cfg.get('API_KEYS', '')
key = next((k.strip() for k in raw.split(',') if k.strip()), '')
if not key:
    sys.exit('API_KEYS missing or empty in $CONFIG_FILE')
print(key)
"
)"
info "using key: ${API_KEY:0:8}..."
ok "API key extracted"

# -- 5. Remove any stale registration ---------------------------------------

step "5. Remove stale $SERVER_NAME registration (idempotent)"
claude mcp remove "$SERVER_NAME" -s local >/dev/null 2>&1 || true
ok "cleared"

# -- 6. Register mangrove-agent -------------------------------------------------

step "6. Register $SERVER_NAME in local scope"
claude mcp add --transport http --scope local "$SERVER_NAME" "$BASE_URL/mcp/" --header "X-API-Key: $API_KEY" >/dev/null
ok "registered"

echo
printf "${GREEN}Done.${CLR} Restart Claude Code in this directory to load the mangrove-agent tools (41 total).\n\n"
echo "  cd $(pwd)"
echo "  claude"
echo
echo 'Then try: "List my tools" or "Create a wallet on Base mainnet".'
