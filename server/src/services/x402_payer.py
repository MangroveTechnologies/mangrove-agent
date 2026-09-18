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
anti-pattern this service replaces, including in the payment demo scripts.

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

- **Spend past the agent's budget.** Every signature is reserved against
  `spend_service` first, so the running total is charged before the
  authorization exists rather than after a receipt comes back. See
  `CustodialSigner.sign_typed_data`.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils import to_checksum_address
from x402 import x402Client, x402ClientSync
from x402.http.clients.httpx import PaymentError as X402TransportError
from x402.http.clients.httpx import x402AsyncTransport
from x402.mcp import MCP_PAYMENT_RESPONSE_META_KEY, x402MCPSession
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.evm.types import TypedDataDomain, TypedDataField
from x402.schemas.errors import NoMatchingRequirementsError
from x402.schemas.errors import PaymentError as X402ProtocolError

from src.services import spend_service, wallet_manager
from src.shared.errors import AgentError, ValidationError, X402PaymentError, X402SpendCapExceeded
from src.shared.logging import get_logger
from src.shared.urls import strip_query
from src.shared.x402.config import get_network, get_payer_wallet
from src.shared.x402.receipts import valid_settlement

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

# The only EIP-712 struct the wallet guard will sign, and so the only one
# the spend cap knows how to price. Kept as a local constant rather than
# imported from wallet_manager: this module states what it can budget, the
# guard states what it can sign, and a test asserts the two still agree.
_EIP3009_PRIMARY_TYPE = "TransferWithAuthorization"

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
    when there is no validated settlement receipt. An HTTP failure may
    still have settled; paid=False does not prove that no funds moved.
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

    def __init__(self, wallet_address: str, *, resource: str | None = None) -> None:
        # Checksummed once, here, so the same canonical form goes into the
        # authorization's `from` field, the guard's payer comparison and the
        # audit log. Deliberately no DB access: constructing a signer is not
        # signing, and the gates belong on the paying path.
        self._address = _checksum(wallet_address)
        # What is being paid for, recorded on the ledger row purely for
        # audit. The signer never sees a URL otherwise — the scheme hands it
        # a struct, not a request.
        self._resource = resource
        # Budget reservations this signer has taken out, oldest first. The
        # caller that drove the request reconciles them once it knows the
        # outcome; see `pay`. Held here because the signer is the only
        # object that exists on both sides of the transport's payment loop.
        self._reservations: list[str] = []

    @property
    def address(self) -> str:
        return self._address

    @property
    def reservations(self) -> tuple[str, ...]:
        """Budget reservations taken out for this exchange, oldest first."""
        return tuple(self._reservations)

    def sign_typed_data(
        self,
        domain: TypedDataDomain | dict,
        types: dict[str, list[TypedDataField | dict]],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        """Sign EIP-712 typed data through the wallet guard.

        This is the narrow waist: every payment, from any caller and any
        transport, passes through here. Two gates therefore live here
        rather than in the layer above, because a caller that bypassed
        this method would bypass them:

        - **Backup confirmation.** `sign_x402_authorization` deliberately
          does not gate (matching `sign()`).
        - **The spend cap.** Budget is claimed BEFORE the signature is
          produced, and claimed against the value in the struct about to
          be signed — not against a price quoted earlier in the exchange,
          which is a different number that nobody has checked. A
          signature that is never taken out of the agent still authorizes
          a debit, so "signed" is the honest moment to charge a budget.

        The x402 scheme hands `TypedDataDomain` / `TypedDataField`
        dataclasses; the guard reads mappings. Converting is the whole of
        the adaptation — no field is renamed, reordered, or re-typed, so
        the struct that was validated is the struct that gets signed.
        """
        wallet_manager.require_backup_confirmed(self._address)

        domain_dict = _domain_to_dict(domain)
        reservation = self._reserve_budget(primary_type, domain_dict, message)

        try:
            return wallet_manager.sign_x402_authorization(
                domain=domain_dict,
                types=_types_to_dicts(types),
                primary_type=primary_type,
                message=message,
                wallet_address=self._address,
            )
        except Exception:
            # No signature exists, so no debit can ever be presented — the
            # one case where giving budget back is provably free. Released
            # here rather than by the caller because a guard refusal
            # travels up as an exception and may never reach `pay`'s
            # reconciliation at all.
            if reservation is not None:
                spend_service.release_unsigned(reservation)
                self._reservations.remove(reservation)
            raise

    def _reserve_budget(
        self, primary_type: str, domain: dict, message: dict[str, Any]
    ) -> str | None:
        """Claim budget for an EIP-3009 authorization, or decline to price it.

        Only `TransferWithAuthorization` carries a `value` the agent can
        read as an amount. Anything else is left entirely to the guard,
        which permits exactly this one struct and refuses the rest — so
        an unpriceable payload is unsignable a moment later regardless.

        The alternative, refusing here because no amount could be found,
        would quietly move the decision about WHICH STRUCTS MAY BE SIGNED
        out of the signing guard and into a budget check. That is the one
        place in this path where the reasoning has to stay in one piece, so
        `test_budgeted_struct_matches_what_the_guard_will_sign` fails if the
        guard is ever widened without revisiting this.
        """
        if primary_type != _EIP3009_PRIMARY_TYPE:
            return None
        fields = message if isinstance(message, Mapping) else {}
        reservation = spend_service.reserve(
            value=fields.get("value"),
            wallet_address=self._address,
            payee=_str_or_none_value(fields.get("to")),
            network=_network_from_domain(domain),
            resource=self._resource,
            # Audit metadata: the instant after which USDC rejects this
            # authorization, so a row can later be proven dead rather than
            # assumed so. See the note in spend_service on why nothing acts
            # on it yet.
            valid_before=fields.get("validBefore"),
            valid_after=fields.get("validAfter"),
            authorization_nonce=fields.get("nonce"),
            asset=domain.get("verifyingContract"),
        )
        self._reservations.append(reservation)
        return reservation


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


def build_payment_client(
    wallet_address: str,
    *,
    resource: str | None = None,
    signer: CustodialSigner | None = None,
) -> x402Client:
    """Build an x402 client pinned to the configured network and wallet.

    Pass `signer` when the caller needs to reconcile the budget
    reservations afterwards — the signer is where they accumulate, and a
    client built without one keeps its signer private. `resource` is
    audit metadata for the ledger and is ignored if `signer` is given.

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
    client = x402Client()
    _configure_payment_client(client, wallet_address, resource=resource, signer=signer)
    return client


def build_sync_payment_client(
    wallet_address: str,
    *,
    resource: str | None = None,
    signer: CustodialSigner | None = None,
) -> x402ClientSync:
    """Sync counterpart with the same network pin, guard and spending controls."""
    client = x402ClientSync()
    _configure_payment_client(client, wallet_address, resource=resource, signer=signer)
    return client


def _configure_payment_client(
    client: x402Client | x402ClientSync,
    wallet_address: str,
    *,
    resource: str | None,
    signer: CustodialSigner | None,
) -> None:
    network = _require_network()
    signer = signer or CustodialSigner(wallet_address, resource=resource)
    client.register(network, ExactEvmClientScheme(signer))
    client.set_spend_controls({"max_amount_per_payment": _MAX_AMOUNT_PER_PAYMENT})


async def pay_mcp(
    session: Any,
    *,
    wallet_address: str | None = None,
    name: str = "hello_mangrove",
    resource: str,
) -> PaymentResult:
    """Pay a local demo MCP tool through the same custody and ledger controls.

    The caller owns the MCP connection, its deadline and cleanup. The SDK makes
    one unsigned call and at most one signed retry. Its ``payment_made`` flag
    means a payload was sent, NOT that settlement succeeded. Only a validated
    receipt reconciles the final reservation; every uncertain signature remains
    counted, including cancellation and errors during the paid retry.
    """
    payer = resolve_payer_wallet(wallet_address)
    wallet_manager.require_backup_confirmed(payer)
    check_payment_budget(resource)
    network = _require_network()
    signer = CustodialSigner(payer, resource=resource)
    client = build_payment_client(payer, signer=signer)
    try:
        paid_session = x402MCPSession(session, client, auto_payment=True)
        await paid_session.initialize()
        response = await paid_session.call_tool(name, {})
        # Validate the wire dictionary, before SDK/Pydantic type coercions
        # (for example, the string "true" must not become a valid boolean).
        metadata = response.raw_result.meta
        receipt = metadata.get(MCP_PAYMENT_RESPONSE_META_KEY) if isinstance(metadata, dict) else None
        settlement = None
        if signer.reservations and valid_settlement(receipt, payer=payer, network=network):
            settlement = {key: receipt[key] for key in ("success", "transaction", "payer", "network")}
        result = PaymentResult(
            status_code=502 if response.is_error else 200,
            body=response.content,
            paid=settlement is not None,
            transaction=_str_or_none(settlement, "transaction"),
            network=_str_or_none(settlement, "network"),
            payer=_str_or_none(settlement, "payer"),
        )
        _reconcile_budget(signer, result, settlement=settlement, url=resource)
        return result
    except AgentError:
        raise
    except Exception:
        # MCP/HTTP exceptions may contain arbitrary remote content. Neither
        # report that text nor retry/release an authorization after failure.
        raise X402PaymentError(
            "The MCP payment could not be completed.",
            suggestion="Check the payment ledger before retrying; any signed authorization remains counted.",
        ) from None


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
    envelope, build a fresh payment authorization and retry with its signature.
    Earlier signatures are never reused or assumed canceled by a server cache.

    Nothing here contacts the facilitator. Verification and settlement are
    the receiver's side of the protocol, so an unreachable facilitator can
    stop a payment from *completing* but cannot stop this module from
    loading or the agent from starting — the failure mode that made the
    server side degrade gracefully does not exist on the paying side.
    """
    if any(name.lower() in {"authorization", "x-api-key", "payment-signature", "x-payment"}
           for name in (headers or {})) or httpx.URL(url).userinfo:
        raise ValidationError(
            "x402 requests cannot carry credentials or an existing payment signature.",
            suggestion="Use the API-key client for key auth; payment requests must start unsigned.",
        )
    # Stripped once here so every log line, every error message and the
    # ledger row all record the same, credential-free form. A URL that is
    # safe in one of those places and raw in another is not sanitised, it
    # is inconsistently sanitised.
    safe_url = _safe_url(url)
    payer = resolve_payer_wallet(wallet_address)
    # Checked eagerly as well as in the signer: an un-backed-up wallet
    # should fail before the request is sent, not after a round trip.
    wallet_manager.require_backup_confirmed(payer)
    check_payment_budget(url)

    network = _require_network()
    signer = CustodialSigner(payer, resource=url)
    client = build_payment_client(payer, signer=signer)
    transport = x402AsyncTransport(client, transport=httpx.AsyncHTTPTransport(trust_env=False))

    _log.info(
        "x402.payment.started",
        url=safe_url,
        method=method,
        wallet_address=payer,
        network=network,
    )

    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, trust_env=False, follow_redirects=False,
        ) as http:
            response = await http.request(method, url, headers=headers, content=content)
    except (X402TransportError, X402ProtocolError) as e:
        # Two unrelated classes both named PaymentError: the transport
        # wraps its own failures in x402.http.clients.httpx.PaymentError,
        # while selection errors (no matching requirements, spend controls)
        # derive from x402.schemas.errors.PaymentError and are only
        # incidentally wrapped. Catching one and not the other would let a
        # protocol failure escape as a bare exception with no error shape.
        raise _translate_payment_error(e, safe_url=safe_url, payer=payer, network=network) from None
    except httpx.HTTPError as e:
        # Reservations are deliberately NOT released here. A connection that
        # dropped after the signed retry went out may still have been
        # received and settled; only the receiver knows. An unreleased
        # reservation costs part of a budget, a wrongly released one costs
        # money that never appears in the total.
        _log.warning("x402.payment.errored", url=safe_url, wallet_address=payer, error_type=type(e).__name__)
        raise X402PaymentError(
            f"x402 payment request to {safe_url} was interrupted.",
            suggestion="Check connectivity and the payment ledger before retrying; an existing authorization may have settled and remains counted.",
        ) from None

    settlement = decode_settlement(response, payer=payer, network=network) if signer.reservations else None
    result = PaymentResult(
        status_code=response.status_code,
        body=_decode_body(response),
        paid=settlement is not None,
        transaction=_str_or_none(settlement, "transaction"),
        network=_str_or_none(settlement, "network"),
        payer=_str_or_none(settlement, "payer"),
    )

    _reconcile_budget(signer, result, settlement=settlement, url=safe_url)

    if result.paid:
        # A settlement naming a different payer means the receiver credited
        # someone else's authorization to this request. Loud, but not fatal:
        # the resource was delivered and the money has already moved.
        if result.payer and result.payer.lower() != payer.lower():
            _log.warning(
                "x402.payment.payer_mismatch",
                url=safe_url,
                expected=payer,
                settled_payer=result.payer,
            )
        _log.info(
            "x402.payment.settled",
            url=safe_url,
            wallet_address=payer,
            network=result.network,
            transaction=result.transaction,
            status_code=result.status_code,
        )
    elif response.status_code == 402:
        # Rejection is an HTTP outcome, not proof of on-chain cancellation.
        _log.warning(
            "x402.payment.refused",
            url=safe_url,
            wallet_address=payer,
            network=network,
        )
    else:
        # No validated receipt: free or uncertain. Retain any authorization.
        _log.info(
            "x402.payment.unsettled",
            url=safe_url,
            wallet_address=payer,
            status_code=response.status_code,
        )

    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe_url(url: str) -> str:
    """A URL fit to log or put in an error message.

    Same rule the ledger applies before persisting a resource. Logs are
    just as durable as the database and error messages travel further
    still -- into the conversation transcript -- so the stripping has to
    happen on every path or it is decoration on one of them.
    """
    return strip_query(url) or "[invalid URL]"


def _safe_error(error: Exception, url: str) -> str:
    """Exception text with the request URL reduced to its safe form.

    httpx and x402 both interpolate the request URL into their messages, so
    an error string can reintroduce a query string that every other path
    has just stripped. Substring replacement rather than re-parsing: the
    library decides how it formats the URL, and the only thing that has to
    be true is that the raw form does not survive.
    """
    text = str(error)
    safe = _safe_url(url)
    return text.replace(url, safe) if url != safe else text


def check_payment_budget(url: str) -> None:
    """Refuse an exhausted budget before a request is ever sent.

    The binding check happens in the signer, which is the only place the
    amount is known. This one is a courtesy: when the budget is already
    gone, the answer does not depend on the price, so there is no reason
    to make a round trip to find it out.
    """
    budget = spend_service.check_before_payment()
    if budget["allowed"]:
        return
    _log.warning(
        "x402.payment.over_budget",
        url=_safe_url(url),
        reason=budget.get("reason"),
        spent_usd=budget.get("spent_usd"),
        cap_usd=budget.get("cap_usd"),
    )
    raise X402SpendCapExceeded(
        f"Not requesting {_safe_url(url)}: {budget.get('reason')}.",
        suggestion=spend_service.TOP_UP_SUGGESTION,
    )


def _reconcile_budget(
    signer: CustodialSigner,
    result: PaymentResult,
    *,
    settlement: dict | None,
    url: str,
) -> None:
    """Hand this exchange's outcome to the ledger.

    Deliberately thin. The outcome-to-state rules live in `spend_service`
    so that every payment driver -- this one, and any transport that
    injects payment into an SDK client -- agrees on what a response means
    and none of them has to re-derive it.
    """
    spend_service.reconcile(
        signer.reservations,
        status_code=result.status_code,
        settlement=settlement,
        resource=url,
    )


def _network_from_domain(domain: dict) -> str | None:
    """CAIP-2 id of the chain in the payload, for the ledger row.

    Taken from the struct being signed rather than from `X402_NETWORK`, so
    the ledger records the chain the authorization actually named. The two
    agree in practice — the client is registered for one network — but an
    audit trail that copies configuration is not an audit trail.

    Returns None rather than raising on anything unexpected: this runs
    before the guard, on input shaped by a remote server, and a missing
    audit field must not pre-empt the guard's own refusal.
    """
    if not isinstance(domain, Mapping):
        return None
    chain_id = domain.get("chainId")
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        return None
    return f"eip155:{chain_id}"


def _str_or_none_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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
    safe_url: str,
    payer: str,
    network: str,
) -> AgentError:
    """Turn the transport's wrapped failure back into a useful error.

    Takes `safe_url`, never a raw one: everything this builds ends up in a
    log line or an error message shown to a user, and both are places a
    query string must not reach.

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
                url=safe_url,
                wallet_address=payer,
                network=network,
                error_type=type(cause).__name__,
            )
            return X402PaymentError(
                f"The resource at {safe_url} offered no payment option this agent can "
                f"satisfy on {network}.",
                suggestion=f"The server is asking for a chain or an asset the agent is not configured for. Confirm X402_NETWORK ({network}) matches what the server advertises, and that the price is quoted in USDC.",
            )
        cause = cause.__cause__

    _log.warning("x402.payment.errored", url=safe_url, wallet_address=payer,
                 error_type=type(error).__name__)
    return X402PaymentError(
        f"x402 payment for {safe_url} could not be authorized.",
        suggestion="Check that the payer wallet holds enough USDC on the configured network, and that the resource server's 402 envelope is well-formed.",
    )


def decode_settlement(response: httpx.Response, *, payer: str | None = None,
                      network: str | None = None) -> dict | None:
    """Return a validated server receipt, or None for an uncertain outcome."""
    raw = next((response.headers.get(name) for name in _SETTLEMENT_HEADERS
                if response.headers.get(name)), None)
    if not raw or len(raw) > 16384:
        return None
    try:
        decoded = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True))
    except (ValueError, RecursionError):
        _log.warning("x402.settlement.undecodable")
        return None
    if not valid_settlement(decoded, payer=payer, network=network):
        _log.warning("x402.settlement.invalid")
        return None
    return {key: decoded[key] for key in ("success", "transaction", "payer", "network")}


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
