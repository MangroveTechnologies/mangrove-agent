"""x402 payer — pay for a resource from a custodied wallet.

This is the client half of x402. The agent already *receives* payment
(`shared/x402/server.py` verifies and settles for `hello_mangrove`); this
module lets it *send* one.

Core invariant — the key is never materialised here
---------------------------------------------------
Every payment is an EIP-3009 `TransferWithAuthorization` signed by
`wallet_manager.sign_x402_authorization`, which validates the payload and
only then decrypts, signs, and discards. This module holds an address and
never a secret. In particular it does NOT read `WALLET_SECRET`, or any
other environment variable, for key material — that is precisely the
anti-pattern the four scripts in `server/scripts/` still carry and that
this service exists to replace.

What this module refuses to do, structurally
--------------------------------------------
- **Pay on a chain it was not configured for.** The scheme is registered
  for `X402_NETWORK` alone, so a Sepolia-configured agent cannot match a
  mainnet payment requirement. See `build_payment_client`.
- **Pay in anything but USDC.** The signing guard pins the verifying
  contract to known USDC for the payload's chain. A server advertising a
  different asset gets a refusal, not a signature.
- **Pay from an un-backed-up wallet.** `require_backup_confirmed` runs
  before any signature, on the same rule as live trading.
- **Settle.** Settlement is always the receiver's job. The agent settles
  only for money coming *in*, never for money going *out*.

What it does NOT do yet
-----------------------
There is no aggregate spend cap here. A signing guard sees one payload,
and this service sees one request; neither can see a running total. The
SDK's per-payment ceiling (`_MAX_AMOUNT_PER_PAYMENT`) is a backstop
against a single absurd charge, not a budget. Portfolio-wide accounting
is a separate concern in a separate layer, exactly as the portfolio kill
switch is separate from the engine's per-strategy risk gates.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils import to_checksum_address
from x402 import x402Client
from x402.http.clients.httpx import PaymentError as X402TransportError
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.types import TypedDataDomain, TypedDataField
from x402.schemas.errors import NoMatchingRequirementsError
from x402.schemas.errors import PaymentError as X402ProtocolError

from src.services import wallet_manager
from src.shared.errors import AgentError, ValidationError, X402PaymentError
from src.shared.logging import get_logger
from src.shared.x402.config import get_network, get_payer_wallet

_log = get_logger(__name__)

# Only eip155 (EVM) networks are payable. The signing guard is EVM-only by
# construction — it validates an EIP-3009 struct against a per-chain USDC
# allowlist — so accepting, say, a Solana CAIP-2 id here would register a
# scheme that could never produce a signature the guard would approve.
_NETWORK_PATTERN = re.compile(r"^eip155:\d+$")

# Values app_config yields for a key that is present but blank. `_Config`
# turns the literal strings "none"/"null" into a None, which str() then
# renders as "None" — so an unset network arrives here as text, not as a
# falsy value, and must be matched as text.
_UNSET_CONFIG_VALUES = frozenset({"", "none", "null"})

# Per-payment ceiling, set explicitly rather than inherited. The SDK applies
# DEFAULT_MAX_AMOUNT_PER_PAYMENT ($1) when spend controls are left alone;
# pinning the same number in source means a library default change cannot
# silently raise what one call is allowed to spend. This is a sanity bound
# on a single charge — priced Mangrove meters run $0.001–$0.05 — and not a
# budget. Raising it is a reviewed source change.
_MAX_AMOUNT_PER_PAYMENT = "$1"

# Generous by design. A priced compute call (a backtest) can run 50–80s, and
# the authorization stays valid for `validBefore` (+300s), so timing out
# early would abandon a payment the server may still settle.
_DEFAULT_TIMEOUT_S = 120.0

# x402 settlement receipt. Servers differ on which of the two they send, so
# read both — the agent's own server emits `x-payment-response`.
_SETTLEMENT_HEADERS = ("payment-response", "x-payment-response")


@dataclass(frozen=True)
class PaymentResult:
    """Outcome of a (possibly) paid request.

    `paid` is False for a resource that never asked for payment — a free
    endpoint is a clean no-payment path, not an error. It is also False
    when the resource errored: on REST the receiver skips settlement for
    any status >= 400, so a failed tool is not charged.
    """

    status_code: int
    body: Any
    paid: bool
    transaction: str | None = None
    network: str | None = None
    payer: str | None = None


class CustodialSigner:
    """A `ClientEvmSigner` whose key lives behind the wallet signing guard.

    Substitutes for x402's `EthAccountSigner`, which holds an unlocked
    `LocalAccount` in memory. Here the signature is produced inside
    `wallet_manager`, so the plaintext key exists only for the duration of
    one guarded call and is never an attribute of this object.
    """

    def __init__(self, wallet_address: str) -> None:
        # Checksummed once, here, so the same canonical form goes into the
        # authorization's `from` field, the guard's payer comparison and the
        # audit log. Deliberately no DB access: constructing a signer is not
        # signing, and the gates belong on the paying path.
        self._address = _checksum(wallet_address)

    @property
    def address(self) -> str:
        return self._address

    def sign_typed_data(
        self,
        domain: TypedDataDomain | dict,
        types: dict[str, list[TypedDataField | dict]],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        """Sign EIP-712 typed data through the wallet guard.

        This is the narrow waist: every payment, from any caller and any
        transport, passes through here. The backup gate therefore lives
        here too — `sign_x402_authorization` deliberately does not gate
        (matching `sign()`), so a caller that bypassed this method would
        bypass the check.

        The x402 scheme hands `TypedDataDomain` / `TypedDataField`
        dataclasses; the guard reads mappings. Converting is the whole of
        the adaptation — no field is renamed, reordered, or re-typed, so
        the struct that was validated is the struct that gets signed.
        """
        wallet_manager.require_backup_confirmed(self._address)
        return wallet_manager.sign_x402_authorization(
            domain=_domain_to_dict(domain),
            types=_types_to_dicts(types),
            primary_type=primary_type,
            message=message,
            wallet_address=self._address,
        )


def resolve_payer_wallet(wallet_address: str | None = None) -> str:
    """Pick the wallet a payment is signed with: explicit arg, then config.

    Raises rather than guessing when neither is set. Spending money is not
    a place for an ambient default, and picking, say, "the first wallet in
    the DB" would silently move funds out of trading capital.
    """
    candidate = (wallet_address or get_payer_wallet() or "").strip()
    if not candidate:
        raise ValidationError(
            "No x402 payer wallet specified.",
            suggestion=(
                "Pass wallet_address explicitly, or set X402_PAYER_WALLET in "
                "your environment config. Prefer a wallet funded only for "
                "payments and separate from trading capital, so a spend bug "
                "cannot reach allocated funds."
            ),
        )
    return _checksum(candidate)


def build_payment_client(wallet_address: str) -> x402Client:
    """Build an x402 client pinned to the configured network and wallet.

    Registration is deliberately narrow. `register_exact_evm_client()` —
    the helper the SDK documents — registers the V2 scheme under the
    `eip155:*` wildcard and, regardless of any `networks` argument,
    registers the V1 scheme for *every* legacy EVM network including Base
    mainnet. Either would let a server's payment requirement choose the
    chain. Registering the V2 scheme for one exact network instead means
    a testnet-configured agent structurally cannot pay a mainnet
    requirement: `find_schemes_by_network` finds nothing and the payment
    is refused before a signature is ever requested.
    """
    network = _require_network()
    client = x402Client()
    client.register(network, ExactEvmClientScheme(CustodialSigner(wallet_address)))
    client.set_spend_controls({"max_amount_per_payment": _MAX_AMOUNT_PER_PAYMENT})
    return client


async def pay(
    url: str,
    *,
    wallet_address: str | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> PaymentResult:
    """Request `url`, paying automatically if it answers 402.

    The transport handles the round trip: send, and on a 402 decode the
    envelope, build a payment payload (a fresh nonce every attempt — a
    nonce is burned by the receiver before verification, so a replayed
    signature always fails), and retry with the signature attached.

    Nothing here contacts the facilitator. Verification and settlement are
    the receiver's side of the protocol, so an unreachable facilitator can
    stop a payment from *completing* but cannot stop this module from
    loading or the agent from starting — the failure mode that made the
    server side degrade gracefully does not exist on the paying side.
    """
    payer = resolve_payer_wallet(wallet_address)
    # Checked eagerly as well as in the signer: an un-backed-up wallet
    # should fail before the request is sent, not after a round trip.
    wallet_manager.require_backup_confirmed(payer)

    network = _require_network()
    client = build_payment_client(payer)
    transport = x402AsyncTransport(client, transport=httpx.AsyncHTTPTransport())

    _log.info(
        "x402.payment.started",
        url=url,
        method=method,
        wallet_address=payer,
        network=network,
    )

    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
            response = await http.request(method, url, headers=headers, content=content)
    except (X402TransportError, X402ProtocolError) as e:
        # Two unrelated classes both named PaymentError: the transport
        # wraps its own failures in x402.http.clients.httpx.PaymentError,
        # while selection errors (no matching requirements, spend controls)
        # derive from x402.schemas.errors.PaymentError and are only
        # incidentally wrapped. Catching one and not the other would let a
        # protocol failure escape as a bare exception with no error shape.
        raise _translate_payment_error(e, url=url, payer=payer, network=network) from e
    except httpx.HTTPError as e:
        _log.warning("x402.payment.errored", url=url, wallet_address=payer, error=str(e))
        raise X402PaymentError(
            f"x402 payment request to {url} failed at the transport layer: {e}",
            suggestion="The resource server could not be reached. Check the URL and that the server is running; no payment was made.",
        ) from e

    settlement = _decode_settlement(response)
    result = PaymentResult(
        status_code=response.status_code,
        body=_decode_body(response),
        paid=settlement is not None,
        transaction=_str_or_none(settlement, "transaction"),
        network=_str_or_none(settlement, "network"),
        payer=_str_or_none(settlement, "payer"),
    )

    if result.paid:
        # A settlement naming a different payer means the receiver credited
        # someone else's authorization to this request. Loud, but not fatal:
        # the resource was delivered and the money has already moved.
        if result.payer and result.payer.lower() != payer.lower():
            _log.warning(
                "x402.payment.payer_mismatch",
                url=url,
                expected=payer,
                settled_payer=result.payer,
            )
        _log.info(
            "x402.payment.settled",
            url=url,
            wallet_address=payer,
            network=result.network,
            transaction=result.transaction,
            status_code=result.status_code,
        )
    elif response.status_code == 402:
        # A 402 that SURVIVED the payment attempt: the receiver looked at
        # the signature and rejected it — a burned nonce, an expired
        # validBefore, an insufficient balance. Nothing was charged, but
        # nothing was delivered either, and unlike the cases below this is
        # not routine. Logged separately so it cannot be mistaken for a
        # free resource in a log tail.
        _log.warning(
            "x402.payment.refused",
            url=url,
            wallet_address=payer,
            network=network,
        )
    else:
        # Either the resource was free, or it errored and the receiver
        # skipped settlement. Both are normal; neither is a charge.
        _log.info(
            "x402.payment.unsettled",
            url=url,
            wallet_address=payer,
            status_code=response.status_code,
        )

    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_network() -> str:
    """Resolve X402_NETWORK, refusing to fall back to anything.

    No `os.environ` read and no literal default. A payer that defaults to a
    chain spends real money the moment its guess happens to match the
    server's, so an unset or malformed value must fail loudly instead.
    """
    network = str(get_network() or "").strip()
    if network.lower() in _UNSET_CONFIG_VALUES:
        raise X402PaymentError(
            "X402_NETWORK is not configured, so there is no network to pay on.",
            suggestion="Set X402_NETWORK in this environment's config file to a CAIP-2 id such as eip155:84532 (Base Sepolia). The payer never guesses a chain.",
        )
    if not _NETWORK_PATTERN.match(network):
        raise X402PaymentError(
            f"X402_NETWORK is {network!r}, which is not an eip155 CAIP-2 network id.",
            suggestion="Use eip155:84532 (Base Sepolia) or eip155:8453 (Base mainnet). The agent pays USDC on EVM chains only.",
        )
    return network


def _checksum(address: str) -> str:
    try:
        return to_checksum_address(address)
    except (ValueError, TypeError) as e:
        raise ValidationError(
            f"{address!r} is not a valid EVM address.",
            suggestion="Payer addresses must be 0x-prefixed 20-byte hex. Check the value against `list_wallets`.",
        ) from e


def _domain_to_dict(domain: TypedDataDomain | dict) -> dict[str, Any]:
    """Normalise the EIP-712 domain to the mapping the guard validates.

    Emits exactly the four keys a USDC payment domain carries. The guard
    rejects unexpected domain keys — `eth_account` derives the
    `EIP712Domain` type from whichever keys are present, so a stray one
    would change the domain separator.
    """
    if isinstance(domain, TypedDataDomain):
        return {
            "name": domain.name,
            "version": domain.version,
            "chainId": domain.chain_id,
            "verifyingContract": domain.verifying_contract,
        }
    return domain


def _types_to_dicts(
    types: dict[str, list[TypedDataField | dict]],
) -> dict[str, list[dict[str, str]]]:
    """Convert `TypedDataField` dataclasses to `{name, type}` mappings.

    Non-field entries are passed through untouched rather than dropped:
    filtering them here would let a junk entry vanish between validation
    and signing, which is the one thing this adapter must never do.
    """
    converted: dict[str, list[dict[str, str]]] = {}
    for type_name, fields in types.items():
        converted[type_name] = [
            {"name": f.name, "type": f.type} if isinstance(f, TypedDataField) else f
            for f in fields
        ]
    return converted


def _translate_payment_error(
    error: Exception,
    *,
    url: str,
    payer: str,
    network: str,
) -> AgentError:
    """Turn the transport's wrapped failure back into a useful error.

    `x402AsyncTransport` catches everything its payment path raises and
    re-raises it as a bare `PaymentError`, which would flatten a guard
    refusal into "payment failed". Walking `__cause__` recovers the
    original: a refused envelope must stay legible as a SIGNING_ERROR,
    with the guard's own explanation intact.
    """
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, AgentError):
            return cause
        if isinstance(cause, NoMatchingRequirementsError):
            _log.warning(
                "x402.payment.errored",
                url=url,
                wallet_address=payer,
                network=network,
                error=str(cause),
            )
            return X402PaymentError(
                f"The resource at {url} offered no payment option this agent can "
                f"satisfy on {network}: {cause}",
                suggestion=f"The server is asking for a chain or an asset the agent is not configured for. Confirm X402_NETWORK ({network}) matches what the server advertises, and that the price is quoted in USDC.",
            )
        cause = cause.__cause__

    _log.warning("x402.payment.errored", url=url, wallet_address=payer, error=str(error))
    return X402PaymentError(
        f"x402 payment for {url} failed: {error}",
        suggestion="Check that the payer wallet holds enough USDC on the configured network, and that the resource server's 402 envelope is well-formed.",
    )


def _decode_settlement(response: httpx.Response) -> dict | None:
    """Decode the base64 settlement receipt, or None if there isn't one.

    Never raises. By the time this runs the resource has been delivered
    and the money has moved; an unreadable receipt costs an audit field,
    not a payment, and must not be reported as a failed call.
    """
    raw = next(
        (response.headers.get(name) for name in _SETTLEMENT_HEADERS if response.headers.get(name)),
        None,
    )
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(padded))
    except ValueError as e:
        # Covers both halves: b64decode raises binascii.Error and
        # json.loads raises JSONDecodeError, and both subclass ValueError.
        _log.warning("x402.settlement.undecodable", error=str(e))
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _str_or_none(settlement: dict | None, key: str) -> str | None:
    if not settlement:
        return None
    value = settlement.get(key)
    return value if isinstance(value, str) and value else None
