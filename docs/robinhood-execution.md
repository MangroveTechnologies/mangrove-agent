# Robinhood execution venue

Mangrove owns research, strategy composition, signals, risk gates, and order sizing.
The local agent evaluates the strategy through Mangrove's stateless API, then
executes its intent through Robinhood's official MCP endpoint. Robinhood supplies
account eligibility, quotes, order validation, execution, and confirmed fills.
The existing DEX execution path remains available for wallet allocations.

## Connect once

Run setup normally, then authorize the local agent:

```sh
./scripts/connect-robinhood.sh
```

This opens Robinhood authorization for **Mangrove Agent**, with a loopback callback
on port 8766. It does not place orders. OAuth credentials are encrypted using the
agent's existing master key and stored under its private data directory. The
agent does not read Claude or Codex credentials. Keep the encrypted grant, master
key, and database private and backed up together. Revoke this application's grant
in Robinhood to disconnect it; if authorization expires, run the script again.
Background workers never open a login window.

The connector discovers and validates the live MCP schemas rather than treating
this document as a frozen broker API specification. The encrypted grant persists
across restarts. The host should run one agent worker against this data directory;
multiple independent processes must not concurrently refresh the same OAuth grant.

## Configure a Mangrove strategy

1. Use Mangrove's existing research, create, and backtest tools to prepare an
   inactive strategy. Its asset must be USD quoted. Start with a fresh strategy;
   strategies with paper/live trade history cannot change venues. Pass explicit
   `market_context` (`base_token`, `quote_token`, `market_data_venue`) when creating
   it, or use `agent_configure_strategy_market` while it is fresh and inactive.
   The market-data source can differ from the execution venue. Saved-strategy
   backtests inherit both this context and the stored execution configuration;
   per-call `config` overrides do not change the saved strategy.
2. Call `agent_robinhood_status` to verify the eligible account, approval setting,
   and available buying power. Only the account authorized for this agent is used.
3. Call `agent_configure_execution_venue` with the local strategy ID and the
   user's capital mandate. For example (illustrative values, not trade advice):

   ```json
   {
     "strategy_id": "<local strategy UUID>",
     "allocation": {
       "venue": "robinhood",
       "asset_class": "crypto",
       "capital": "100",
       "max_order_usd": "25",
       "slippage_pct": "0.0025"
     }
   }
   ```

   `capital` is the strategy's accounting allocation in USD, not a transfer or a
   reservation at Robinhood. Orders also check actual broker buying power. The
   adapter does not import or sell unrelated account holdings. `max_order_usd`
   bounds each order, and `slippage_pct` bounds its limit price relative to the
   Mangrove evaluation price (maximum 0.25%). The strategy must size exits within
   the same order cap. Configuration selects caller-owned stateless evaluation.
4. The local activation MCP tool is retired. Existing execution REST endpoints
   remain, but a supported saved-strategy-to-local-deployment flow is deferred.
   Server `evaluate_strategy` checks signals; it does not activate this runner.

The agent leaves Robinhood's approval preference unchanged. If approvals are off,
orders can execute directly. If Robinhood requires approval for a particular
order, a preview that reports that requirement stops submission. If a placement
unexpectedly creates an approval, the journal tracks it and blocks further orders
until its outcome is known. It never bypasses broker controls.

## Observe and stop

- Local lifecycle responses report `execution_venue`
  separately from `market_context`, including the broker capital and order cap.
  These responses never include raw broker account identifiers.
- `agent_list_venue_orders(strategy_id)` shows local order identity, broker order
  identity, state, confirmed quantity, notional, and fees.
- `agent_reconcile_venue_orders(strategy_id)` reads broker history and updates
  confirmed trades and holdings; it never submits or retries an order.
- `agent_cancel_venue_order(order_id)` requests cancellation of a known pending
  order. A cancel acknowledgement is not proof that it did not fill.
- Set the strategy inactive with the existing lifecycle tool to stop new orders.
  The exit-only strategy monitor and capital allocation remain while exposure or unresolved orders remain. Already submitted orders remain at Robinhood; cancel them separately if desired.
  The 15-second reconciliation worker continues while the agent is running,
  including for paused strategies.

REST equivalents (all require the agent API key):

| Operation | Endpoint under `/api/v1/agent` |
| --- | --- |
| Read readiness | `GET /brokers/robinhood/status` |
| Configure mandate | `PUT /brokers/strategies/{id}/venue` |
| Read journal | `GET /brokers/strategies/{id}/orders` |
| Reconcile | `POST /brokers/strategies/{id}/reconcile` |
| Cancel | `POST /brokers/orders/{id}/cancel` |

## Execution and recovery behavior

Before submission, the agent commits an order reservation with a client-generated
Robinhood `ref_id`. Pending or uncertain orders fence their position. Unknown legacy position identities fence all submissions; one entry may be pending per strategy. A network timeout or crash is **unknown**, never assumed rejected.
Recovery searches broker history by that identity and never automatically resends
an ambiguous placement. Missing history alone cannot clear the reservation.

Trades, exact decimal holdings, and journal changes commit together from cumulative
broker fills. Repeated snapshots cannot double-count fills. Partial fills and
canceled remainders affect only confirmed quantities. Mangrove's simulated fills
are retained in the evaluation audit, not recorded as broker executions. Each next
Mangrove evaluation receives cash and open quantities rebuilt from the broker
journal, along with retained bracket and cross-bar risk state.

Malformed, regressing, or unexpectedly revised cash snapshots stop reconciliation.
An unresolved submission requires investigation against Robinhood's order history;
there is deliberately no "assume failed and retry" tool. Broker data, including
fees and withholding, determines cash. Corporate actions and externally edited or
liquidated positions are not automatically reconciled into the strategy book.

## Initial scope and limits

- Long-only spot crypto and whole-share equities; limit orders only. Crypto
  quantities round down to venue increments. Fractional equity intent is rejected.
- Crossed quotes (bid greater than ask) are rejected before preview or placement.
- Equity placement is restricted to regular weekday market hours. Fresh quotes,
  tradability, and broker pre-trade checks must also pass. Crypto pairs must support
  both sides and limit orders.
- Distinct position exits submit serially. Exits defer entries until a fresh confirmed evaluation. Entries are limited to one per evaluation and one pending entry per strategy.
- Bracket exits are evaluated by Mangrove on the strategy schedule. They are not
  native standing Robinhood protective orders. Pending orders do not stop
  evaluation of confirmed holdings. If Mangrove requests an exit while an entry
  remainder is pending, the adapter requests cancellation of that entry. It waits
  for a confirmed terminal outcome and re-evaluates the resulting holdings on the
  next scheduled tick before placing an exit. Pending sells fence their own position. Unknown placements without position identity fence the entire strategy; unrelated protective exits may continue when their own confirmed holdings are available. Evaluation cannot make an uncertain order safe to duplicate. Exit execution can therefore still be delayed by broker uncertainty
  and the strategy schedule. The local agent must remain running.
- Risk state is committed from evaluations of the actual confirmed book. The engine
  receives one fully closed position's aggregate net PnL, including all partial
  exits and actual fees, with durable close receipts for idempotent ingestion.
  Prospective positions are activated only by fills. See
  [confirmed execution](confirmed-execution.md) for the shared contract.
- This build's validation uses offline order/fill fixtures plus live read-only
  account, catalog, pair, and quote checks. No live order was placed for validation.

Official broker references:
[Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/),
[Onboarding an external agent](https://robinhood.com/us/en/support/articles/onboarding-an-external-agent/).
