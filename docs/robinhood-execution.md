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

Venue configuration applies to a local execution record in `draft` or `inactive`
status. Local strategy creation is retired (the create routes return HTTP 410), and
the agent has no supported flow that turns a saved MangroveAI strategy into a local
record. MangroveAI's `evaluate_strategy` tool checks signals on the server; it does
not create or activate this runner.

1. Start with a fresh local record whose asset is USD quoted. A record with trade
   history or broker orders cannot change venues. Give it an explicit market with
   `agent_configure_strategy_market` (or `PATCH /api/v1/agent/strategies/{id}/market`):
   `market_context` holds `base_token`, `quote_token` and `market_data_venue`, all
   uppercase, and `base_token` must match the strategy asset. This works only on a
   `draft` or `inactive` record that uses the stateless evaluation lane, and it never
   activates or trades. The market-data source can differ from the execution venue.
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
4. Activate with the status route, `PATCH /api/v1/agent/strategies/{id}/status`, using
   `{"status": "live", "confirm": true}`. There is no local MCP tool for activation.
   Do not send an `allocation` block: a Robinhood route is rejected if it carries a
   wallet allocation. Activation runs a Robinhood preflight, then registers the
   strategy's cron job. Activation does not change Robinhood's approval preference.
   See [strategy lifecycle](strategy-lifecycle.md) for the status rules.

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
- Set the strategy `inactive` with `PATCH /api/v1/agent/strategies/{id}/status`
  (`confirm: true`) to stop new orders. Already submitted orders remain at Robinhood;
  cancel them separately if desired. The 15-second reconciliation worker continues while the agent is running,
  including for paused strategies.

REST equivalents (all require the agent API key):

| Operation | Endpoint under `/api/v1/agent` |
| --- | --- |
| Read readiness | `GET /brokers/robinhood/status` |
| Configure mandate | `PUT /brokers/strategies/{id}/venue` |
| Set strategy market | `PATCH /strategies/{id}/market` |
| Activate or stop | `PATCH /strategies/{id}/status` |
| Read journal | `GET /brokers/strategies/{id}/orders` |
| Reconcile | `POST /brokers/strategies/{id}/reconcile` |
| Cancel | `POST /brokers/orders/{id}/cancel` |

## Execution and recovery behavior

Before submission, the agent commits an order reservation with a client-generated
Robinhood `ref_id`. Pending or uncertain orders block another evaluation from
submitting. A network timeout or crash is **unknown**, never assumed rejected.
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
- Equity placement is restricted to weekdays from 9:30 to 16:00 Eastern. Fresh quotes,
  tradability, and broker pre-trade checks must also pass. Crypto pairs must support
  both sides and limit orders.
- One actionable Mangrove intent per evaluation and one pending order per strategy.
  Multi-order evaluations are rejected before placement.
- Bracket exits are evaluated by Mangrove on the strategy schedule. They are not
  native standing Robinhood protective orders. Pending orders do not stop
  evaluation of confirmed holdings. If Mangrove requests an exit while an entry
  remainder is pending, the adapter requests cancellation of that entry. It waits
  for a confirmed terminal outcome and re-evaluates the resulting holdings on the
  next scheduled tick before placing an exit. Pending sells and unknown placements
  continue blocking new orders; evaluation cannot make an uncertain order safe to
  duplicate. Exit execution can therefore still be delayed by broker uncertainty
  and the strategy schedule. The local agent must remain running.
- Risk state from a modeled trade advances only after an actual fill. Partial fills
  can conservatively retain the modeled trade's cooldown/counter effects. If an
  actual exit loss differs from a modeled non-loss, new entries latch off with
  `broker_loss_diverged_from_model` rather than silently undercounting loss risk.

Official broker references:
[Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/),
[Onboarding an external agent](https://robinhood.com/us/en/support/articles/onboarding-an-external-agent/).
