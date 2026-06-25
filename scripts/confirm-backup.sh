#!/usr/bin/env bash
# confirm-backup.sh — flip wallets.backup_confirmed_at for a given wallet.
#
# Gate: execute_swap and update_strategy_status(live) REFUSE to run for
# wallets whose backup_confirmed_at is null. This script is how you lift
# that gate AFTER you've saved the wallet's secret outside the agent
# (via ./scripts/reveal-secret.sh + your password manager / hardware
# wallet / paper).
#
# This script does NOT see or transmit the wallet's secret. It only tells
# the server "yes, I've backed this up; unlock live trading."

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

LOCAL_AGENT_URL="${LOCAL_AGENT_URL:-http://localhost:9080}"
CONFIG_FILE="server/src/config/local-config.json"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }

if [ $# -ne 1 ]; then
  echo "Usage: $0 <wallet-address>" >&2
  exit 2
fi
ADDRESS="$1"

if [ ! -f "$CONFIG_FILE" ]; then
  fail "$CONFIG_FILE not found. Run ./setup.sh first."
fi

API_KEY="$(python3 -c "
import json
raw = json.load(open('$CONFIG_FILE')).get('API_KEYS', '')
print(next((k.strip() for k in raw.split(',') if k.strip()), ''))
")"
if [ -z "$API_KEY" ]; then
  fail "No API key in $CONFIG_FILE."
fi

if ! curl -fsS -m 5 "$LOCAL_AGENT_URL/health" >/dev/null 2>&1; then
  fail "$LOCAL_AGENT_URL/health not responding. Is the agent running?"
fi

# Export so the heredoc (with quoted 'PY' tag to disable shell
# interpolation) can read via os.environ.
export API_KEY LOCAL_AGENT_URL ADDRESS
RESPONSE="$(python3 - <<'PY'
import json, urllib.request, os, sys
url = os.environ["LOCAL_AGENT_URL"] + "/api/v1/agent/wallet/" + os.environ["ADDRESS"] + "/confirm-backup"
req = urllib.request.Request(
    url,
    data=b"{}",
    method="POST",
    headers={"Content-Type": "application/json", "X-API-Key": os.environ["API_KEY"]},
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write(f"HTTP {e.code}: {e.read().decode()}\n")
    sys.exit(1)
PY
)"

CONFIRMED="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; print(json.loads(sys.stdin.read())['backup_confirmed_at'])")"
MESSAGE="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; print(json.loads(sys.stdin.read())['message'])")"

printf "${GREEN}  \u2713${CLR} %s\n" "$ADDRESS"
printf "${DIM}    confirmed_at: %s${CLR}\n" "$CONFIRMED"
echo
echo "$MESSAGE"
