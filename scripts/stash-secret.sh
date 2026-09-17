#!/usr/bin/env bash
# Read wallet input directly in Python: never export it or pass it as argv.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_agent_home.sh
source "$SCRIPT_DIR/_agent_home.sh"
exec python3 "$SCRIPT_DIR/stash-secret.py"
