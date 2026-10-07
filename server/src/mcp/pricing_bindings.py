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
    "agent_create_strategy_autonomous": PriceBinding(("rest:signals_list", "rest:strategy_list_or_create", "rest:backtest_async_submit", "rest:backtest_async_status"), variable=True),
    "agent_create_strategy_manual": PriceBinding(("rest:strategy_list_or_create",)),
    "agent_update_strategy_status": PriceBinding(("rest:strategy_status_update",), variable=True),
    "agent_evaluate_strategy": PriceBinding(("rest:managers_evaluate_by_id", "rest:managers_evaluate_by_object"), variable=True),
    "agent_backtest_strategy": PriceBinding(("rest:backtest_async_submit", "rest:backtest_async_status", "skill:crypto_ohlcv"), variable=True),
}
