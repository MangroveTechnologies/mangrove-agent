"""Synchronous, custodied x402 payments below the MangroveAI SDK.

Each request gets its own signer and payment client. The HTTP pool and session
identity can be shared across threads, but authorizations and reservations cannot.
There is one paid retry per call; another SDK attempt starts unsigned and creates
a fresh nonce. Settlement and accounting remain in the existing payer services.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Sequence

import httpx
from x402.http import x402HTTPClientSync

from src.services import spend_service, wallet_manager, x402_payer
from src.shared.errors import AgentError, ValidationError, X402PaymentError
from src.shared.logging import get_logger
from src.shared.urls import strip_query
from src.shared.x402.config import get_payer_wallet

_log = get_logger(__name__)
_FORBIDDEN_HEADERS = ("authorization", "x-api-key", "payment-signature", "x-payment")


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


class X402SyncTransport(httpx.BaseTransport):
    """Pay REST 402 challenges only at explicitly configured origins.

    Caller-supplied auth/payment headers are refused, not forwarded. In particular,
    an ambient SDK API key must never silently turn a payment call into key auth.
    HTTPS is required except for loopback development servers. Redirects to other
    origins are refused even if a caller enables httpx redirect following.

    Construction does no wallet, database, or network work. Wallet backup and
    budget checks run only after a 402, so free resources remain usable without
    a funded wallet or remaining budget.
    """

    def __init__(
        self,
        *,
        allowed_origins: Sequence[str],
        wallet_address: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        origins = set()
        for value in allowed_origins:
            url = httpx.URL(value)
            if (not url.host or url.userinfo or url.query or url.fragment
                    or (url.scheme != "https" and not (
                        url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "::1"}
                    ))):
                raise ValidationError("x402 origins require HTTPS (or HTTP loopback), without credentials, queries or fragments.")
            origins.add(_origin(url))
        if not origins:
            raise ValidationError("At least one explicit x402 origin is required.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValidationError("The x402 request timeout must be finite and positive.")
        self._origins = frozenset(origins)
        self._wallet_address = wallet_address
        self._timeout = httpx.Timeout(timeout).as_dict()
        self._session_id = str(uuid.uuid4())
        self._transport = transport if transport is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if _origin(request.url) not in self._origins or request.url.userinfo:
            raise ValidationError("Refusing x402 request outside the configured origins or with URL credentials.")
        if any(name in request.headers for name in _FORBIDDEN_HEADERS):
            raise ValidationError(
                "x402 requests cannot carry API-key credentials or an existing payment signature.",
                suggestion="Use the API-key client for key auth. For wallet payments, unset the process MANGROVE_API_KEY and restart; payment requests must start unsigned.",
            )

        # Preserve bytes, query, method and timeout across the paid retry. Never
        # mutate the SDK's request: it may be reused for its own retry later.
        request.read()
        headers = httpx.Headers(request.headers)
        headers.pop("X-Wallet-Address", None)
        headers["X-Mcp-Session-Id"] = self._session_id
        extensions = dict(request.extensions)
        # mangroveai 1.16 passes timeout=None on normal service calls, overriding
        # httpx.Client's configured timeout. Restore a finite bound here.
        requested_timeout = extensions.get("timeout") or {}
        extensions["timeout"] = {
            key: requested_timeout[key] if requested_timeout.get(key) is not None else value
            for key, value in self._timeout.items()
        }
        payer = None
        # A configured identity is useful for free calls, but do not require one
        # until payment is requested. Resolve again for every exchange, not once
        # for the lifetime of a cached SDK client.
        if self._wallet_address or get_payer_wallet():
            payer = x402_payer.resolve_payer_wallet(self._wallet_address)
            headers["X-Wallet-Address"] = payer

        def send(outgoing_headers: httpx.Headers) -> httpx.Response:
            outgoing = httpx.Request(
                request.method, request.url, headers=outgoing_headers,
                content=request.content, extensions=dict(extensions),
            )
            try:
                return self._transport.handle_request(outgoing)
            except httpx.HTTPError as error:
                _log.warning("x402.sync.transport_failed", error_type=type(error).__name__)
                raise X402PaymentError(
                    "The x402 resource request was interrupted.",
                    suggestion="Check connectivity and the payment ledger before retrying; any existing authorization remains counted because settlement may have occurred.",
                ) from None

        response = send(headers)
        if response.status_code != 402:
            return response

        # A discarded challenge must release its connection even if parsing,
        # wallet lookup, or signing fails. The final response belongs to httpx.
        try:
            response.read()
            try:
                body = response.json()
            except ValueError:
                body = None
            payer = payer or x402_payer.resolve_payer_wallet(self._wallet_address)
            wallet_manager.require_backup_confirmed(payer)
            x402_payer.check_payment_budget(str(request.url))
            signer = x402_payer.CustodialSigner(payer, resource=str(request.url))
            client = x402_payer.build_sync_payment_client(payer, signer=signer)
            http_client = x402HTTPClientSync(client)
            required = http_client.get_payment_required_response(response.headers.get, body)
            payload = client.create_payment_payload(required)
            headers["X-Wallet-Address"] = payer
            headers.update(http_client.encode_payment_signature_header(payload))
        except AgentError:
            raise
        except Exception as error:
            # Remote envelopes/errors may contain credentials or payment data.
            # Keep raw text out of logs and the public error (including causes).
            _log.warning("x402.sync.payment_failed", error_type=type(error).__name__)
            raise X402PaymentError(
                "Could not authorize the x402 payment.",
                suggestion="Check the server's payment requirements, configured network, and USDC balance.",
            ) from None
        finally:
            response.close()

        # A network failure after signing is ambiguous: retain its reservation.
        # Never silently retry that failure or claim that no money moved.
        paid_response = send(headers)
        try:
            spend_service.reconcile(
                signer.reservations,
                status_code=paid_response.status_code,
                settlement=x402_payer.decode_settlement(paid_response, payer=payer, network=x402_payer.get_network()),
                resource=strip_query(str(request.url)),
            )
        except Exception:
            paid_response.close()
            raise
        return paid_response

    def close(self) -> None:
        self._transport.close()
