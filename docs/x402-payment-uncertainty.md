# Payment operations and reconciliation

Payment uncertainty belongs to one operation. It never pauses a wallet or network.
New independent requests can proceed immediately if their price fits the remaining
budget. Every uncertain signed
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

The agent keeps a durable SQLite record for each operation and links its
reservations to it. Claiming an operation and reserving its payment use IMMEDIATE
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

The synchronous Mangrove SDK transport ([details](x402-sync-transport.md)), the
explicit async HTTP payer and the MCP payer ([details](remote-mcp-payments.md)) all use
this policy. The local `hello_mangrove` demo tool deduplicates pending operations but
has no result-recovery contract; an uncertain demo operation requires reconciliation.
The MangroveAI receiver deduplicates exact signed tool calls and recovers stored
results independently of JSON-RPC request IDs.

## Payment versus fulfillment

A confirmed payment can coexist with unfinished execution. A receiver's
`X-Payment-Operation-State: pending` response must not become the final cached
result, even when it includes a successful payment receipt. The agent records the
charge and keeps the operation recoverable. `X402_PAYMENT_UNCERTAIN` includes
`operation_id`, reservation IDs and a `payment_state`; `retry_payment: false`
means no replacement authorization, not a ban on unrelated requests.

The error message distinguishes an unresolved authorization from a confirmed
payment whose result is pending. A pending MCP receipt settles the local charge
while preserving the original operation for recovery; it does not release budget
or authorize a replacement payment.

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
token and authorization nonce to the configured provider, so choose a provider you
trust with that metadata. The same URLs back the pre-purchase USDC balance check for
approved marketplace purchases (see [marketplace wallets](marketplace-wallets.md)).

The background scheduler polls every 15 seconds, claims up to five
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

`server/scripts/inspect_x402_payment.py` provides explicit inspection and `--apply`
reconciliation of one reservation. Run it from the repository root with
`ENVIRONMENT=local` and `MANGROVE_AGENT_HOME` unset:

```sh
env -u MANGROVE_AGENT_HOME ENVIRONMENT=local \
  .venv/bin/python server/scripts/inspect_x402_payment.py --reservation RESERVATION_ID
```

Add `--transaction HASH` to match a candidate transaction, or `--apply` to record a
proven correction. It opens the database read-only for inspection and uses fixed
public Base RPC endpoints (`https://mainnet.base.org` and `https://sepolia.base.org`),
which are separate from the worker's configured endpoints, so use it only if you
accept that disclosure. A used nonce can mean settlement or cancellation and is not by
itself a payment receipt. An unavailable finalized-state RPC produces an unavailable
result, never a release. It never signs or transfers funds.

## Limits

Without configured reconciliation RPC URLs, operation isolation and recovery still
work and uncertain amounts stay reserved.

The receiver stores ordinary bounded responses before sending them. Streaming
responses, and responses above 8 MiB, keep their operation and receipt but need
application-specific result storage for replay. A crash during a mutating handler is
not evidence that the mutation failed. Such operations remain unresolved or enter
refund review; they are never blindly executed again. Generic exactly-once
fulfillment across arbitrary external systems is not promised. Completed-response
recovery and payment reconciliation do not themselves implement refunds.

Keep operation identity records as tombstones if you archive response payloads.
Deleting unresolved identities or resetting nonce caches must never become a payment
retry mechanism. After upgrading the agent, restart every process that shares the
ledger so they all apply the same accounting rules.

## Confirmed refunds

A separate refund ledger tracks refunds. Completed paid failures retain their
encrypted recovery credentials for read-only refund-status polling on the
configured Mangrove origin. When approved reconciliation RPCs are configured,
the observer independently checks finalized exact USDC repayments before
crediting the original budget period once. Original charges and limits remain
intact; refunds cannot top up a newer period. `x402_spend_status` includes refund
status. Uncertain authorizations still require payment reconciliation rather
than a refund. Failures whose recovery credentials were already cleared are
not imported into the observer.
