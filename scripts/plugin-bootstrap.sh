#!/usr/bin/env bash
# plugin-bootstrap.sh — SessionStart hook for the mangrove-agent Claude Code plugin.
#
# Everything printed to stdout becomes session context for Claude, so this
# prints (1) the operating rules and (2) one status block saying whether the
# local agent is up. It never blocks a session on a first-run dependency
# install: that runs in the background and the status says so.

set -uo pipefail
umask 077  # logs and state under the agent home hold wallet-adjacent data

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
AGENT_HOME="${MANGROVE_AGENT_HOME:-$HOME/.mangrove-agent}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$AGENT_HOME}"
PORT="${MANGROVE_AGENT_PORT:-9080}"
KEY="${CLAUDE_PLUGIN_OPTION_MANGROVE_API_KEY:-}"
STATE="$AGENT_HOME/agent-data"
export CLAUDE_PLUGIN_ROOT="$ROOT" MANGROVE_AGENT_HOME="$AGENT_HOME" CLAUDE_PLUGIN_DATA="$DATA_DIR"

PY=""
for cmd in python3.13 python3.12 python3.11 python3; do
  if command -v "$cmd" >/dev/null 2>&1 \
     && "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cmd"; break
  fi
done

# -- 1. operating context -------------------------------------------------------
CONTEXT="$ROOT/.claude-plugin/session-context.md"
if [ -f "$CONTEXT" ]; then
  if [ -n "$PY" ]; then
    "$PY" - "$CONTEXT" "$ROOT" "$AGENT_HOME" <<'PYEOF'
import sys
text = open(sys.argv[1]).read()
print(text.replace("${CLAUDE_PLUGIN_ROOT}", sys.argv[2]).replace("${MANGROVE_AGENT_HOME}", sys.argv[3]))
PYEOF
  else
    cat "$CONTEXT"
  fi
fi

# -- 2. agent status ----------------------------------------------------------------
status() { printf '\n## mangrove-agent status\n\n%s\n' "$*"; exit 0; }

[ -n "$PY" ] || status "NOT RUNNING: Python 3.11+ was not found on this machine. Tell the user to install Python 3.11 or newer (e.g. \`brew install python@3.12\`), then start a new Claude Code session."

if [ -z "$KEY" ] && [ -f "$AGENT_HOME/config/local-config.json" ]; then
  KEY="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("MANGROVE_API_KEY") or "")' "$AGENT_HOME/config/local-config.json" 2>/dev/null)"
fi
[ -n "$KEY" ] || status "NOT CONFIGURED: no MangroveAI API key. Tell the user to get a free key at https://mangrovedeveloper.ai and set it with \`/plugin\` (mangrove-agent -> configure), or in a terminal: \`claude plugin install mangrove-agent@mangrove --config mangrove_api_key=<key>\`. They must never paste the key into this chat."
export CLAUDE_PLUGIN_OPTION_MANGROVE_API_KEY="$KEY"

# /strategies requires the key (unlike /health, /status, /tools), so a 200 means
# this agent accepts the plugin's key, not just that something holds the port.
code="$("$PY" - "http://127.0.0.1:$PORT/api/v1/agent/strategies" "$KEY" <<'PYEOF'
import sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers={"X-API-Key": sys.argv[2]})
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PYEOF
)"

if [ "$code" = "200" ] && [ "$(cat "$STATE/agent.root" 2>/dev/null)" = "$ROOT" ]; then
  status "RUNNING at http://127.0.0.1:$PORT (state in $AGENT_HOME). MCP tools are available."
fi

mkdir -p "$STATE" 2>/dev/null && chmod 700 "$AGENT_HOME" "$STATE" 2>/dev/null
touch "$STATE/bootstrap.log" 2>/dev/null && chmod 600 "$STATE/bootstrap.log" 2>/dev/null
LOCK_SHA="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$ROOT/server/requirements.lock" 2>/dev/null)"
if [ -x "$DATA_DIR/venv/bin/python" ] && [ "$(cat "$DATA_DIR/venv/.lock-sha256" 2>/dev/null)" = "$LOCK_SHA" ]; then
  # Deps already installed: start in the foreground so the MCP tools are up for this session.
  if "$ROOT/scripts/plugin-start-agent.sh" >> "$STATE/bootstrap.log" 2>&1; then
    status "RUNNING at http://127.0.0.1:$PORT (just started; state in $AGENT_HOME). If mangrove-agent MCP tools are missing, run /mcp and reconnect mangrove-agent."
  fi
  status "FAILED TO START. Show the user the end of $STATE/bootstrap.log and $STATE/agent.log, then help them fix it. To retry, run in a terminal: \`$ROOT/scripts/plugin-start-agent.sh\`."
fi

nohup "$ROOT/scripts/plugin-start-agent.sh" >> "$STATE/bootstrap.log" 2>&1 < /dev/null &
status "STARTING: first run is installing dependencies in the background (about 1-3 minutes). The mangrove-agent MCP tools will not respond until it finishes. Tell the user, then check progress by reading the end of $STATE/bootstrap.log. When it logs \"agent healthy\", ask the user to run /mcp and reconnect mangrove-agent."
