#!/bin/bash
# block-wallet-secrets.sh — refuse to let private keys or mnemonics flow
# through Claude Code's conversation.
#
# Usage:
#   .claude/hooks/block-wallet-secrets.sh --mode user   # strict (UserPromptSubmit)
#   .claude/hooks/block-wallet-secrets.sh --mode tool   # context-aware (PostToolUse)
#
# --mode user:
#   A 0x+64-hex or 12/24-word lowercase-mnemonic anywhere in the user's
#   message is blocked. User input doesn't contain tx hashes (those come
#   from tool output), so a bare pattern match is safe and catches the
#   common case of an attendee pasting their key.
#
# --mode tool:
#   Tool responses routinely contain tx hashes that match the 0x+64-hex
#   pattern. A bare match would block every live trade. We only block
#   when the key-shaped value is adjacent to a field name that indicates
#   key material: "private_key", "seed_phrase", "secret", "mnemonic",
#   "wallet_secret". This catches server regressions where a MCP tool
#   response carries a key in a field meant for it, without false-
#   positive-ing on tx hashes.
#
# Exit codes (per Claude Code hook protocol):
#   0  — allow
#   2  — block, pass stderr message back to Claude for user-visible reply
#
# This hook is harness-enforced in .claude/settings.json. Neither the
# user nor the agent can disable it mid-session. Changes to its behavior
# require a git commit (visible in review).

set -uo pipefail

# Shared footer: names this hook's absolute path + the don't-bypass rule.
source "$(dirname "${BASH_SOURCE[0]}")/_hooklib.sh" 2>/dev/null || block_footer() { :; }

MODE=""
for arg in "$@"; do
    case "$arg" in
        --mode=user|--mode=tool)
            MODE="${arg#--mode=}"
            ;;
        --mode)
            shift
            MODE="$1"
            ;;
    esac
done

if [ -z "$MODE" ]; then
    # Default: conservative mode (strict user-style matching). Keeps the
    # hook functional if someone registers it without --mode.
    MODE="user"
fi

INPUT="$(cat)"

# Pattern: EVM private-key-shaped string (0x + exactly 64 hex chars, word-bounded).
EVM_KEY_RE='(^|[^0-9a-fA-F])0x[0-9a-fA-F]{64}([^0-9a-fA-F]|$)'

# Pattern: BIP39 mnemonic — 12 or 24 lowercase words, 3-8 chars each.
MNEMONIC_12_RE='(^|[^a-z])([a-z]{3,8} ){11}[a-z]{3,8}([^a-z]|$)'
MNEMONIC_24_RE='(^|[^a-z])([a-z]{3,8} ){23}[a-z]{3,8}([^a-z]|$)'

# Key-context field names (for tool-mode context check).
KEY_FIELD_RE='"(private_key|seed_phrase|secret|mnemonic|wallet_secret|master_key|key)"[[:space:]]*:'

HIT=""

if [ "$MODE" = "user" ]; then
    # Strict: any pattern match blocks.
    if printf '%s' "$INPUT" | grep -qE "$EVM_KEY_RE"; then
        HIT="evm_private_key"
    elif printf '%s' "$INPUT" | grep -qE "$MNEMONIC_12_RE" \
      || printf '%s' "$INPUT" | grep -qE "$MNEMONIC_24_RE"; then
        HIT="bip39_mnemonic"
    fi
else
    # Tool mode: only block if BOTH a key-shaped value AND a key-naming
    # field are present in the same payload. This catches
    #     {"private_key": "0x...64hex..."}
    # while allowing
    #     {"tx_hash": "0x...64hex..."}
    if printf '%s' "$INPUT" | grep -qE "$KEY_FIELD_RE"; then
        if printf '%s' "$INPUT" | grep -qE "$EVM_KEY_RE"; then
            HIT="evm_private_key_in_named_field"
        elif printf '%s' "$INPUT" | grep -qE "$MNEMONIC_12_RE" \
          || printf '%s' "$INPUT" | grep -qE "$MNEMONIC_24_RE"; then
            HIT="bip39_mnemonic_in_named_field"
        fi
    fi
fi

if [ -z "$HIT" ]; then
    exit 0
fi

cat >&2 <<EOF
BLOCKED by .claude/hooks/block-wallet-secrets.sh (mode=$MODE): detected
a $HIT pattern in the conversation. Wallet secrets MUST NOT flow through
Claude Code's chat — they would end up in your transcript file
(~/.claude/projects/**/*.jsonl) and in the Anthropic API context.

If this was you pasting a key, use the out-of-band flow instead:

  1. Open a terminal (the integrated one in VSCode is fine).
  2. Run:  ./scripts/stash-secret.sh
  3. The script prompts for your key with input hidden (no echo).
     It stashes the key in an in-process vault on the agent and
     prints a short vault_token.
  4. Come back to this chat and tell me:  "import wallet vault_token <ID>"
     I'll call the import_wallet MCP tool with that id — the plaintext
     never touches this conversation.

To create a fresh wallet, just ask me to — no key needs to be typed.

If this was a tool response that matched the pattern (defense-in-depth
trip), inspect the tool's code and make sure it's not returning
plaintext key material. Tool responses should carry a vault_token only;
the plaintext should go through the SecretVault + reveal-secret.sh CLI.

If this was a false positive (not a real key/mnemonic), rephrase to
avoid the 0x + 64-hex or 12/24-lowercase-word structure.
EOF
block_footer "${BASH_SOURCE[0]}"
exit 2
