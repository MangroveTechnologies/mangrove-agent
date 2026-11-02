# Agent-scheduled, server-owned paper execution

In this flow the agent supplies the clock and MangroveAI does the work. MangroveAI
owns the saved strategy, balances, positions, trades and evaluation receipts. The
agent decides when to ask for an evaluation, enforces how many it may ask for, and
keeps a small local record of that schedule. This flow does not use the platform
scheduler, and it does not use the agent's local paper-fill code described in
[strategy lifecycle](strategy-lifecycle.md).

## Requirements

The strategy must be saved and backtested through MangroveAI under the same upstream
identity the agent uses (an API key or the x402 payer wallet). MangroveAI decides
whether a strategy is eligible and rejects a start with one of these reasons:

- The strategy has no passing backtest of its exact configuration and market.
- The strategy is not backtested, or is archived.
- The strategy uses v3 position sizing, which server-owned paper execution does not
  support yet.
- The strategy already belongs to another execution flow.
- The strategy does not have valid server-owned paper balances.

The installed MangroveAI SDK must provide the paper execution methods. Otherwise
`agent_start_paper_schedule` fails with a message asking you to install a release with
paper execution support.

## Starting a schedule

Call `agent_start_paper_schedule` with:

- `strategy_id` - the MangroveAI strategy id, as a UUID.
- `interval_seconds` - one of 60, 300, 900, 3600, 14400 or 86400.
- `max_evaluations` - an integer from 1 to 10000.
- `confirm` - must be `true`. Recurring evaluations can incur API or x402 usage, and
  the call is refused without this confirmation.

For example, `interval_seconds=300` and `max_evaluations=12` authorize up to 12
evaluation slots, roughly five minutes apart. The scheduler checks for due
evaluations every five seconds, so the first one runs within about five seconds of
the start. Your existing payment limits still apply to the start request and to every
evaluation.

## Controls

| Tool | What it does |
| --- | --- |
| `agent_list_paper_schedules` | Lists local schedule IDs and cached progress for the current upstream identity. Makes no upstream call. |
| `agent_get_paper_schedule` | Returns cached progress. With `refresh=true` it also fetches server state, which counts as an upstream status request. |
| `agent_pause_paper_schedule` | Stops new local requests immediately, then asks the server to pause. An evaluation already running may finish. Pause does not sell or close positions. |
| `agent_reconcile_paper_schedule` | Retries an evaluation whose outcome is unknown, using the same run and slot. |

The same service is available through authenticated local REST under
`/api/v1/agent/strategies/paper-schedules`:

- `POST` starts a schedule, with the same fields as the tool.
- `GET` lists schedules.
- `GET /{schedule_id}` returns status. Pass `?refresh=true` to fetch server state.
- `POST /{schedule_id}/pause` pauses a schedule.
- `POST /{schedule_id}/reconcile` reconciles an unresolved evaluation.

## Schedule states

A schedule is in one of these states:

| State | Meaning |
| --- | --- |
| `starting` | The start request is in flight. |
| `active` | The agent requests an evaluation each interval. |
| `attention` | An evaluation failed or its outcome is unknown. Automatic calls stop until you reconcile. |
| `pausing` | Local requests are stopped but the server pause is not confirmed. Retry pause. |
| `paused` | The server run is paused. |
| `exhausted` | `max_evaluations` slots have been used. Local requests stop. |
| `start_rejected` | MangroveAI explicitly refused the start. No evaluations are scheduled. |
| `start_uncertain` | The start outcome could not be confirmed. No evaluations are scheduled. |

A start fails as `start_rejected` for an explicit validation or access rejection. It
fails as `start_uncertain` when the server outcome cannot be confirmed. An explicit
start after a rejection uses a new request ID. Recovering an uncertain start keeps the
original request ID. The agent shows only known eligibility explanations from
MangroveAI, not arbitrary upstream error payloads.

## Recovery and limits

SQLite stores schedule metadata and the last response. It does not store strategy
definitions or authoritative portfolio balances. Schedules survive a restart.

- **The agent must be running.** Evaluations are requested only while the agent runs.
  Missed intervals are skipped, not replayed against old market data.
- **Interrupted evaluations recover with their original identity.** The pending slot
  is retried with the same run and slot, so MangroveAI can recognize it.
- **A failed or unknown evaluation stops automatic calls.** The schedule moves to
  `attention` and needs explicit reconciliation. Reconciliation also works while a
  schedule is `paused` or `pausing`, and leaves scheduling stopped in those states.
- **Expired slots are not forced.** If a slot expired without a committed result,
  reconciliation is refused, the pending slot is kept for investigation, and a new
  start is blocked until that evaluation is resolved.
- **At most four evaluations run at once** in one agent process.
- **The quota counts attempted slots,** including attempts with an unknown outcome.
  Reconciling reuses its slot. When the quota is used, the schedule becomes
  `exhausted` and local requests stop. This does not liquidate positions or change
  the server's run state. Pause explicitly to mark the server run paused.
- **Restarting a paused or exhausted schedule renews its quota,** but only when no
  evaluation is unresolved. Starting again with different limits is refused: the
  limits of an existing schedule cannot be changed in place.
- **Status exposes `pending_slot`.** Pause does not clear it. A late pause response
  cannot overwrite a newer command, and a new run clears the previous run's cached
  evaluation result.

Schedules are scoped to the configured upstream destination and the API key or x402
payer wallet. If you change the API key, schedules authorized under the old key are
hidden. To recover after losing local state or credentials, pause the run using
authenticated MangroveAI paper controls, then start a new local schedule. MangroveAI
permits one active controller per strategy. Do not run several agent processes against
the same SQLite database; the agent is a single-process design.

## Costs and payment recovery

Stable request IDs prevent duplicate execution. They do not make every repeated
API-key HTTP request free: usage accounting follows MangroveAI's metering rules.

In x402 mode, paper calls use the same payment path as every other paid request, with
the same budget checks, spending cap and operation ledger. An uncertain payment keeps
its amount reserved and is never replaced automatically. See
[payment operations and reconciliation](x402-payment-uncertainty.md). To let the agent
verify uncertain payments against chain data, set `X402_RECONCILIATION_RPC_URLS`
as described there.

Paper tools do not touch live execution. Robinhood and DEX execution are separate
(see [strategy lifecycle](strategy-lifecycle.md) and
[Robinhood execution](robinhood-execution.md)).
