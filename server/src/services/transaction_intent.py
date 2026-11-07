"""Bind unsigned DEX transactions to locally constructed economic intent.

This validator has no wallet/database/key access and runs again in custody.
Router addresses alone do not authorize a call. Only canonical approve and
generic swap ABI encodings whose effects match the operation are supported.
Optimized pool routes need a separate, independently verified pool adapter.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import Literal

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from src.shared.errors import SigningError

ROUTER_V5 = "0x1111111254eeb25477b68fb85ed929f73a960582"
ROUTER_V6 = "0x111111125421ca6dc452d289314280a0f8842a65"
# Mainnet chains served by the current adapter, excluding zkSync's different
# deployment. No deterministic-address assumption authorizes a testnet.
SUPPORTED_CHAINS = frozenset({1, 10, 56, 100, 137, 8453, 42161, 43114, 59144})
NATIVE_TOKEN = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ZERO_ADDRESS = "0x" + "00" * 20
MAX_GAS = 2_000_000
MAX_GAS_PRICE = 100_000_000_000
MAX_TOTAL_FEE = 20_000_000_000_000_000
MAX_CALLDATA_BYTES = 131_072
# Current Markets Classic Swap partner fee (0.25%), separately authorized from
# price slippage. Its quote adapter returns gross output, while prepare_swap
# adds this fee. This reviewed local cap cannot come from unsigned calldata.
ONEINCH_ROUTING_FEE_BPS = 25
_DESCRIPTION = "(address,address,address,address,uint256,uint256,uint256)"
# V6 ABI: 1inch/1inch-sdk-go @ 321b27436ff7f9ca5d3990006e2d88728780eda0,
# constants/abi/aggregationRouterV6.abi.json. V5's verified GenericRouter ABI:
# https://etherscan.io/address/0x1111111254eeb25477b68fb85ed929f73a960582#code
_SWAP_TYPES = {
    ROUTER_V5: ("address", _DESCRIPTION, "bytes", "bytes"),
    ROUTER_V6: ("address", _DESCRIPTION, "bytes"),
}
_SWAP_SELECTORS = {
    router: keccak(text="swap(" + ",".join(types) + ")")[:4]
    for router, types in _SWAP_TYPES.items()
}
_APPROVE_SELECTOR = bytes.fromhex("095ea7b3")
_INTEGER = re.compile(r"(?:0x[0-9a-fA-F]+|[0-9]+)\Z")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True)
class ApprovalIntent:
    chain_id: int
    wallet_address: str
    token_address: str
    spender: str
    amount: int
    kind: Literal["approval"] = "approval"


@dataclass(frozen=True)
class SwapIntent:
    chain_id: int
    wallet_address: str
    router_address: str
    input_token_address: str
    output_token_address: str
    amount: int
    min_return_amount: int
    routing_fee_bps: int = 0
    kind: Literal["swap"] = "swap"


TransactionIntent = ApprovalIntent | SwapIntent


def intent_to_dict(intent: TransactionIntent) -> dict:
    """Serialize the operation, independently of the prepared transaction."""
    if type(intent) not in (ApprovalIntent, SwapIntent):
        raise SigningError("A recognized transaction intent is required.")
    return asdict(intent)


def intent_from_dict(value: dict) -> TransactionIntent:
    """Parse a strict custody wire intent; extra or missing fields fail closed."""
    try:
        if type(value) is not dict:
            raise ValueError
        cls = {"approval": ApprovalIntent, "swap": SwapIntent}[value["kind"]]
        result = cls(**value)
        _validate_intent(result)
        return result
    except (KeyError, TypeError, ValueError):
        raise SigningError("Invalid transaction intent.") from None


def _uint(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is int:
        number = value
    elif type(value) is str and len(value) <= 80 and _INTEGER.fullmatch(value):
        number = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise SigningError(f"Invalid unsigned integer field: {name}.")
    if not (int(positive) <= number < 2**256):
        raise SigningError(f"Unsigned integer out of bounds: {name}.")
    return number


def address(value: object) -> str:
    """Return a structurally valid, normalized EVM address."""
    if type(value) is not str or not _ADDRESS.fullmatch(value):
        raise SigningError("Invalid EVM address in transaction intent or payload.")
    return value.lower()


def router_for_chain(chain_id: int) -> str:
    """The current API uses V6; unreviewed chains have no signing policy."""
    if type(chain_id) is not int or chain_id not in SUPPORTED_CHAINS:
        raise SigningError("Unsupported signing chain: no reviewed 1inch deployment policy.")
    return ROUTER_V6


def _validate_intent(intent: TransactionIntent) -> None:
    if type(intent) not in (ApprovalIntent, SwapIntent):
        raise SigningError("A transaction intent is required before signing.")
    router_for_chain(intent.chain_id)
    if address(intent.wallet_address) == ZERO_ADDRESS:
        raise SigningError("The signing wallet cannot be the zero address.")
    # Intent quantities have a single representation on the protected wire.
    if type(intent.amount) is not int:
        raise SigningError("Intent amount must be an integer in base units.")
    _uint(intent.amount, "intent.amount", positive=True)
    if isinstance(intent, ApprovalIntent):
        if intent.kind != "approval":
            raise SigningError("Invalid approval intent kind.")
        if address(intent.token_address) in (ZERO_ADDRESS, NATIVE_TOKEN, ROUTER_V5, ROUTER_V6):
            raise SigningError("An approval must name the requested ERC-20 token.")
        router = address(intent.spender)
    else:
        if intent.kind != "swap":
            raise SigningError("Invalid swap intent kind.")
        src, dst = address(intent.input_token_address), address(intent.output_token_address)
        if src == dst or ZERO_ADDRESS in (src, dst):
            raise SigningError("Swap intent must name distinct, nonzero input and output assets.")
        if type(intent.min_return_amount) is not int:
            raise SigningError("Minimum return must be an integer in base units.")
        _uint(intent.min_return_amount, "intent.min_return_amount", positive=True)
        if type(intent.routing_fee_bps) is not int or not 0 <= intent.routing_fee_bps <= ONEINCH_ROUTING_FEE_BPS:
            raise SigningError("Swap routing fee exceeds the reviewed maximum of 25 basis points.")
        router = address(intent.router_address)
    if router not in _SWAP_TYPES:
        raise SigningError("Intent router is not a reviewed 1inch deployment.")


def minimum_return(quoted_amount: object, slippage_pct: float, *, routing_fee_bps: int = 0) -> int:
    """Apply a separately bounded fee, then the unchanged slippage tolerance.

    Slippage remains in (0, 0.0025]. The current Markets quote is gross of its
    fixed 25 bps routing fee; callers explicitly pass that reviewed cap.
    Never infer a higher fee or lower floor from a prepared transaction.
    """
    try:
        quoted = Decimal(str(quoted_amount))
        slippage = Decimal(str(slippage_pct))
        if not quoted.is_finite() or quoted <= 0 or quoted != quoted.to_integral_value():
            raise ValueError
        if not slippage.is_finite() or not 0 < slippage <= Decimal("0.0025"):
            raise ValueError
        if type(routing_fee_bps) is not int or not 0 <= routing_fee_bps <= ONEINCH_ROUTING_FEE_BPS:
            raise ValueError
        with localcontext() as context:
            context.prec = 100  # all uint256 quantities plus decimal slippage
            net_quote = quoted * (Decimal(10_000 - routing_fee_bps) / 10_000)
            return int((net_quote * (1 - slippage)).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, InvalidOperation, TypeError):
        raise SigningError("A positive integral quote and valid slippage are required.") from None


def quote_after_max_routing_fee(quoted_amount: object, routing_fee_bps: int) -> int:
    """Display the quote after the maximum approved fee, before price slippage.

    This is an allowance calculation, not a measurement of an executed fee.
    Integer division rounds this display floor down conservatively.
    """
    try:
        quoted = Decimal(str(quoted_amount))
        if not quoted.is_finite() or quoted != quoted.to_integral_value():
            raise ValueError
        gross = _uint(int(quoted), "quoted_amount", positive=True)
    except (ValueError, InvalidOperation, TypeError):
        raise SigningError("Quote output must be a positive integer in base units.") from None
    if type(routing_fee_bps) is not int or not 0 <= routing_fee_bps <= ONEINCH_ROUTING_FEE_BPS:
        raise SigningError("Invalid routing fee quote allowance.")
    return gross * (10_000 - routing_fee_bps) // 10_000


def _calldata(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = bytes(value)
    elif type(value) is str and value.startswith("0x") and len(value) % 2 == 0 and len(value) <= 2 + 2 * MAX_CALLDATA_BYTES:
        try:
            raw = bytes.fromhex(value[2:])
        except ValueError:
            raise SigningError("Malformed transaction calldata.") from None
        if value[2:].lower() != raw.hex():  # fromhex otherwise accepts whitespace
            raise SigningError("Malformed transaction calldata.")
    else:
        raise SigningError("Malformed transaction calldata.")
    if not 4 <= len(raw) <= MAX_CALLDATA_BYTES:
        raise SigningError("Transaction calldata is missing or exceeds the signing limit.")
    return raw


def _decode(types: tuple[str, ...], raw: bytes) -> tuple:
    try:
        decoded = decode(types, raw, strict=True)
        if encode(types, decoded) != raw:
            raise ValueError
        return decoded
    except Exception:
        raise SigningError("Malformed or noncanonical transaction ABI encoding.") from None


def swap_executor(normalized_tx: dict) -> str:
    """Read the executor for protected policy after full intent validation."""
    router = address(normalized_tx.get("to"))
    raw = _calldata(normalized_tx.get("data"))
    if router not in _SWAP_TYPES or raw[:4] != _SWAP_SELECTORS[router]:
        raise SigningError("Unsupported swap ABI for executor policy.")
    return address(_decode(_SWAP_TYPES[router], raw[4:])[0])


def validate_transaction(
    unsigned_tx: dict,
    intent: TransactionIntent,
    wallet_address: str,
    chain_id: int | None = None,
) -> dict:
    """Validate complete intent before key access and return signing-ready fields.

    Type and field checks precede normalization, so a legacy gasPrice cannot
    erase type 4 or an authorization list. No unknown transaction fields pass.
    Protected custody policy must additionally authorize the intent itself.
    """
    _validate_intent(intent)
    if address(wallet_address) != address(intent.wallet_address):
        raise SigningError("Transaction intent does not match the signing wallet.")
    if chain_id is not None and (type(chain_id) is not int or chain_id != intent.chain_id):
        raise SigningError("Caller chain does not match transaction intent.")
    if type(unsigned_tx) is not dict:
        raise SigningError("Transaction payload must be an object.")
    if "authorizationList" in unsigned_tx or "authorization_list" in unsigned_tx:
        raise SigningError("EIP-7702 authorization lists are forbidden.")
    allowed = {
        "to", "from", "data", "value", "chainId", "nonce", "gas", "gasLimit",
        "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "type",
    }
    if set(unsigned_tx) - allowed:
        raise SigningError("Unsupported transaction fields.")
    tx = {k: v for k, v in unsigned_tx.items() if v is not None}
    if "type" in unsigned_tx and (unsigned_tx["type"] is None or _uint(unsigned_tx["type"], "type") not in (0, 2)):
        raise SigningError("Only transaction types 0 and 2 are permitted; delegation is forbidden.")
    if "gasLimit" in tx:
        if "gas" in tx:
            raise SigningError("Ambiguous gas and gasLimit fields.")
        tx["gas"] = tx.pop("gasLimit")
    for field in ("nonce", "gas"):
        if field not in tx:
            raise SigningError(f"Missing transaction field: {field}.")
        tx[field] = _uint(tx[field], field, positive=field == "gas")
    tx["value"] = _uint(tx.get("value", 0), "value")
    tx["chainId"] = _uint(tx.get("chainId", intent.chain_id), "chainId", positive=True)
    if tx["chainId"] != intent.chain_id:
        raise SigningError("Payload chain does not match transaction intent.")
    if "from" in tx and address(tx.pop("from")) != address(wallet_address):
        raise SigningError("Payload sender does not match the signing wallet.")
    tx["to"] = to_checksum_address(address(tx.get("to")))
    raw = _calldata(tx.get("data"))
    tx["data"] = "0x" + raw.hex()
    legacy = "gasPrice" in tx
    eip1559 = "maxFeePerGas" in tx or "maxPriorityFeePerGas" in tx
    if legacy == eip1559:
        raise SigningError("Exactly one complete gas pricing scheme is required.")
    expected_type = 0 if legacy else 2
    if "type" in tx and _uint(tx["type"], "type") != expected_type:
        raise SigningError("Transaction type conflicts with gas pricing.")
    if legacy:
        tx.pop("type", None)
        tx["gasPrice"] = price = _uint(tx["gasPrice"], "gasPrice", positive=True)
    else:
        for field in ("maxFeePerGas", "maxPriorityFeePerGas"):
            if field not in tx:
                raise SigningError("Incomplete EIP-1559 gas pricing.")
            tx[field] = _uint(tx[field], field)
        price = tx["maxFeePerGas"]
        if price == 0 or tx["maxPriorityFeePerGas"] > price:
            raise SigningError("Invalid EIP-1559 gas price bounds.")
        tx["type"] = 2
    if tx["gas"] > MAX_GAS or price > MAX_GAS_PRICE or tx["gas"] * price > MAX_TOTAL_FEE:
        raise SigningError("Transaction exceeds the maximum gas fee policy.")
    if isinstance(intent, ApprovalIntent):
        if address(tx["to"]) != address(intent.token_address) or tx["value"] != 0:
            raise SigningError("Approval must target the requested token with zero native value.")
        if raw[:4] != _APPROVE_SELECTOR:
            raise SigningError("Approval intent requires approve(address,uint256).")
        spender, amount = _decode(("address", "uint256"), raw[4:])
        if address(spender) != address(intent.spender) or amount != intent.amount:
            raise SigningError("Approval spender or amount differs from transaction intent.")
    else:
        router = address(intent.router_address)
        if address(tx["to"]) != router:
            raise SigningError("Swap destination differs from transaction intent.")
        if raw[:4] != _SWAP_SELECTORS[router]:
            raise SigningError(
                "UNSUPPORTED_SWAP_SELECTOR: only reviewed generic 1inch V5/V6 swap calls are supported; "
                "optimized pool routes require an independent pool-validation adapter."
            )
        args = _decode(_SWAP_TYPES[router], raw[4:])
        executor, desc = args[:2]
        src, dst, src_receiver, dst_receiver, amount, min_return, flags = desc
        if address(src) != address(intent.input_token_address) or address(dst) != address(intent.output_token_address):
            raise SigningError("Swap assets differ from transaction intent.")
        if address(dst_receiver) not in (address(wallet_address), ZERO_ADDRESS):
            raise SigningError("Swap recipient differs from the signing wallet.")
        if address(executor) == ZERO_ADDRESS or address(src_receiver) != address(executor):
            raise SigningError("Unsupported swap route: input receiver must be its nonzero executor.")
        if amount != intent.amount or min_return < intent.min_return_amount:
            raise SigningError("Swap amount or minimum return violates transaction intent.")
        if flags != 0 or (router == ROUTER_V5 and args[2] != b""):
            raise SigningError("Swap flags and embedded permits are not authorized.")
        expected_value = intent.amount if address(src) == NATIVE_TOKEN else 0
        if tx["value"] != expected_value:
            raise SigningError("Swap native value differs from transaction intent.")
    return tx
