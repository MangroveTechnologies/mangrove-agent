#!/usr/bin/env bash
# update.sh — fast-forward this checkout to the latest published code.
#
# Called automatically by setup.sh (and therefore `mangrove-agent start`), and
# directly by `mangrove-agent update`. It never rewrites history and never
# discards local work:
#
#   * only acts on a git checkout whose top level IS this repository (a copy
#     nested inside some other repository is left alone);
#   * only acts on the default branch (main) tracking its remote, so feature
#     branches and detached checkouts are never moved;
#   * fetches with prompts disabled and a hard timeout, so an offline machine,
#     a passphrase-protected SSH key or a slow network cannot hang setup;
#   * applies only a fast-forward (`git merge --ff-only`). Diverged history,
#     an in-progress merge/rebase, or local edits to files the update also
#     changes are reported and skipped. Local edits to other files (for example
#     the personalised Project Context in SAGE.md) are kept; git refuses the
#     fast-forward atomically if it would overwrite anything.
#
# Opt out with MANGROVE_AGENT_NO_UPDATE=1 (or setup.sh --no-update).
#
# Exit status:
#   0  up to date, or updated
#   1  could not check or apply (network, git error)
#   2  bad usage
#   3  skipped on purpose (opted out, not on main, local changes, diverged ...)
# setup.sh treats every non-zero status as a warning and continues on the
# code that is already checked out.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
FETCH_TIMEOUT="${MANGROVE_AGENT_UPDATE_TIMEOUT:-20}"
QUIET="no"

YELLOW="\033[33m"; GREEN="\033[32m"; DIM="\033[2m"; CLR="\033[0m"
if [ ! -t 1 ] || [ -n "${NO_COLOR:-}" ]; then YELLOW=""; GREEN=""; DIM=""; CLR=""; fi
say()  { printf "${YELLOW}==>${CLR} %s\n" "$1"; }
ok()   { printf "${GREEN}  ✓${CLR} %s\n" "$1"; }
info() { [ "$QUIET" = yes ] || printf "${DIM}    %s${CLR}\n" "$1"; }
warn() { printf "${YELLOW}  !${CLR} %s\n" "$1" >&2; }

usage() {
  cat <<EOF
Usage: $0 [--quiet]

Fast-forward this mangrove-agent checkout to the latest code on its remote
default branch. Safe to run at any time; it never discards local work.

  --quiet   Print only changes, warnings and errors.

Environment:
  MANGROVE_AGENT_NO_UPDATE=1        Skip the update entirely.
  MANGROVE_AGENT_UPDATE_TIMEOUT=N   Seconds to wait for the remote (default 20).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) QUIET="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
done

case "$FETCH_TIMEOUT" in ''|*[!0-9]*|0) printf 'MANGROVE_AGENT_UPDATE_TIMEOUT must be a positive integer\n' >&2; exit 2 ;; esac

skip() { warn "$1"; [ -z "${2:-}" ] || info "$2"; exit 3; }

case "${MANGROVE_AGENT_NO_UPDATE:-}" in
  ""|0|false|no) ;;
  *) info "update check disabled (MANGROVE_AGENT_NO_UPDATE)"; exit 3 ;;
esac

[ "$QUIET" = yes ] || say "Check for updates"

command -v git >/dev/null 2>&1 || skip "git is not installed; skipping the update check."

# Only manage the repository this script ships in. `git -C` alone would walk up
# and find an enclosing repository for a copied or vendored checkout.
TOP="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$TOP" ] || [ "$(cd "$TOP" && pwd -P)" != "$REPO_ROOT" ]; then
  info "not a git clone of mangrove-agent; skipping the update check"
  exit 3
fi
g() { git -C "$REPO_ROOT" "$@"; }

BRANCH="$(g symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[ -n "$BRANCH" ] || skip "Detached HEAD; automatic updates only follow the main branch."

UPSTREAM="$(g rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
REMOTE="origin"
[ -z "$UPSTREAM" ] || REMOTE="${UPSTREAM%%/*}"
REMOTE_BRANCH="${UPSTREAM#*/}"

# The remote's default branch, falling back to main when origin/HEAD is unset.
DEFAULT="$(g symbolic-ref --quiet --short "refs/remotes/$REMOTE/HEAD" 2>/dev/null || true)"
DEFAULT="${DEFAULT#"$REMOTE"/}"
DEFAULT="${DEFAULT:-main}"
if [ "$BRANCH" != "$DEFAULT" ]; then
  skip "On branch '$BRANCH'; automatic updates only follow '$DEFAULT'." \
       "Switch with 'git switch $DEFAULT' to receive updates, or pull this branch yourself."
fi
if [ -z "$UPSTREAM" ] || [ "$REMOTE_BRANCH" != "$DEFAULT" ]; then
  skip "'$BRANCH' does not track $REMOTE/$DEFAULT; skipping the update check." \
       "Fix with: git branch --set-upstream-to=$REMOTE/$DEFAULT"
fi

for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  if [ -e "$(g rev-parse --git-path "$marker")" ]; then
    skip "A git merge/rebase is in progress; finish or abort it, then run the update again."
  fi
done

# run_with_timeout SECS CMD... — portable (macOS has no coreutils `timeout`).
# The watchdog's output is detached so callers capturing our output never wait
# on a stray sleep.
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$! rc=0
  ( trap 'kill "$sp" 2>/dev/null; exit 0' TERM
    sleep "$secs" & sp=$!
    wait "$sp"
    kill -TERM "$pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 &
  local watchdog=$!
  wait "$pid" || rc=$?
  kill -TERM "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  return "$rc"
}

# Never prompt: no credential helpers popping dialogs, no SSH passphrase or
# host-key questions. Respect an explicitly configured SSH command.
export GIT_TERMINAL_PROMPT=0
if [ -z "${GIT_SSH_COMMAND:-}" ] && [ -z "$(g config --get core.sshCommand 2>/dev/null || true)" ]; then
  export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10"
fi

FETCH_ERR="$(mktemp "${TMPDIR:-/tmp}/mangrove-update.XXXXXX")"
trap 'rm -f "$FETCH_ERR"' EXIT
if ! run_with_timeout "$FETCH_TIMEOUT" git -C "$REPO_ROOT" \
     -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=15 \
     fetch --quiet --no-tags "$REMOTE" "+refs/heads/$REMOTE_BRANCH:refs/remotes/$REMOTE/$REMOTE_BRANCH" \
     </dev/null 2>"$FETCH_ERR"; then
  warn "Could not reach '$REMOTE' to check for updates; continuing with the installed version."
  REASON="$(tail -n 1 "$FETCH_ERR" 2>/dev/null || true)"
  [ -z "$REASON" ] || info "$REASON"
  exit 1
fi

COUNTS="$(g rev-list --left-right --count "HEAD...$UPSTREAM")"
AHEAD="${COUNTS%%[[:space:]]*}"
BEHIND="${COUNTS##*[[:space:]]}"

if [ "$BEHIND" -eq 0 ]; then
  [ "$QUIET" = yes ] || ok "already up to date ($(g rev-parse --short HEAD))"
  exit 0
fi
if [ "$AHEAD" -gt 0 ]; then
  skip "Local '$BRANCH' has $AHEAD commit(s) not on $UPSTREAM and is $BEHIND behind; not updating automatically." \
       "Reconcile with 'git pull --rebase' (or move your commits to a branch), then rerun."
fi

# Report local edits that the update would collide with, by name, instead of
# relying on git's generic refusal.
CHANGED_UPSTREAM="$(g diff --name-only HEAD "$UPSTREAM")"
LOCAL_EDITS="$( { g diff --name-only; g diff --name-only --cached; } | sort -u)"
CONFLICTS=""
if [ -n "$LOCAL_EDITS" ]; then
  CONFLICTS="$(printf '%s\n' "$LOCAL_EDITS" | grep -Fxf <(printf '%s\n' "$CHANGED_UPSTREAM") || true)"
fi
if [ -n "$CONFLICTS" ]; then
  warn "An update is available ($BEHIND commit(s)), but you have local edits to files it changes:"
  printf '%s\n' "$CONFLICTS" | sed 's/^/      /' >&2
  info "Commit or stash those edits (git stash), then run 'mangrove-agent update'."
  exit 3
fi

OLD="$(g rev-parse --short HEAD)"
MERGE_ERR="$(g merge --ff-only --quiet "$UPSTREAM" 2>&1 >/dev/null)" || {
  warn "The update could not be applied cleanly; your checkout was not changed."
  printf '%s\n' "$MERGE_ERR" | sed 's/^/      /' >&2
  exit 1
}
NEW="$(g rev-parse --short HEAD)"
ok "updated $OLD -> $NEW ($BEHIND new commit(s))"
if [ "$QUIET" != yes ]; then
  g log --no-decorate --format='      %h %s' -n 5 "$OLD..$NEW"
  [ "$BEHIND" -le 5 ] || info "... and $((BEHIND - 5)) more"
fi
exit 0
