## Summary

Use MangroveAI as the authority for saved strategies and paper execution, remove overlapping agent tools, and let the local server schedule bounded evaluation requests.

## What

- Remove duplicate exposed strategy saving/evaluation/history tools where MangroveAI already provides the implementation. Preserve distinct local execution consumers.
- Add local MCP/REST controls to start, inspect, list, pause and reconcile server-owned paper schedules.
- Persist schedule/controller/run identities, evaluation limits and pending slots in SQLite. The local server dispatches evaluations without requiring the chat agent to stay involved.
- Reuse shared x402 payment recovery and local spending controls for all paid requests, including pause.
- Preserve unresolved evaluations when paused; allow reconciliation while stopped and refuse a new start until the pending evaluation is resolved.
- Guard against delayed pause responses overwriting a newer command, and clear stale evaluation results when starting a new run.
- Surface useful start failures and update tool descriptions, contracts, migrations 018–020 and rollout documentation.

## Why

The agent should coordinate when to evaluate while MangroveAI owns the strategy and paper ledger. Removing duplicate tools avoids reading unrelated local records instead of server data. Durable scheduling and shared recovery keep failures visible and preserve the exact operation needed for reconciliation without silently resuming trading.

## Tests performed

- [x] Post-fix agent regression selection: **358 passed**, covering scheduling, shared payment transport, payment operations/recovery, payer controls, upstream errors, MCP tools and contracts.
- [x] Regression cases cover paused/pausing reconciliation, retained pending slots after failed recovery, SQLite reconnection, and delayed pause success/error after a newer start.
- [x] Local chat acceptance exercised bounded paper evaluations, synthetic buy/sell history, evaluation history, no open positions and matching local/server paused states.
- [x] `git diff --check` passed.

Automated upstream/payment failures are synthetic. Local acceptance does not replace staging facilitator recovery or multi-user load verification.

## Rollout notes

Deploy the required MangroveAI endpoints/migrations and install the SDK release containing paper execution methods before upgrading the agent. Apply local migrations 018–020. One agent process per SQLite database remains the supported model. Expired unresolved evaluations remain blocked for authoritative investigation; pause does not liquidate positions.
