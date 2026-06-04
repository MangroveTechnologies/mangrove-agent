#!/usr/bin/env bash
# start.sh — (re)start the agent server + Web UI from the repo root.
#
# Safe to run any time:
#   - Already running?  → skips, reports status
#   - Crashed/stale PID? → detects it, restarts cleanly
#   - First time?        → tells you to run ./scripts/setup.sh first
#   - No Anthropic key?  → starts server only, skips UI with a hint
#
# To stop everything: ./stop.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

BASE_URL="http://localhost:9080"
SERVER_PID="agent-data/bare.pid"
SERVER_LOG="agent-data/bare.log"
UI_PID="agent-data/ui.pid"
UI_LOG="agent-data/ui.log"

GREEN="\033[32m"; YELLOW="\033[33m"; DIM="\033[2m"; RED="\033[31m"; CLR="\033[0m"
step() { printf "\n${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }
warn() { printf "${RED}  !${CLR} %s\n" "$1"; }

# Returns true if a PID file exists and its process is alive
is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# Removes a PID file whose process is no longer alive
clear_stale() {
  local f="$1" name="$2"
  if [ -f "$f" ] && ! kill -0 "$(cat "$f")" 2>/dev/null; then
    warn "$name had a stale PID — will restart it"
    rm -f "$f"
  fi
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

if [ ! -d ".venv" ] || [ ! -f "server/src/config/local-config.json" ]; then
  echo ""
  echo "Looks like this is a fresh clone. Run the one-time setup first:"
  echo ""
  echo "  ./scripts/setup.sh"
  echo ""
  exit 1
fi

# Activate the shared virtualenv
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$REPO_ROOT/server:${PYTHONPATH:-}"

# ---------------------------------------------------------------------------
# Agent server
# ---------------------------------------------------------------------------

step "Agent server"
clear_stale "$SERVER_PID" "server"

if is_running "$SERVER_PID"; then
  ok "already running (pid $(cat "$SERVER_PID"))"
elif curl -fsS -m 2 "$BASE_URL/health" >/dev/null 2>&1; then
  ok "already healthy at $BASE_URL"
else
  nohup env ENVIRONMENT=local PYTHONPATH="$PYTHONPATH" \
    python3 -m uvicorn src.app:app \
    --host 0.0.0.0 --port 9080 --workers 1 --timeout-keep-alive 120 \
    > "$SERVER_LOG" 2>&1 &
  echo $! > "$SERVER_PID"
  info "starting (pid $!) — waiting for /health..."

  for i in $(seq 1 20); do
    if curl -fsS -m 2 "$BASE_URL/health" >/dev/null 2>&1; then
      ok "healthy after ${i}s"
      break
    fi
    [ "$i" = "20" ] && { echo ""; warn "server did not start — last log lines:"; tail -8 "$SERVER_LOG"; exit 1; }
    sleep 1
  done
fi

# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

step "Web UI"
clear_stale "$UI_PID" "UI"

if ! python -c "import chainlit" 2>/dev/null; then
  info "chainlit not installed — skipping UI"
  info "run ./scripts/setup.sh and choose Web UI to enable it"
elif is_running "$UI_PID"; then
  ok "already running (pid $(cat "$UI_PID"))"
else
  cd ui
  nohup env MANGROVE_AGENT_URL="http://localhost:9080" \
    chainlit run app.py --port 8001 --host 0.0.0.0 \
    > "$REPO_ROOT/$UI_LOG" 2>&1 &
  echo $! > "$REPO_ROOT/$UI_PID"
  cd "$REPO_ROOT"
  ok "started (pid $(cat "$UI_PID"))"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
printf "${GREEN}Everything is up.${CLR}\n\n"
echo "  Agent  →  $BASE_URL"
if python -c "import chainlit" 2>/dev/null; then
  echo "  UI     →  http://localhost:8001"
fi
echo ""
echo "  Stop:       ./stop.sh"
echo "  Server log: tail -f $SERVER_LOG"
if python -c "import chainlit" 2>/dev/null; then
  echo "  UI log:     tail -f $UI_LOG"
fi
echo ""
