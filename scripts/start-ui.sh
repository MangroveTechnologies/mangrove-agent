#!/usr/bin/env bash
# Start the Sage chat UI (Chainlit).
# Requires: ANTHROPIC_API_KEY set, agent server already running on port 8080.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="$SCRIPT_DIR/../ui"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Error: ANTHROPIC_API_KEY is not set."
  echo ""
  echo "Export it first:"
  echo "  export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

cd "$UI_DIR"

if ! python -c "import chainlit" 2>/dev/null; then
  echo "Installing UI dependencies..."
  pip install -r requirements.txt -q
fi

echo "Sage UI → http://localhost:8001"
exec chainlit run app.py --port 8001 --host 0.0.0.0
