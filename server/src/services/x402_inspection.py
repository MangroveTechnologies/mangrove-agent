"""Read-only reconciliation evidence for one recorded EIP-3009 authorization.

No signing, sending, budget reset, release or settlement mutation occurs here.
A used nonce is not itself a payment receipt: cancellation also consumes a nonce.
"""
from __future__ import annotations

import re

from eth_utils import keccak

TOKENS = {"eip155:84532": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
          "eip155:8453": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"}


def _quantity(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("Malformed RPC quantity")
    return int(value, 16)


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
    if _quantity(rpc("eth_chainId", [])) != int(network.split(":")[1]):
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
    result.update(finalized_block=_quantity(block["number"]), finalized_block_hash=block["hash"],
                  authorization_used=bool(int(used, 16)), finalized_timestamp=_quantity(block["timestamp"]))
    if int(used, 16):
        return {**result, "outcome": "used_or_cancelled",
                "next_step": "Match AuthorizationUsed/AuthorizationCanceled and Transfer events before reconciling."}
    if result["finalized_timestamp"] > int(row["valid_before"]):
        return {**result, "outcome": "expired_unused_at_finalized_block",
                "next_step": "Evidence supports non-settlement; review before an explicit ledger correction."}
    return {**result, "outcome": "unused_not_expired",
            "next_step": "Keep reserved; the authorization can still settle."}


def inspect_transaction(row: dict, transaction: str, rpc) -> dict:
    """Confirm this exact authorization and transfer in one finalized receipt.

    A successful receipt or matching Transfer alone is insufficient. The token's
    AuthorizationUsed event must bind the recorded payer AND nonce to this tx.
    """
    result = inspect_authorization(row, rpc)
    if result["outcome"] != "used_or_cancelled":
        return result
    if not isinstance(transaction, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction):
        raise ValueError("Invalid transaction hash")
    receipt = rpc("eth_getTransactionReceipt", [transaction])
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        return {**result, "outcome": "transaction_unconfirmed"}
    if str(receipt.get("transactionHash", "")).lower() != transaction.lower():
        raise ValueError("Receipt transaction mismatch")
    number = _quantity(receipt["blockNumber"])
    if not isinstance(receipt.get("blockHash"), str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", receipt["blockHash"]):
        raise ValueError("Receipt block hash unavailable")
    if number > result["finalized_block"]:
        return {**result, "outcome": "transaction_not_finalized"}
    block = rpc("eth_getBlockByNumber", [hex(number), False])
    if (not isinstance(block, dict) or block.get("hash") != receipt["blockHash"]
            or _quantity(block.get("number")) != number):
        raise ValueError("Receipt is not in the canonical finalized chain")
    payer = "0x" + row["wallet_address"][2:].lower().rjust(64, "0")
    nonce = row["authorization_nonce"].lower()
    used_topic = "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()
    cancel_topic = "0x" + keccak(text="AuthorizationCanceled(address,bytes32)").hex()
    transfer_topic = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ValueError("Receipt logs unavailable")
    token_logs = [log for log in logs if isinstance(log, dict)
                  and str(log.get("address", "")).lower() == row["asset"].lower()
                  and log.get("removed", False) is False]
    def matches(log, topics):
        return [str(t).lower() for t in log.get("topics", [])] == topics
    used = [log for log in token_logs if matches(log, [used_topic, payer, nonce])]
    cancelled = [log for log in token_logs if matches(log, [cancel_topic, payer, nonce])]
    evidence = {**result, "transaction": transaction.lower(), "receipt_block": number,
                "receipt_block_hash": receipt["blockHash"]}
    if len(cancelled) == 1 and not used:
        return {**evidence, "outcome": "cancelled_at_finalized_block"}
    payee = row.get("payee")
    if not isinstance(payee, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", payee):
        return {**result, "outcome": "legacy_metadata_missing"}
    recipient = "0x" + payee[2:].lower().rjust(64, "0")
    transfers = [log for log in token_logs if matches(log, [transfer_topic, payer, recipient])
                 and isinstance(log.get("data"), str)
                 and re.fullmatch(r"0x[0-9a-fA-F]{64}", log["data"])
                 and int(log["data"], 16) == row["amount_micro_usd"]]
    if len(used) == 1 and not cancelled and len(transfers) == 1:
        return {**evidence, "outcome": "settled_at_finalized_block"}
    return {**result, "outcome": "transaction_evidence_mismatch"}
