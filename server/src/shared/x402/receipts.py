"""Validate server-reported EVM settlement receipts without trusting arbitrary JSON."""
from __future__ import annotations

import re


def valid_settlement(receipt: object, *, payer: str | None = None,
                     network: str | None = None) -> bool:
    if not isinstance(receipt, dict) or receipt.get("success") is not True:
        return False
    if not isinstance(receipt.get("transaction"), str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", receipt["transaction"]):
        return False
    if not isinstance(receipt.get("payer"), str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", receipt["payer"]):
        return False
    if not isinstance(receipt.get("network"), str) or not re.fullmatch(r"eip155:[1-9][0-9]*", receipt["network"]):
        return False
    return ((payer is None or receipt["payer"].lower() == payer.lower())
            and (network is None or receipt["network"] == network))
