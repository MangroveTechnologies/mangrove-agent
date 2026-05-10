#!/usr/bin/env bash
# run-bare.sh — start the mangrove-agent bare-metal (no Docker required).
#
# Primary install path (bare-metal vs Docker decision in docs/):
#   - Bare-metal is the default because the `keyring` library can reach
#     the OS keychain (macOS Keychain, Linux Secret Service, Windows
#     Credential Manager) only when the Python process is running
#     natively on the host — Docker containers are walled off from
#     those backends.
#   - This script: venv → pip install → start uvicorn.
#
# Prereqs (enforced below):
#   - python3 ≥ 3.11
#   - local-config.json exists with a valid MANGROVE_API_KEY
#   - ./agent-data/ directory (created on first run)
#
# On macOS / Linux, the keychain path JUST WORKS — no init-master-key.sh
# needed. The script validates keychain access once and reports.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

CONFIG_FILE="server/src/config/local-config.json"
VENV_DIR=".venv"
HOST="${BARE_HOST:-0.0.0.0}"
PORT="${BARE_PORT:-9080}"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

# -- 1. python3 version -----------------------------------------------------

step "1. python3 ≥ 3.11"
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not on PATH. Install Python 3.11+: https://www.python.org/downloads/"
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  fail "python3 $PY_VER found; need ≥ 3.11. Install a newer Python."
fi
ok "python3 $PY_VER"

# -- 2. config file present + has real API key -----------------------------

step "2. $CONFIG_FILE"
if [ ! -f "$CONFIG_FILE" ]; then
  fail "$CONFIG_FILE not found. Run ./setup.sh first (it seeds the config from the example)."
fi
PLACEHOLDER="$(python3 -c "
import json
v = json.load(open('$CONFIG_FILE')).get('MANGROVE_API_KEY', '')
print('yes' if v == 'REPLACE_WITH_YOUR_DEV_OR_PROD_KEY' or not v else 'no')
")"
if [ "$PLACEHOLDER" = "yes" ]; then
  fail "MANGROVE_API_KEY in $CONFIG_FILE is still the placeholder. Edit the file."
fi
ok "config looks good"

# -- 3. agent-data directory -------------------------------------------------

step "3. agent-data/ directory"
if [ ! -d agent-data ]; then
  mkdir -p agent-data
  chmod 700 agent-data
  info "created agent-data/ (chmod 700)"
fi
ok "agent-data/ ready"

# -- 4. venv ----------------------------------------------------------------

step "4. $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
  info "created $VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
ok "venv active: $(which python3)"

# -- 5. pip install ----------------------------------------------------------

step "5. pip install -r server/requirements.txt"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r server/requirements.txt
ok "deps installed"

# -- 6. keychain smoke test --------------------------------------------------

step "6. keychain reachable"
KC_STATUS="$(python3 - <<'PY'
try:
    import keyring
    # Non-destructive: read-only probe. No key is created.
    keyring.get_password("mangrove-agent-probe", "user")
    print("ok")
except Exception as e:
    print(f"fail: {e}")
PY
)"
if [[ "$KC_STATUS" == "ok" ]]; then
  ok "keychain reachable ($(python3 -c 'import keyring, os; print(keyring.get_keyring().__class__.__name__)'))"
else
  info "keychain not reachable ($KC_STATUS); keyfile at ./agent-data/master.key will be used"
fi

# -- 7. start uvicorn --------------------------------------------------------

step "7. start uvicorn on $HOST:$PORT"
echo
info "press Ctrl+C to stop. logs stream below."
echo

# Run from repo root (CWD = $REPO_ROOT) so relative paths in config
# like ./agent-data/agent.db resolve to the repo-root dir, matching how
# Docker runs it (CWD=/app with agent-data/ bind-mounted alongside src/).
# PYTHONPATH=server lets the `src.app:app` import resolve.
export PYTHONPATH="$REPO_ROOT/server:${PYTHONPATH:-}"
exec env ENVIRONMENT=local python3 -m uvicorn src.app:app \
  --host "$HOST" --port "$PORT" \
  --workers 1 --timeout-keep-alive 120
