#!/usr/bin/env bash
# plugin-start-agent.sh — install deps (when needed), seed config, and start the
# agent for a Claude Code plugin install. Idempotent; safe to run repeatedly.
#
# Normally run by plugin-bootstrap.sh (the plugin's SessionStart hook). You can
# also run it from a terminal to (re)start the agent after an update.
#
# Layout (plugin code is replaceable, state is not):
#   ${CLAUDE_PLUGIN_ROOT}              plugin code (versioned cache dir)
#   ${CLAUDE_PLUGIN_DATA}/venv         Python deps, rebuilt when requirements.lock changes
#   ${MANGROVE_AGENT_HOME}/config/     local-config.json (chmod 600)
#   ${MANGROVE_AGENT_HOME}/agent-data/ agent.db (wallets, strategies), master.key, logs
#
# Env:
#   CLAUDE_PLUGIN_OPTION_MANGROVE_API_KEY  MangroveAI key (plugin userConfig); falls back
#                                          to the key already in local-config.json
#   MANGROVE_AGENT_HOME                    default ~/.mangrove-agent
#   MANGROVE_AGENT_PORT                    default 9080 (the plugin's MCP entry uses 9080)

set -euo pipefail

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
AGENT_HOME="${MANGROVE_AGENT_HOME:-$HOME/.mangrove-agent}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$AGENT_HOME}"
PORT="${MANGROVE_AGENT_PORT:-9080}"
KEY="${CLAUDE_PLUGIN_OPTION_MANGROVE_API_KEY:-}"
VENV="$DATA_DIR/venv"
STATE="$AGENT_HOME/agent-data"
CFG_DIR="$AGENT_HOME/config"
CFG="$CFG_DIR/local-config.json"
PID_FILE="$STATE/agent.pid"
ROOT_FILE="$STATE/agent.root"
LOG_FILE="$STATE/agent.log"
LOCK_DIR="$STATE/.start.lock"
MARKETS_URL_DEFAULT="https://mangrovemarkets-pcqgpciucq-uc.a.run.app"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

umask 077
mkdir -p "$STATE" "$CFG_DIR" "$DATA_DIR"
chmod 700 "$AGENT_HOME" "$STATE" "$CFG_DIR"

# One starter at a time (two sessions opening together must not double-install).
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
    rm -rf "$LOCK_DIR" && mkdir "$LOCK_DIR"
  else
    log "another start is already in progress; exiting"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

pick_python() {
  local cmd
  for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" >/dev/null 2>&1 \
       && "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s\n' "$cmd"
      return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || die "Python >= 3.11 not found. Install it (e.g. 'brew install python@3.12') and start a new session."

if [ -z "$KEY" ] && [ -f "$CFG" ]; then
  KEY="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("MANGROVE_API_KEY") or "")' "$CFG")"
fi
[ -n "$KEY" ] || die "no MangroveAI API key configured (plugin option mangrove_api_key)"

http_code() {  # http_code <url> [api-key]
  "$PY" - "$@" <<'PYEOF'
import sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1])
if len(sys.argv) > 2:
    req.add_header("X-API-Key", sys.argv[2])
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PYEOF
}
# /health, /status and /tools are unauthenticated, so they can't tell our agent
# from another one on the port. /strategies requires the key and reads only the
# local DB (no upstream call).
ours_healthy() { [ "$(http_code "http://127.0.0.1:$PORT/api/v1/agent/strategies" "$KEY")" = "200" ]; }

# -- 1. dependencies -----------------------------------------------------------
LOCK_SHA="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$ROOT/server/requirements.lock")"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$VENV/.lock-sha256" 2>/dev/null || true)" != "$LOCK_SHA" ]; then
  log "installing dependencies into $VENV (first run or requirements changed)"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r "$ROOT/server/requirements.lock"
  printf '%s\n' "$LOCK_SHA" > "$VENV/.lock-sha256"
  log "dependencies installed"
fi

# -- 2. config -----------------------------------------------------------------
"$VENV/bin/python" - "$ROOT/server/src/config/local-example-config.json" "$CFG" "$KEY" "$PORT" "$MARKETS_URL_DEFAULT" <<'PYEOF'
import json, os, sys
example, cfg_path, key, port, markets_default = sys.argv[1:6]
cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else json.load(open(example))
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
PYEOF
chmod 600 "$CFG"

# -- 3. (re)start ----------------------------------------------------------------
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  OLD_PID="$(cat "$PID_FILE")"
  if [ "$(cat "$ROOT_FILE" 2>/dev/null || true)" = "$ROOT" ] && ours_healthy; then
    log "agent already running (pid $OLD_PID) from this plugin version"
    exit 0
  fi
  if ps -p "$OLD_PID" -o command= 2>/dev/null | grep -q 'uvicorn src.app:app'; then
    log "restarting agent pid $OLD_PID (plugin updated or unhealthy)"
    kill "$OLD_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.5; done
  fi
fi

if [ "$(http_code "http://127.0.0.1:$PORT/health")" != "0" ]; then
  if ours_healthy; then
    log "an agent accepting this key is already serving :$PORT; leaving it running"
    exit 0
  fi
  die "port $PORT is already in use by another process (a git-clone mangrove-agent?). Stop it, or set MANGROVE_AGENT_PORT."
fi

cd "$AGENT_HOME"
nohup env ENVIRONMENT=local MANGROVE_AGENT_HOME="$AGENT_HOME" PYTHONPATH="$ROOT/server" \
  "$VENV/bin/python" -m uvicorn src.app:app --app-dir "$ROOT/server" \
  --host 127.0.0.1 --port "$PORT" --workers 1 --timeout-keep-alive 120 \
  >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
printf '%s\n' "$ROOT" > "$ROOT_FILE"
log "agent starting (pid $(cat "$PID_FILE")) on 127.0.0.1:$PORT"

for _ in $(seq 1 60); do
  if ours_healthy; then
    log "agent healthy on 127.0.0.1:$PORT"
    exit 0
  fi
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null || die "agent exited during startup; see $LOG_FILE"
  sleep 1
done
die "agent did not become healthy within 60s; see $LOG_FILE"
