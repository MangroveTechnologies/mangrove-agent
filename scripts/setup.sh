#!/usr/bin/env bash
# setup.sh — single-command quickstart for the mangrove-agent.
#
# Default: bare-metal (venv + uvicorn + OS keychain). The primary path.
# Alt:     Docker (compose up with ./agent-data/ directory mount +
#          keyfile-based master key). Use --docker.
#
# Flow (bare-metal, default):
#   0. Preflight (python3, claude CLI if we're registering MCP)
#   1. Seed server/src/config/local-config.json from the example if missing
#   2. Choose API-key or x402 access; preserve existing settings on reruns
#   3. Ensure agent-data/ directory exists (keyfile + DB live there)
#   4. Install deps into .venv (pip install)
#   5. Start uvicorn in the background (honors BARE_PORT; default 9080),
#      unless --foreground
#   6. Verify authenticated local access
#   7. Register the MCP server with Claude Code (unless --no-mcp)
#   8. Run verify_quickstart.sh (unless --no-verify)
#
# Flow (--docker):
#   0. Preflight (docker daemon, python3)
#   1-3. Same as above (config + agent-data/)
#   4. docker compose up -d --build
#   5. Verify authenticated local access
#   6. Register MCP (unless --no-mcp)
#   7. Run verify_quickstart.sh (unless --no-verify)
#
# Idempotent: re-running is a no-op (config exists, venv exists, container
# or uvicorn already healthy, MCP already registered).

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

REQUESTED_BASE_URL="${BASE_URL:-}"
PORT="${BARE_PORT:-9080}"
# This setup flow binds loopback only because it holds wallet secrets.
HOST="${BARE_HOST:-127.0.0.1}"
# Exported so the child scripts (setup-mcp.sh, verify_quickstart.sh) target the
# SAME port. Honors BARE_PORT so a busy 9080 (e.g. squatted by VSCode/Code
# Helper) can be sidestepped end-to-end with one env var.
export BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
CONFIG_FILE="server/src/config/local-config.json"
PID_FILE="agent-data/bare.pid"
LOG_FILE="agent-data/bare.log"

# Defaults
MODE="bare"
DO_MCP="yes"
DO_VERIFY="yes"
ASSUME_YES="no"
FOREGROUND="no"
SKIP_TOUR="no"
API_KEY_STDIN=""
AUTH_MODE=""
WALLET_ACTION=""
CONFIG_ARGS=(--url "$BASE_URL")
MARKETS_URL_DEFAULT="https://mangrovemarkets-pcqgpciucq-uc.a.run.app"
MARKETS_URL_ARG=""

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

# pick_python [MIN_MINOR]  — echo the first interpreter that is >= 3.MIN_MINOR
# (default 10; x402 requires >= 3.10). On stock macOS, bare `python3` can still
# be the system 3.9 even after Homebrew installs python@3.12 — Homebrew lands it
# as `python3.12`, not as `python3` on PATH — so prefer versioned names before
# falling back to python3/python. (#100)
pick_python() {
  local min_minor="${1:-10}" cmd
  for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 \
       && "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, $min_minor) else 1)" 2>/dev/null; then
      printf '%s\n' "$cmd"
      return 0
    fi
  done
  return 1
}

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --docker              Use Docker (default is bare-metal).
  --foreground          Run uvicorn in the foreground (bare-metal only).
                        Default: background via nohup, logs in $LOG_FILE.
  --no-mcp              Skip Claude Code MCP registration.
  --no-verify           Skip the final verify pass.
  --skip-tour           Suppress the first-run platform tour by writing the
                        .claude/.onboarded marker before Claude Code launches.
                        Replay it later by asking or 'rm .claude/.onboarded'.
  --auth MODE           api-key or x402. Existing mode is preserved by default.
  --api-key-stdin       Read upstream key from stdin (use a private pipe).
                        Interactive users should use --auth api-key instead.
  --wallet ACTION       Run an explicit user-requested create/import/list/select
                        action, then exit. Normal setup only prints instructions.
  --markets-url URL     Non-interactive: set MANGROVEMARKETS_BASE_URL.
                        Default: $MARKETS_URL_DEFAULT
  --yes                 Skip prompts; new installs default to x402. No wallet actions.
  -h, --help            Show this help.
EOF
  exit 0
}

# -- parse args -------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --docker) MODE="docker"; shift ;;
    --foreground) FOREGROUND="yes"; shift ;;
    --no-mcp) DO_MCP="no"; shift ;;
    --no-verify) DO_VERIFY="no"; shift ;;
    --skip-tour) SKIP_TOUR="yes"; shift ;;
    --api-key) fail "--api-key was removed to protect credentials. Use --auth api-key for hidden entry, or --api-key-stdin with a private pipe." ;;
    --api-key-stdin) API_KEY_STDIN="yes"; shift ;;
    --auth) [ $# -ge 2 ] || fail "--auth needs api-key or x402"; AUTH_MODE="$2"; shift 2 ;;
    --wallet) [ $# -ge 2 ] || fail "--wallet needs create/import/list/select"; WALLET_ACTION="$2"; shift 2 ;;
    --markets-url) [ $# -ge 2 ] && [ -n "$2" ] || fail "--markets-url needs a value"; MARKETS_URL_ARG="$2"; shift 2 ;;
    --yes) ASSUME_YES="yes"; shift ;;
    -h|--help) usage ;;
    *) fail "Unknown option (try --help)" ;;
  esac
done

# -- 0. validate before changing configuration or state -----------------------
case "$AUTH_MODE" in ""|api-key|x402) ;; *) fail "--auth must be api-key or x402" ;; esac
case "$WALLET_ACTION" in ""|create|import|list|select) ;; *) fail "--wallet must be create/import/list/select" ;; esac
[ "$AUTH_MODE" != "x402" ] || [ -z "$API_KEY_STDIN" ] || fail "--api-key conflicts with --auth x402"
[ "$MODE" != "docker" ] || [ "$FOREGROUND" = "no" ] || fail "--foreground cannot be combined with --docker"
[ -z "${MANGROVE_AGENT_HOME:-}" ] || fail "Run clone setup in a terminal without MANGROVE_AGENT_HOME (plugin state override)."
[ "$HOST" = "127.0.0.1" ] || fail "setup.sh requires the loopback bind 127.0.0.1"
PY="$(pick_python 11)" || fail "Python 3.11+ required. Install it, then rerun setup."
export SETUP_PYTHON="$PY"
SUPPORT="$SCRIPT_DIR/setup_support.py"
# A rerun must retain the selected port, including commands printed by onboarding.
if [ -z "${BARE_PORT:-}$REQUESTED_BASE_URL" ] && [ -f "$CONFIG_FILE" ]; then
  BASE_URL="$("$PY" - "$CONFIG_FILE" <<'PYURL'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from setup_support import origin, read_config, SetupError
try:
    print(origin(read_config(Path(sys.argv[1])).get('LOCAL_AGENT_URL') or 'http://127.0.0.1:9080'))
except SetupError as exc:
    sys.exit(str(exc))
PYURL
)"
  PORT="${BASE_URL##*:}"
  export BASE_URL
  CONFIG_ARGS=(--url "$BASE_URL")
fi
if [ -n "$WALLET_ACTION" ]; then
  [ -z "$AUTH_MODE$API_KEY_STDIN$MARKETS_URL_ARG" ] && [ "$ASSUME_YES" = "no" ] && [ "$FOREGROUND" = "no" ] \
    || fail "Run --wallet by itself; it cannot be combined with setup/configuration options."
else
  "$PY" - "$PORT" "$BASE_URL" "$MODE" <<'PYCHECK'
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from setup_support import origin, SetupError
try:
    port = int(sys.argv[1])
    if not 1 <= port <= 65535: raise ValueError
    if origin(sys.argv[2]) != f'http://127.0.0.1:{port}': raise ValueError
    if sys.argv[3] == 'docker' and port != 9080: raise ValueError
except (ValueError, AssertionError, SetupError):
    sys.exit('Invalid port/BASE_URL combination. Docker setup currently uses port 9080.')
PYCHECK
  if [ "$MODE" = "docker" ]; then
    command -v docker >/dev/null 2>&1 || fail "Docker not found."
    docker info >/dev/null 2>&1 || fail "Docker daemon is not running."
  fi
fi

# Serialize all setup/configuration actions. Never steal a potentially live lock.
mkdir -p agent-data
chmod 700 agent-data
LOCK_DIR="agent-data/.setup.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "Another setup is active, or a previous run was interrupted. Check agent-data/.setup.lock/pid; remove the lock directory only after confirming no setup is running."
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
LOCK_OWNED=yes
release_lock() {
  if [ "$LOCK_OWNED" = yes ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  LOCK_OWNED=no
}
cleanup() { release_lock; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$WALLET_ACTION" ]; then
  "$PY" "$SUPPORT" wallet --action "$WALLET_ACTION"
  exit 0
fi
if [ "$DO_MCP" = "yes" ] && ! command -v claude >/dev/null 2>&1; then
  info "claude CLI not found; local agent setup continues, MCP registration will be skipped."
  DO_MCP="no"
fi

step "1. Configure access"
[ -z "$AUTH_MODE" ] || CONFIG_ARGS+=(--auth "$AUTH_MODE")
[ "$ASSUME_YES" = "no" ] || CONFIG_ARGS+=(--yes)
[ -z "$MARKETS_URL_ARG" ] || CONFIG_ARGS+=(--markets-url "$MARKETS_URL_ARG")
if [ -n "$API_KEY_STDIN" ]; then
  "$PY" "$SUPPORT" configure "${CONFIG_ARGS[@]}" --api-key-stdin
else
  "$PY" "$SUPPORT" configure "${CONFIG_ARGS[@]}"
fi
ok "config ready; existing wallets and spending records preserved"

# -- 2. agent-data directory -------------------------------------------------

step "2. agent-data/ directory"
if [ ! -d agent-data ]; then
  mkdir -p agent-data
  chmod 700 agent-data
  info "created agent-data/ (chmod 700)"
fi
chmod 700 agent-data

# --skip-tour: write the (gitignored, per-user) marker the trading-bot-workflow
# rule gates on, so the first-run platform tour is suppressed. Replay it later
# by asking the agent or removing the marker.
if [ "$SKIP_TOUR" = "yes" ] && [ ! -f .claude/.onboarded ]; then
  touch .claude/.onboarded
  info "wrote .claude/.onboarded — first-run tour suppressed (rm to replay)"
fi
# Bare-metal only: a prior Docker run bind-mounts agent-data/ as root, which a
# non-root bare-metal run then can't write. (Docker mode legitimately owns it as
# root, so skip the check there.) Fail with a clear reclaim path, not an opaque
# SQLite permission error.
if [ "$MODE" != "docker" ] && [ -d agent-data ] && [ ! -w agent-data ]; then
  fail "agent-data/ exists but is not writable by $(id -un). A previous Docker run likely created it as root. Reclaim it with:
      docker run --rm -v \"\$PWD\":/r alpine chown -R $(id -u):$(id -g) /r/agent-data
    (or: sudo chown -R $(id -u):$(id -g) agent-data), then re-run."
fi
ok "agent-data/ ready"

# -- 3. install + start the server ------------------------------------------

if [ "$MODE" = "docker" ]; then
  step "3. docker compose up -d --build"
  # Config is atomically replaced; recreate to refresh the single-file bind mount.
  docker compose up -d --build --force-recreate >/dev/null
  ok "container built + started"
else
  step "3. venv + pip install"
  if [ ! -d .venv ]; then
    PY="$(pick_python 11)" || fail "Python >= 3.11 is required (requirements.lock is resolved for 3.11+) but none was found. Install it (e.g. 'brew install python@3.12') and re-run."
    "$PY" -m venv .venv
    info "created .venv ($PY -> $("$PY" --version 2>&1))"
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python3 -m pip install --quiet --upgrade pip
  python3 -m pip install --quiet -r server/requirements.lock
  ok "deps installed"

  step "4. start uvicorn"
  # Run from repo root so relative config paths (./agent-data/…) resolve
  # the same way Docker resolves them (CWD=/app, agent-data/ alongside src/).
  export PYTHONPATH="$REPO_ROOT/server:${PYTHONPATH:-}"
  FINGERPRINT="$("$PY" "$SUPPORT" fingerprint)"
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE")"
    case "$OLD_PID" in ""|*[!0-9]*) fail "Invalid agent pid file; inspect it before retrying." ;; esac
    if kill -0 "$OLD_PID" 2>/dev/null; then
      # Only stop a process with this checkout's absolute app-dir in its argv.
      # Older setup versions lack this evidence: ask the user to stop them.
      COMMAND="$(ps -p "$OLD_PID" -o command=)"
      case "$COMMAND" in
        *"uvicorn src.app:app --app-dir $REPO_ROOT/server --host 127.0.0.1 --port "*) ;;
        *) fail "Recorded PID cannot be identified as this setup's agent. Stop your existing agent manually, then rerun setup." ;;
      esac
      if [ "$FOREGROUND" = "yes" ]; then
        fail "An agent is already running. Stop it before using --foreground."
      fi
      if [ "$(cat agent-data/bare.fingerprint 2>/dev/null || true)" != "$FINGERPRINT" ]; then
        info "restarting this checkout's agent to apply configuration/code changes"
        kill "$OLD_PID"
        for _ in $(seq 1 40); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.25; done
        if kill -0 "$OLD_PID" 2>/dev/null; then
          fail "Agent has not stopped; inspect it before rerunning. No second process was started."
        fi
        rm -f "$PID_FILE"
      else
        info "existing agent configuration matches"
      fi
    else
      rm -f "$PID_FILE"
    fi
  fi
  if [ ! -f "$PID_FILE" ]; then
    if "$PY" - "$PORT" <<'PYPORT'
import socket, sys
with socket.socket() as sock:
    sock.settimeout(1)
    sys.exit(0 if sock.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)
PYPORT
    then
      fail "Port $PORT is already in use. Stop its owner or use BARE_PORT=<free port>."
    fi
    # Start first, then authenticate/register/verify in both modes. Foreground
    # attaches below, so it does not skip the rest of setup.
    nohup env ENVIRONMENT=local PYTHONPATH="$PYTHONPATH" python3 -m uvicorn src.app:app \
      --app-dir "$REPO_ROOT/server" --host "$HOST" --port "$PORT" --workers 1 --timeout-keep-alive 120 \
      >> "$REPO_ROOT/$LOG_FILE" 2>&1 < /dev/null &
    CHILD_PID=$!
    echo "$CHILD_PID" > "$PID_FILE"
    info "agent started (pid $CHILD_PID); logs: $LOG_FILE"
    if [ "$FOREGROUND" = "yes" ]; then
      cleanup() {
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
        if [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$CHILD_PID" ]; then rm -f "$PID_FILE"; fi
        release_lock
      }
    fi
  fi
  ok "server starting"
fi

# -- 4. wait for /health -----------------------------------------------------

step "5. Verify local authenticated access"
READY="no"
DEADLINE=$((SECONDS + 60))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  if "$PY" "$SUPPORT" check >/dev/null 2>&1; then READY="yes"; break; fi
  if [ "$MODE" = "bare" ] && ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    fail "Agent exited during startup; inspect $LOG_FILE."
  fi
  sleep 1
done
[ "$READY" = "yes" ] || fail "Local authenticated readiness failed. Inspect agent logs and config; MCP was not registered."
ok "local authentication verified; no paid request made"
if [ "$MODE" = "bare" ]; then
  printf '%s\n' "$FINGERPRINT" > agent-data/bare.fingerprint
fi

# -- 5. register MCP ---------------------------------------------------------

if [ "$DO_MCP" = "yes" ]; then
  step "6. Register MCP with Claude Code"
  SETUP_PARENT=1 "$SCRIPT_DIR/setup-mcp.sh" | tail -10
  ok "MCP registered"
else
  info "skipped MCP registration (--no-mcp or claude CLI missing)"
fi

# -- 6. verify ---------------------------------------------------------------

if [ "$DO_VERIFY" = "yes" ]; then
  step "7. Verify"
  if [ "$MODE" = "docker" ]; then
    SETUP_PARENT=1 "$SCRIPT_DIR/verify_quickstart.sh" 2>&1 | tail -12 || fail "Verification failed; setup is incomplete."
  else
    SETUP_PARENT=1 "$SCRIPT_DIR/verify_quickstart.sh" --bare 2>&1 | tail -12 || fail "Verification failed; setup is incomplete."
  fi
fi

echo
if [ "$DO_MCP" = "yes" ]; then
  echo "Claude MCP registration saved. Open/reconnect Claude in this directory to load tools."
else
  echo "Claude MCP is not registered. Install Claude CLI and rerun setup when ready."
fi
GUIDE_ARGS=(guide)
[ "$ASSUME_YES" = "no" ] || GUIDE_ARGS+=(--yes)
[ "$MODE" != "docker" ] || GUIDE_ARGS+=(--docker)
"$PY" "$SUPPORT" "${GUIDE_ARGS[@]}"
printf "${GREEN}Done.${CLR} Local agent authenticated at %s. Payment readiness is separate.\n" "$BASE_URL"
if [ "$SKIP_TOUR" = "yes" ]; then
  echo "First-run tour suppressed; ask for it later if desired."
fi
if [ "$FOREGROUND" = "yes" ]; then
  # Release the setup lock after onboarding while retaining signal cleanup.
  release_lock
  echo "Agent attached to this terminal. Ctrl+C stops it. Logs: $LOG_FILE"
  wait "$CHILD_PID"
fi
