#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export ENVIRONMENT=local PYTHONPATH="$PWD/server${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python scripts/robinhood_connect.py "$@"
