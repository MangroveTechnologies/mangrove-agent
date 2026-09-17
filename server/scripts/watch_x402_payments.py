#!/usr/bin/env python3
"""Read-only live view of the x402 ledger and checker evidence. Never pays."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.shared.private_files import append_private  # noqa: E402
from src.shared.redaction import redact_diagnostics  # noqa: E402

# Only known summary fields may leave the evidence file; never forward arbitrary
# body/header/error fields from a JSONL record into a shareable terminal log.
EVIDENCE_FIELDS = {
    "event", "time", "run_id", "case", "pay", "maximum_usdc", "method", "url",
    "signed", "status", "correlation_id", "network", "asset", "receiver",
    "amount_usdc", "domain_valid", "settlement_header_present", "payer_present",
    "settlement_success", "transaction", "signal_count", "total", "block",
    "explorer", "onchain_verified", "balances_after_available", "signed_requests",
    "payment_sent", "error_type",
}
EVIDENCE_EVENTS = {"start", "request", "response", "quote", "payment_response",
                   "resource_received", "onchain_verified", "balances_after_unavailable",
                   "PASS", "FAIL", "quote_only_pass"}


def snapshot(path):
    # mode=ro does not create, migrate, release, or otherwise update the ledger.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        state = conn.execute("SELECT period_id, exhausted FROM x402_spend_state WHERE id=1").fetchone()
        rows = conn.execute("SELECT id, period_id, state, amount_micro_usd, wallet_address, payee, "
                            "network, resource, transaction_hash, created_at, updated_at "
                            "FROM x402_payments ORDER BY created_at DESC, id DESC LIMIT 500").fetchall()
        totals = conn.execute("SELECT state, COUNT(*) AS count, SUM(amount_micro_usd) AS amount "
                              "FROM x402_payments WHERE period_id=? GROUP BY state", (state["period_id"],)).fetchall()
        return dict(state), [dict(row) for row in rows], [dict(row) for row in totals]
    finally:
        conn.close()


def payment_event(row):
    return {"event": "ledger_payment", "reservation_id": row["id"],
            "state": row["state"], "period_id": row["period_id"],
            "amount_usdc": str(Decimal(row["amount_micro_usd"]) / 1000000),
            "payer": row["wallet_address"], "receiver": row["payee"],
            "network": row["network"], "resource": row["resource"],
            "transaction": row["transaction_hash"],
            "evidence": "server_receipt" if row["state"] == "settled" else "ledger_state"}


def evidence_event(line):
    try:
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("event"), str) or value["event"] not in EVIDENCE_EVENTS:
            return None
        # Disallow nested remote data even under an otherwise allowed name.
        return {k: v for k, v in value.items() if k in EVIDENCE_FIELDS
                and (v is None or isinstance(v, (str, int, float, bool)))}
    except (ValueError, RecursionError):
        return None


class EvidenceTail:
    def __init__(self, path):
        self.path = Path(path)
        self.identity = None
        self.offset = 0
        if self.path.exists():
            stat = self.path.stat()
            self.identity = (stat.st_dev, stat.st_ino)
            self.offset = stat.st_size  # Only events produced after watching starts.

    def read(self):
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.identity or stat.st_size < self.offset:
                self.offset = 0
                self.identity = identity
            events = []
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                for _ in range(1000):
                    line = stream.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        # Oversized diagnostics are not safe summary records.
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(65537)
                        self.offset = stream.tell()
                        continue
                    if not line.endswith(b"\n"):
                        break  # Writer has not finished the record yet.
                    self.offset = stream.tell()
                    event = evidence_event(line)
                    if event is not None:
                        events.append(event)
            return events
        except (FileNotFoundError, OSError):
            return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("agent-data/agent.db"))
    parser.add_argument("--evidence", type=Path, default=Path("agent-data/x402-e2e.jsonl"))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log", type=Path, help="Optional owner-only summary log")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or not .25 <= args.interval <= 60:
        parser.error("--interval must be between 0.25 and 60 seconds")
    if args.log and args.log.resolve() in {args.db.resolve(), args.evidence.resolve()}:
        parser.error("Summary log must differ from the database and evidence input")
    def emit(value):
        line = json.dumps(redact_diagnostics({"observed_at": datetime.now(timezone.utc).isoformat(), **value}))
        print(line, flush=True)
        if args.log:
            append_private(args.log, line)
    tail = EvidenceTail(args.evidence)
    seen, previous, last_error = {}, None, None
    initialized = False
    emit({"event": "watch_started", "read_only": True, "payment_sent": False})
    try:
        while True:
            try:
                state, rows, totals = snapshot(args.db)
                summary = {"event": "budget_snapshot", **state, "totals": [
                    {"state": t["state"], "count": t["count"],
                     "amount_usdc": str(Decimal(t["amount"]) / 1000000)} for t in totals]}
                if summary != previous:
                    emit(summary)
                    previous = summary
                if not initialized:
                    emit({"event": "ledger_baseline", "recent_rows": len(rows)})
                    initialized = True
                else:
                    for row in reversed(rows):
                        key = (row["state"], row["transaction_hash"], row["updated_at"])
                        if seen.get(row["id"]) != key:
                            emit(payment_event(row))
                seen = {row["id"]: (row["state"], row["transaction_hash"], row["updated_at"]) for row in rows}
                last_error = None
            except (sqlite3.Error, TypeError, KeyError) as error:
                if type(error).__name__ != last_error:
                    emit({"event": "ledger_unavailable", "error_type": type(error).__name__})
                    last_error = type(error).__name__
                if args.once:
                    return 1
            for event in tail.read():
                emit({"source": "checker_evidence", **event})
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
