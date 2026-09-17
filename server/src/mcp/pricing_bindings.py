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


TOOL_PRICING: dict[str, PriceBinding] = {
    "get_ohlcv": PriceBinding(("skill:crypto_ohlcv",)),
    "get_benchmark": PriceBinding(("skill:crypto_ohlcv",), variable=True),
    "get_market_data": PriceBinding(("skill:crypto_market_data",)),
    "get_trending": PriceBinding(("skill:crypto_trending",)),
    "list_approved_assets": PriceBinding(("rest:crypto_assets_all",)),
    "get_asset": PriceBinding(("skill:crypto_symbol_detail",)),
    "get_global_market": PriceBinding(("skill:crypto_global_market",)),
    "list_signals": PriceBinding(("rest:signals_list",), variable=True),
    "get_signal": PriceBinding(("rest:signals_get",)),
    "match_signals": PriceBinding(("rest:signals_match",)),
    "search_signals": PriceBinding(("rest:signals_search",)),
    "get_whale_activity": PriceBinding(("skill:onchain_whale_activity",)),
    "get_whale_transactions": PriceBinding(("skill:onchain_whale_transactions",)),
    "get_smart_money_sentiment": PriceBinding(("skill:onchain_smart_money_sentiment",)),
    "screen_smart_money": PriceBinding(("skill:onchain_smart_money_screen",)),
    "get_token_holders": PriceBinding(("skill:onchain_token_holders",)),
    "get_exchange_flows": PriceBinding(("skill:onchain_exchange_flows",)),
    "get_smart_money_historical_holdings": PriceBinding(("skill:onchain_smart_money_historical_holdings",)),
    "get_smart_money_dex_trades": PriceBinding(("skill:onchain_smart_money_dex_trades",)),
    "get_smart_money_perp_trades": PriceBinding(("skill:onchain_smart_money_perp_trades",)),
    "get_token_dex_trades": PriceBinding(("skill:onchain_token_dex_trades",)),
    "get_token_flows": PriceBinding(("skill:onchain_token_flows",)),
    "get_chain_tvl": PriceBinding(("skill:defi_chain_tvl",)),
    "get_protocol_tvl": PriceBinding(("skill:defi_protocol_tvl",)),
    "get_stablecoin_metrics": PriceBinding(("skill:defi_stablecoins_metrics",)),
    "get_token_unlocks": PriceBinding(("skill:defi_token_unlocks",)),
    "get_perp_funding": PriceBinding(("skill:defi_perp_funding",)),
    "get_treasuries": PriceBinding(("skill:defi_treasuries",)),
    "get_etf_flows": PriceBinding(("skill:defi_etf_flows",)),
    "get_lending_borrow_rates": PriceBinding(("skill:defi_lending_rates",)),
    "get_sentiment": PriceBinding(("skill:social_sentiment",)),
    "get_mentions": PriceBinding(("skill:social_mentions",)),
    "get_influence_score": PriceBinding(("skill:social_influence",)),
    "create_strategy_autonomous": PriceBinding(("rest:signals_list", "rest:strategy_list_or_create", "rest:backtest_async_submit", "rest:backtest_async_status"), variable=True),
    "create_strategy_manual": PriceBinding(("rest:strategy_list_or_create",)),
    "update_strategy_status": PriceBinding(("rest:strategy_status_update",), variable=True),
    "delete_strategy": PriceBinding(("rest:strategy_detail",)),
    "evaluate_strategy": PriceBinding(("rest:managers_evaluate_by_id", "rest:managers_evaluate_by_object"), variable=True),
    "backtest_strategy": PriceBinding(("rest:backtest_async_submit", "rest:backtest_async_status", "skill:crypto_ohlcv"), variable=True),
    "get_backtest": PriceBinding(("rest:backtest_get", "skill:crypto_ohlcv"), variable=True),
    "list_account_positions": PriceBinding(("rest:managers_positions_list",)),
    "list_account_trades": PriceBinding(("rest:managers_trades_list",)),
    "sieve_score": PriceBinding(("proxy:oracle_sieve_score",)),
    "oracle_data_query": PriceBinding(("proxy:oracle_data_query",)),
    "oracle_backtest": PriceBinding(("proxy:oracle_backtest",)),
    "oracle_backtest_async": PriceBinding(("proxy:oracle_backtest",)),
    "oracle_backtest_poll": PriceBinding(("proxy:oracle_backtest",)),
    "oracle_backtest_bulk": PriceBinding(("proxy:oracle_backtest",)),
    "oracle_list_results": PriceBinding(("proxy:oracle_results_read",)),
    "oracle_launch_experiment": PriceBinding(("proxy:oracle_experiment", "proxy:api_calls"), variable=True),
}

# These wrappers use a separate KB origin or lack an authoritative published
# billing definition. Do not borrow a same-named MCP skill's price.
for _name in (
    "kb_search", "kb_glossary_get", "kb_get_document", "kb_list_indicators",
    "kb_list_tags", "list_docs", "get_doc_content", "list_backtests",
    "get_account_position", "oracle_list_datasets",
    "oracle_list_signals", "oracle_list_templates",
):
    TOOL_PRICING[_name] = PriceBinding(())

for _operation in ("create", "list", "get", "update", "delete", "validate", "pause"):
    _name = f"oracle_{_operation}_experiment" + ("s" if _operation == "list" else "")
    TOOL_PRICING[_name] = PriceBinding(("proxy:api_calls",))
