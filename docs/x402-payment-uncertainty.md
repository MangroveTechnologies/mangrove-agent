# Payment operations and reconciliation

Payment uncertainty belongs to one operation. There is no wallet/network pause
and no five-minute waiting period. New independent requests can proceed
immediately if their price fits the remaining budget. Every uncertain signed
amount remains counted; time passing, a timeout, HTTP rejection, restart, or a
server replay-cache entry never releases money.

## Identity and recovery

HTTP callers can provide a UUID in `X-Payment-Operation-Id`. Reuse it for retries
of the same exact request. Reusing it with changed method, URL, body, payer or
network is rejected. Without a caller ID, the agent assigns one and uses a digest
of those fields to recognize identical pending work. Completed calls without an
explicit ID are new operations on the next invocation. This fallback cannot infer
whether differently worded/parameterized requests represent the same business
intent; callers must carry the same operation ID through a logical retry.

SQLite migration 012 adds durable operation records and links reservations to
operations. Claiming an operation and reserving its payment use IMMEDIATE
transactions. An operation can have only one outstanding authorization. Requests
for different operations share only the existing atomic spending-cap check.

Before a signed HTTP request is sent, its payment header and a random 256-bit
`X-Payment-Recovery-Token` are encrypted with the
existing wallet master key and persisted. Neither a private key nor a plaintext
signature is stored in the operation record. The request URL/body are represented
by a digest; completed results are also encrypted. Missing encryption keys fail
closed without generating replacements. Preserve the database and original
master key together when backing up or moving the agent.

A receiver advertising `X-Payment-Idempotency: v1` supports recovery of the exact
original signed request. A user-triggered retry may resend that same proof to
retrieve the stored result or current operation status. The receiver requires the
separate recovery token and stores only its digest: public on-chain payment
signatures alone cannot unlock cached responses. Legacy callers without a
recovery token cannot retrieve cached results. It does not create a new
nonce, reserve more money, or repeat settlement. Receivers without that capability
leave the operation pending for reconciliation instead of receiving automatic
replacement payments. No purchase retry is scheduled in the background.

The synchronous Mangrove SDK transport and explicit async HTTP payer both use
this policy. The local native MCP donation demo deduplicates pending operations
but its SDK has no result-recovery contract; uncertain demo operations require
reconciliation. Receiver-side MangroveAI SSOT MCP deduplicates exact signed tool
calls and recovers stored results independently of JSON-RPC request IDs.

## Payment versus fulfillment

A confirmed payment can coexist with unfinished execution. A receiver's
`X-Payment-Operation-State: pending` response must not become the final cached
result, even when it includes a successful payment receipt. The agent records the
charge and keeps the operation recoverable. `X402_PAYMENT_UNCERTAIN` includes
`operation_id`, reservation IDs and a `payment_state`; `retry_payment: false`
means no replacement authorization, not a ban on unrelated requests.

`x402_spend_status` exposes pending operation IDs and their pending/settled amounts
in integer micro-USDC, plus unresolved payer/network groups across all periods.
The compatibility fields `payment_pauses: []` and `payment_pause_seconds: 0`
explicitly indicate that no wallet pause is active. `spent_usd` still includes
pending amounts in the current budget period. An explicitly authorized budget
reset retains its existing semantics of granting a new period; it never deletes
historical reservations or clears a pending operation's identity.

## Background reconciliation

Configure `X402_RECONCILIATION_RPC_URLS` in the agent's environment JSON as a mapping
from `eip155:8453` and/or `eip155:84532` to operator-approved RPC URLs. For example:

```json
{
  "X402_RECONCILIATION_RPC_URLS": {
    "eip155:8453": "https://YOUR_BASE_RPC_PROVIDER",
    "eip155:84532": "https://YOUR_BASE_SEPOLIA_RPC_PROVIDER"
  }
}
```

No public RPC fallback is enabled. These queries disclose the public payer,
token and authorization nonce to the configured provider. Configuration is an
operator choice; the implementation's synthetic tests do not contact these URLs.

The existing background scheduler polls every 15 seconds, claims up to five
reservations under a durable lease, and performs bounded read-only RPC requests.
Per-row backoff with jitter and scan progress survive restarts. Calls have finite
timeouts and response limits; HTTP redirects and ambient proxies are disabled.
Only read RPC methods are permitted. Logs contain outcome codes and exception
types, not RPC URLs or provider bodies.

| Evidence | Accounting result |
| --- | --- |
| Valid matching server receipt | Settled, still counted |
| Exact finalized AuthorizationUsed and matching Transfer | Settled, audited |
| Finalized state after validity ends, nonce unused | Released, audited |
| Exact finalized AuthorizationCanceled | Released, audited |
| Timeout, live nonce, missing metadata, unmatched evidence | Remains reserved |

Used nonces require exact receipt matching because cancellation also consumes a
nonce. Transaction discovery scans bounded finalized log windows. Evidence and
ledger changes commit atomically; a concurrent receipt wins over stale evidence.
Releasing a proven non-payment restores capacity within the same authorized cap,
including clearing exhaustion caused by that reservation. It never increases the
cap or creates a new budget period. Old records lacking exact authorization
metadata require operator investigation; the worker never guesses their outcome.

Operators can inspect `x402_reconciliation_jobs` for attempts, next check, outcome
and scan cursor, and `x402_reconciliation_evidence` for applied corrections. Alert
on repeatedly unavailable evidence and aging pending operations. These are
operational recovery cases, not reasons to stop unrelated user requests.

The existing `server/scripts/inspect_x402_payment.py` remains available for
explicit inspection and `--apply` reconciliation. Its public RPC destinations
are separate from the worker's configured endpoints. Use it only with approval
for that disclosure. It never signs or transfers funds.

## Rollout and limits

1. Apply MangroveAI migration 080 using its normal migration runner, then restart
   receiver workers. The new recovery store is required before signed execution.
2. Restart the local agent with `./scripts/setup.sh --yes`; startup applies
   migrations 011/012 as needed. Restart every process sharing that ledger.
3. Configure approved reconciliation RPC URLs and restart the agent to enable
   background checks. Without this configuration, operation isolation/recovery
   still works and uncertain amounts remain reserved.

The receiver stores ordinary bounded responses before sending them. Streaming or
responses above 8 MiB retain their operation/receipt but need application-specific
result storage for replay. A crash during a mutating handler is not evidence that
the mutation failed. Such operations remain unresolved or enter refund review;
they are never blindly executed again. Generic exactly-once fulfillment across
arbitrary external systems is not promised. Completed response recovery and
payment reconciliation do not themselves implement refunds.

Keep operation identity records as tombstones if response payloads are archived;
deleting unresolved identities or resetting nonce caches must never become a
payment retry mechanism. Historical reconciliation, remote CI and live release
acceptance remain separate from local synthetic verification.

## Confirmed refunds

Migration 013 adds a separate refund ledger. Completed paid failures retain their
encrypted recovery credentials for read-only refund-status polling on the
configured Mangrove origin. When approved reconciliation RPCs are configured,
the observer independently checks finalized exact USDC repayments before
crediting the original budget period once. Original charges and limits remain
intact; refunds cannot top up a newer period. `x402_spend_status` includes refund
status. Uncertain authorizations still require payment reconciliation rather
than a refund. Historical failures whose recovery credentials were already
cleared are not automatically imported into this new observer.
