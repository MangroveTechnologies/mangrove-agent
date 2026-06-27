#!/usr/bin/env bash
# stash-kraken-secret.sh — connect a Kraken account WITHOUT the API key or
# secret passing through Claude Code's conversation context.
#
# Flow:
#   1. You run this in a terminal (VSCode integrated terminal is fine).
#   2. It prompts for the Kraken API key + private key via `read -s` (no echo).
#   3. It POSTs them to the agent's localhost REST endpoint.
#   4. The agent stashes them in an in-process vault and returns a short
#      `vault_token`.
#   5. You tell the agent (in Claude Code): "connect kraken vault_token <ID>".
#      The agent calls cex/connect, which persists the creds ENCRYPTED at rest
#      (Fernet, agent-data/cex-kraken.enc) and consumes the vault entry.
#
# The key + secret go terminal -> bash -> localhost HTTP -> agent memory ->
# encrypted file. They never touch Claude Code's conversation or Anthropic's API.
#
# Least privilege: create the Kraken key with Query + Create/Cancel Orders only.
# Leave WITHDRAW FUNDS OFF. See the setup-kraken skill.
#
# Prereqs: agent running (./setup.sh); config has an API_KEYS entry.

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

[ -f "$CONFIG_FILE" ] || fail "$CONFIG_FILE not found. Run ./setup.sh first."

API_KEY="$(python3 -c "
import json
raw = json.load(open('$CONFIG_FILE')).get('API_KEYS', '')
print(next((k.strip() for k in raw.split(',') if k.strip()), ''))
")"
[ -n "$API_KEY" ] || fail "No API key in $CONFIG_FILE (API_KEYS field)."

curl -fsS -m 5 "$LOCAL_AGENT_URL/health" >/dev/null 2>&1 \
  || fail "$LOCAL_AGENT_URL/health not responding. Is the agent running? (./setup.sh)"

echo
echo "Connect your Kraken account. Input is hidden — nothing will echo."
echo "Create the key at Kraken > Settings > API with LEAST privilege"
echo "(Query + Create/Cancel Orders). Leave WITHDRAW FUNDS OFF."
echo

printf "Kraken API key: "
read -rs KR_KEY
echo
printf "Kraken private key (secret): "
read -rs KR_SECRET
echo

[ -n "$KR_KEY" ] && [ -n "$KR_SECRET" ] || fail "Empty key or secret. Aborted."

step "Stashing in the agent's in-process vault"
export KR_KEY KR_SECRET API_KEY LOCAL_AGENT_URL
RESPONSE="$(python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
body = json.dumps({"api_key": os.environ["KR_KEY"], "api_secret": os.environ["KR_SECRET"]}).encode()
req = urllib.request.Request(
    os.environ["LOCAL_AGENT_URL"] + "/api/v1/agent/cex/stash-kraken",
    data=body, method="POST",
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
KR_KEY=""; KR_SECRET=""; unset KR_KEY KR_SECRET

VAULT_TOKEN="$(printf '%s' "$RESPONSE" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['vault_token'])")"
TTL="$(printf '%s' "$RESPONSE" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['secret_ttl_seconds'])")"

ok "stashed"
info "vault_token: $VAULT_TOKEN"
info "expires in: ${TTL}s (single-read)"
echo
printf "${GREEN}Next:${CLR} in Claude Code, say:\n\n"
echo "    connect kraken vault_token $VAULT_TOKEN"
echo
echo "The agent calls cex/connect, persists the creds encrypted, and consumes"
echo "the token. Your key never enters the conversation or the transcript."
echo
