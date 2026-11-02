#!/usr/bin/env bash
# Register with a dynamic header helper: no credential in CLI arguments/config.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
command -v claude >/dev/null 2>&1 || { echo 'Claude Code CLI is required.' >&2; exit 1; }
"${SETUP_PYTHON:-python3}" "$SCRIPT_DIR/setup_support.py" register
if [ "${SETUP_PARENT:-0}" != 1 ]; then
  echo
  echo 'Run Claude from your agent folder:'
  printf '  cd %q && claude\n' "$PWD"
fi
