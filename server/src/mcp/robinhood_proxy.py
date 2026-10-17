"""Expose Robinhood's live MCP tools behind the local Agent API key."""

from __future__ import annotations

import json
import re

import anyio
from jsonschema import Draft202012Validator
from mcp.types import CallToolResult, TextContent, Tool

from src.services import broker_execution
from src.shared.auth.middleware import get_request_api_key
from src.shared.logging import get_logger

PREFIX = "robinhood_mcp__"
_log = get_logger(__name__)


def _failure(code: str, message: str) -> CallToolResult:
    body = {"error": True, "code": code, "message": message}
    return CallToolResult(
        isError=True,
        structuredContent=body,
        content=[TextContent(type="text", text=json.dumps(body))],
    )


def _authenticated() -> bool:
    from src.mcp.tools import _require

    return _require(get_request_api_key())


def _audit(tool_name: str, outcome: str) -> None:
    # Never include arguments, account identifiers, or remote results.
    safe_name = tool_name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", tool_name) else "invalid"
    _log.info("robinhood.mcp.tool_call", tool_name=safe_name, outcome=outcome)


async def list_tools() -> list[Tool]:
    """Return the current Robinhood catalog, namespaced for the local MCP."""
    if not _authenticated():
        return []
    try:
        return await anyio.to_thread.run_sync(lambda: broker_execution.run(_list_from_venue))
    except Exception:
        _log.warning("robinhood.mcp.catalog_unavailable")
        return []


async def _list_from_venue(venue) -> list[Tool]:
    return [
        tool.model_copy(update={"name": PREFIX + name}, deep=True)
        for name, tool in venue.tools.items()
    ]


async def call_tool(name: str, arguments: dict | None) -> CallToolResult:
    """Validate against the live schema and forward one call exactly once."""
    if not _authenticated():
        _audit(name.removeprefix(PREFIX), "unauthorized")
        return _failure("AUTH_REQUIRED", "Authenticate to the local Agent with its X-API-Key header.")
    if not name.startswith(PREFIX):
        return _failure("UNKNOWN_TOOL", "The Robinhood MCP tool name is invalid.")

    remote_name = name.removeprefix(PREFIX)
    if not remote_name:
        return _failure("UNKNOWN_TOOL", "The Robinhood MCP tool name is invalid.")
    if not isinstance(arguments, dict):
        _audit(remote_name, "invalid_arguments")
        return _failure("VALIDATION_ERROR", "Tool arguments must be a JSON object matching the live Robinhood schema.")

    try:
        result, outcome = await anyio.to_thread.run_sync(
            lambda: broker_execution.run(_call_from_venue, remote_name, arguments)
        )
        _audit(remote_name, outcome)
        return result
    except Exception:
        _audit(remote_name, "transport_error")
        return _failure(
            "OUTCOME_UNKNOWN",
            "The Robinhood call failed. Its outcome may be unknown; check Robinhood before retrying. No automatic retry was attempted.",
        )


async def _call_from_venue(venue, remote_name: str, arguments: dict) -> tuple[CallToolResult, str]:
    tool = venue.tools.get(remote_name)
    if tool is None:
        return _failure("UNKNOWN_TOOL", "This tool is not present in Robinhood's current MCP catalog."), "unknown_tool"
    if not Draft202012Validator(tool.inputSchema).is_valid(arguments):
        return _failure(
            "VALIDATION_ERROR", "Arguments do not match Robinhood's current input schema."
        ), "invalid_arguments"

    # A single call only. In particular, do not retry order/cancel calls after
    # a timeout or lost response. broker_execution.run serializes this session
    # with managed execution so OAuth refresh-token writes cannot race.
    result = await venue.client.call_tool(remote_name, arguments)
    if not result.isError and tool.outputSchema is not None:
        output = result.structuredContent
        if output is None:
            texts = [item.text for item in result.content if item.type == "text"]
            if len(texts) == 1:
                try:
                    output = json.loads(texts[0])
                except (TypeError, ValueError):
                    output = None
        if output is None or not Draft202012Validator(tool.outputSchema).is_valid(output):
            return _failure(
                "UPSTREAM_CONTRACT_MISMATCH",
                "Robinhood's response did not match its current output schema. The action outcome may be unknown; check Robinhood before retrying.",
            ), "output_contract_mismatch"
    return result.model_copy(deep=True), "upstream_error" if result.isError else "success"
