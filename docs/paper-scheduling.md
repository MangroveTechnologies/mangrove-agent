# Agent-scheduled, server-owned paper execution

The agent supplies the clock. MangroveAI owns the saved strategy, balances, positions,
trades and evaluation receipts. This flow does not call the platform scheduler or
run the agent's legacy paper-fill engine.

## Use

Save and backtest a strategy through MangroveAI under the same upstream identity.
It must be unarchived, inactive, use v1/v2 sizing, have valid server cash state,
and have a completed passing backtest whose rules, market and effective execution
settings match the current strategy. v3 is rejected because the existing persisted
evaluator does not support it.

Call `agent_start_paper_schedule` with the server `strategy_id`,
`interval_seconds=300`, `max_evaluations=12`, and `confirm=true` to authorize up to
12 evaluation slots, roughly five minutes apart. The first evaluation is due on
the next local dispatch, within about five seconds. API/x402 usage applies to the
start and evaluation requests. Existing payment limits still apply.

- `agent_list_paper_schedules`: discover local schedule IDs and cached progress.
- `agent_get_paper_schedule`: cached progress; `refresh=true` fetches server state
  and incurs the upstream status-request usage.
- `agent_pause_paper_schedule`: stop new local requests immediately, then request
  server pause. An already running evaluation may finish. Pause does not sell or
  close positions. If server pause fails, the local state is `pausing`; retry pause.
- `agent_reconcile_paper_schedule`: retry an uncertain evaluation with its original
  run, slot and payment operation ID. Recovery also works while paused or pausing
  and leaves scheduling stopped in those states. An expired slot without a
  committed result is refused; the pending slot is retained for authoritative
  investigation, and a new start is blocked until that evaluation is resolved.

The same service is available through authenticated local REST under
`/api/v1/agent/strategies/paper-schedules`: POST to start, GET to list,
GET `/{schedule_id}` for status, and POST `/{schedule_id}/pause` or `/reconcile`.

## Recovery and limits

SQLite stores schedule metadata and the last response, not strategy definitions
or authoritative portfolio balances. Schedules survive restart. The agent must be
running to call evaluations; missed intervals are skipped, not replayed as old
market data. An interrupted pending slot is recovered using its original identity.
A failed/uncertain evaluation stops automatic calls and needs explicit reconciliation.

At most four evaluations run concurrently in one agent process. The evaluation
quota counts attempted slots, including uncertain attempts. Reconciliation reuses
that slot. Exhaustion stops local requests; it does not liquidate positions or
change the server's run state. Pause explicitly to mark the server run paused.
Starting a paused/exhausted schedule with confirmation renews the quota only when
no evaluation remains unresolved. Status exposes `pending_slot`; pause does not
clear it. Late pause responses cannot overwrite a newer command, and a new run
clears the previous run's cached evaluation result. Existing
schedule limits cannot be changed in place.

Schedules are scoped to the configured upstream destination and API key or x402
payer wallet. A changed API key hides schedules authorized under the old key.
To recover after losing local state/credentials, pause the run using authenticated
MangroveAI paper controls, then start a new local schedule. The server permits one
active controller per strategy. Do not run multiple agent processes against the
same SQLite database; the existing agent architecture is single-process.

Stable request IDs prevent duplicate execution. API request accounting still
follows server metering rules; execution deduplication is not a promise that every
repeated API-key HTTP request is free. x402 uses the existing payment-recovery flow.

Paper pause uses the same payment recovery as every other paid tool request.
The scheduling service retains its command ID; the shared transport handles payment
attempts. MangroveAI decides whether a result can be replayed, a paid action that
never started can run, or a proven-unused payment can be replaced. Running/unknown
work is not dispatched again. The agent independently preserves wallet budget checks.

The backend requires migration 115 and trusted `X402_RECOVERY_RPC_URLS` configuration
for chain-evidence recovery. Agent migration 020 adds shared attempt links; the old
pause-specific column from migration 019 remains only for compatibility. Legacy
operations missing verified terms or private credentials remain pending safely.
See `MangroveAI/docs/x402-shared-recovery.md` in the backend repository for the shared
state transitions and rollout order.

## Rollout

1. Apply MangroveAI migration `114_paper_execution.sql` before deploying backend
   code, including its guards on existing persisted-evaluation/status/state paths.
2. Deploy MangroveAI's paper endpoints and smoke-test them with a staging identity.
3. Release/install the SDK checkout containing `execution.paper_start`,
   `paper_pause`, `paper_tick`, and `paper_status`. Older SDKs fail start explicitly.
4. Deploy/restart the agent; its normal migration runner applies
   `018_paper_schedules.sql`, `019_paper_payment_attempt.sql` and `020_x402_shared_recovery.sql`. For local development install the SDK checkout into
   the agent environment; the published SDK dependency alone does not include
   these unshipped methods yet.
5. Verify API-key and x402 flows in staging, including actual payment recovery and
   concurrent-user load, before production rollout.

Existing local broker/live execution is separate and has not been migrated into
this flow. No real trades, production migrations, package publication, or live
payments were performed during implementation.

Start failures are recorded as `start_rejected` for explicit validation/access
rejections, or `start_uncertain` when the server outcome cannot be confirmed.
Neither state dispatches evaluations. An explicit start after rejection uses a new
request ID; recovery of an uncertain start retains the original request ID.
Known paper eligibility explanations are allowlisted; arbitrary upstream payloads
are withheld. Valid upstream correlation IDs are retained for SDK start errors.
