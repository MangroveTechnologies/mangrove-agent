# Robinhood execution venue

Mangrove owns research, strategy composition, signals, risk gates, and order sizing.
The local agent evaluates the strategy through Mangrove's stateless API, then
executes its intent through Robinhood's official MCP endpoint. Robinhood supplies
account eligibility, quotes, order validation, execution, and confirmed fills.
The existing DEX execution path remains available for wallet allocations.

## Connect once

Run `./scripts/setup.sh` and choose **Connect now** at the optional Robinhood
step, or **Skip for now** to finish without connecting. To connect later:

```sh
./scripts/setup.sh --robinhood connect
```

Setup checks any saved connection first and verifies access after authorization.
While browser authorization is pending, **Skip for now** is selected in the
terminal. Press Enter to continue setup without connecting. Successful login is detected
automatically; closing the browser alone does not cancel the wait. If
authorization fails or is canceled, choose retry or skip. `--yes` and
`--robinhood skip` never start Robinhood login or change a saved connection.
The existing `./scripts/connect-robinhood.sh` remains available as a standalone
helper; setup uses the same Python connector, not a separate OAuth implementation.
This browser step currently supports bare-metal setup only. Docker setup defers
it because Docker needs its own callback routing and encryption-key context.
Robinhood authorization is separate from Mangrove sign-in; connecting it does
not change Mangrove access mode or enable SDK deployments in OAuth-only mode.

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

1. Use Mangrove's existing research, save, verify and backtest tools. The saved
   strategy needs an explicit USD quote and market-data venue, long-only rules,
   v1/v2 sizing, and a timeframe supported by the existing local scheduler.
   Call `agent_adopt_strategy(server_strategy_id, asset_class="crypto"|"equity")`.
   This owner-scoped server read returns a local `deployment_id` and pins a
   validated snapshot and fingerprint. It neither schedules nor trades. Drafts
   can be prepared, but activation requires an inactive server strategy and a
   passing backtest of its exact rules, sizing configuration and market.
   The market-data source can differ from the execution venue. No rule copying
   in chat or local strategy authoring is needed.
2. Call `agent_robinhood_status` to verify the eligible account, approval setting,
   and available buying power. Only the account authorized for this agent is used.
3. Call `agent_configure_execution_venue` with the returned deployment ID and the
   user's capital mandate. For example (illustrative values, not trade advice):

   ```json
   {
     "strategy_id": "<deployment_id returned by agent_adopt_strategy>",
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
4. After explicit authorization for live execution and recurring API/x402
   charges, call `agent_activate_deployment(deployment_id, confirm=true)`.
   This revalidates the definition, ownership and passing backtest, checks the
   configured broker account and uses the existing local APScheduler job.
   Server `evaluate_strategy` alone does not activate this runner. Generic
   status-setting and market-edit operations cannot modify adopted deployments.

### Binding, changes and recovery

The server's read-only `GET /strategies/{id}/local-deployment?asset_class=crypto`
contract (version 1) contains the owned server ID, owner, complete execution
snapshot, timeframe and SHA-256 fingerprint. The fingerprint includes market
context and resolved execution defaults, not just the existing deduplication
hash. It excludes names and mutable execution balances. Local records retain
both IDs; a remote ID cannot be substituted for the local deployment ID.

Each broker tick validates the binding before evaluation or order submission.
A changed, archived, inaccessible or no-longer-approved definition stops new
entries without discarding pending orders. Exit-only jobs remain while exposure
or unresolved orders exist, but evaluation and submission require successful
binding validation. Reconciliation continues independently. Resuming entries
requires explicit activation after resolution.
The existing object evaluator receives the pinned server snapshot and confirmed
local holdings/state. There is no new local strategy engine.

Repeated adoption of the same definition returns the same durable deployment.
Repeated identical venue configuration preserves execution state. Different
configuration is refused after any execution history. Explicit
`agent_adopt_strategy(..., rebind=true)` can accept a revised snapshot only while
inactive with no execution history; it clears the old allocation for review.
After execution history, use a new server-owned strategy for new rules.

Activation persists local lifecycle state and registers the existing stable
`eval-<deployment_id>` job. Startup repairs a crash between those steps. Retries
do not create another deployment or scheduler. Stopping atomically saves inactive
status and any required exit-only monitoring, so restart also repairs an
interrupted stop without resuming new entries. Pending/uncertain broker orders
remain in the existing durable journal and block new activation/submissions.

The REST equivalents are `POST /api/v1/agent/strategies/deployments`,
`GET /api/v1/agent/strategies/deployments/{id}`, and `POST` to its `/activate` and
`/stop` subpaths. They use the same services and local authentication as MCP.

### Access and rollout

The normal installation uses published `mangroveai==2.2.0`, which does not contain
`strategies.local_deployment`. An unversioned SDK upgrade does not enable these
tools while that is the latest published release. The existing adapter refuses
the missing capability before making a request.

Local deployment needs the helper in prepared **SDK 2.3.0** and the validated
contract from **API 7.11 or later** actually serving. The coordinated API packaging
candidate is now **7.12.0**; it does not change that minimum capability.
Prepared SDK source does not establish registry publication or production
serving. Follow the
[release compatibility gates](release-compatibility.md) before enabling the
workflow. After the compatible SDK is published and the API is qualified as
serving, update the SDK minimum and regenerate the lock. Verify the installed
package rather than a source checkout:

```sh
.venv/bin/python -c 'from mangrove_ai._services.strategies import StrategiesService; assert callable(getattr(StrategiesService, "local_deployment", None)), "Installed SDK lacks local_deployment support"'
```

This uses the existing SDK API-key/x402 access configuration, quota meter and
payment recovery. OAuth-only managed MCP access is not an SDK identity and is
refused without falling back to a wallet or linking platform identities.
Recurring ticks incur the existing strategy-detail validation charge as well
as evaluation charges. Broker credentials and activation authorization stay
local. The server definition's status is not changed by local start/stop.

This is separate from server-owned paper scheduling. Adopted deployments use
the existing confirmed-execution journal, close-history delivery, evaluation
locks and exit-only monitoring described in [confirmed execution](confirmed-execution.md).
Strategy rules remain pinned to the adopted snapshot; fills and risk inputs come
from the current local execution book. Live broker authorization and execution
still require end-to-end verification before production rollout.

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
- Call `agent_stop_deployment(deployment_id, confirm=true)` to stop new entries.
  This works without a server read, including after upstream credentials change.
  It does not cancel orders already submitted. Reconciliation continues.
  Already submitted orders remain at Robinhood; cancel them separately if desired.
  The exit-only strategy monitor and capital allocation remain while exposure or unresolved orders remain.
  The 15-second reconciliation worker continues while the agent is running,
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
- Equity placement is restricted to weekdays from 9:30 to 16:00 Eastern. Fresh quotes,
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
