#!/bin/bash
# Claude adapter. Client hooks supplement the independent custody boundary.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$ROOT/scripts/wallet_secret_guard.py" --client claude "$@"
