# Strategy lifecycle and execution

This page explains how a strategy moves from a saved MangroveAI strategy to paper
evaluation and to live execution, which part of the system owns each step, and
what the agent records locally along the way. Each section names the file that
implements the behavior.

## 1. Who owns what

MangroveAI owns strategy authoring, backtests, saved strategies, signals and the
evaluation engine. The agent is a client of that service: it discovers MangroveAI's
tools over MCP and forwards calls to them. Local strategy creation is retired - the
`POST /api/v1/agent/strategies/autonomous` and `/manual` routes return HTTP 410
(`server/src/api/routes/strategies.py`). Create strategies with MangroveAI's
`load_skill` and `save_strategy` tools instead.

The agent owns the parts that must stay on your machine: the scheduler clock, wallets
and signing, spending limits, execution routing, and the local record of what was
executed. The two kinds of strategy id are not interchangeable. MangroveAI tools take
MangroveAI strategy ids. The agent's local execution records have their own ids,
which the local REST routes and the `agent_*` Robinhood tools take.

There are two execution paths:

- **Paper.** The agent supplies the clock and MangroveAI runs the paper evaluation,
  holding the balances, positions and trades. Explicit stateless paper instead
  uses a separate local simulated-fill journal. See [paper scheduling](paper-scheduling.md).
- **Live.** The agent evaluates a local execution record through MangroveAI, then
  executes the resulting orders itself: as a DEX swap signed by a local wallet, or as
  a Robinhood limit order. See [Robinhood execution](robinhood-execution.md).

The rest of this page covers the local scheduler, the local status transitions and
the local live path.

## 2. The scheduler is in-process

Everything runs inside one `uvicorn src.app:app` process. There is no separate
daemon, system cron or cloud scheduler.

- FastAPI serves the REST and MCP endpoints.
- An APScheduler `BackgroundScheduler` runs alongside it
  (`server/src/services/scheduler_service.py`). Its job store is a
  `SQLAlchemyJobStore` in the same SQLite file as the rest of the agent's data
  (`DB_PATH`), so job registrations survive a restart. Jobs run in a thread pool of
  10 workers, so a slow tick does not block an HTTP request.
- Job defaults are `coalesce=True`, `max_instances=1` and `misfire_grace_time=60`
  seconds. If the process was down, a job runs once to catch up rather than once per
  missed tick.

A strategy job is registered under the id `eval-<strategy_id>` with a cron trigger
chosen from the strategy's timeframe:

| Timeframe | Cron fields |
| --- | --- |
| `1m` | minute `*/1` |
| `5m` | minute `*/5` |
| `15m` | minute `*/15` |
| `1h` | minute `0` |
| `4h` | minute `0`, hour `*/4` |
| `1d` | minute `0`, hour `0` |

An unknown timeframe is rejected with a `SchedulerError`. The job calls
`src.services.strategy_service:tick(strategy_id)`.

The same scheduler also runs these fixed jobs, registered at startup:

| Job id | Interval | Purpose |
| --- | --- | --- |
| `paper-dispatch` | 5 seconds | Dispatches due paper-schedule evaluations |
| `broker-reconciliation` | 15 seconds | Reconciles pending Robinhood orders |
| `x402-reconciliation` | 15 seconds | Reconciles uncertain x402 payments; registered only when `X402_RECONCILIATION_RPC_URLS` is configured |
| `x402-refunds` | 30 seconds | Observes confirmed refunds; registered only when `X402_RECONCILIATION_RPC_URLS` is configured |

Because the clock lives in this process, strategies and schedules evaluate only
while the agent is running. If you close the laptop or stop the process, nothing
fires, and missed ticks are not replayed.

## 3. Status transitions

A local execution record has one of five statuses. `strategy_service.update_status`
is the single place transitions are validated and applied
(`server/src/services/strategy_service.py`):

| From | Allowed targets |
| --- | --- |
| `draft` | `inactive`, `archived` |
| `inactive` | `paper`, `live`, `archived` |
| `paper` | `live`, `inactive`, `archived` |
| `live` | `inactive`, `archived` |
| `archived` | none |

Setting the current status again is a no-op. Any other transition raises
`StrategyInvalidStatusTransition`.

The transition is exposed as
`PATCH /api/v1/agent/strategies/{strategy_id}/status` with the body
`{"status": ..., "confirm": ..., "allocation": {...}}`. The local MCP server has no
tool for this transition. Routes under `/api/v1/agent` require the agent's
`X-API-Key` header.

The confirm rules:

- A transition to `live` requires `confirm: true`.
- Leaving `live` for `inactive` or `archived` also requires `confirm: true`, because
  it stops new entries; protective exits continue while exposure remains.
- A missing confirmation raises `ConfirmationRequired` and changes nothing.

A successful transition then:

1. Syncs the status to MangroveAI (`strategies.update_status`). If that call fails
   during a promotion to live, the allocation just recorded is released and the
   transition fails.
2. Registers the cron job for `paper` and `live`. Leaving `live` retains an
   exit-only cron and allocation while confirmed exposure or unresolved orders
   remain. Cancellation and release wait until the account is confirmed flat.
3. Writes the new status to the local record.
4. When the strategy enters or leaves `live`, re-baselines the portfolio
   high-water mark so the change is not read as drawdown (section 6).

A strategy bound to Robinhood cannot move to `paper`. It uses live execution, and you
test with a separate paper strategy before binding.

## 4. Paper

For paper trading, use the agent-scheduled, server-owned flow in
[paper scheduling](paper-scheduling.md). It is the supported way to run a saved
MangroveAI strategy in paper mode.

A local record set to `paper` through the status route also registers the cron job.
On each tick `strategy_service.tick` asks MangroveAI to evaluate the strategy and
records the result. Each filled engine order becomes a simulated trade in the local
`trades` table, priced at the current market price, with `mode="paper"` and
`status="simulated"`. No wallet is touched and no wallet backup is required
(`server/src/services/order_executor.py`, `_paper_fill`).

## 5. Live

### Wallet gates

Live DEX execution needs a funded wallet whose secret you have backed up outside
the agent. The agent never shows the secret in chat:

1. `create_wallet` returns a `vault_token`, not the secret. To import an existing
   wallet, run `./scripts/stash-secret.sh` in a terminal and pass the resulting
   token to `import_wallet`.
2. Run `./scripts/reveal-secret.sh --address <address>` in a private terminal to
   print the secret once, and save it somewhere safe.
3. Run `./scripts/confirm-backup.sh <address>`. This sets
   `wallets.backup_confirmed_at`, which opens the live-trading gate for that wallet.
4. Fund the wallet. This happens outside the agent.

### Promoting to live

A promotion to live through the status route needs `confirm: true` and, for a DEX
strategy, an `allocation` block:

```json
{
  "status": "live",
  "confirm": true,
  "allocation": {
    "wallet_address": "<backed-up wallet address>",
    "token": "USDC",
    "token_address": "<token contract address>",
    "amount": 5.0,
    "slippage_pct": 0.002
  }
}
```

`slippage_pct` is a decimal (0.002 is 0.2%), is required, and must be greater than 0
and at most 0.0025. The service checks that the wallet exists and has a confirmed
backup (`require_backup_confirmed` raises an error otherwise) and records the
allocation.

For a Robinhood-bound strategy, the `allocation` block is rejected. The route's
venue mandate sets the capital, and a Robinhood preflight runs instead. See
[Robinhood execution](robinhood-execution.md).

### What a tick does

`tick` reconciles execution and loads the confirmed account before requesting a
proposal. Robinhood-bound records use the broker adapter; DEX LIVE uses
`dex_confirmed`. Both call `execution.evaluate_by_object(..., persist=False)`
with actual cash, physical holdings, net close history and retained risk state,
regardless of a legacy record's evaluation lane. Signal evaluation and sizing
remain in MangroveAI. Marks used to value holdings do not become simulated fills.

The journal acknowledges complete close history and preserves booked-time cursors
and stable position identities. Unknown history, pending submissions or incomplete
fee valuation hold new entries. Known holdings retain their protective exits.
Risk responses commit only against the same account revision; returned account
estimates never replace actual settlement facts.

Engine orders, including `status: "filled"`, are proposals. An entry creates no
position before an actual fill. DEX execution reserves the intent and locally
computed signed transaction hash before broadcast, then verifies finalized raw
receipts for the signed transaction, chain, wallet, router and ERC-20 flows.
Approvals, reverts and native gas charges retain their own evidence. Native fees
without a quote valuation hold entry/close-history acknowledgment; no USD
conversion is guessed. Broker partial fills update only confirmed quantities,
and one fully closed position produces one aggregate net close including fees.

Distinct exits are processed before entries. Uncertain submissions are reconciled
by the existing identity rather than automatically resubmitted. A strategy made
inactive or stopped by the portfolio latch retains exit-only monitoring until
confirmed flatness. Revision fences and the owning tick lock protect status and
allocation changes. See [confirmed execution](confirmed-execution.md).

Live DEX signing still requires a backed-up wallet, the 1inch-only signing guard
and the configured slippage cap (at most 0.25%). No private key reaches the SDK.
An exception is logged as an `error` evaluation and never propagates.

## 6. Portfolio kill switch

The agent tracks the aggregate live book across all live strategies. The book value
is the sum of active allocation amounts and Robinhood route capital, plus realized
profit and loss on live trades (`server/src/services/portfolio_risk_service.py`).
On every live tick the agent updates the high-water mark. If drawdown from it
reaches `PORTFOLIO_MAX_DRAWDOWN_PCT` (default 0.30), the switch trips:

- Every live strategy is set to `inactive`.
- The switch stays latched. It does not resume on its own.
- New entries stop until a person resets it; protective exits and reconciliation
  continue. Allocations and exit-only jobs remain until confirmed flatness.

Read the state with `GET /api/v1/agent/portfolio/risk`. After a trip, clear it with
`POST /api/v1/agent/portfolio/risk/reset`, which re-baselines to the current book
value. Then promote strategies back to live.

## 7. What the agent records locally

Every tick leaves a local record in `agent.db`, whichever evaluation lane produced
it:

| Table or column | Written by | Content |
| --- | --- | --- |
| `evaluations` | `strategy_service.tick` | The engine response for each tick, with status `ok`, `error` or `skipped` |
| `strategies.execution_state_json` | Confirmed execution | Accepted risk and candle state; actual account facts are overlaid from the journal |
| `strategies.paper_execution_state_json` | Stateless paper execution | Separate paper risk state |
| `strategies.open_positions_json` | Confirmed execution | Retained protective templates, activated only against actual fills |
| `trades` | The owning execution journal | Confirmed LIVE or simulated PAPER fills; actual exit PnL is retained |
| `positions` | The owning execution journal | Physical or simulated positions created and changed only by their fills |

Read them over REST (all under `/api/v1/agent`):

- `GET /strategies/{strategy_id}/evaluations`
- `GET /strategies/{strategy_id}/trades`
- `GET /trades`, with optional `strategy_id` and `mode` filters
- `POST /strategies/{strategy_id}/evaluate` runs one tick immediately through the
  same code path the cron job uses. Use it for debugging.

`GET /status` (also the `status` tool) reports strategy counts by status, the number
of active cron jobs and the portfolio risk state. The MangroveAI tools
`list_evaluations`, `list_trades`, `list_positions` and `get_trades_for_strategy`
read MangroveAI's own records, not these local tables.

## 8. Design choices

The scheduler is an in-process APScheduler rather than a system cron, a separate
daemon or a cloud scheduler. That keeps the agent to a single process with a
persistent job store and no extra infrastructure, isolates ticks in threads, and uses
cron syntax that is easy to reason about. A task queue such as Celery is heavier than
the handful of strategies one person runs locally.

## 9. What can go wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| No evaluations appear after a promotion | The agent process is not running | Check `GET /health`, then restart the agent with `./scripts/setup.sh` |
| An evaluation has `status="error"` | The MangroveAI call failed (service down, bad key) | Read `error_msg` on the evaluation and `agent-data/bare.log` |
| A promotion to live fails with "not backed up" | The wallet's `backup_confirmed_at` is null | Run `./scripts/reveal-secret.sh --address <address>`, then `./scripts/confirm-backup.sh <address>` |
| A request is rejected for slippage | `slippage_pct` is above 0.0025 or missing | Resend with a value above 0 and up to 0.0025 |
| Live ticks stop placing orders | The portfolio kill switch tripped | Check `GET /api/v1/agent/portfolio/risk`, then reset it deliberately |
| `active_cron_jobs` is 0 after a restart | The job store was not persisted, for example `DB_PATH` changed or is `:memory:` | Check `DB_PATH` and promote the strategies again |
