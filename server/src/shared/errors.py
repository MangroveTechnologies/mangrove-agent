"""Agent error hierarchy and FastAPI exception handler.

All domain errors inherit from AgentError, which carries:
- code: short uppercase identifier (e.g. WALLET_NOT_FOUND)
- message: human-readable description
- suggestion: what to do about it (optional)
- http_status: HTTP status code for REST responses
- correlation_id: UUID, auto-generated unless provided

The response shape (enforced by agent_error_handler) is:
    {
        "error": true,
        "code": "ERROR_CODE",
        "message": "...",
        "suggestion": "...",   # null if not provided
        "correlation_id": "uuid"
    }

MCP tools return the same shape as JSON text (no HTTP status).

See docs/specification.md Error Handling section for the full table.
"""
from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


class AgentError(Exception):
    """Base class for all domain errors in mangrove-agent.

    Subclasses set class-level `code` and `http_status`. Instances carry a
    human-readable `message`, an optional `suggestion`, and a `correlation_id`
    (auto-generated UUID unless provided).
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        suggestion: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        # Inherit the request-scoped correlation_id bound by
        # CorrelationIdMiddleware, so the error body and the
        # X-Correlation-Id response header always agree. Fall back to
        # a fresh UUID when raised outside any request (e.g. scheduler
        # tick started from APScheduler — has its own id bound via
        # with_correlation_id()).
        if correlation_id is None:
            from src.shared.logging import _correlation_id_var
            correlation_id = _correlation_id_var.get() or str(uuid.uuid4())
        self.correlation_id = correlation_id

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
            "correlation_id": self.correlation_id,
        }


# -- Auth ----------------------------------------------------------------


class AuthMissingApiKey(AgentError):
    code = "AUTH_MISSING_API_KEY"
    http_status = 401


class AuthInvalidApiKey(AgentError):
    code = "AUTH_INVALID_API_KEY"
    http_status = 401


# -- Validation / confirmation ------------------------------------------


class ValidationError(AgentError):
    code = "VALIDATION_ERROR"
    http_status = 400


class ConfirmationRequired(AgentError):
    code = "CONFIRMATION_REQUIRED"
    http_status = 400


# -- Wallet -------------------------------------------------------------


class WalletNotFound(AgentError):
    code = "WALLET_NOT_FOUND"
    http_status = 404


class WalletAlreadyExists(AgentError):
    code = "WALLET_ALREADY_EXISTS"
    http_status = 409


# -- Strategy -----------------------------------------------------------


class StrategyNotFound(AgentError):
    code = "STRATEGY_NOT_FOUND"
    http_status = 404


class StrategyInvalidStatusTransition(AgentError):
    code = "STRATEGY_INVALID_STATUS_TRANSITION"
    http_status = 400


class StrategyInvalidComposition(AgentError):
    code = "STRATEGY_INVALID_COMPOSITION"
    http_status = 400


class StrategyNoViableCandidates(AgentError):
    code = "STRATEGY_NO_VIABLE_CANDIDATES"
    http_status = 422


class AllocationInsufficient(AgentError):
    code = "ALLOCATION_INSUFFICIENT"
    http_status = 400


# -- Knowledge / market data -------------------------------------------


class InsufficientData(AgentError):
    code = "INSUFFICIENT_DATA"
    http_status = 422


# -- External / internal -----------------------------------------------


class CexCredentialsMissing(AgentError):
    """A local exchange account must be connected before using its tools."""

    code = "CEX_CREDENTIALS_MISSING"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("No Kraken account is connected on this agent.")


class SdkError(AgentError):
    code = "SDK_ERROR"
    http_status = 502


_DENIAL_REASONS = frozenset({"insufficient_scope", "insufficient_role", "not_org_member", "unmapped_tool"})
_DENIAL_SCOPES = frozenset("""
strategy:create strategy:read strategy:update_status strategy:delete
backtest:create backtest:read signal:read signal:evaluate position:read position:create
execution:read execution:write org:read org:update org:invite org:manage_members
subscription:read subscription:manage admin:read admin:manage oracle:backtest oracle:sweep
data:query service:delegate strategies:read strategies:write strategies:archive
strategies:evaluate backtests:read backtests:run sweeps:read sweeps:run
signals:read signals:evaluate positions:read data:read
""".split())


def safe_denial_details(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if not isinstance(error, dict):
        return {}
    data = error.get("data")
    if (not isinstance(data, dict) or not isinstance(data.get("code"), str)
            or data["code"] not in _DENIAL_REASONS):
        return {}
    result = {"reason": data["code"]}
    for field in ("required_scopes", "missing_scopes"):
        values = data.get(field)
        if (isinstance(values, list) and len(values) <= 64
                and all(isinstance(v, str) and v in _DENIAL_SCOPES for v in values)):
            result[field] = sorted(set(values))
    return result


class UpstreamAccessError(SdkError):
    """Sanitized upstream access denial, distinct from local auth and spend caps."""

    def __init__(self, status: int, *, details: dict | None = None) -> None:
        if status not in (401, 403):
            raise ValueError("UpstreamAccessError requires 401 or 403")
        self.details = details or {}
        self.http_status = status
        self.upstream_status = status
        self.code = "UPSTREAM_AUTHENTICATION_FAILED" if status == 401 else "UPSTREAM_ACCESS_DENIED"
        message = (
            "The upstream service rejected the configured credential (HTTP 401)."
            if status == 401 else
            "The upstream service denied access to the requested operation (HTTP 403)."
        )
        reason = self.details.get("reason")
        missing = self.details.get("missing_scopes", [])
        if reason == "insufficient_scope" and missing:
            message = "Access denied: the current connection lacks permissions: " + ", ".join(missing) + "."
        elif reason == "insufficient_role":
            message = "Access denied: your organization role does not allow this operation."
        elif reason == "not_org_member":
            message = "Access denied: this account is not a member of the requested organization."
        elif reason == "unmapped_tool":
            message = "This tool is not available through this credential's authorization rules."
        super().__init__(message, suggestion=(
            "Briefly state the failed action, the access error and its supported reason, then stop. "
            "Do not append questions, retry offers or offers of technical details. "
            + ("Explain that the configured upstream credential was rejected. "
               if status == 401 else
               "Use the structured denial reason and missing scopes when provided; otherwise say the server did not identify the specific permission. ")
            + "Do not inspect local files, logs or configuration, print credentials, retry with another key, "
              "or switch to wallet payment. Troubleshoot only if the user explicitly requests it."
        ))

    def to_dict(self) -> dict:
        return {**super().to_dict(), **self.details, "upstream_status": self.upstream_status,
                "retryable": False, "retry_payment": False}


def upstream_access_error(error: Exception) -> UpstreamAccessError | None:
    """Recognize typed access failures, including explicitly chained SDK wrappers.

    Never infer status from message text or arbitrary attributes. Preserve domain
    errors (especially payment uncertainty and spend caps), and follow only SDK
    wrappers' explicit causes, with cycle protection.
    """
    from httpx import HTTPStatusError, ResponseNotRead
    from mangrove_ai.exceptions import APIError as AIAPIError
    from mangrove_markets.exceptions import APIError as MarketsAPIError
    from mcp.shared.exceptions import McpError

    correlation_id = getattr(error, "correlation_id", None)
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, UpstreamAccessError):
            return current
        if type(current) is SdkError:
            current = current.__cause__
            continue
        payload = None
        if isinstance(current, (AIAPIError, MarketsAPIError)):
            status = current.status_code
            if isinstance(current, MarketsAPIError):
                payload = current.response_body
            if payload is None and isinstance(current.code, str):
                payload = {"error": {"data": {"code": current.code.lower()}}}
        elif isinstance(current, HTTPStatusError):
            status = current.response.status_code
            if status == 403:
                try:
                    if len(current.response.content) <= 16384:
                        payload = current.response.json()
                except (ValueError, UnicodeError, RecursionError, ResponseNotRead):
                    pass
        elif isinstance(current, McpError) and current.error.code in {-32010, -32012}:
            status = 401 if current.error.code == -32010 else 403
            payload = {"error": {"data": current.error.data}}
        else:
            return None
        if status not in (401, 403):
            return None
        details = safe_denial_details(payload) if status == 403 else {}
        if status == 403 and isinstance(current, HTTPStatusError):
            captured = current.response.extensions.get("mangrove_denial")
            if isinstance(captured, dict):
                details = safe_denial_details({"error": {"data": {
                    **captured, "code": captured.get("reason"),
                }}}) or details
        result = UpstreamAccessError(status, details=details)
        if correlation_id is not None:
            result.correlation_id = correlation_id
        return result
    return None


class SigningError(AgentError):
    code = "SIGNING_ERROR"
    http_status = 500


class X402PaymentError(AgentError):
    """An outbound x402 payment could not be completed.

    Covers the payment protocol itself — no matching requirements, a
    transport failure, a 402 that never cleared. It deliberately does NOT
    cover a refused payload: when the wallet signing guard rejects an
    envelope the original SigningError is re-raised, so an attempted
    forgery stays legible as SIGNING_ERROR rather than being flattened
    into a generic payment failure.

    502, not 500: the failure is in the exchange with an upstream resource
    server, matching SdkError.
    """

    code = "X402_PAYMENT_ERROR"
    http_status = 502

    def to_dict(self) -> dict:
        return {**super().to_dict(), "retry_payment": False}


class X402PaymentUncertain(X402PaymentError):
    """Uncertain payment: retain its amount and forbid automatic retries."""

    code = "X402_PAYMENT_UNCERTAIN"
    http_status = 409

    def __init__(self, *, reservation_ids=(), upstream_status=None, correlation_id=None, upstream_error=None,
                 operation_id=None, pause_until=None):
        super().__init__(
            "This operation's payment outcome is unresolved; its amount remains reserved.",
            suggestion="A new user request can proceed within the remaining budget, including a fresh call to the same tool. For a fresh request omit the old operation ID; for recovery reuse it. Never turn an automatic recovery into a new payment. The original payment remains tracked by background reconciliation.",
            correlation_id=correlation_id,
        )
        self.reservation_ids = list(reservation_ids)
        self.upstream_status = upstream_status
        self.upstream_error = upstream_error
        self.operation_id = operation_id
        self.payment_state = "unresolved"
        self.pause_until = None

    @property
    def payment_state(self):
        return self._payment_state

    @payment_state.setter
    def payment_state(self, value):
        if value not in {"settled", "unresolved", "released"}:
            raise ValueError("Invalid payment state")
        self._payment_state = value
        if value == "settled":
            self.message = "Payment is confirmed, but this operation's result is not yet available."
            self.suggestion = "Recover the original operation using its operation ID. Do not authorize another payment for this request. Independent requests may proceed within the remaining budget."
        elif value == "released":
            self.message = "The old payment expired unused or was cancelled and its reservation was released, but the original recovery proof is missing."
            self.suggestion = "Do not repeat this recovery attempt. Operator review must confirm the server did not execute the request before retiring it. Independent requests may proceed within the existing budget."
        else:
            self.message = "This operation's payment outcome is unresolved; its amount remains reserved."
        self.args = (self.message,)

    def to_dict(self) -> dict:
        return {**super().to_dict(), "retry_payment": False,
                "payment_state": self.payment_state, "reservation_ids": self.reservation_ids,
                "operation_id": self.operation_id, "pause_until": None,
                "upstream_status": self.upstream_status, "upstream_error": self.upstream_error}


class X402SpendCapExceeded(AgentError):
    """The agent refused to make an outbound x402 payment on budget grounds.

    Either the aggregate spend cap is latched, or this one payment would
    take total spending past it. Distinct from X402PaymentError: nothing
    failed and nothing was signed — the agent declined to spend.

    403, not 402: 402 is what a server sends when IT wants to be paid, and
    reusing it for the opposite direction would make the agent's own refusal
    indistinguishable from an upstream price quote in a log or a client.
    """

    code = "X402_SPEND_CAP_EXCEEDED"
    http_status = 403


class EvaluationError(AgentError):
    code = "EVALUATION_ERROR"
    http_status = 500


class SchedulerError(AgentError):
    code = "SCHEDULER_ERROR"
    http_status = 500


class ChainNotSupportedInV1(AgentError):
    code = "CHAIN_NOT_SUPPORTED_IN_V1"
    http_status = 501


class InternalError(AgentError):
    code = "INTERNAL_ERROR"
    http_status = 500


# -- FastAPI handler ---------------------------------------------------


async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
    """FastAPI exception handler for AgentError and subclasses.

    Returns the standard error response shape with the exception's http_status.
    Register via `app.add_exception_handler(AgentError, agent_error_handler)`.

    Logs a structured event with level=error. The event name is the error code
    lowercased (e.g. `wallet.not_found`), so tailers can grep per-class.
    """
    # Lazy import so errors.py stays usable by tests that don't configure logging.
    from src.shared.logging import get_logger

    exc = upstream_access_error(exc) or exc
    event_name = exc.code.lower().replace("_", ".")
    get_logger(__name__).error(
        event_name,
        code=exc.code,
        message=exc.message,
        suggestion=exc.suggestion,
        correlation_id=exc.correlation_id,
        path=str(request.url.path),
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_dict(),
    )
