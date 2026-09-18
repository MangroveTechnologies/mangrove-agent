#!/usr/bin/env bash
# Read-only installation verification. Never starts services or makes payments.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for arg in "$@"; do
  case "$arg" in
    --bare) ;; # Compatibility: both modes now verify the running agent only.
    -h|--help) echo 'Usage: scripts/verify_quickstart.sh [--bare]'; exit 0 ;;
    *) echo 'Unknown verification option' >&2; exit 2 ;;
  esac
done
PY="${SETUP_PYTHON:-python3}"
"$PY" "$SCRIPT_DIR/setup_support.py" verify
