"""Pure receipt controls: no quotes, wallets, RPC calls or database mutation."""

from __future__ import annotations

import copy

import pytest

from src.services.receipt_settlement import ReceiptSettlementError, verify_erc20_receipt, verify_fee_receipt

WALLET = "0x" + "11" * 20
ROUTER = "0x" + "22" * 20
INPUT = "0x" + "33" * 20
OUTPUT = "0x" + "44" * 20
OTHER = "0x" + "55" * 20
TX = "0x" + "aa" * 32
BLOCK = "0x" + "bb" * 32
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def transfer(token, sender, recipient, amount, index):
    return {
        "address": token,
        "topics": [TRANSFER, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
        "data": "0x" + f"{amount:064x}",
        "logIndex": hex(index),
        "removed": False,
        "transactionHash": TX,
        "transactionIndex": "0x2",
        "blockHash": BLOCK,
        "blockNumber": "0x123",
    }


def receipt():
    return {
        "transactionHash": TX,
        "transactionIndex": "0x2",
        "from": WALLET,
        "to": ROUTER,
        "status": "0x1",
        "blockHash": BLOCK,
        "blockNumber": "0x123",
        "gasUsed": "0x5208",
        "effectiveGasPrice": hex(2_000_000_000),
        "cumulativeGasUsed": hex(999_999_999),
        "logs": [
            transfer(INPUT, WALLET, ROUTER, 100_000_000, 5),
            transfer(OUTPUT, ROUTER, WALLET, 250_000_000_000_000_000, 7),
        ],
    }


def verify(raw, **overrides):
    context = dict(
        expected_tx_hash=TX,
        expected_chain_id=1,
        reported_chain_id=1,
        wallet_address=WALLET,
        router_address=ROUTER,
        input_token_address=INPUT,
        output_token_address=OUTPUT,
        input_decimals=6,
        output_decimals=18,
    )
    context.update(overrides)
    return verify_erc20_receipt(raw, **context)


def code(raw, expected, **kwargs):
    with pytest.raises(ReceiptSettlementError) as caught:
        verify(raw, **kwargs)
    assert caught.value.code == expected
    return caught.value


def test_actual_wallet_net_amounts_and_native_fee_are_exact_and_do_not_mutate_input():
    raw = receipt()
    before = copy.deepcopy(raw)
    result = verify(raw)
    assert raw == before
    assert result["physical_verified"] is True
    assert result["input_amount"] == "100"
    assert result["output_amount"] == "0.25"
    assert result["token_deltas_raw"] == {INPUT: "-100000000", OUTPUT: "250000000000000000"}
    assert result["native_fee_amount_wei"] == "42000000000000"
    assert result["native_fee_amount"] == "0.000042"
    assert result["native_fee_asset"] == "native:1"
    assert result["native_fee_complete"] is True
    assert result["chain_verification"] == "provider_route"
    assert "gas_usd" not in result


def test_partial_fill_refunds_and_token_taxes_use_actual_net_events_not_quotes():
    raw = receipt()
    raw["estimated_output_amount"] = "999999999"
    raw["logs"] = [
        transfer(INPUT, WALLET, ROUTER, 100_000_000, 0),
        transfer(INPUT, ROUTER, WALLET, 20_000_000, 1),
        transfer(OUTPUT, ROUTER, WALLET, 300_000_000_000_000_000, 2),
        transfer(OUTPUT, WALLET, OTHER, 50_000_000_000_000_000, 3),
    ]
    result = verify(raw)
    assert result["input_amount"] == "80"
    assert result["output_amount"] == "0.25"


def test_self_and_zero_transfers_do_not_create_a_fill_or_change_net_amounts():
    raw = receipt()
    raw["logs"] += [transfer(INPUT, WALLET, WALLET, 10**18, 8), transfer(OUTPUT, ROUTER, WALLET, 0, 9)]
    assert verify(raw)["input_amount"] == "100"
    raw["logs"] = [transfer(INPUT, WALLET, WALLET, 100, 0)]
    code(raw, "unverified_wallet_flow")


def test_uint256_amounts_do_not_pass_through_float_or_decimal_context_rounding():
    raw = receipt()
    raw["logs"][0]["data"] = "0x" + f"{2**200 + 7:064x}"
    raw["logs"][1]["data"] = "0x" + f"{10**18 + 1:064x}"
    result = verify(raw, input_decimals=0)
    assert result["input_amount"] == str(2**200 + 7)
    assert result["output_amount"] == "1.000000000000000001"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("transactionHash", "0x" + "cc" * 32, "transaction_mismatch"),
        ("from", OTHER, "wallet_mismatch"),
        ("to", OTHER, "router_mismatch"),
        ("chainId", "0x2", "chain_mismatch"),
        ("status", "0x2", "malformed_receipt"),
        ("status", True, "malformed_receipt"),
        ("status", "0x01", "malformed_receipt"),
        ("transactionHash", "0x123", "malformed_receipt"),
        ("from", "wallet", "malformed_receipt"),
        ("blockNumber", "0x", "malformed_receipt"),
        ("blockHash", "0x00", "malformed_receipt"),
        ("transactionIndex", "0x02", "malformed_receipt"),
    ],
)
def test_wrong_or_malformed_raw_identity_refuses_booking(field, value, expected):
    raw = receipt()
    raw[field] = value
    code(raw, expected)


def test_reported_route_and_optional_raw_chain_must_match_signed_chain():
    code(receipt(), "chain_mismatch", reported_chain_id=8453)
    raw = receipt()
    raw["chainId"] = "0x1"
    assert verify(raw)["chain_verification"] == "raw_chain_id_and_provider_route"


@pytest.mark.parametrize("field", ["blockNumber", "blockHash"])
def test_unmined_receipt_and_absent_receipt_remain_pending(field):
    code(None, "receipt_pending")
    raw = receipt()
    raw[field] = None
    code(raw, "receipt_pending")


def test_reverted_transaction_never_confirms_a_swap_but_preserves_actual_gas_evidence():
    raw = receipt()
    raw.update(status="0x0", logs=[])
    error = code(raw, "transaction_reverted")
    assert error.details["native_fee_amount"] == "0.000042"
    assert error.details["raw_fee_evidence"]["gasUsed"] == "0x5208"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("address", OTHER, "unexpected_token"),
        ("address", "0x12", "malformed_receipt"),
        ("topics", [TRANSFER], "malformed_transfer"),
        ("topics", [TRANSFER, "0x" + "01" * 32, "0x" + "0" * 24 + ROUTER[2:]], "malformed_transfer"),
        ("topics", [TRANSFER, "garbage", "0x" + "0" * 24 + ROUTER[2:]], "malformed_receipt"),
        ("data", "0x01", "malformed_receipt"),
        ("data", "0x" + "gg" * 32, "malformed_receipt"),
        ("logIndex", "0x00", "malformed_receipt"),
        ("removed", True, "removed_or_unconfirmed_log"),
        ("removed", 0, "removed_or_unconfirmed_log"),
        ("transactionHash", "0x" + "cc" * 32, "log_identity_mismatch"),
        ("blockHash", "0x" + "cc" * 32, "log_identity_mismatch"),
        ("blockNumber", "0x124", "log_identity_mismatch"),
        ("transactionIndex", "0x3", "log_identity_mismatch"),
    ],
)
def test_malformed_or_wrong_log_cannot_change_verified_book(field, value, expected):
    raw = receipt()
    raw["logs"][0][field] = value
    code(raw, expected)


def test_duplicate_or_out_of_order_logs_refuse_double_booking():
    raw = receipt()
    raw["logs"].append(copy.deepcopy(raw["logs"][0]))
    code(raw, "duplicate_or_unordered_log")
    raw = receipt()
    raw["logs"].reverse()
    code(raw, "duplicate_or_unordered_log")


@pytest.mark.parametrize("field", ["transactionHash", "blockHash", "blockNumber", "transactionIndex", "removed"])
def test_log_identity_fields_cannot_be_omitted(field):
    raw = receipt()
    raw["logs"][0].pop(field)
    error = code(raw, "removed_or_unconfirmed_log" if field == "removed" else "malformed_receipt")
    assert error.details["native_fee_amount_wei"] == "42000000000000"


def test_rpc_receipts_without_transaction_index_still_require_each_log_hash_and_block():
    raw = receipt()
    raw.pop("transactionIndex")
    for log in raw["logs"]:
        log.pop("transactionIndex")
    assert verify(raw)["input_amount"] == "100"
    raw["logs"][0]["transactionIndex"] = "0x2"
    code(raw, "log_identity_mismatch")


def test_proxy_token_contract_is_exact_and_cannot_be_replaced_by_implementation_or_symbol():
    raw = receipt()
    raw["logs"][0]["address"] = OTHER
    raw["logs"][0]["symbol"] = "USDC"
    raw["logs"][0]["implementation"] = INPUT
    code(raw, "unexpected_token")
    raw["logs"][0]["address"] = INPUT.upper().replace("0X", "0x")
    assert verify(raw)["input_amount_raw"] == "100000000"


def test_extra_venue_fee_in_input_token_is_in_wallet_net_spend():
    raw = receipt()
    raw["logs"].append(transfer(INPUT, WALLET, OTHER, 1_000_001, 8))
    assert verify(raw)["input_amount"] == "101.000001"
    raw["logs"][-1]["address"] = OTHER
    code(raw, "unexpected_token")


def test_irrelevant_structurally_valid_contract_event_is_not_a_transfer():
    raw = receipt()
    raw["logs"].append(
        {
            **transfer(OTHER, ROUTER, OTHER, 0, 9),
            "topics": ["0x" + "ab" * 32],
            "data": "0x",
        }
    )
    assert verify(raw)["output_amount"] == "0.25"


@pytest.mark.parametrize("decimals", [True, -1, 256, None, 1.5])
def test_unknown_or_invalid_token_metadata_is_not_guessed(decimals):
    code(receipt(), "invalid_context", input_decimals=decimals)


@pytest.mark.parametrize("native", ["0x" + "e" * 40, "0x" + "0" * 40])
def test_native_or_internal_asset_flow_requires_trace_not_quote(native):
    code(receipt(), "unsupported_native_flow", input_token_address=native)


@pytest.mark.parametrize(
    ("field", "value"), [("gasUsed", None), ("effectiveGasPrice", "estimated"), ("effectiveGasPrice", "0x-1")]
)
def test_incomplete_fee_evidence_preserves_physical_tokens_and_holds_financial_completion(field, value):
    raw = receipt()
    raw[field] = value
    result = verify(raw)
    assert result["physical_verified"] is True
    assert result["output_amount"] == "0.25"
    assert result["native_fee_complete"] is False
    assert result["native_fee_amount"] is None
    assert result["raw_fee_evidence"][field] == value


def test_base_chain_adds_actual_l1_and_operator_fees_without_estimating_fork_formula():
    raw = receipt()
    raw.update(l1Fee="0x7", operatorFee="0x3")
    result = verify(raw, expected_chain_id=8453, reported_chain_id=8453)
    assert result["native_fee_amount_wei"] == "42000000000010"
    assert result["native_fee_complete"] is True
    raw.pop("operatorFee")
    raw.update(operatorFeeScalar="0x0", operatorFeeConstant="0x0")
    assert verify(raw, expected_chain_id=8453, reported_chain_id=8453)["native_fee_complete"] is True
    raw["operatorFeeScalar"] = "0x1"
    partial = verify(raw, expected_chain_id=8453, reported_chain_id=8453)
    assert partial["native_fee_complete"] is False
    assert partial["native_fee_known_amount_wei"] == "42000000000007"


def test_missing_rollup_or_unknown_extension_fee_is_incomplete_not_zero():
    raw = receipt()
    assert not verify(raw, expected_chain_id=8453, reported_chain_id=8453)["native_fee_complete"]
    raw["unknownChainFee"] = "0x10"
    result = verify(raw)
    assert result["physical_verified"] and not result["native_fee_complete"]
    assert result["raw_fee_evidence"]["unknownChainFee"] == "0x10"
    assert not verify(receipt(), expected_chain_id=999, reported_chain_id=999)["native_fee_complete"]


def test_arbitrum_l1_gas_is_already_in_execution_gas_and_blob_fees_are_explicit():
    raw = receipt()
    raw["gasUsedForL1"] = "0x100"
    result = verify(raw, expected_chain_id=42161, reported_chain_id=42161)
    assert result["native_fee_amount_wei"] == "42000000000000"
    raw = receipt()
    raw.update(blobGasUsed="0x10", blobGasPrice="0x3")
    assert verify(raw)["native_fee_amount_wei"] == "42000000000048"
    raw.pop("blobGasPrice")
    assert not verify(raw)["native_fee_complete"]


@pytest.mark.parametrize("chain_id", [1, 42161, 421614])
@pytest.mark.parametrize("field", ["l1Fee", "operatorFee"])
def test_op_fee_fields_do_not_create_a_separate_charge_on_other_chain_models(chain_id, field):
    raw = receipt()
    raw[field] = "0x7"
    result = verify(raw, expected_chain_id=chain_id, reported_chain_id=chain_id)
    assert result["native_fee_complete"] is False
    assert result["native_fee_amount_wei"] is None
    assert result["native_fee_known_amount_wei"] == "42000000000000"
    assert f"unsupported_separate_{field}" in result["native_fee_issues"]


@pytest.mark.parametrize("chain_id", [10, 8453, 84532, 11155420])
def test_each_op_route_requires_l1_and_operator_evidence_even_when_missing_fields_might_mean_zero(chain_id):
    raw = receipt()
    raw["l1Fee"] = "0x0"
    result = verify(raw, expected_chain_id=chain_id, reported_chain_id=chain_id)
    assert result["native_fee_issues"] == ["missing_operator_fee_evidence"]
    raw["operatorFee"] = "0x0"
    result = verify(raw, expected_chain_id=chain_id, reported_chain_id=chain_id)
    assert result["native_fee_complete"]
    assert result["native_fee_amount_wei"] == "42000000000000"


@pytest.mark.parametrize("value", [None, 1, 1.0, True, "1", "0x01", "0x-1", hex(2**256)])
def test_noncanonical_or_overflow_gas_evidence_does_not_become_an_actual_fee(value):
    raw = receipt()
    raw["gasUsed"] = value
    result = verify(raw)
    assert result["native_fee_amount_wei"] is None
    assert result["native_fee_issues"] == ["missing_or_malformed_execution_fee"]


def test_unknown_and_quoted_fee_fields_cannot_silently_be_financial_truth():
    raw = receipt()
    raw.update(gasPrice="0xffff", gas_usd="999", estimatedGas="0xffff")
    result = verify(raw)
    assert result["native_fee_known_amount_wei"] == "42000000000000"
    assert result["native_fee_complete"] is False
    assert "gas_usd" not in result


@pytest.mark.parametrize("status", ["0x1", "0x0"])
def test_successful_and_failed_approval_fees_are_real_costs_not_asset_fills(status):
    raw = receipt()
    raw.update(status=status, to=INPUT, logs=[])
    result = verify_fee_receipt(
        raw,
        expected_tx_hash=TX,
        expected_chain_id=1,
        reported_chain_id=1,
        wallet_address=WALLET,
        recipient_address=INPUT,
    )
    assert result["receipt_status"] == ("confirmed" if status == "0x1" else "reverted")
    assert result["asset_fill_verified"] is False
    assert result["native_fee_amount"] == "0.000042"
    assert "input_amount" not in result


def test_missing_approval_receipt_does_not_become_a_zero_fee():
    with pytest.raises(ReceiptSettlementError) as caught:
        verify_fee_receipt(
            None,
            expected_tx_hash=TX,
            expected_chain_id=1,
            reported_chain_id=1,
            wallet_address=WALLET,
            recipient_address=INPUT,
        )
    assert caught.value.code == "receipt_pending"
    assert "native_fee_amount" not in caught.value.details


def test_unresolved_asset_fill_preserves_verified_actual_fee_evidence():
    raw = receipt()
    raw["logs"] = []
    error = code(raw, "unverified_wallet_flow")
    assert error.details["native_fee_complete"] is True
    assert error.details["native_fee_amount"] == "0.000042"
