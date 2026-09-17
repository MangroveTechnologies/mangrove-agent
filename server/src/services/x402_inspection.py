"""Read-only reconciliation evidence for one recorded EIP-3009 authorization.

No signing, sending, budget reset, release or settlement mutation occurs here.
A used nonce is not itself a payment receipt: cancellation also consumes a nonce.
"""
from __future__ import annotations

import re

from eth_utils import keccak

TOKENS = {"eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
          "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"}


def inspect_authorization(row: dict, rpc) -> dict:
    """rpc is a read-only JSON-RPC callable; use finalized state for expiry proof."""
    result = {"reservation_id": row["id"], "ledger_state": row["state"],
              "ledger_changed": False, "payment_sent": False}
    nonce, asset = row.get("authorization_nonce"), row.get("asset")
    payer = row.get("wallet_address")
    if not nonce or not asset or row.get("valid_before") is None:
        return {**result, "outcome": "legacy_metadata_missing",
                "next_step": "Keep reserved; this row cannot identify its exact authorization."}
    network = row.get("network")
    if (network not in TOKENS or asset.lower() != TOKENS[network]
            or not isinstance(nonce, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", nonce)
            or not isinstance(payer, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", payer)):
        raise ValueError("Unsupported or malformed authorization metadata")
    if int(rpc("eth_chainId", []), 16) != int(network.split(":")[1]):
        raise ValueError("RPC network does not match the recorded authorization")
    block = rpc("eth_getBlockByNumber", ["finalized", False])
    if not isinstance(block, dict) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(block.get("hash", ""))):
        raise ValueError("Finalized block unavailable")
    # Pin eth_call to the same block by hash, not a moving latest head.
    selector = keccak(text="authorizationState(address,bytes32)")[:4].hex()
    data = "0x" + selector + payer[2:].lower().rjust(64, "0") + nonce[2:].lower()
    used = rpc("eth_call", [{"to": asset, "data": data},
                           {"blockHash": block["hash"], "requireCanonical": True}])
    if not isinstance(used, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", used) or int(used, 16) not in (0, 1):
        raise ValueError("RPC returned invalid authorization state")
    result.update(finalized_block=int(block["number"], 16), finalized_block_hash=block["hash"],
                  authorization_used=bool(int(used, 16)))
    if int(used, 16):
        return {**result, "outcome": "used_or_cancelled",
                "next_step": "Match AuthorizationUsed/AuthorizationCanceled and Transfer events before reconciling."}
    if int(block["timestamp"], 16) > int(row["valid_before"]):
        return {**result, "outcome": "expired_unused_at_finalized_block",
                "next_step": "Evidence supports non-settlement; review before an explicit ledger correction."}
    return {**result, "outcome": "unused_not_expired",
            "next_step": "Keep reserved; the authorization can still settle."}
