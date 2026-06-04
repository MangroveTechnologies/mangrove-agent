#!/usr/bin/env bash
# stop.sh — stop the agent server and Web UI cleanly.
# Run ./start.sh to start them again.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

SERVER_PID="agent-data/bare.pid"
UI_PID="agent-data/ui.pid"

GREEN="\033[32m"; DIM="\033[2m"; CLR="\033[0m"
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

stop_process() {
  local pid_file="$1" name="$2"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      ok "stopped $name (pid $pid)"
    else
      info "$name was not running (cleared stale PID)"
    fi
    rm -f "$pid_file"
  else
    info "$name not running"
  fi
}

echo ""
stop_process "$SERVER_PID" "agent server"
stop_process "$UI_PID"     "web UI"
echo ""
echo "Stopped. Run ./start.sh to start again."
echo ""
