#!/usr/bin/env bash
# reveal-secret.sh — retrieve a wallet's plaintext secret OUT-OF-BAND.
#
# Two modes:
#   1. By vault_token (freshly-created wallets):
#        ./scripts/reveal-secret.sh <vault_token>
#      Consumes the vault entry (single-read, TTL-bound). Use this right
#      after `create_wallet` returns a vault_token.
#
#   2. By address (already-stored wallets):
#        ./scripts/reveal-secret.sh --address <wallet-address>
#      Decrypts the wallet's encrypted_secret with the master key and
#      prints the plaintext. Use this to back up an existing wallet
#      you forgot to back up at creation time.
#
# The secret is printed to THIS terminal only. It does not pass through
# Claude Code or get logged anywhere beyond structured event logs
# (which record only the address / vault_token, not the plaintext).
#
# After you copy it into a password manager / hardware wallet / paper,
# clear your terminal scrollback if you care. The agent keeps no record
# of the reveal event containing the secret itself.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

LOCAL_AGENT_URL="${LOCAL_AGENT_URL:-http://localhost:9080}"
CONFIG_FILE="server/src/config/local-config.json"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }

usage() {
  cat >&2 <<EOF
Usage:
  $0 <vault_token>                  # reveal a stashed secret (single-read)
  $0 --address <wallet-address>   # reveal an already-stored wallet secret
EOF
  exit 2
}

if [ $# -lt 1 ]; then usage; fi

MODE=""
ARG=""
case "$1" in
  --address)
    [ $# -eq 2 ] || usage
    MODE="address"
    ARG="$2"
    ;;
  --help|-h)
    usage
    ;;
  *)
    [ $# -eq 1 ] || usage
    MODE="id"
    ARG="$1"
    ;;
esac

if [ ! -f "$CONFIG_FILE" ]; then
  fail "$CONFIG_FILE not found. Run ./setup.sh first."
fi

API_KEY="$(python3 -c "
import json
raw = json.load(open('$CONFIG_FILE')).get('API_KEYS', '')
print(next((k.strip() for k in raw.split(',') if k.strip()), ''))
")"
if [ -z "$API_KEY" ]; then
  fail "No API key in $CONFIG_FILE (API_KEYS field)."
fi

if ! curl -fsS -m 5 "$LOCAL_AGENT_URL/health" >/dev/null 2>&1; then
  fail "$LOCAL_AGENT_URL/health not responding. Is the agent running?"
fi

if [ "$MODE" = "id" ]; then
  URL="$LOCAL_AGENT_URL/api/v1/agent/wallet/reveal-secret/$ARG"
else
  URL="$LOCAL_AGENT_URL/api/v1/agent/wallet/$ARG/reveal"
fi

# Export so the Python heredoc can read via os.environ.
export API_KEY URL

RESPONSE="$(python3 - <<'PY'
import json, urllib.request, os, sys
req = urllib.request.Request(
    os.environ["URL"],
    headers={"X-API-Key": os.environ["API_KEY"]},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(f"HTTP {e.code}: {e.read().decode()}\n")
    sys.exit(1)
PY
)"

SECRET="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; print(json.loads(sys.stdin.read())['secret'])")"
ADDR="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; d=json.loads(sys.stdin.read()); print(d.get('address') or '')")"

echo
printf "${YELLOW}Wallet secret (copy to a password manager / hardware wallet / paper):${CLR}\n"
echo
printf "${GREEN}%s${CLR}\n" "$SECRET"
echo
if [ -n "$ADDR" ]; then
  printf "${DIM}Derived address: %s${CLR}\n" "$ADDR"
fi
printf "${DIM}Clear this terminal's scrollback when you're done (Cmd/Ctrl+K).${CLR}\n"
echo
echo "After saving, confirm the backup to unlock live trading:"
if [ -n "$ADDR" ]; then
  printf "    ${DIM}./scripts/confirm-backup.sh %s${CLR}\n" "$ADDR"
else
  echo "    ./scripts/confirm-backup.sh <address>"
fi
echo
