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

# pick_python [MIN_MINOR]  — echo the first interpreter that is >= 3.MIN_MINOR
# (default 10). On stock macOS, bare `python3` can still be the system 3.9 even
# after Homebrew installs python@3.12 — Homebrew lands it as `python3.12`, not
# as `python3` on PATH — so prefer versioned names before falling back. (#100)
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

# -- 1. python version ------------------------------------------------------

step "1. python ≥ 3.11"
# Select a >=3.11 interpreter by name (python3.12, …), not bare `python3`:
# on stock macOS the latter is still the system 3.9 even after python@3.12
# is installed. This same PY is reused for the venv below so it inherits the
# right interpreter. (#100)
PY="$(pick_python 11)" || fail "Python ≥ 3.11 not found. On macOS: 'brew install python@3.12' (Homebrew installs it as python3.12, not python3). Otherwise: https://www.python.org/downloads/"
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "python $PY_VER ($PY)"

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
# Docker bind-mounts create agent-data/ as root; a later bare-metal run then
# can't write the DB / master key. Catch it here with a clear fix instead of an
# opaque permission error deep inside uvicorn/SQLite.
if [ -d agent-data ] && [ ! -w agent-data ]; then
  fail "agent-data/ exists but is not writable by $(id -un). A previous Docker run likely created it as root. Reclaim it with:
      docker run --rm -v \"\$PWD\":/r alpine chown -R $(id -u):$(id -g) /r/agent-data
    (or: sudo chown -R $(id -u):$(id -g) agent-data), then re-run."
fi
ok "agent-data/ ready"

# -- 4. venv ----------------------------------------------------------------

step "4. $VENV_DIR"
if [ ! -d "$VENV_DIR" ]; then
  "$PY" -m venv "$VENV_DIR"
  info "created $VENV_DIR ($PY -> $("$PY" --version 2>&1))"
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
