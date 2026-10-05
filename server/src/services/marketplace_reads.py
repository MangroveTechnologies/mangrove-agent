"""Markets reads select API-key quota mode or the shared wallet payer."""
from __future__ import annotations

import logging
import uuid

import anyio
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from mcp import ClientSession
from src.config import app_config
from src.services import payment_operations, x402_payer
from src.services.marketplace import MarketplaceError, _url
from src.shared.clients.mangrove import _api_key
from src.shared.errors import AgentError, X402PaymentUncertain, upstream_access_error
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics

logger = logging.getLogger(__name__)


async def read(
    operation: str, arguments: dict, *, wallet_address: str | None = None,
    operation_id: str | None = None, tool: Tool | None = None,
) -> x402_payer.PaymentResult:
    """A configured upstream key selects quota mode; failures never change modes."""
    from src.services import marketplace_catalog

    if not isinstance(arguments, dict) or "payment" in arguments:
        raise MarketplaceError("Select a supported marketplace read without a payment argument.")
    key = _api_key(app_config)
    endpoint = _url(app_config.MANGROVEMARKETS_BASE_URL) + "/mcp/"
    if key is not None:
        try:
            quota_id = str(uuid.UUID(operation_id)) if operation_id is not None else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            raise MarketplaceError("Quota operation_id must be a UUID.") from None
    tool = tool or await marketplace_catalog.get_tool(operation)
    if tool.name != operation or marketplace_catalog.contract(tool)["mode"] != "read":
        raise MarketplaceError("Select a supported marketplace read without a payment argument.")
    if key is not None:
        return await _key_read(endpoint, key, operation, arguments, quota_id)
    attempt = payment_operations.PaymentAttempt(operation_id or str(uuid.uuid4()))
    token = payment_operations.current_attempt.set(attempt)
    protect_mcp_diagnostics()
    try:
        with x402_payer._private_mcp_diagnostics(), anyio.fail_after(30):
            async with httpx.AsyncClient(
                timeout=20,
                trust_env=False, follow_redirects=False,
            ) as http:
                async with streamable_http_client(endpoint, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        return await x402_payer.pay_mcp(
                            session, name=operation, arguments=arguments, resource=endpoint,
                            wallet_address=wallet_address, operation_id=attempt.operation_id,
                        )
    except Exception as error:
        pending = [error]
        known = None
        access = None
        while pending:
            child = pending.pop()
            access = access or upstream_access_error(child)
            if isinstance(child, AgentError):
                known = known or child
            if isinstance(child, BaseExceptionGroup):
                pending.extend(child.exceptions)
        if known is not None:
            raise known from None
        try:
            reservations = payment_operations.reservation_ids(attempt.operation_id)
        except Exception:
            raise X402PaymentUncertain(operation_id=attempt.operation_id) from None
        if reservations:
            raise X402PaymentUncertain(operation_id=attempt.operation_id, reservation_ids=reservations) from None
        if access is not None:
            raise access from None
        raise MarketplaceError("Markets MCP request could not be completed.") from None
    finally:
        payment_operations.current_attempt.reset(token)


async def _key_read(
    endpoint: str, key: str, operation: str, arguments: dict, operation_id: str,
) -> x402_payer.PaymentResult:
    protect_mcp_diagnostics()
    stage = "connect"
    try:
        with x402_payer._private_mcp_diagnostics(), anyio.fail_after(30):
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {key}"}, timeout=20,
                trust_env=False, follow_redirects=False,
            ) as http:
                async with streamable_http_client(endpoint, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        stage = "initialize"
                        await session.initialize()
                        stage = "call_tool"
                        result = await session.call_tool(
                            operation, arguments=arguments,
                            meta={"mangrove/quota": {"operation_id": operation_id}},
                        )
                        stage = "decode_result"
                        result.meta = {**(result.meta or {}), "mangrove/quota": {
                            **(result.meta or {}).get("mangrove/quota", {}),
                            "operation_id": operation_id,
                        }}
                        logger.info("Markets quota MCP result operation_id=%s tool=%s is_error=%s",
                                    operation_id, operation, result.isError)
                        return x402_payer.PaymentResult(
                            status_code=502 if result.isError else 200, body=result.content, paid=False,
                            mcp_result=result.model_dump(by_alias=True, exclude_none=True),
                        )
    except Exception as error:
        pending = [error]
        while pending:
            child = pending.pop()
            if not isinstance(child, BaseExceptionGroup):
                logger.warning(
                    "Markets quota MCP failed operation_id=%s tool=%s stage=%s error_type=%s",
                    operation_id, operation, stage, type(child).__name__,
                )
            access = upstream_access_error(child)
            if access is not None:
                raise access from None
            if isinstance(child, AgentError):
                raise child from None
            if isinstance(child, BaseExceptionGroup):
                pending.extend(child.exceptions)
        raise MarketplaceError(
            "Markets quota request could not be confirmed.",
            suggestion=f"Retry with operation_id {operation_id} and unchanged arguments. No wallet payment was attempted.",
        ) from None
