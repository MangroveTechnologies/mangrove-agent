#!/usr/bin/env python3
"""Inspect one authorization using public finalized chain state; never mutate it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reservation", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if Path.cwd().resolve() != root or os.environ.get("ENVIRONMENT") != "local" or os.environ.get("MANGROVE_AGENT_HOME"):
        parser.error("Run from this repo with ENVIRONMENT=local and MANGROVE_AGENT_HOME unset.")
    sys.path.insert(0, str(root / "server"))
    import sqlite3

    import httpx

    from src.config import app_config
    from src.services.x402_inspection import inspect_authorization
    from src.shared.redaction import redact_diagnostics
    # Open in SQLite read-only mode; never create/migrate the live database.
    try:
        db = Path(str(app_config.DB_PATH)).resolve()
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM x402_payments WHERE id = ?", (args.reservation,)).fetchone()
        if row is None:
            raise ValueError("Reservation not found")
        urls = {"eip155:84532": "https://sepolia.base.org", "eip155:8453": "https://mainnet.base.org"}
        url = urls.get(row["network"])
        if url is None:
            raise ValueError("Unsupported network")
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            def rpc(method, params):
                response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                response.raise_for_status()
                payload = response.json()
                if "error" in payload or "result" not in payload:
                    raise ValueError("RPC evidence unavailable")
                return payload["result"]
            result = inspect_authorization(dict(row), rpc)
        print(json.dumps(redact_diagnostics(result)))
        return 0
    except Exception as error:
        print(json.dumps({"outcome": "inspection_unavailable", "error_type": type(error).__name__,
                          "ledger_changed": False, "payment_sent": False,
                          "next_step": "Keep the reservation; check the database, migration and public RPC."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
