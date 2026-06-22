#!/bin/bash
# block-main-commits.sh — refuse git write operations on main/master.
#
# Why this exists:
#   The git-workflow rule (.claude/rules/git-workflow.md) says NEVER
#   commit directly to main. Agents have slipped this rule twice in
#   recent sessions and had to move commits to feature branches after
#   the fact. This hook makes the rule harness-enforced instead of
#   convention-enforced.
#
# Registered as a PreToolUse hook on the Bash tool in
# .claude/settings.json — it intercepts Bash tool calls before they
# run, inspects the command, and blocks if the agent is about to
# commit to main or push to origin/main.
#
# Hook protocol (Claude Code):
#   stdin  — JSON with tool_name + tool_input.command
#   exit 0 — allow
#   exit 2 — block; stderr becomes the blocking message to the LLM
#
# The hook is permissive outside a git repo (no-op) and on non-Bash
# tools (no-op). It only engages when:
#   * tool_name == "Bash"
#   * cwd is inside a git repo
#   * command matches a git write-operation pattern
#   * current branch is main or master
#
# Human commits via git CLI directly are NOT blocked by this hook —
# it only sees agent Bash tool calls. For human-side enforcement, use
# GitHub branch protection in the repo settings (separate toggle).

set -uo pipefail

# Shared footer: names this hook's absolute path + the don't-bypass rule.
# Degrades to a no-op if the lib is somehow absent (never breaks the guard).
source "$(dirname "${BASH_SOURCE[0]}")/_hooklib.sh" 2>/dev/null || block_footer() { :; }

INPUT="$(cat)"

# Extract tool_name + the command payload. The PreToolUse JSON shape is
#   {"tool_name": "Bash", "tool_input": {"command": "...", ...}}
# jq would be cleaner; we use Python for portability (macOS ships awk +
# python3 but not jq by default).
TOOL_NAME="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)"

if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

COMMAND="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('command', ''))
except Exception:
    print('')
" 2>/dev/null)"

if [ -z "$COMMAND" ]; then
    exit 0
fi

# Only inspect git commands. Anything else — passes through.
case "$COMMAND" in
    *'git commit'*|*'git push'*|*'git merge'*|*'git rebase'*|*'git reset --hard'*|*'git reset HEAD~'*)
        ;;
    *)
        exit 0
        ;;
esac

# Detect the target repo from `git -C <path> ...`. Worktree workflows MUST
# use this form — without it, the hook's own cwd (typically the main tree
# on `main`) false-blocks legitimate feature-branch commits in a sibling
# worktree. `cd <path> && git ...` is NOT supported here because static
# regex parsing of arbitrary shell is fragile.
TARGET_DIR=""
if [[ "$COMMAND" =~ git[[:space:]]+-C[[:space:]]+([^[:space:]]+) ]]; then
    TARGET_DIR="${BASH_REMATCH[1]}"
fi

if [ -n "$TARGET_DIR" ]; then
    if ! git -C "$TARGET_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
        exit 0  # target dir isn't a git repo — nothing to guard
    fi
    CURRENT_BRANCH="$(git -C "$TARGET_DIR" branch --show-current 2>/dev/null)"
else
    if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
        exit 0
    fi
    CURRENT_BRANCH="$(git branch --show-current 2>/dev/null)"
fi

# --- Rule 1: git commit / merge / rebase / reset-hard on main or master -------

case "$CURRENT_BRANCH" in
    main|master)
        case "$COMMAND" in
            *'git commit'*|*'git merge'*|*'git rebase'*|*'git reset --hard'*|*'git reset HEAD~'*)
                cat >&2 <<EOF
BLOCKED by .claude/hooks/block-main-commits.sh

Refusing to run this git command while on branch "$CURRENT_BRANCH":

  $COMMAND

The git-workflow rule (.claude/rules/git-workflow.md) requires every
change to go through a feature branch + PR. No direct commits to main.

Fix:
  git checkout -b feature/<short-description>   # move to a feature branch
  # then re-run your original command

If you intended to rebase, merge, or reset main to track origin, do it
from a throwaway branch or via the GitHub UI so the history is reviewed.
EOF
                block_footer "${BASH_SOURCE[0]}"
                exit 2
                ;;
        esac
        ;;
esac

# --- Rule 2: git push origin main / master -----------------------------------

case "$COMMAND" in
    *'git push'*'origin main'*|*'git push'*'origin master'*|\
    *'git push origin HEAD:main'*|*'git push origin HEAD:master'*|\
    *'git push --force'*'main'*|*'git push --force'*'master'*|\
    *'git push -f'*'main'*|*'git push -f'*'master'*)
        cat >&2 <<EOF
BLOCKED by .claude/hooks/block-main-commits.sh

Refusing to push directly to origin/main or origin/master:

  $COMMAND

Every change to main goes through a pull request. Push to a feature
branch and open a PR:

  git checkout -b feature/<short-description>
  git push -u origin feature/<short-description>
  gh pr create
EOF
        block_footer "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac

exit 0
