"""Explicit, evidence-checked ledger correction. Never signs or transfers funds."""
from __future__ import annotations

import json

from src.services import spend_service
from src.services.x402_inspection import inspect_authorization, inspect_transaction
from src.shared.db.sqlite import get_connection
from src.shared.errors import ValidationError


def reconcile_authorization(reservation_id: str, rpc, *, transaction: str | None = None) -> dict:
    """Read fresh finalized evidence, then atomically record a single transition.

    This service accepts an RPC client, never caller-supplied evidence. It is an
    service used by the operator CLI and background worker, never a purchase retry.
    RPC failure, live/legacy metadata and used-but-unmatched nonces stay reserved.
    """
    row = get_connection().execute("SELECT * FROM x402_payments WHERE id = ?", (reservation_id,)).fetchone()
    if row is None:
        raise ValidationError("Payment reservation not found.")
    original = dict(row)
    if original["state"] != "authorized":
        return {"reservation_id": reservation_id, "outcome": "already_resolved",
                "ledger_state": original["state"], "ledger_changed": False, "payment_sent": False}
    evidence = (inspect_transaction(original, transaction, rpc) if transaction
                else inspect_authorization(original, rpc))
    outcome = evidence["outcome"]
    if outcome not in {"expired_unused_at_finalized_block", "cancelled_at_finalized_block", "settled_at_finalized_block"}:
        return evidence
    state = "settled" if outcome == "settled_at_finalized_block" else "released"
    with spend_service._budget_transaction() as conn:
        current = conn.execute("SELECT * FROM x402_payments WHERE id = ?", (reservation_id,)).fetchone()
        if current is None or dict(current) != original:
            # A receipt, reset-independent reconciliation, or metadata change
            # won the race. Never apply evidence to a different snapshot.
            return {**evidence, "outcome": "ledger_changed_during_inspection", "ledger_changed": False}
        now = spend_service._now()
        conn.execute(
            "INSERT INTO x402_reconciliation_evidence (reservation_id, outcome, evidence_json, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            (reservation_id, outcome, json.dumps({"authorization": original, "evidence": evidence}, sort_keys=True), now),
        )
        conn.execute(
            "UPDATE x402_payments SET state = ?, transaction_hash = ?, release_reason = ?, updated_at = ? WHERE id = ?",
            (state, evidence.get("transaction") if state == "settled" else None,
             outcome if state == "released" else None, now, reservation_id),
        )
        # Proven non-payment restores capacity within the SAME authorized cap.
        # It never grants a new period or raises the user's limit.
        budget = spend_service._get_state(conn)
        if (state == "released" and original["period_id"] == budget["period_id"]
                and budget["exhausted"] and spend_service._spent_micro_usd(budget["period_id"], conn) < spend_service._cap_micro_usd(budget)):
            spend_service._update_state(conn=conn, exhausted=0, exhausted_at=None, exhausted_reason=None)
        if state == "released" and original.get("operation_id"):
            conn.execute("UPDATE x402_operations SET state = 'unsigned_failed', payment_headers = NULL, updated_at = ? WHERE id = ? AND state = 'pending' "
                         "AND NOT EXISTS (SELECT 1 FROM x402_payments WHERE operation_id = ? AND state != 'released')",
                         (now, original["operation_id"], original["operation_id"]))
    return {**evidence, "ledger_state": state, "ledger_changed": True,
            "next_step": "Evidence recorded. Review spend status before any new paid request."}
