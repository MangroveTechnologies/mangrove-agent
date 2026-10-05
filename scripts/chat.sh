#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 0 ]; then
  echo 'chat.sh does not accept Claude option overrides. Run it without arguments.' >&2
  exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SETUP_PYTHON:-python3}" "$SCRIPT_DIR/setup_support.py" chat
