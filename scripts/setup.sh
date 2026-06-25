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
#   2. Ensure MANGROVE_API_KEY is set (prompt if missing, unless --yes)
#   3. Ensure agent-data/ directory exists (keyfile + DB live there)
#   4. Install deps into .venv (pip install)
#   5. Start uvicorn in the background (via run-bare.sh under nohup,
#      unless --foreground)
#   6. Wait for /health
#   7. Register the MCP server with Claude Code (unless --no-mcp)
#   8. Run verify_quickstart.sh (unless --no-verify)
#
# Flow (--docker):
#   0. Preflight (docker daemon, python3)
#   1-3. Same as above (config + agent-data/)
#   4. docker compose up -d --build
#   5. Wait for /health
#   6. Register MCP (unless --no-mcp)
#   7. Run verify_quickstart.sh (unless --no-verify)
#
# Idempotent: re-running is a no-op (config exists, venv exists, container
# or uvicorn already healthy, MCP already registered).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BASE_URL="${BASE_URL:-http://localhost:9080}"
CONFIG_FILE="server/src/config/local-config.json"
EXAMPLE_CONFIG="server/src/config/local-example-config.json"
PID_FILE="agent-data/bare.pid"
LOG_FILE="agent-data/bare.log"

# Defaults
MODE="bare"
DO_MCP="yes"
DO_VERIFY="yes"
ASSUME_YES="no"
FOREGROUND="no"
API_KEY_ARG=""
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
  --api-key KEY         Non-interactive: set MANGROVE_API_KEY.
  --markets-url URL     Non-interactive: set MANGROVEMARKETS_BASE_URL.
                        Default: $MARKETS_URL_DEFAULT
  --yes                 Accept all defaults, skip prompts.
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
    --api-key) API_KEY_ARG="$2"; shift 2 ;;
    --markets-url) MARKETS_URL_ARG="$2"; shift 2 ;;
    --yes) ASSUME_YES="yes"; shift ;;
    -h|--help) usage ;;
    *) fail "Unknown option: $1 (try --help)" ;;
  esac
done

# -- 0. preflight -----------------------------------------------------------

step "0. Preflight"
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not on PATH. Install Python 3.11+: https://www.python.org/downloads/"
fi
if [ "$MODE" = "docker" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker not on PATH. Install Docker Desktop or run without --docker."
  fi
  if ! docker info >/dev/null 2>&1; then
    fail "Docker daemon is not running. Start Docker Desktop, then re-run."
  fi
fi
if [ "$DO_MCP" = "yes" ] && ! command -v claude >/dev/null 2>&1; then
  info "claude CLI not found — MCP registration will be skipped"
  info "(install: npm install -g @anthropic-ai/claude-code)"
  DO_MCP="no"
fi
ok "preflight clean"

# -- 1. seed config ---------------------------------------------------------

step "1. Config at $CONFIG_FILE"
if [ ! -f "$CONFIG_FILE" ]; then
  if [ ! -f "$EXAMPLE_CONFIG" ]; then
    fail "$EXAMPLE_CONFIG missing — repo is in an inconsistent state."
  fi
  cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
  info "seeded $CONFIG_FILE from example"
fi

# Update MANGROVE_API_KEY if needed.
CURRENT_KEY="$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('MANGROVE_API_KEY',''))")"
if [ "$CURRENT_KEY" = "REPLACE_WITH_YOUR_DEV_OR_PROD_KEY" ] || [ -z "$CURRENT_KEY" ]; then
  if [ -n "$API_KEY_ARG" ]; then
    NEW_KEY="$API_KEY_ARG"
  elif [ "$ASSUME_YES" = "yes" ]; then
    fail "MANGROVE_API_KEY unset and --yes given. Use --api-key to provide one, or run interactively."
  else
    echo
    echo "MANGROVE_API_KEY is not set. Get a free key at https://mangrovedeveloper.ai"
    printf "Paste your dev_/prod_ key (input hidden): "
    read -rs NEW_KEY
    echo
    if [ -z "$NEW_KEY" ]; then
      fail "Empty API key. Aborted."
    fi
  fi
  python3 - <<PY
import json, os
p = "$CONFIG_FILE"
c = json.load(open(p))
c["MANGROVE_API_KEY"] = os.environ.get("NEW_KEY", "") or "$NEW_KEY"
json.dump(c, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
  info "MANGROVE_API_KEY written"
fi

# Update MANGROVEMARKETS_BASE_URL if still localhost (the example default is
# localhost, which is wrong for most users who want the hosted server).
CURRENT_URL="$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('MANGROVEMARKETS_BASE_URL',''))")"
if [ "$CURRENT_URL" = "http://localhost:9081" ]; then
  NEW_URL="${MARKETS_URL_ARG:-$MARKETS_URL_DEFAULT}"
  if [ "$ASSUME_YES" != "yes" ] && [ -z "$MARKETS_URL_ARG" ]; then
    echo
    echo "MANGROVEMARKETS_BASE_URL is currently http://localhost:9081 (self-hosted placeholder)."
    echo "Most users want the hosted URL. Accept the default, or paste your own."
    echo "(Self-host note: 9081 avoids the VSCode Helper :8080 collision — if you run"
    echo " MangroveMarkets-MCP-Server locally, bind it on 9081 to match this config.)"
    printf "[default: %s] " "$NEW_URL"
    read -r USER_URL
    [ -n "$USER_URL" ] && NEW_URL="$USER_URL"
  fi
  python3 - <<PY
import json
p = "$CONFIG_FILE"
c = json.load(open(p))
c["MANGROVEMARKETS_BASE_URL"] = "$NEW_URL"
json.dump(c, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
  info "MANGROVEMARKETS_BASE_URL set to $NEW_URL"
fi
ok "config ready"

# -- 2. agent-data directory -------------------------------------------------

step "2. agent-data/ directory"
if [ ! -d agent-data ]; then
  mkdir -p agent-data
  chmod 700 agent-data
  info "created agent-data/ (chmod 700)"
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
  docker compose up -d --build >/dev/null
  ok "container built + started"
else
  step "3. venv + pip install"
  if [ ! -d .venv ]; then
    PY="$(pick_python)" || fail "Python >= 3.10 is required (x402 needs it) but none was found. Install it (e.g. 'brew install python@3.12') and re-run."
    "$PY" -m venv .venv
    info "created .venv ($PY -> $("$PY" --version 2>&1))"
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python3 -m pip install --quiet --upgrade pip
  python3 -m pip install --quiet -r server/requirements.txt
  ok "deps installed"

  step "4. start uvicorn"
  # Run from repo root so relative config paths (./agent-data/…) resolve
  # the same way Docker resolves them (CWD=/app, agent-data/ alongside src/).
  export PYTHONPATH="$REPO_ROOT/server:${PYTHONPATH:-}"
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    info "uvicorn already running (pid $(cat "$PID_FILE"))"
  else
    if [ "$FOREGROUND" = "yes" ]; then
      info "running in foreground (Ctrl+C to stop)"
      exec env ENVIRONMENT=local PYTHONPATH="$PYTHONPATH" python3 -m uvicorn src.app:app \
        --host 0.0.0.0 --port 9080 --workers 1 --timeout-keep-alive 120
    else
      nohup env ENVIRONMENT=local PYTHONPATH="$PYTHONPATH" python3 -m uvicorn src.app:app \
        --host 0.0.0.0 --port 9080 --workers 1 --timeout-keep-alive 120 \
        > "$REPO_ROOT/$LOG_FILE" 2>&1 &
      echo $! > "$REPO_ROOT/$PID_FILE"
      info "uvicorn started in background (pid $(cat "$PID_FILE"))"
      info "logs: tail -f $LOG_FILE"
      info "stop: kill \$(cat $PID_FILE)"
    fi
  fi
  ok "server starting"
fi

# -- 4. wait for /health -----------------------------------------------------

step "5. Wait for /health"
for i in $(seq 1 30); do
  if curl -fsS -m 2 "$BASE_URL/health" >/dev/null 2>&1; then
    ok "/health 200 after ${i}s"
    break
  fi
  if [ "$i" = "30" ]; then
    if [ "$MODE" = "docker" ]; then
      info "recent container logs:"
      docker compose logs app --tail 20 || true
    else
      info "recent uvicorn logs:"
      tail -20 "$LOG_FILE" 2>/dev/null || true
    fi
    fail "/health never responded"
  fi
  sleep 1
done

# -- 5. register MCP ---------------------------------------------------------

if [ "$DO_MCP" = "yes" ]; then
  step "6. Register MCP with Claude Code"
  "$SCRIPT_DIR/setup-mcp.sh" | tail -10
  ok "MCP registered"
else
  info "skipped MCP registration (--no-mcp or claude CLI missing)"
fi

# -- 6. verify ---------------------------------------------------------------

if [ "$DO_VERIFY" = "yes" ]; then
  step "7. Verify"
  if [ "$MODE" = "docker" ]; then
    "$SCRIPT_DIR/verify_quickstart.sh" 2>&1 | tail -12 || info "verify had warnings"
  else
    "$SCRIPT_DIR/verify_quickstart.sh" --bare 2>&1 | tail -12 || info "verify had warnings"
  fi
fi

echo
printf "${GREEN}Done.${CLR} mangrove-agent is running at $BASE_URL\n\n"
echo "Next:"
echo "  - Restart Claude Code in this directory. The agent will greet you"
echo "    and walk through wallet setup + security. It will refuse to"
echo "    accept pasted private keys in chat — if you want to import an"
echo "    existing wallet, the agent will tell you to run"
echo "    ./scripts/stash-secret.sh first."
echo
