"""Shared x402 resource server -- singleton used by both HTTP middleware and MCP tools.

The same x402ResourceServer instance handles payment verification and settlement
regardless of transport (HTTP middleware or MCP tool calls).
"""
from __future__ import annotations

from x402 import ResourceConfig, x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.facilitator_client_base import CreateHeadersAuthProvider, FacilitatorConfig
from x402.http.utils import decode_payment_signature_header, encode_payment_required_header
from x402.mechanisms.evm.exact import register_exact_evm_server
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.schemas import ResourceInfo

from src.shared.logging import get_logger
from src.shared.x402.config import (
    get_cdp_api_key_id,
    get_cdp_api_key_secret,
    get_facilitator_url,
    get_network,
    get_pay_to,
)
from src.shared.x402.diagnostics import safe_error_code

_log = get_logger(__name__)


async def _log_verify_failure(context):
    error = context.error
    reason = getattr(error, "invalid_reason", None)
    if reason is None and error.args:
        reason = error.args[0]
    _log.warning("x402.receiver.verify_failed", error_type=type(error).__name__,
                 error_code=safe_error_code(reason))


async def _log_settle_failure(context):
    error = context.error
    reason = getattr(error, "error_reason", None)
    if reason is None and error.args:
        reason = error.args[0]
    _log.warning("x402.receiver.settle_failed", error_type=type(error).__name__,
                 error_code=safe_error_code(reason))


_server: x402ResourceServer | None = None
_initialized: bool = False


def _build_cdp_auth_provider() -> CreateHeadersAuthProvider | None:
    """Build auth provider for CDP facilitator if API keys are configured."""
    key_id = get_cdp_api_key_id()
    key_secret = get_cdp_api_key_secret()
    if not key_id or not key_secret:
        return None

    from urllib.parse import urlparse

    from cdp.auth import GetAuthHeadersOptions, get_auth_headers

    parsed = urlparse(get_facilitator_url())

    def create_headers():
        headers_map = {}
        for endpoint, method in [("verify", "POST"), ("settle", "POST"), ("supported", "GET")]:
            path = f"{parsed.path}/{endpoint}"
            h = get_auth_headers(GetAuthHeadersOptions(
                api_key_id=key_id,
                api_key_secret=key_secret,
                request_method=method,
                request_host=parsed.hostname,
                request_path=path,
            ))
            headers_map[endpoint] = h
        headers_map["list"] = headers_map.pop("supported")
        return headers_map

    return CreateHeadersAuthProvider(create_headers)


def get_x402_server() -> x402ResourceServer:
    """Get the shared x402ResourceServer singleton."""
    global _server
    if _server is not None:
        return _server

    facilitator_url = get_facilitator_url()
    auth_provider = _build_cdp_auth_provider()

    fc_config = FacilitatorConfig(url=facilitator_url)
    if auth_provider:
        fc_config = FacilitatorConfig(url=facilitator_url, auth_provider=auth_provider)

    facilitator = HTTPFacilitatorClient(config=fc_config)
    _server = x402ResourceServer(facilitator)
    _server.on_verify_failure(_log_verify_failure)
    _server.on_settle_failure(_log_settle_failure)
    register_exact_evm_server(_server)
    v1_scheme = ExactEvmScheme()
    _server.register("base", v1_scheme)
    _server.register("base-sepolia", v1_scheme)

    return _server


def _ensure_initialized() -> x402ResourceServer:
    """Get the server and ensure it's initialized (facilitator support fetched)."""
    global _initialized
    server = get_x402_server()
    if not _initialized:
        server.initialize()
        _initialized = True
    return server


def build_hello_mangrove_requirements() -> dict:
    """Build payment requirements for the hello_mangrove resource.

    Returns a dict suitable for JSON serialization, matching the x402 402 response format.
    """
    server = _ensure_initialized()
    network = get_network()
    pay_to = get_pay_to()

    config = ResourceConfig(
        scheme="exact",
        network=network,
        pay_to=pay_to,
        price="$0.05",
    )

    requirements = server.build_payment_requirements(config)
    payment_required = server.create_payment_required_response(
        requirements,
        ResourceInfo(
            url="mcp://hello_mangrove",
            description="hello_mangrove message — $0.05 USDC donation",
        ),
        "Payment required",
    )

    return {
        "error": True,
        "code": "PAYMENT_REQUIRED",
        "message": "This tool requires x402 payment. Pass a signed payment in the 'payment' parameter.",
        "payment_required": encode_payment_required_header(payment_required),
        "payment_required_decoded": payment_required.model_dump(by_alias=True, exclude_none=True),
    }


async def verify_and_settle_payment(payment_header: str) -> dict:
    """Verify and settle a payment from a base64-encoded payment signature.

    Returns a dict with either the settlement result or an error.
    """
    server = _ensure_initialized()
    network = get_network()
    pay_to = get_pay_to()

    # Decode the payment signature
    try:
        payload = decode_payment_signature_header(payment_header)
    except Exception as e:
        return {"error": True, "code": "INVALID_PAYMENT", "message": f"Failed to decode payment: {e}"}

    # Build requirements to verify against
    config = ResourceConfig(
        scheme="exact",
        network=network,
        pay_to=pay_to,
        price="$0.05",
    )
    requirements_list = server.build_payment_requirements(config)

    # Find matching requirements
    matching = server.find_matching_requirements(requirements_list, payload)
    if matching is None:
        return {"error": True, "code": "NO_MATCH", "message": "Payment does not match requirements"}

    # Verify
    verify_result = await server.verify_payment(payload, matching)
    if not verify_result.is_valid:
        return {
            "error": True,
            "code": "VERIFICATION_FAILED",
            "message": verify_result.invalid_reason or "Payment verification failed",
        }

    # Settle
    settle_result = await server.settle_payment(payload, matching)
    if not settle_result.success:
        return {
            "error": True,
            "code": "SETTLEMENT_FAILED",
            "message": settle_result.error_reason or "Payment settlement failed",
        }

    return {
        "verified": True,
        "settled": True,
        "transaction": settle_result.transaction,
        "network": settle_result.network,
        "payer": settle_result.payer,
    }
