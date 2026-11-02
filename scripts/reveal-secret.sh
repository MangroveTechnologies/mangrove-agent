#!/usr/bin/env bash
# Explicit local wallet backup operation. Never called by normal setup.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Wizard backup always targets this checkout, even in a terminal with plugin overrides.
if [ "${1:-}" = --setup ]; then
  BACKUP_PY="$SCRIPT_DIR/../.venv/bin/python3"
  [ -x "$BACKUP_PY" ] || BACKUP_PY=python3
  exec "$BACKUP_PY" "$SCRIPT_DIR/wallet_backup.py" reveal "$@"
fi
source "$SCRIPT_DIR/_agent_home.sh"
exec "${SETUP_PYTHON:-python3}" "$SCRIPT_DIR/wallet_backup.py" reveal "$@"
