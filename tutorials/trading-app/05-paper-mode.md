# Chapter 05 - Paper mode

*15 minutes. No live trading capital required. Upstream API access can still
cost money. The first chapter where the agent acts on a schedule.*

You ended Chapter 04 with a strategy saved on MangroveAI and a backtest of it.
In this chapter you ask the agent to evaluate that strategy repeatedly in paper
mode. No real orders are placed and no wallet is needed. In x402 mode the
evaluation calls still require a funded payment wallet; with an API key they
count against your allowance.

This completes the path without live capital. If you stop here you have done
the full compose, backtest, paper-evaluate loop. Chapters 06 and 07 are for
when you want to move real money.

## How paper mode works

Paper mode is split between MangroveAI and your agent:

- **MangroveAI owns the paper portfolio.** It holds the simulated balances,
  positions, trades and evaluation receipts for the strategy. Nothing about the
  strategy definition is copied to your machine.
- **Your agent supplies the clock.** A schedule stored in the agent's SQLite
  database decides when the next evaluation is due, and the agent asks
  MangroveAI to run it. A small dispatcher inside the agent checks for due
  schedules every 5 seconds.

Because the agent supplies the clock, it must be running for evaluations to
happen. Missed intervals are skipped, not replayed against old market data.

### What a strategy needs first

MangroveAI decides whether a strategy is eligible, and the agent reports the
reason when a start is refused. The conditions it knows about:

- The strategy is saved on MangroveAI under the same API key or payment wallet
  the agent uses now, and is not archived.
- A completed, passing backtest exists for this exact strategy configuration
  and market. Changing the rules or market after the backtest makes it
  ineligible until you backtest again.
- It uses a position-sizing mode MangroveAI's paper execution supports. Version
  3 sizing is not supported.
- It has valid server-side paper balances.
- It does not already belong to another execution flow.

## Start a paper schedule

In your AI client:

> "Start paper trading for that strategy. Evaluate every 5 minutes, up to 12
> times."

The client calls `agent_start_paper_schedule` with:

| Argument | Meaning |
|---|---|
| `strategy_id` | The MangroveAI strategy ID from Chapter 04 |
| `interval_seconds` | One of 60, 300, 900, 3600, 14400 or 86400 |
| `max_evaluations` | How many evaluation slots to authorize, 1 to 10000 |
| `confirm` | Must be `true`; it acknowledges the recurring API or x402 cost |

Without `confirm=true` the call is refused. With an API key, each evaluation
request counts against your allowance. In x402 mode each one is a paid call that
passes through the spend budget described below. The first evaluation is due on
the next dispatcher pass, within about five seconds.

The interval is independent of the strategy's candle size. A 1h strategy
evaluated every 5 minutes is checked more often than it has new bars; choose an
interval that matches how closely you want to watch it and how much you want to
spend.

## Check on it

> "List my paper schedules."

`agent_list_paper_schedules` reads only the agent's local records and makes no
upstream call, so it is free. Each schedule shows:

| Field | Meaning |
|---|---|
| `id` | Schedule ID, used by the other paper tools |
| `strategy_id` | The MangroveAI strategy |
| `run_id` | MangroveAI's paper run, set once the start succeeds |
| `state` | See the table below |
| `interval_seconds`, `max_evaluations` | The limits you authorized |
| `evaluations` | Slots attempted so far |
| `pending_slot` | An evaluation whose outcome is not yet recorded, or empty |
| `last_result` | MangroveAI's response to the latest evaluation |
| `error` | The latest problem, if any |

For authoritative state from MangroveAI, ask for details with a refresh:

> "Get the paper schedule status, refreshed from the server."

`agent_get_paper_schedule` with `refresh=true` fetches the server's paper
status. That is an upstream call and can use allowance or payment.

### Schedule states

| State | Meaning |
|---|---|
| `starting` | The start request is in flight |
| `active` | Evaluations are being issued on the interval |
| `exhausted` | `max_evaluations` slots have been used; no further requests |
| `pausing` | A pause was requested and the server pause is not confirmed yet |
| `paused` | Stopped locally and on the server |
| `attention` | An evaluation failed or its outcome is uncertain; automatic requests stop |
| `start_rejected` | MangroveAI refused the start; nothing is scheduled |
| `start_uncertain` | The start outcome could not be confirmed; nothing is scheduled |

## Read the results

The paper portfolio lives on MangroveAI, so you read its positions, trades and
evaluations through MangroveAI's tools:

> "Show me the paper positions and trades for that strategy."

The client can use `list_positions`, `get_trades_for_strategy` and
`list_evaluations` with the strategy ID. What those tools return, and its
format, is defined by MangroveAI. A paper trade is a simulated fill in the
server's portfolio: nothing is sent to an exchange or a chain.

Many evaluations will produce no trade. A quiet strategy is normal; its
conditions are simply not met.

## Pause a schedule

> "Pause that paper schedule."

`agent_pause_paper_schedule` stops new local requests at once, then asks
MangroveAI to pause the run. An evaluation already running may finish. Pausing
does not close or sell paper positions. If the server pause fails, the schedule
stays in `pausing` and you retry the same call.

To start again, call `agent_start_paper_schedule` with the same interval and
`max_evaluations`. A paused or exhausted schedule renews its quota on an
explicit, confirmed start, provided no evaluation is left unresolved. Limits on
an existing schedule cannot be changed in place.

## When a schedule needs attention

If an evaluation fails or its outcome cannot be confirmed, including when a
payment is refused, the schedule moves to `attention` and stops issuing
requests. The cause is in its `error` field and in `pending_slot`. Ask the
client to reconcile it:

> "Reconcile that paper schedule."

`agent_reconcile_paper_schedule` retries the unresolved evaluation with its
original run, slot and payment operation identity, so it cannot double-execute
or double-pay. The quota counts attempted slots, and a reconcile reuses the
slot it recovers. A new start is blocked until the pending evaluation is
resolved.

## Watch the cost

Each start, evaluation and refreshed status is a metered call. In x402 mode,
check the budget before a long schedule:

> "Show my x402 spend status."

`x402_spend_status` reports the budget (spent, remaining and whether it is
exhausted; the default cap is $25) and the recent payment ledger. The budget
governs only outbound x402 payments, not API-key usage. When the budget is exhausted,
further paid calls are refused and any schedule evaluation that needs one goes
to `attention`. Raising the cap is your decision: `x402_spend_reset` records a
new cap only after you agree and the client passes `confirm=true`. Do not ask
the client to reset it just to get past a refusal.

## Restart the server and keep your schedules

Schedules are stored in the same SQLite file as everything else, so they
survive a restart. Try it:

```bash
# Stop the server
kill $(cat agent-data/bare.pid)

# Confirm it is gone
sleep 2
curl -s http://localhost:9080/health 2>&1 | head -1
# Expect a connection failure

# Start it again
./scripts/setup.sh --yes --no-mcp --no-verify
```

Then ask the client to list your paper schedules. The schedule is still
`active` and resumes on the next due slot. If the stop interrupted an
evaluation, that slot is kept as `pending_slot` and is retried with its original
identity, so it is not run twice. Do not run more than one agent process
against the same database.

Schedules belong to the upstream identity (API key or payer wallet) that
created them. If you change the API key or payer wallet, schedules created
under the old identity are hidden from the new one.

## REST equivalents

The same service is available under `/api/v1/agent/strategies/paper-schedules`
with the local API key: `POST` to start, `GET` to list, `GET /{schedule_id}` for
status (`?refresh=true` for server state), and `POST /{schedule_id}/pause` or
`/{schedule_id}/reconcile`. The [paper scheduling reference](../../docs/paper-scheduling.md)
covers recovery and limits in more detail.

## How long to paper before going live?

There is no fixed rule. A paper run should span enough of the strategy's own
timeframe to see both winners and losers. As a rough guide, for a 1h strategy
that means several days, and for 4h or 1D strategies one to two weeks. A
schedule covers `interval_seconds * max_evaluations` of wall-clock time, so
size `max_evaluations` to the period you want and start a new schedule when it
is exhausted.

## What not to do in paper

- **Do not mistake a quiet day for a broken strategy.** Zero trades after a few
  evaluations usually means the conditions have not been met. If a full cycle
  of the strategy's timeframe passes with no activity, the signals may be too
  strict for current conditions. Iterate with a new strategy.
- **Do not judge on one fill.** One fill is an anecdote. Wait for both winners
  and losers on the entry and exit paths.
- **Do not edit the strategy mid-run.** Eligibility depends on a backtest of
  the exact configuration, and a changed strategy invalidates the history you
  have accumulated. To try different parameters, save a new strategy,
  backtest it, and run a second schedule.
- **Do not schedule more than you can afford to evaluate.** Every slot is a
  metered call.

## What to take away

- Paper mode is server-owned: MangroveAI keeps the simulated portfolio and the
  agent supplies the schedule.
- A strategy needs a passing backtest of its exact configuration before it can
  be scheduled.
- Starting a schedule needs `confirm=true`, and a failed or uncertain
  evaluation stops the schedule until you reconcile it.
- Schedules survive restarts, but they only advance while the agent is running.

From here you can stop and tinker with more strategies, or continue to
Chapter 06 to set up a wallet and make a small live swap.

[Chapter 06 - Wallet setup](06-wallet-setup.md)
