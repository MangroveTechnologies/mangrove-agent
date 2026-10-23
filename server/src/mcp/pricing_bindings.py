"""Agent tools -> upstream billing definitions. No prices live here.

Namespaces distinguish REST decorators, delegated skills and Oracle proxies.
Bindings describe the actual SDK path, not similarly named upstream MCP tools.
An empty binding explicitly means that a reliable price is not published here
(notably KB calls use a separate host). Missing prices must never imply free.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceBinding:
    meters: tuple[str, ...]
    variable: bool = False
    alternatives: tuple[str, ...] = ()


TOOL_PRICING: dict[str, PriceBinding] = {
    "agent_start_paper_schedule": PriceBinding(("rest:strategy_status_update", "rest:managers_evaluate_by_id"), variable=True),
    "agent_pause_paper_schedule": PriceBinding(("rest:strategy_status_update",)),
    "agent_get_paper_schedule": PriceBinding(("rest:strategy_detail",), variable=True),
    "agent_reconcile_paper_schedule": PriceBinding(("rest:managers_evaluate_by_id",), variable=True),
}
