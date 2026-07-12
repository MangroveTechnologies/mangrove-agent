#!/bin/bash
# block-wallet-secrets.sh — refuse to let private keys or mnemonics flow
# through Claude Code's conversation.
#
# Usage:
#   .claude/hooks/block-wallet-secrets.sh --mode user   # strict (UserPromptSubmit)
#   .claude/hooks/block-wallet-secrets.sh --mode tool   # context-aware (PostToolUse)
#
# --mode user:
#   A 0x+64-hex or a real BIP39 mnemonic anywhere in the user's message is
#   blocked. User input doesn't contain tx hashes (those come from tool
#   output), so a bare pattern match is safe and catches the common case of
#   a user pasting their key.
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

# Key-context field names (for tool-mode context check).
KEY_FIELD_RE='"(private_key|seed_phrase|secret|mnemonic|wallet_secret|master_key|key)"[[:space:]]*:'

# BIP39 mnemonic detection.
#
# A real mnemonic is a run of >=12 consecutive words ALL drawn from the
# 2048-word BIP39 list. The previous version matched ANY 12 consecutive
# lowercase 3-8 char words and never consulted the wordlist — so it
# false-positived on ordinary prose (and on this codebase's normal chatter),
# blocking the user's regular messages. We now require wordlist membership.
# This is sound because common English stopwords (the/and/of/to/is/for/that...)
# are NOT in the BIP39 list, so real prose cannot sustain a 12-word run, while
# a genuine 12/15/18/21/24-word mnemonic will.
WORDLIST="$(dirname "${BASH_SOURCE[0]}")/bip39-english.txt"

# Returns 0 (true) if "$1" contains a run of >=12 consecutive BIP39 words.
# The text is passed via env var (NOT stdin), because `python3 - <<'PY'` already
# uses stdin to read the program — piping data in would be silently discarded.
# Fail-open (exit 1 = "no mnemonic") if the wordlist is unreadable, so a missing
# list never wedges every message — the EVM-key check is independent.
mnemonic_present() {
    MNEMONIC_TEXT="$1" WORDLIST="$WORDLIST" python3 - <<'PY'
import os, re, sys
try:
    words = {w.strip() for w in open(os.environ["WORDLIST"]) if w.strip()}
except Exception:
    sys.exit(1)
if not words:
    sys.exit(1)
toks = re.findall(r"[a-z]+", os.environ.get("MNEMONIC_TEXT", "").lower())
run = 0
for t in toks:
    run = run + 1 if t in words else 0
    if run >= 12:
        sys.exit(0)   # mnemonic-shaped run of real BIP39 words found
sys.exit(1)
PY
}

HIT=""

if [ "$MODE" = "user" ]; then
    # Strict: any pattern match blocks.
    if printf '%s' "$INPUT" | grep -qE "$EVM_KEY_RE"; then
        HIT="evm_private_key"
    elif mnemonic_present "$INPUT"; then
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
        elif mnemonic_present "$INPUT"; then
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
avoid the 0x + 64-hex structure.
EOF
block_footer "${BASH_SOURCE[0]}"
exit 2
