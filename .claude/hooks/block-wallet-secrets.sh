#!/bin/bash
# Claude Code adapter for the shared wallet-secret detector. Client hooks
# supplement the agent's server-side secret guard.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$ROOT/scripts/wallet_secret_guard.py" --client claude "$@"
