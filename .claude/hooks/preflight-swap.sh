#!/usr/bin/env bash
# preflight-swap.sh — PreToolUse hook for mcp__mangrove-agent__execute_swap.
#
# Refuses the swap if the wallet holds zero of the input token on the target
# chain. Catches the "did my deposit actually land?" case cleanly BEFORE
# the SDK burns cycles constructing a quote against an empty wallet, and
# before the wallet_manager signing guard has to trip on an unfundable tx.
#
# This is the companion to the hard signing guard in
# server/src/services/wallet_manager.py::_validate_sign_target — that guard
# restricts WHAT can be signed (1inch-only); this hook restricts WHEN we
# even try (funds actually present).
#
# Exit codes (Claude Code hook protocol):
#   0 — allow (balance sufficient OR can't cleanly verify — let execute_swap proceed and raise its own error)
#   2 — block (wallet empty of the input_token on this chain; stderr message surfaces to Claude)

set -uo pipefail

# Shared footer: names this hook's absolute path + the don't-bypass rule.
source "$(dirname "${BASH_SOURCE[0]}")/_hooklib.sh" 2>/dev/null || block_footer() { :; }

INPUT="$(cat)"

# Fast path: only fire on the execute_swap MCP tool. All other tools pass through.
TOOL=$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print('')
    sys.exit(0)
print(d.get('tool_name', ''))
" 2>/dev/null)

if [ "$TOOL" != "mcp__mangrove-agent__execute_swap" ]; then
    exit 0
fi

# Extract args. If any required field is missing, fall through — execute_swap's
# own Pydantic validator will surface the right error shape.
PARAMS=$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    args = d.get('tool_input', {}) or d.get('arguments', {})
    print(json.dumps({
        'wallet':       args.get('wallet_address', ''),
        'input_token':  args.get('input_token', ''),
        'amount':       args.get('amount', 0),
        'chain_id':     args.get('chain_id', 0),
    }))
except Exception:
    print('{}')
")

WALLET=$(printf '%s' "$PARAMS" | python3 -c "import json, sys; print(json.loads(sys.stdin.read()).get('wallet',''))")
INPUT_TOKEN=$(printf '%s' "$PARAMS" | python3 -c "import json, sys; print(json.loads(sys.stdin.read()).get('input_token',''))")
AMOUNT=$(printf '%s' "$PARAMS" | python3 -c "import json, sys; print(json.loads(sys.stdin.read()).get('amount',0))")
CHAIN_ID=$(printf '%s' "$PARAMS" | python3 -c "import json, sys; print(json.loads(sys.stdin.read()).get('chain_id',0))")

if [ -z "$WALLET" ] || [ -z "$INPUT_TOKEN" ] || [ "$CHAIN_ID" = "0" ]; then
    exit 0  # incomplete payload — let Pydantic handle it
fi

# Symbol-style input_token (not a 0x address) — we can't look it up by symbol
# in the balances dict without decimals metadata. Fall through; the SDK
# resolves the symbol and the signing guard still protects us if the route
# ends up being non-1inch.
case "$INPUT_TOKEN" in
    0x*|0X*) ;;
    *) exit 0 ;;
esac

# Locate repo root and config.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG_FILE="$REPO_ROOT/server/src/config/local-config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    exit 0  # can't authenticate without config — fall through
fi

API_KEY=$(python3 -c "
import json
try:
    raw = json.load(open('$CONFIG_FILE')).get('API_KEYS', '')
except Exception:
    raw = ''
print(next((k.strip() for k in raw.split(',') if k.strip()), ''))
")

if [ -z "$API_KEY" ]; then
    exit 0
fi

BASE_URL="${BASE_URL:-http://localhost:9080}"

# Query balances. 3-second timeout keeps the hook fast; server unreachable =
# fall through so the user sees the real error from execute_swap.
RESPONSE=$(curl -fsS -m 3 \
    -H "X-API-Key: $API_KEY" \
    "$BASE_URL/api/v1/agent/wallet/$WALLET/balances?chain_id=$CHAIN_ID" 2>/dev/null)

if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Look up the specific input_token balance (case-insensitive key match).
BAL=$(printf '%s' "$RESPONSE" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
except Exception:
    print('UNKNOWN'); sys.exit()
balances = data.get('balances', {}) or {}
target = '$INPUT_TOKEN'.lower()
for k, v in balances.items():
    if str(k).lower() == target:
        print(v); break
else:
    print('UNKNOWN')
")

if [ "$BAL" = "UNKNOWN" ] || [ -z "$BAL" ]; then
    exit 0  # balance not in the tracked set — can't cleanly judge
fi

# Block only the zero-balance case. If balance is >0 but < amount, the SDK's
# own slippage / min-amount check surfaces a better error with token decimals
# already applied.
if [ "$BAL" = "0" ]; then
    cat >&2 <<EOF
BLOCKED by .claude/hooks/preflight-swap.sh:

Wallet $WALLET holds 0 of input_token $INPUT_TOKEN on chain_id $CHAIN_ID.
Cannot execute a swap of $AMOUNT — the wallet is empty of the token you're
trying to spend.

If you expected this wallet to be funded:
  1. Check basescan:
       https://basescan.org/address/$WALLET
     (use sepolia.basescan.org for Base Sepolia testnet)
  2. Confirm the deposit tx confirmed on the RIGHT CHAIN (chain_id $CHAIN_ID).
     Same address exists on every EVM chain — funds sent to Ethereum mainnet
     or Arbitrum are NOT visible on Base.
  3. If you're on testnet (chain_id 84532), use a Base Sepolia faucet to fund
     the wallet first — you won't get quotes from 1inch on testnet either way,
     so testnet execution is best used for signing-path validation, not real
     swaps.

Re-run execute_swap once the deposit is confirmed on the target chain.
EOF
    block_footer "${BASH_SOURCE[0]}"
    exit 2
fi

exit 0
