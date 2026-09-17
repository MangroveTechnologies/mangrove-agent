#!/usr/bin/env python3
"""Isolated B4 smoke-test MCP server, not the production Mangrove tool wiring.

One fixed local signals endpoint, Base Sepolia only, max 0.001 test USDC.
A persistent attempt marker prevents a second payment after server restarts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]


def claim_attempt(path):
    # Caller must create the private parent directory before starting Claude.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receiver", required=True)
    parser.add_argument("--attempt-file", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.receiver):
        parser.error("Expected a public EVM receiver address")
    if not args.attempt_file.is_absolute() or args.attempt_file.parent.resolve() != (ROOT / "agent-data").resolve():
        parser.error("Attempt file must be inside this checkout's agent-data directory")
    server = FastMCP("Mangrove B4 test only")
    lock = asyncio.Lock()

    async def run_check(pay):
        async with lock:
            if pay:
                # Fail before claiming/signing if the agent was not restarted.
                # Read only the public database path from the local config.
                config = json.loads((ROOT / "server/src/config/local-config.json").read_text())
                db = Path(config.get("DB_PATH", "agent-data/agent.db"))
                if not db.is_absolute():
                    db = ROOT / db
                try:
                    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        migrated = conn.execute("SELECT 1 FROM _migrations WHERE filename=?",
                            ("010_x402_authorization_metadata.sql",)).fetchone()
                    finally:
                        conn.close()
                except sqlite3.Error:
                    migrated = None
                if not migrated:
                    return {"result": "restart_agent_required", "payment_attempted": False,
                            "reason": "Restart the agent to apply migration 010 before this test."}
                try:
                    claim_attempt(args.attempt_file)
                except FileExistsError:
                    return {"result": "blocked", "reason": "This test session already attempted payment. Do not retry.",
                            "payment_attempted": False}
            env = os.environ.copy()
            env.pop("MANGROVE_AGENT_HOME", None)
            env.pop("MANGROVE_API_KEY", None)
            env["ENVIRONMENT"] = "local"
            command = [sys.executable, str(ROOT / "server/scripts/check_x402_e2e.py"),
                       "--case", "signals", "--receiver", args.receiver]
            if pay:
                command.append("--pay")
            process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=180)
            except (TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                return {"result": "unconfirmed", "reason": "Checker interrupted; inspect the ledger before any further payment.",
                        "payment_attempted": pay}
            events = []
            for line in stdout.splitlines():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict) and value.get("event") in {
                    "start", "request", "response", "quote", "payment_response", "resource_received",
                    "onchain_verified", "PASS", "FAIL", "quote_only_pass"}:
                    events.append(value)
            return {"test_scope": "B4 explicit SDK payment client; normal tools still require B5",
                    "result": "passed" if process.returncode == 0 else "failed_or_unconfirmed",
                    "payment_attempted": pay, "events": events}

    @server.tool()
    async def quote_signals() -> dict:
        """Check the local MangroveAI signals price without paying or signing."""
        return await run_check(False)

    @server.tool()
    async def pay_signals_once(confirm_testnet_payment: bool) -> dict:
        """Authorize ONE local signals read, at most 0.001 Base Sepolia test USDC.

        Requires explicit user authorization. A failed/uncertain attempt cannot be
        repeated in this session. Returns HTTP, receipt and on-chain test evidence.
        """
        if confirm_testnet_payment is not True:
            return {"result": "confirmation_required", "payment_attempted": False}
        return await run_check(True)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
