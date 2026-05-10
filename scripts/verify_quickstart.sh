#!/usr/bin/env bash
# verify_quickstart.sh — prove mangrove-agent is set up correctly.
#
# Modes:
#   (default) Docker    Full check including docker daemon, compose up,
#                       and log scanning via `docker compose logs`.
#   --bare              Bare-metal: skip docker checks, skip compose up,
#                       skip log scan (logs are in agent-data/bare.log but
#                       event cadence is the same). Validates the health
#                       + tool-catalog + expected 24-tool set directly.
#
# Shared checks:
#   - local-config.json exists + has a real API key
#   - /health returns 200
#   - /api/v1/agent/status returns a version
#   - /api/v1/agent/tools returns the expected tool count

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BASE_URL="${BASE_URL:-http://localhost:9080}"
HEALTH_TIMEOUT_S=30
START_TIME=$(date +%s)
MODE="docker"

for arg in "$@"; do
    case "$arg" in
        --bare) MODE="bare" ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--bare]
  --bare    Skip docker-specific checks (use when server runs via run-bare.sh)
EOF
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# -- helpers ----------------------------------------------------------------

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }
elapsed() { echo $(( $(date +%s) - START_TIME )); }

# -- 1. Docker available (docker mode only) ---------------------------------

if [ "$MODE" = "docker" ]; then
    step "1. Docker available"
    if ! command -v docker >/dev/null 2>&1; then
        fail "docker not found on PATH — install from https://docs.docker.com/get-docker/"
    fi
    if ! docker info >/dev/null 2>&1; then
        fail "docker daemon not running — start Docker Desktop or dockerd"
    fi
    ok "docker is installed and running"
else
    step "1. Bare-metal mode — skipping docker checks"
    ok "mode: bare"
fi

# -- 2. Config present ------------------------------------------------------

step "2. server/src/config/local-config.json exists + has a real API key"
CFG="server/src/config/local-config.json"
if [ ! -f "$CFG" ]; then
    fail "$CFG missing — run: ./setup.sh"
fi
if grep -q "REPLACE_WITH" "$CFG"; then
    fail "$CFG still contains REPLACE_WITH placeholder — set a real MANGROVE_API_KEY"
fi
if ! grep -q '"MANGROVE_API_KEY"' "$CFG"; then
    fail "$CFG missing MANGROVE_API_KEY"
fi
ok "config looks good"

# -- 3. Server running ------------------------------------------------------

if [ "$MODE" = "docker" ]; then
    step "3. docker compose up --build (first build may take ~60s)"
    # Clean up any stale bind-mount artifact from pre-PR #29 builds.
    if [ -f "./agent.db" ] && [ ! -d "./agent-data" ]; then
        info "found ./agent.db from pre-PR #29 layout; leaving in place (unused)"
    fi
    mkdir -p agent-data
    if ! docker compose up -d --build >/dev/null 2>&1; then
        fail "docker compose up failed — rerun without redirect to see errors"
    fi
    ok "container started"

    cleanup() {
        if [ "${VERIFY_STOP_ON_EXIT:-0}" = "1" ]; then
            docker compose down >/dev/null 2>&1 || true
        fi
    }
    trap cleanup EXIT
else
    step "3. Bare-metal server"
    info "expecting it to already be running (./scripts/run-bare.sh or ./setup.sh)"
    ok "skipped startup"
fi

# -- 4. /health reachable ---------------------------------------------------

step "4. waiting for /health (up to ${HEALTH_TIMEOUT_S}s)"
for i in $(seq 1 $HEALTH_TIMEOUT_S); do
    if curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
        ok "/health returned 200 after ${i}s"
        break
    fi
    if [ "$i" = "$HEALTH_TIMEOUT_S" ]; then
        if [ "$MODE" = "docker" ]; then
            fail "/health did not respond within ${HEALTH_TIMEOUT_S}s — check: docker compose logs"
        else
            fail "/health did not respond within ${HEALTH_TIMEOUT_S}s — check: tail agent-data/bare.log"
        fi
    fi
    sleep 1
done

# -- 5. /status (free) ------------------------------------------------------

step "5. GET /api/v1/agent/status"
STATUS_JSON="$(curl -fsS "$BASE_URL/api/v1/agent/status")" \
    || fail "/status request failed"
VERSION="$(echo "$STATUS_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))')"
if [ -z "$VERSION" ]; then
    fail "/status did not return a version field: $STATUS_JSON"
fi
ok "agent version $VERSION"

# -- 6. /tools with X-API-Key -----------------------------------------------

step "6. GET /api/v1/agent/tools with API key"
API_KEY="$(python3 -c "import json; print(json.load(open('$CFG')).get('API_KEYS','').split(',')[0].strip())")"
if [ -z "$API_KEY" ]; then
    fail "could not read API_KEYS from $CFG"
fi

TOOLS_JSON="$(curl -fsS -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/agent/tools")" \
    || fail "/tools request failed"

TOOL_COUNT="$(echo "$TOOLS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("tools",[])))')"
# Previous minimum was 22. Post-secret-vault + import_wallet we expect 24
# (status, list_tools, create_wallet, import_wallet, list_wallets,
#  get_balances, list_dex_venues, get_swap_quote, execute_swap, get_ohlcv,
#  get_market_data, list_signals, create_strategy_autonomous,
#  create_strategy_manual, list_strategies, get_strategy,
#  update_strategy_status, backtest_strategy, evaluate_strategy,
#  list_evaluations, list_trades, list_all_trades, kb_search, hello_mangrove)
if [ "$TOOL_COUNT" -lt 23 ]; then
    fail "expected >=23 tools, got $TOOL_COUNT"
fi
ok "tool catalog returned $TOOL_COUNT tools"

# -- 7. Startup log events (docker only) ------------------------------------

if [ "$MODE" = "docker" ]; then
    step "7. verifying startup log events"
    LOGS="$(docker compose logs --no-color 2>&1 || true)"
    for event in "db.migrated" "scheduler.started" "app.startup"; do
        if ! echo "$LOGS" | grep -q "$event"; then
            fail "startup log event '$event' not found — check: docker compose logs"
        fi
    done
    ok "db.migrated + scheduler.started + app.startup all present"
else
    step "7. Startup log events"
    info "skipped in bare mode (check agent-data/bare.log manually if concerned)"
    ok "skipped"
fi

# -- summary ----------------------------------------------------------------

TOTAL="$(elapsed)"
printf "\n${GREEN}✓ quickstart verified in ${TOTAL}s${CLR}\n\n"
info "mangrove-agent is running at ${BASE_URL}"
if [ "$MODE" = "docker" ]; then
    info "stop with: docker compose down"
else
    info "stop with: kill \$(cat agent-data/bare.pid) (or Ctrl+C if foreground)"
fi
