#!/usr/bin/env bash
# Register with a dynamic header helper: no credential in CLI arguments/config.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
command -v claude >/dev/null 2>&1 || { echo 'Claude Code CLI is required.' >&2; exit 1; }
"${SETUP_PYTHON:-python3}" "$SCRIPT_DIR/setup_support.py" register
echo 'Open Claude in this directory to load tools. Payment wallet readiness is separate.'
