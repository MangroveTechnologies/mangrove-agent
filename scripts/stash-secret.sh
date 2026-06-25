#!/usr/bin/env bash
# stash-secret.sh — import an existing wallet's private key WITHOUT
# the key passing through Claude Code's conversation context.
#
# Flow:
#   1. You run this in a terminal (VSCode integrated terminal is fine).
#   2. It prompts for the key via `read -s` — no echo to the terminal.
#   3. It POSTs the key to the agent's localhost REST endpoint.
#   4. The agent stashes the key in an in-process vault (TTL ~300s,
#      single-read) and returns an opaque `vault_token`.
#   5. You tell the agent (in Claude Code) to import that id.
#      The agent calls the `import_wallet` MCP tool, which consumes the
#      id and stores the wallet.
#
# Why: the private key goes terminal → bash → localhost HTTP → server
# memory. It never touches Claude Code's conversation, so it never ends
# up in the transcript file or on Anthropic's API.
#
# Prereqs: agent is running (./setup.sh). Config has an API_KEYS entry.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

LOCAL_AGENT_URL="${LOCAL_AGENT_URL:-http://localhost:9080}"
CONFIG_FILE="server/src/config/local-config.json"

GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; DIM="\033[2m"; CLR="\033[0m"
step() { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
fail() { printf "${RED}  ✗${CLR} %s\n" "$1" >&2; exit 1; }
info() { printf "${DIM}    %s${CLR}\n" "$1"; }

# -- preflight --------------------------------------------------------------

if [ ! -f "$CONFIG_FILE" ]; then
  fail "$CONFIG_FILE not found. Run ./setup.sh first."
fi

API_KEY="$(python3 -c "
import json
raw = json.load(open('$CONFIG_FILE')).get('API_KEYS', '')
key = next((k.strip() for k in raw.split(',') if k.strip()), '')
print(key)
")"
if [ -z "$API_KEY" ]; then
  fail "No API key in $CONFIG_FILE (API_KEYS field)."
fi

if ! curl -fsS -m 5 "$LOCAL_AGENT_URL/health" >/dev/null 2>&1; then
  fail "$LOCAL_AGENT_URL/health not responding. Is the agent running? (./setup.sh)"
fi

# -- prompt for secret (hidden) ---------------------------------------------

echo
echo "Paste your private key below. Input is hidden — nothing will echo."
echo "Accept: 0x-prefixed 64 hex chars, OR a 12/24-word BIP39 mnemonic."
echo

# `read -s` suppresses echo. -r preserves backslashes. 2>/dev/tty so the
# prompt works even if stdin is redirected (we don't pipe secrets through
# stdin, but belt + suspenders).
printf "secret: "
read -rs SECRET
echo

if [ -z "$SECRET" ]; then
  fail "Empty secret. Aborted."
fi

# -- stash --------------------------------------------------------------------

step "Stashing in the agent's in-process vault"
# Send as JSON body. Use python to safely escape in case the secret
# contains characters that would break a shell-quoted curl data arg.
# Export so the heredoc (run in a subshell via python3 - <<'PY') can
# read via os.environ.
export SECRET API_KEY LOCAL_AGENT_URL
RESPONSE="$(python3 - <<'PY'
import json, urllib.request, os, sys
body = json.dumps({"secret": os.environ["SECRET"]}).encode()
req = urllib.request.Request(
    os.environ["LOCAL_AGENT_URL"] + "/api/v1/agent/wallet/stash-secret",
    data=body,
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

# Drop the secret from the environment immediately after use.
SECRET=""
unset SECRET

VAULT_TOKEN="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; print(json.loads(sys.stdin.read())['vault_token'])")"
TTL="$(printf '%s' "$RESPONSE" | python3 -c "import json, sys; print(json.loads(sys.stdin.read())['secret_ttl_seconds'])")"

ok "stashed"
info "vault_token: $VAULT_TOKEN"
info "expires in: ${TTL}s (single-read)"

echo
printf "${GREEN}Next:${CLR} in Claude Code, say:\n\n"
echo "    Import my wallet with vault_token $VAULT_TOKEN"
echo
echo "The agent will call import_wallet(vault_token=$VAULT_TOKEN). Your key"
echo "never enters the conversation or the transcript."
echo
