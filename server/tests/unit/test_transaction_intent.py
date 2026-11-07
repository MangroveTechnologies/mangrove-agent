"""Adversarial unsigned transaction checks without wallet storage or broadcasts."""
from dataclasses import replace

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from src.services import transaction_intent as ti
from src.shared.errors import SigningError

WALLET = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
OUTPUT = "0x" + "33" * 20
EXECUTOR = "0x" + "44" * 20
OTHER = "0x" + "55" * 20


def swap_intent(**overrides):
    return replace(ti.SwapIntent(8453, WALLET, ti.ROUTER_V6, TOKEN, OUTPUT, 1_000_000, 990_000), **overrides)


def swap_data(intent=None, *, desc_overrides=None, permit=b"", route=b"route-data"):
    intent = intent or swap_intent()
    desc = dict(src=TOKEN, dst=OUTPUT, src_receiver=EXECUTOR, dst_receiver=WALLET,
                amount=1_000_000, min_return=990_000, flags=0)
    desc.update(desc_overrides or {})
    types = ti._SWAP_TYPES[intent.router_address]
    args = [EXECUTOR, tuple(desc.values())]
    if intent.router_address == ti.ROUTER_V5:
        args.append(permit)
    args.append(route)
    return "0x" + (ti._SWAP_SELECTORS[intent.router_address] + encode(types, args)).hex()


def payload(data=None, **overrides):
    value = dict(to=ti.ROUTER_V6, data=data or swap_data(), value=0, nonce=0,
                 chainId=8453, gas=300_000, maxFeePerGas=10_000_000,
                 maxPriorityFeePerGas=1_000_000)
    value.update(overrides)
    return value


def approval_intent():
    return ti.ApprovalIntent(8453, WALLET, TOKEN, ti.ROUTER_V6, 1_000_000)


def approval_data(spender=ti.ROUTER_V6, amount=1_000_000):
    return "0x095ea7b3" + encode(("address", "uint256"), (spender, amount)).hex()


@pytest.mark.parametrize("router", [ti.ROUTER_V5, ti.ROUTER_V6])
def test_valid_generic_swaps_produce_real_recoverable_transactions(router):
    # Generated locally, unfunded, discarded; no network/database access.
    account = Account.create()
    intent = swap_intent(router_address=router, wallet_address=account.address)
    data = swap_data(intent, desc_overrides={"dst_receiver": account.address})
    normalized = ti.validate_transaction(payload(data, to=router), intent, account.address, 8453)
    signed = account.sign_transaction(normalized)
    assert Account.recover_transaction(signed.raw_transaction) == account.address
    assert normalized["chainId"] == 8453


def test_valid_approval_has_real_recoverable_signature():
    account = Account.create()
    intent = replace(approval_intent(), wallet_address=account.address)
    tx = payload(approval_data(), to=TOKEN, gasPrice="1000000", maxFeePerGas=None,
                 maxPriorityFeePerGas=None, type="0x0")
    normalized = ti.validate_transaction(tx, intent, account.address)
    signed = account.sign_transaction(normalized)
    assert Account.recover_transaction(signed.raw_transaction) == account.address


@pytest.mark.parametrize("changes", [
    {"to": OTHER}, {"value": 1}, {"chainId": 1}, {"from": OTHER},
    {"gas": 2_000_001}, {"maxFeePerGas": 100_000_000_001},
    {"gas": 2_000_000, "maxFeePerGas": 100_000_000_000},
    {"maxPriorityFeePerGas": 11_000_000}, {"maxFeePerGas": None},
    {"gasPrice": 1}, {"nonce": -1}, {"nonce": True}, {"nonce": 0.0},
    {"gasLimit": 300_000}, {"gas": 0}, {"chain_id": 8453},
    {"authorizationList": []}, {"authorization_list": None},
    {"type": 4}, {"type": 3}, {"type": 1}, {"type": True},
    {"type": None}, {"type": "0x4", "gasPrice": 1},
    {"to": "0x" + "zz" * 20}, {"data": "0xdeadbeef"},
])
def test_adversarial_envelopes_fail_closed(changes):
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(**changes), swap_intent(), WALLET, 8453)


@pytest.mark.parametrize("desc", [
    {"src": OTHER}, {"dst": OTHER}, {"dst_receiver": OTHER},
    {"src_receiver": OTHER}, {"amount": 999_999}, {"amount": 1_000_001},
    {"min_return": 989_999}, {"flags": 1}, {"flags": 2}, {"flags": 4},
])
def test_decoded_swap_economic_changes_are_rejected(desc):
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(swap_data(desc_overrides=desc)), swap_intent(), WALLET)


@pytest.mark.parametrize("data", [
    "0x095ea7b3", approval_data()[:-64], approval_data() + "00",
    "0x095ea7b3" + "01" + approval_data()[12:],
    approval_data(OTHER), approval_data(amount=2**256 - 1), approval_data(amount=999_999),
])
def test_approvals_require_exact_canonical_spender_and_amount(data):
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(data, to=TOKEN), approval_intent(), WALLET)


@pytest.mark.parametrize("target,value", [(OTHER, 0), (TOKEN, 1), (OTHER, 1000)])
def test_approve_shaped_eoa_and_native_value_are_rejected(target, value):
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(approval_data(), to=target, value=value), approval_intent(), WALLET)


def test_swap_v5_embedded_permit_is_refused():
    intent = swap_intent(router_address=ti.ROUTER_V5)
    with pytest.raises(SigningError, match="permits"):
        ti.validate_transaction(payload(swap_data(intent, permit=b"malicious"), to=ti.ROUTER_V5), intent, WALLET)


@pytest.mark.parametrize("mutation", ["truncated", "tail", "offset", "padding", "huge_length", "aliased_offset"])
def test_dynamic_abi_malleability_is_rejected(mutation):
    raw = bytearray.fromhex(swap_data()[2:])
    if mutation == "truncated":
        raw = raw[:-1]
    elif mutation == "tail":
        raw += b"\x00" * 32
    elif mutation == "offset":
        raw[4 + 8 * 32:4 + 9 * 32] = (32).to_bytes(32, "big")
    elif mutation == "padding":
        raw[-1] = 1
    elif mutation == "huge_length":
        raw[4 + 9 * 32:4 + 10 * 32] = (2**255).to_bytes(32, "big")
    else:
        # An extra empty dynamic slot must not alias part of the static tuple.
        raw[4 + 8 * 32:4 + 9 * 32] = (0).to_bytes(32, "big")
    with pytest.raises(SigningError, match="ABI"):
        ti.validate_transaction(payload("0x" + raw.hex()), swap_intent(), WALLET)


def test_zero_destination_means_signing_wallet_and_native_amount_is_exact():
    intent = swap_intent(input_token_address=ti.NATIVE_TOKEN)
    data = swap_data(desc_overrides={"src": ti.NATIVE_TOKEN, "dst_receiver": ti.ZERO_ADDRESS})
    assert ti.validate_transaction(payload(data, value=intent.amount), intent, WALLET)["value"] == intent.amount
    with pytest.raises(SigningError, match="native value"):
        ti.validate_transaction(payload(data, value=intent.amount + 1), intent, WALLET)


def test_unsupported_optimized_selector_has_explicit_compatibility_error():
    selector = keccak(text="unoswap(uint256,uint256,uint256,uint256)")[:4]
    tx = payload("0x" + (selector + encode(("uint256",) * 4, (1, 2, 3, 4))).hex())
    with pytest.raises(SigningError, match="UNSUPPORTED_SWAP_SELECTOR"):
        ti.validate_transaction(tx, swap_intent(), WALLET)


@pytest.mark.parametrize("chain", [84532, 324, 0, True])
def test_unreviewed_chain_policy_refuses_testnet_and_different_deployments(chain):
    with pytest.raises(SigningError, match="chain"):
        ti.validate_transaction(payload(chainId=chain), swap_intent(chain_id=chain), WALLET)


def test_intent_is_frozen_and_required():
    original = swap_intent()
    with pytest.raises(AttributeError):
        original.amount = 2
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(), None, WALLET)


def test_caller_wallet_and_chain_cannot_disagree_with_intent():
    with pytest.raises(SigningError, match="wallet"):
        ti.validate_transaction(payload(), swap_intent(), OTHER)
    with pytest.raises(SigningError, match="chain"):
        ti.validate_transaction(payload(), swap_intent(), WALLET, 1)


def test_missing_chain_is_filled_from_intent_and_zero_fees_are_rejected():
    tx = payload()
    del tx["chainId"]
    assert ti.validate_transaction(tx, swap_intent(), WALLET)["chainId"] == 8453
    with pytest.raises(SigningError):
        ti.validate_transaction(payload(maxFeePerGas=0, maxPriorityFeePerGas=0), swap_intent(), WALLET)


@pytest.mark.parametrize("quoted,slippage,expected", [(101, .0025, 101), (10_000, .0025, 9975), (1001, .002, 999)])
def test_minimum_rounds_up(quoted, slippage, expected):
    assert ti.minimum_return(quoted, slippage) == expected


@pytest.mark.parametrize("quote,slippage", [(0, .002), (1.5, .002), ("NaN", .002), (100, float("nan")), (100, .003)])
def test_unusable_price_or_slippage_is_refused(quote, slippage):
    with pytest.raises(SigningError):
        ti.minimum_return(quote, slippage)


def test_routing_fee_and_slippage_are_separate_and_round_conservatively():
    # Current provider contract: a gross 100,000 unit quote, 25 bps partner
    # fee and the caller's .20% slippage produce 99,550.5 -> 99,551 units.
    assert ti.minimum_return(100_000, .002, routing_fee_bps=25) == 99_551
    assert ti.minimum_return(100_000, .002, routing_fee_bps=0) == 99_800
    # An unsigned payload cannot increase the reviewed cap or slippage.
    for fee, slippage in ((26, .002), (-1, .002), (True, .002), (25, .0026)):
        with pytest.raises(SigningError):
            ti.minimum_return(100_000, slippage, routing_fee_bps=fee)


def test_large_uint256_quote_keeps_integer_precision():
    quoted = 2**200 + 1
    numerator = quoted * 9975 * 998
    denominator = 10_000 * 1000
    expected = (numerator + denominator - 1) // denominator
    assert ti.minimum_return(quoted, .002, routing_fee_bps=25) == expected
