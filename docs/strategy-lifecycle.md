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
  holding the balances, positions and trades. See [paper scheduling](paper-scheduling.md).
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
  it stops real execution.
- A missing confirmation raises `ConfirmationRequired` and changes nothing.

A successful transition then:

1. Syncs the status to MangroveAI (`strategies.update_status`). If that call fails
   during a promotion to live, the allocation just recorded is released and the
   transition fails.
2. Registers the cron job for `paper` and `live`, or cancels it for any other target.
   Leaving `live` also releases the strategy's active allocation.
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

`tick` first checks whether the strategy is bound to Robinhood and, if so, hands off
to the broker path. Otherwise it:

1. Loads the local record and skips it unless its status is `paper` or `live`.
2. Asks MangroveAI to evaluate. The default evaluation lane is `server`:
   `execution.evaluate(mangrove_id, persist=True)`, and the engine keeps the
   position state for both paper and live so stop-loss, take-profit and time exits
   keep evaluating. A record on the `stateless` lane sends its own execution state and
   open positions with each call (`execution.evaluate_by_object`) and stores the
   returned values. Robinhood-bound strategies are always stateless.
3. Extracts order intents from the response. Only engine orders with
   `status: "filled"` are executed. Resting orders, such as the pending
   stop-loss and take-profit orders that accompany an entry, are logged and skipped.
   They execute on the tick where the engine reports them filled.
4. Logs the evaluation before any trade, so trades can reference it.
5. On a live tick, runs the portfolio kill-switch check (section 6) and stops
   without placing orders if it does not allow execution.
6. Dispatches each intent to `order_executor.execute_many`. A failure on one intent
   is logged and does not abort the rest.

An exception in a tick is logged as an `error` evaluation and never propagates.

For a live DEX swap, `order_executor._live_swap`:

1. Validates `slippage_pct` again (see below).
2. Caps the input amount at the allocation's `amount`. The engine sizes intents from
   its own simulated account, so the allocation is the limit on real spend.
3. Gets a quote through `dex_service`.
4. Approves the input token if needed, signs the approval locally, broadcasts it and
   polls until it confirms.
5. Prepares the swap, signs it locally, broadcasts it and polls the transaction for
   up to 60 seconds.
6. Writes a `trades` row with `mode="live"`, the transaction hash and a status of
   `confirmed`, `failed` or `pending`.

Live execution adds these guarantees over paper:

- **Local signing.** The SDK never sees a private key. `wallet_manager.sign`
  decrypts the key in process only after the signing guard accepts the payload, and
  discards the plaintext after signing. The guard allows only 1inch router swaps and
  the ERC-20 `approve()` calls that feed them.
- **Backup gate.** `execute_one` calls `require_backup_confirmed` before any live
  swap and refuses a wallet whose backup is unconfirmed.
- **Slippage cap.** A `slippage_pct` above 0.0025 is rejected at the request boundary
  and again in `_live_swap` as defense in depth.

## 6. Portfolio kill switch

The agent tracks the aggregate live book across all live strategies. The book value
is the sum of active allocation amounts and Robinhood route capital, plus realized
profit and loss on live trades (`server/src/services/portfolio_risk_service.py`).
On every live tick the agent updates the high-water mark. If drawdown from it
reaches `PORTFOLIO_MAX_DRAWDOWN_PCT` (default 0.30), the switch trips:

- Every live strategy is set to `inactive`.
- The switch stays latched. It does not resume on its own.
- No new orders are placed until a person resets it.

Read the state with `GET /api/v1/agent/portfolio/risk`. After a trip, clear it with
`POST /api/v1/agent/portfolio/risk/reset`, which re-baselines to the current book
value. Then promote strategies back to live.

## 7. What the agent records locally

Every tick leaves a local record in `agent.db`, whichever evaluation lane produced
it:

| Table or column | Written by | Content |
| --- | --- | --- |
| `evaluations` | `strategy_service.tick` | The engine response for each tick, with status `ok`, `error` or `skipped` |
| `strategies.execution_state_json` | `strategy_service.tick` | The latest engine account and risk state |
| `strategies.open_positions_json` | `strategy_service.tick` | The engine's open-positions blob on the stateless lane, echoed on the next tick |
| `trades` | `order_executor` | Every fill, with `mode` set to `paper` or `live`; exit trades carry `p_and_l` |
| `positions` | `order_executor._maintain_position` | Opened on entry fills and closed with P&L on exit fills |

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
