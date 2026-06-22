#!/bin/bash
# _hooklib.sh — shared helpers for this repo's .claude/hooks/*.sh guardrails.
#
# Source it near the top of a hook:
#     source "$(dirname "${BASH_SOURCE[0]}")/_hooklib.sh"
# then, right before `exit 2` on a block, append the standard footer:
#     block_footer "${BASH_SOURCE[0]}"; exit 2
#
# The footer names the EXACT hook file (absolute path) so the user always knows
# which guardrail fired and where it lives, and states the standing rule that
# the agent must not disable/delete the hook to route around it — only the human
# edits or removes it. Keep this dependency co-located in .claude/hooks/.

# block_footer <hook-path>  — emit the standard "which guardrail + don't-bypass"
# footer to stderr (the channel Claude Code surfaces back to the agent/user).
block_footer() {
    local src="${1:-${BASH_SOURCE[0]}}" abs
    abs="$(cd "$(dirname "$src")" 2>/dev/null && pwd)/$(basename "$src")"
    cat >&2 <<EOF

──────────────────────────────────────────────────────────────────────────
Guardrail: ${abs}
This block is intentional. Do NOT modify, disable, or delete this hook to get
around it. If the rule itself is wrong, the human edits/removes that file —
the agent must not. (See .claude/hooks/README.md for what each hook guards.)
──────────────────────────────────────────────────────────────────────────
EOF
}
