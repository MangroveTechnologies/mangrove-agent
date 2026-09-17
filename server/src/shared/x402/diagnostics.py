"""Allowlisted payment-failure metadata, never raw envelopes or exception text."""
from __future__ import annotations

import base64
import json
import re

_ERROR_CODES = frozenset({
    "insufficient_funds", "insufficient_balance", "insufficient_allowance",
    "invalid_signature", "invalid_payload", "invalid_payment_requirements",
    "invalid_network", "unsupported_scheme", "unsupported_network",
    "settlement_failed", "settlement_pending", "transaction_failed",
    "simulation_failed", "unexpected_settle_error", "facilitator_error",
    "invalid_exact_evm_payload_signature", "invalid_exact_evm_payload_recipient_mismatch",
    "invalid_exact_evm_payload_authorization_value", "invalid_exact_evm_payload_authorization_valid_before",
    "invalid_exact_evm_payload_authorization_valid_after", "invalid_exact_evm_payload_authorization_used",
    "invalid_exact_evm_payload_authorization_nonce", "invalid_exact_evm_payload_authorization",
    "extension_echo_mismatch",
})


def safe_error_code(value):
    return value if isinstance(value, str) and value in _ERROR_CODES else "unclassified"


def payment_response_metadata(response):
    raw = response.headers.get("payment-response") or response.headers.get("x-payment-response")
    result = {"settlement_header_present": bool(raw),
              "requirements_header_present": bool(response.headers.get("payment-required"))}
    if not raw or len(raw) > 16384:
        return result
    try:
        payload = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True))
        if not isinstance(payload, dict):
            return result
    except (ValueError, RecursionError):
        result["settlement_header_decodable"] = False
        return result
    result["settlement_header_decodable"] = True
    result["payer_present"] = bool(payload.get("payer"))
    if type(payload.get("success")) is bool:
        result["settlement_success"] = payload["success"]
    if payload.get("success") is not True:
        result["error_code"] = safe_error_code(payload.get("errorReason"))
    transaction = payload.get("transaction")
    if isinstance(transaction, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction):
        result["transaction"] = transaction
    return result
