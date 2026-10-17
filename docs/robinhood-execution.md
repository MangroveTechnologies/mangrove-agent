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
   The default `external_execution_mode` is `legacy`. To opt into broker-confirmed
   risk feedback, set it to `confirmed_v2` only after the Mangrove hosted v2 route
   and compatible SDK are available. This mode can be enabled only on a pristine
   strategy (or re-saved unchanged on an existing v2 route); it cannot be enabled
   after broker or trade history exists. It preserves confirmed broker holdings
   separately from hosted position templates and sends first-fill `opened` and
   fully-flat `closed` events with exact quantities, realized PnL, and fees.
4. Activate with `agent_update_strategy_status(strategy_id, status="live",
   confirm=true)`. A configured broker strategy needs no wallet allocation.
   Activation authorizes scheduled execution within the mandate; it does not
   introduce a per-order human review step.

Before a separate activation decision, an operator can run the maintained
read-only route check:

```sh
MANGROVE_AGENT_API_KEY="<local API key>" \
  python scripts/robinhood_preflight.py "<local strategy UUID>"
```

The command calls the authenticated Robinhood status route and the exact local
strategy-detail route. It requires an inactive strategy already configured for
Robinhood, uses a loopback URL only, and reports the masked account, approval
setting, buying power, execution mandate, and market context. It does not run an
evaluation, change strategy state, reconcile orders, or submit orders. A passing
result is input for human review, not an activation command.

The agent leaves Robinhood's approval preference unchanged. If approvals are off,
orders can execute directly. If Robinhood requires approval for a particular
order, a preview that reports that requirement stops submission. If a placement
unexpectedly creates an approval, the journal tracks it and blocks further orders
until its outcome is known. It never bypasses broker controls.

## Observe and stop

- `agent_get_strategy` and `agent_list_strategies` report `execution_venue`
  separately from `market_context`, including the broker capital and order cap.
  These responses never include raw broker account identifiers.
- `agent_list_venue_orders(strategy_id)` shows local order identity, broker order
  identity, state, confirmed quantity, notional, and fees.
- `agent_reconcile_venue_orders(strategy_id)` reads broker history and updates
  confirmed trades and holdings; it never submits or retries an order.
- `agent_list_venue_protection(strategy_id)` reports the last observed stop
  coverage and any saved hosted exit waiting for a broker order to become
  terminal. It does not call Robinhood.
- `agent_cancel_venue_order(order_id)` requests cancellation of a known pending
  order. A cancel acknowledgement is not proof that it did not fill.
- Set the strategy inactive with the existing lifecycle tool to stop new orders.
  Already submitted orders remain at Robinhood; cancel them separately if desired.
  The 15-second reconciliation worker continues while the agent is running,
  including for paused strategies.

REST equivalents (all require the agent API key):

| Operation | Endpoint under `/api/v1/agent` |
| --- | --- |
| Read readiness | `GET /brokers/robinhood/status` |
| Configure mandate | `PUT /brokers/strategies/{id}/venue` |
| Read journal | `GET /brokers/strategies/{id}/orders` |
| Read stop coverage | `GET /brokers/strategies/{id}/protection` |
| Reconcile | `POST /brokers/strategies/{id}/reconcile` |
| Cancel | `POST /brokers/orders/{id}/cancel` |

## Execution and recovery behavior

Before submission, the agent commits an order reservation with a client-generated
Robinhood `ref_id`. Pending or uncertain orders block another evaluation from
submitting. A network timeout or crash is **unknown**, never assumed rejected.
Recovery searches broker history by that identity and never automatically resends
an ambiguous placement. Missing history alone cannot clear the reservation.

In `confirmed_v2` mode, the agent requires a typed hosted acknowledgement and a
matching receipt for every submitted broker event. The state, broker-confirmed
positions, event receipts, and (when an intent is executable) the local submitting
reservation commit together before the broker request. A crash after that commit
leaves a durable reservation for reconciliation; an unknown broker outcome is
never retried automatically. No-intent and duplicate-bar evaluations are saved in
the local evaluation audit before their acknowledgement is committed. A duplicate
bar response containing a new order is rejected.

Trades, exact decimal holdings, and journal changes commit together from cumulative
broker fills. Repeated snapshots cannot double-count fills. Partial fills and
canceled remainders affect only confirmed quantities. Mangrove's simulated fills
are retained in the evaluation audit, not recorded as broker executions. Each next
Mangrove evaluation receives cash and open quantities rebuilt from the broker
journal, along with retained bracket and cross-bar risk state.

Each distinct reconciled order snapshot is also retained in `broker_events` with
its local `observed_at` time and a deterministic receipt key. When Robinhood
provides per-execution details, `snapshot_json` preserves the raw optional fields
and original timestamps; reconciliation time is not substituted for fill time.
Identical snapshots for one order and lifecycle status produce one receipt, while
a status transition such as `partial` to `cancel_pending` remains separately
auditable. Receipt writes share the SQLite transaction with fill, trade, and
position updates.

Malformed, regressing, or unexpectedly revised cash snapshots stop reconciliation.
An unresolved submission requires investigation against Robinhood's order history;
there is deliberately no "assume failed and retry" tool. Broker data, including
fees and withholding, determines cash. Corporate actions and externally edited or
liquidated positions are not automatically reconciled into the strategy book.

## Initial scope and limits

- Long-only spot crypto and whole-share equities. Strategy entries and exits use
  limit orders. Crypto quantities round down to venue increments. Fractional
  equity intent is rejected.
- Crossed quotes (bid greater than ask) are rejected before preview or placement.
- Equity placement is restricted to regular weekday market hours. Fresh quotes,
  tradability, and broker pre-trade checks must also pass. Crypto pairs must support
  both sides and limit orders.
- One actionable Mangrove intent per evaluation and one pending order per strategy.
  Multi-order evaluations are rejected before placement.
- Native crypto stop-loss protection is opt-in, default off, and available only
  with `confirmed_v2` on a pristine strategy before its first broker order. It
  takes the trigger price from Mangrove's actual pending stop template; the
  adapter does not calculate or move the hosted risk level. The adapter may round
  the routed trigger up to Robinhood's price increment, and reports both values.
  It requires a fresh quote, sellable confirmed quantity, route limits, a broker
  order acknowledgement, and an observed original order quantity before it
  reports standing coverage. Missing or mismatched quantity evidence remains
  unverified. The status is last-observed and can become stale if the agent stops.
- Robinhood's crypto `stop_loss` is a stop-triggered market order with a venue
  execution collar; it is not the hosted strategy's limit exit and can execute at
  a different price. Crypto OCO is unavailable, so this feature installs only the
  hosted stop and does not couple it to take-profit. It never claims coverage
  while a submit, cancel, partial entry, unknown outcome, or replacement is
  unresolved. A strategy-wide single-pending-order limit serializes protection
  and regular orders. If an exit arrives behind a stop or partial entry, the
  exact hosted intent is durably saved with its evaluation acknowledgement. A
  stop or entry blocker is canceled and reconciled before the saved exit can
  reserve a sell; a restart can resume this process. An existing strategy sell
  is left in place. A newer exit waits for its terminal outcome and subtracts
  subsequent fills on the same position; this does not introduce automatic
  cancellation or repricing of an open strategy sell. The status exposes that
  wait, which can continue while the earlier sell remains open.
  Unknown or paused/halted states do not submit
  a replacement. The agent must be running and the strategy live to install or
  replace protection. Pausing or a portfolio halt leaves any already-accepted
  broker stop in place and prevents new orders.
- In `legacy` mode, risk state from a modeled trade advances only after an actual fill. Partial fills
  can conservatively retain the modeled trade's cooldown/counter effects. If an
  actual exit loss differs from a modeled non-loss, new entries latch off with
  `broker_loss_diverged_from_model` rather than silently undercounting loss risk.
  Persisted risk halts survive delayed first fills, later partial fills, restarts,
  and subsequent evaluations; automatic state updates cannot clear the latch or
  replace its original recorded cause. Confirmed fills still update the book.
  `confirmed_v2` instead sends durable broker-confirmed open/close events to Mangrove;
  the hosted feature must be enabled separately before opting a strategy into it.
- This build's validation uses offline order/fill fixtures plus live read-only
  account, catalog, pair, and quote checks. No live order was placed for validation.

Official broker references:
[Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/),
[Onboarding an external agent](https://robinhood.com/us/en/support/articles/onboarding-an-external-agent/).
