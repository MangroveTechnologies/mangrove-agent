#!/usr/bin/env bash
# Explicit local wallet backup operation. Never called by normal setup.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_agent_home.sh"
exec "${SETUP_PYTHON:-python3}" "$SCRIPT_DIR/wallet_backup.py" confirm "$@"
