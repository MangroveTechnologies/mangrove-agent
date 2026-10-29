"""Verify ERC-20 wallet flows from an actual EVM transaction receipt.

No network calls, quotes, wallet-balance snapshots or USD valuations are used.
Addresses and decimals come from the retained signed-execution context. Chain
identity is checked against the queried provider route and raw chainId when
present; ordinary Ethereum receipts do not independently attest their chain.

Sources: https://eips.ethereum.org/EIPS/eip-1474,
https://eips.ethereum.org/EIPS/eip-20,
https://specs.optimism.io/protocol/isthmus/exec-engine.html and
https://developer.arbitrum.io/how-arbitrum-works/deep-dives/gas-and-fees.
"""

from __future__ import annotations

import copy
import re
from typing import Any

_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_NATIVE = {"0x" + "0" * 40, "0x" + "e" * 40}
# These OP Stack routes have a separate L1 charge. Missing evidence is not zero.
_OP_CHAINS = {10, 8453, 84532, 11155420}
_ARBITRUM_CHAINS = {42161, 421614}
# Explicitly supported fee models; unknown chains retain physical token facts
# but require a fee adapter before the financial ledger can be complete.
_EXECUTION_FEE_CHAINS = {1, 56, 97, 137, 17000, 43113, 43114, 80002, 11155111}


class ReceiptSettlementError(ValueError):
    """An unverified outcome, with machine-readable reason and retained evidence."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _error(code: str, message: str):
    raise ReceiptSettlementError(code, message)


def _data(value: Any, length: int | None, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        _error("malformed_receipt", f"{field} must be hex data")
    body = value[2:]
    if len(body) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", body):
        _error("malformed_receipt", f"{field} is malformed hex data")
    if length is not None and len(body) != length * 2:
        _error("malformed_receipt", f"{field} must contain {length} bytes")
    return "0x" + body.lower()


def _quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        _error("malformed_receipt", f"{field} must be a canonical nonnegative hex quantity")
    number = int(value, 16)
    if number >= 2**256:
        _error("malformed_receipt", f"{field} exceeds uint256")
    return number


def _units(amount: int, decimals: int) -> str:
    """Exact decimal representation without floating point or Decimal rounding."""
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 10**decimals)
    if not fraction:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:0{decimals}d}".rstrip("0")


def _indexed_address(topic: Any, field: str) -> str:
    encoded = _data(topic, 32, field)
    if encoded[2:26] != "0" * 24:
        _error("malformed_transfer", f"{field} has nonzero address padding")
    return "0x" + encoded[-40:]


def _fees(raw: dict[str, Any], chain_id: int) -> dict[str, Any]:
    evidence = {k: copy.deepcopy(v) for k, v in raw.items() if "fee" in k.lower() or "gas" in k.lower()}
    issues: list[str] = []
    components: dict[str, int] = {}
    try:
        components["execution"] = _quantity(raw.get("gasUsed"), "gasUsed") * _quantity(
            raw.get("effectiveGasPrice"), "effectiveGasPrice"
        )
    except ReceiptSettlementError:
        issues.append("missing_or_malformed_execution_fee")
    if chain_id not in _OP_CHAINS | _ARBITRUM_CHAINS | _EXECUTION_FEE_CHAINS:
        issues.append("unsupported_chain_fee_model")
    for key in ("l1Fee", "operatorFee"):
        if key in raw:
            try:
                charged = _quantity(raw[key], key)
                if chain_id in _OP_CHAINS:
                    components[key] = charged
                else:
                    # A field named l1Fee is not evidence of a *separate*
                    # charge on another fee model. Arbitrum already includes
                    # L1 posting gas in gasUsed. Hold rather than double count.
                    issues.append(f"unsupported_separate_{key}")
            except ReceiptSettlementError:
                issues.append(f"malformed_{key}")
    if chain_id in _OP_CHAINS and "l1Fee" not in raw:
        issues.append("missing_l1_fee")
    if "blobGasUsed" in raw or "blobGasPrice" in raw:
        try:
            components["blob"] = _quantity(raw.get("blobGasUsed"), "blobGasUsed") * _quantity(
                raw.get("blobGasPrice"), "blobGasPrice"
            )
        except ReceiptSettlementError:
            issues.append("missing_or_malformed_blob_fee")
    # Operator scalar formulas are fork-dependent. Never guess a charged total.
    if (
        chain_id in _OP_CHAINS
        and "operatorFee" not in raw
        and not all(key in raw for key in ("operatorFeeScalar", "operatorFeeConstant"))
    ):
        issues.append("missing_operator_fee_evidence")
    if "operatorFee" not in raw:
        for key in ("operatorFeeScalar", "operatorFeeConstant"):
            if key in raw:
                try:
                    if _quantity(raw[key], key):
                        issues.append("operator_fee_requires_explicit_total")
                except ReceiptSettlementError:
                    issues.append("malformed_operator_fee_parameters")
    informational = {
        "gasUsed",
        "effectiveGasPrice",
        "cumulativeGasUsed",
        "l1Fee",
        "operatorFee",
        "blobGasUsed",
        "blobGasPrice",
        "l1GasPrice",
        "l1BlobBaseFee",
        "l1GasUsed",
        "l1FeeScalar",
        "l1BaseFeeScalar",
        "l1BlobBaseFeeScalar",
        "feeScalar",
        "operatorFeeScalar",
        "operatorFeeConstant",
        "daFootprintGasScalar",
    }
    if chain_id in _ARBITRUM_CHAINS:
        # This is already part of gasUsed, not a second native charge.
        informational.add("gasUsedForL1")
    unknown = sorted(set(evidence) - informational)
    if unknown:
        issues.append("unrecognized_fee_evidence:" + ",".join(unknown))
    if any(k in raw for k in ("l1GasUsed", "l1GasPrice", "l1BlobBaseFee")) and "l1Fee" not in raw:
        issues.append("l1_fee_requires_explicit_total")
    known = sum(components.values())
    complete = not issues
    return {
        "native_fee_asset": f"native:{chain_id}",
        "native_fee_amount": _units(known, 18) if complete else None,
        "native_fee_amount_wei": str(known) if complete else None,
        "native_fee_known_amount": _units(known, 18),
        "native_fee_known_amount_wei": str(known),
        "native_fee_components_wei": {k: str(v) for k, v in components.items()},
        "native_fee_complete": complete,
        "native_fee_issues": sorted(set(issues)),
        "raw_fee_evidence": evidence,
    }


def verify_fee_receipt(
    raw: dict[str, Any] | None,
    *,
    expected_tx_hash: str,
    expected_chain_id: int,
    reported_chain_id: int,
    wallet_address: str,
    recipient_address: str,
) -> dict[str, Any]:
    """Verify actual native fees for a signed swap or approval, including reverts.

    This never verifies an asset fill. Every required approval and swap receipt
    must be accounted for separately; a missing receipt is unresolved, not zero.
    """
    if raw is None:
        _error("receipt_pending", "No mined receipt is available; execution remains unresolved")
    if not isinstance(raw, dict):
        _error("malformed_receipt", "Receipt must be an object")
    if (
        isinstance(expected_chain_id, bool)
        or not isinstance(expected_chain_id, int)
        or expected_chain_id <= 0
        or isinstance(reported_chain_id, bool)
        or not isinstance(reported_chain_id, int)
        or reported_chain_id <= 0
    ):
        _error("invalid_context", "Chain IDs must be positive integers")
    if reported_chain_id != expected_chain_id:
        _error("chain_mismatch", "Receipt provider route differs from the signed chain")
    expected = _data(expected_tx_hash, 32, "expected transaction hash")
    if _data(raw.get("transactionHash"), 32, "transactionHash") != expected:
        _error("transaction_mismatch", "Receipt transaction hash differs from the signed transaction")
    wallet = _data(wallet_address, 20, "wallet address")
    recipient = _data(recipient_address, 20, "expected recipient")
    if _data(raw.get("from"), 20, "receipt from") != wallet:
        _error("wallet_mismatch", "Receipt sender differs from the signing wallet")
    if _data(raw.get("to"), 20, "receipt to") != recipient:
        _error("recipient_mismatch", "Receipt recipient differs from the signed transaction")
    if "chainId" in raw and _quantity(raw["chainId"], "chainId") != expected_chain_id:
        _error("chain_mismatch", "Raw receipt chain differs from the signed chain")
    if raw.get("blockNumber") is None or raw.get("blockHash") is None:
        _error("receipt_pending", "Receipt has no mined block; execution remains unresolved")
    block = _quantity(raw["blockNumber"], "blockNumber")
    block_hash = _data(raw["blockHash"], 32, "blockHash")
    status = _quantity(raw.get("status"), "status")
    if status not in (0, 1):
        _error("malformed_receipt", "Receipt status must be success or revert")
    transaction_index = (
        _quantity(raw["transactionIndex"], "transactionIndex") if "transactionIndex" in raw else None
    )
    return {
        "tx_hash": expected,
        "chain_id": expected_chain_id,
        "chain_verification": "raw_chain_id_and_provider_route" if "chainId" in raw else "provider_route",
        "block_number": block,
        "block_hash": block_hash,
        "transaction_index": transaction_index,
        "wallet_address": wallet,
        "recipient_address": recipient,
        "receipt_status": "confirmed" if status == 1 else "reverted",
        "asset_fill_verified": False,
        **_fees(raw, expected_chain_id),
    }


def verify_erc20_receipt(
    raw: dict[str, Any] | None,
    *,
    expected_tx_hash: str,
    expected_chain_id: int,
    reported_chain_id: int,
    wallet_address: str,
    router_address: str,
    input_token_address: str,
    output_token_address: str,
    input_decimals: int,
    output_decimals: int,
) -> dict[str, Any]:
    """Verify actual net wallet ERC-20 flows without estimating financial values.

    Native/internal flows require traces. Fee incompleteness preserves verified
    token amounts; callers hold financial accounting until fees and required
    valuations are known. Validation errors after identity verification preserve
    actual fee evidence in .details, so failed attempts cannot silently cost zero.
    """
    try:
        verified = verify_fee_receipt(
            raw,
            expected_tx_hash=expected_tx_hash,
            expected_chain_id=expected_chain_id,
            reported_chain_id=reported_chain_id,
            wallet_address=wallet_address,
            recipient_address=router_address,
        )
    except ReceiptSettlementError as exc:
        if exc.code == "recipient_mismatch":
            raise ReceiptSettlementError("router_mismatch", "Receipt recipient differs from the signed router") from exc
        raise
    if verified["receipt_status"] == "reverted":
        raise ReceiptSettlementError(
            "transaction_reverted", "Transaction reverted; no swap fill is confirmed", details=verified
        )
    try:
        return _verify_erc20_flows(
            raw,
            verified,
            input_token_address,
            output_token_address,
            input_decimals,
            output_decimals,
        )
    except ReceiptSettlementError as exc:
        exc.details = {**verified, **exc.details}
        raise


def _verify_erc20_flows(raw, verified, input_token_address, output_token_address, input_decimals, output_decimals):
    wallet, router = verified["wallet_address"], verified["recipient_address"]
    expected, block, block_hash = verified["tx_hash"], verified["block_number"], verified["block_hash"]
    tokens = [_data(input_token_address, 20, "input token"), _data(output_token_address, 20, "output token")]
    for decimals in (input_decimals, output_decimals):
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
            _error("invalid_context", "Token decimals must be integers from 0 through 255")
    if tokens[0] == tokens[1]:
        _error("unsupported_flow", "Same-token swaps require a dedicated settlement adapter")
    if any(token in _NATIVE for token in tokens):
        _error("unsupported_native_flow", "Native or internal asset flows require transaction traces")
    logs = raw.get("logs")
    if not isinstance(logs, list):
        _error("malformed_logs", "Receipt logs must be a list")
    deltas = {token: 0 for token in tokens}
    indices: set[int] = set()
    last_index = -1
    for index, log in enumerate(logs):
        if not isinstance(log, dict):
            _error("malformed_logs", f"logs[{index}] must be an object")
        address = _data(log.get("address"), 20, f"logs[{index}].address")
        if log.get("removed") is not False:
            _error("removed_or_unconfirmed_log", "Removed or unconfirmed logs cannot settle a fill")
        log_index = _quantity(log.get("logIndex"), f"logs[{index}].logIndex")
        if log_index in indices or log_index <= last_index:
            _error("duplicate_or_unordered_log", "Log indices must be unique and increasing")
        indices.add(log_index)
        last_index = log_index
        for field, wanted, length in (("transactionHash", expected, 32), ("blockHash", block_hash, 32)):
            if _data(log.get(field), length, f"logs[{index}].{field}") != wanted:
                _error("log_identity_mismatch", f"Log {field} differs from its receipt")
        if _quantity(log.get("blockNumber"), "log blockNumber") != block:
            _error("log_identity_mismatch", "Log block differs from its receipt")
        if "transactionIndex" in log or verified["transaction_index"] is not None:
            transaction_index = _quantity(log.get("transactionIndex"), "log transactionIndex")
            if verified["transaction_index"] is None or transaction_index != verified["transaction_index"]:
                _error("log_identity_mismatch", "Log transaction index differs from its receipt")
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) > 4:
            _error("malformed_logs", "Log topics must be a list of at most four words")
        topics = [_data(topic, 32, "log topic") for topic in topics]
        _data(log.get("data"), None, "log data")
        if not topics or topics[0] != _TRANSFER:
            continue
        if len(topics) != 3:
            _error("malformed_transfer", "ERC-20 Transfer must have three topics")
        sender = _indexed_address(topics[1], "Transfer sender")
        recipient = _indexed_address(topics[2], "Transfer recipient")
        amount = int(_data(log["data"], 32, "Transfer value"), 16)
        if wallet not in (sender, recipient):
            continue
        if address not in deltas:
            _error("unexpected_token", "Wallet transfer involves an asset outside the signed swap context")
        deltas[address] += (amount if recipient == wallet else 0) - (amount if sender == wallet else 0)
    spent, received = -deltas[tokens[0]], deltas[tokens[1]]
    if spent <= 0 or received <= 0:
        _error("unverified_wallet_flow", "Receipt does not prove positive net input spent and output received")
    return {
        **verified,
        "physical_verified": True,
        "asset_fill_verified": True,
        "router_address": router,
        "input_token_address": tokens[0],
        "output_token_address": tokens[1],
        "input_amount_raw": str(spent),
        "output_amount_raw": str(received),
        "input_amount": _units(spent, input_decimals),
        "output_amount": _units(received, output_decimals),
        "token_deltas_raw": {token: str(value) for token, value in deltas.items()},
    }
