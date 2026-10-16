---
name: setup-robinhood
description: Connect Robinhood as Mangrove Agent's execution venue, inspect the connection and strategy routing, and explain setup or recovery. Use for Robinhood connection and routing requests; use the existing strategy workflow for research and signal selection.
---

# Robinhood execution venue

Mangrove hosted services own signals, sizing, risk controls, exits and order
intents. The local agent owns scheduling, execution routing and reconciliation.
Robinhood executes orders. Read `docs/robinhood-execution.md` for the supported
scope, route schema, API equivalents and recovery behavior.

## Connection and inspection

Use `agent_robinhood_status` to inspect an existing connection. If authorization
is missing or expired, run `./scripts/connect-robinhood.sh` in the repository's
terminal. The user completes Robinhood's browser authorization for Mangrove
Agent. Never ask for passwords or tokens in chat, copy another assistant's OAuth
credentials, or alter Robinhood's approval setting. Verify status after connection.

Use `agent_get_strategy` or `agent_list_strategies` to read `execution_venue`.
`market_context.market_data_venue` is the price feed, not the execution broker.
A Robinhood route needs no local wallet. A live status does not prove any order
has filled: inspect evaluations, `agent_list_venue_orders`, and confirmed trades.

## Prepare a route

Carry the user's chosen strategy, capital and limits through the existing create
and backtest workflow. Keep its execution config and explicit market context.
Configure the broker with `agent_configure_execution_venue` while the strategy is
fresh and inactive; this does not activate it or move money. Read back the saved
strategy and route. Do not invent a capital allocation or change a live mandate.

Activation is a separate existing lifecycle operation, described in the execution
document. Observe the acting assistant's authorization and execution constraints.
Once activated, the scheduler runs locally; the Mac must stay awake and online.
Do not imply that the hosted engine runs the local scheduler or that connecting
Robinhood itself starts trading.

## Recovery

Inspect the journal before proposing a retry. Unknown submissions must be
reconciled, never automatically resent. A cancellation acknowledgement does not
prove absence of fills. Pausing prevents new submissions but does not cancel
already submitted orders. Protective exits are evaluated on the strategy
schedule, not installed as standing Robinhood bracket orders. Pending-order
uncertainty can delay execution even though strategy evaluation continues.
