#!/usr/bin/env bash
# init-master-key.sh — generate the Fernet master keyfile (bare-metal
# users: usually unnecessary, the OS keychain handles it; Docker users:
# required because the container can't reach the host keychain).
#
# Behavior:
#   - If ./agent-data/master.key exists: no-op (idempotent).
#   - Else: generate a Fernet key (32 random bytes, urlsafe-b64 via
#     Python stdlib — no cryptography package needed on the host), write
#     to ./agent-data/master.key with chmod 600.
#
# This replaces the old MASTER_KEY_ENV_FALLBACK config-string approach.
# Keyfile > config string: file permissions, no JSON-diff footguns, no
# accidental commits via `git add` of local-config.json.
#
# Usually you don't need to run this directly — ./setup.sh and the agent
# both create the keyfile on demand. It's exposed as a script for
# explicit, idempotent pre-first-run setup in CI.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

KEY_PATH="./agent-data/master.key"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

step "1. python3 available"
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not on PATH."
fi
ok "python3 found"

step "2. ./agent-data/ directory"
mkdir -p agent-data
chmod 700 agent-data
ok "ready (chmod 700)"

step "3. Check $KEY_PATH"
if [ -f "$KEY_PATH" ]; then
  BYTES="$(wc -c < "$KEY_PATH" | tr -d ' ')"
  if [ "$BYTES" -eq 44 ]; then
    info "already exists (44 bytes — valid Fernet key)"
    ok "idempotent — nothing to do"
    exit 0
  else
    fail "$KEY_PATH exists but is $BYTES bytes (expected 44). Inspect manually."
  fi
fi
info "not found — generating"

step "4. Generate + write Fernet key"
python3 - <<PY
import base64, os, sys
# Fernet.generate_key() == base64.urlsafe_b64encode(os.urandom(32))
key = base64.urlsafe_b64encode(os.urandom(32))
assert len(key) == 44
with open("$KEY_PATH", "wb") as f:
    f.write(key)
os.chmod("$KEY_PATH", 0o600)
PY
ok "written with chmod 600"

echo
printf "${GREEN}Done.${CLR} Master key at $KEY_PATH\n\n"
echo "This key protects agent credential/payment state; wallet custody uses"
echo "its separate protected identity and master key. Preserve existing keys"
echo "and recovery data. Human wallet backup/import is documented in docs/custody.md."
