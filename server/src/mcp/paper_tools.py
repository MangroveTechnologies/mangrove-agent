"""Scheduling controls; strategy evaluation remains on MangroveAI."""

import json

import anyio
from mcp.types import ToolAnnotations

from src.services import paper_schedule_service as paper


def register(server):
    from src.mcp.tools import _auth_error, _handle_agent_error, _require

    async def call(api_key, operation, *args):
        if not _require(api_key):
            return _auth_error()
        try:
            result = await anyio.to_thread.run_sync(lambda: operation(*args))
            return json.dumps(result)
        except Exception as error:
            return _handle_agent_error(error)

    @server.tool(
        title="Start paper evaluation schedule",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_start_paper_schedule(
        strategy_id: str, interval_seconds: int, max_evaluations: int, confirm: bool = False, api_key: str = ""
    ) -> str:
        """Schedule bounded server-owned paper evaluations of an existing MangroveAI strategy.

        Requires explicit confirmation of recurring API/x402 costs. Agent must stay running.
        No live execution. v3 is unsupported. Does not create or copy strategy definitions.
        """
        return await call(api_key, paper.start, strategy_id, interval_seconds, max_evaluations, confirm)

    @server.tool(
        title="List local paper schedules",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def agent_list_paper_schedules(api_key: str = "") -> str:
        """List local schedule IDs and cached progress for the current upstream identity; no upstream call."""
        return await call(api_key, paper.list_schedules)

    @server.tool(
        title="Pause paper evaluation schedule",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_pause_paper_schedule(schedule_id: str, api_key: str = "") -> str:
        """Stop new local requests and confirm server pause. Does not close paper positions."""
        return await call(api_key, paper.pause, schedule_id)

    @server.tool(
        title="Read paper schedule status",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def agent_get_paper_schedule(schedule_id: str, refresh: bool = False, api_key: str = "") -> str:
        """Read local scheduling status. refresh fetches authoritative server state and may cost API/x402 usage."""
        return await call(api_key, paper.inspect, schedule_id, refresh)

    @server.tool(
        title="Reconcile unresolved paper evaluation",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
    )
    async def agent_reconcile_paper_schedule(schedule_id: str, api_key: str = "") -> str:
        """Recover an unresolved evaluation using the original tick and payment operation identity."""
        return await call(api_key, paper.reconcile, schedule_id)

    from src.mcp.registry import ToolEntry, ToolParam, register_tool
    from src.mcp.tools import _APIKEY

    schedule = ToolParam(name="schedule_id", type="string", required=True)
    entries = [
        ("agent_list_paper_schedules", []),
        (
            "agent_start_paper_schedule",
            [
                ToolParam(name="strategy_id", type="string", required=True),
                ToolParam(name="interval_seconds", type="integer", required=True),
                ToolParam(name="max_evaluations", type="integer", required=True),
                ToolParam(name="confirm", type="boolean", required=False),
            ],
        ),
        ("agent_pause_paper_schedule", [schedule]),
        ("agent_get_paper_schedule", [schedule, ToolParam(name="refresh", type="boolean", required=False)]),
        ("agent_reconcile_paper_schedule", [schedule]),
    ]
    for name, parameters in entries:
        register_tool(
            ToolEntry(
                name=name,
                description="Local paper scheduling; upstream evaluation/control calls may incur API/x402 usage.",
                access="auth",
                parameters=[*parameters, _APIKEY],
            )
        )
