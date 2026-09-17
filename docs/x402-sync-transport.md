# B4: synchronous x402 transport

Implemented on `feat/x402-sync-transport`, based on B3 (`fe77a79`).

`X402SyncTransport` handles the REST 402 challenge below the synchronous
MangroveAI SDK, while headers are still available. It uses the official synchronous
x402 client and the existing custodial signer, network pin, $1 per-payment limit,
and agent-wide spend budget. Each exchange owns its signer and reservations.
The existing API-key singleton and `reset_clients()` behavior are unchanged.

`create_x402_mangrove_client(environment=..., base_url=..., kb_base_url=...,
wallet_address=...)` explicitly injects the transport through the SDK's supported
`httpx_client` argument. The returned SDK supports a context manager and must be
closed. B5 will select the authentication mode automatically. This factory requires
explicit endpoints and environment, disables dotenv loading, and refuses any API
key inherited from the environment before sending it. It does not change process
environment variables or SDK internals.

## Payment behavior

- Send an unsigned request, then at most one signed retry on a 402. A subsequent
  call creates a fresh nonce; authorizations are never replayed from earlier calls.
- Reserve budget in an SQLite IMMEDIATE transaction on a dedicated connection,
  serializing cap checks and period resets across processes as well as threads.
- Keep uncertain signed authorizations counted, including HTTP 402/500 errors and
  earlier attempts. Only failure before signature disclosure releases a reservation.
  Its atomic rollback clears an exhaustion latch only when releasing that positive
  current-period reservation reduces usage from full to below the cap.
- Validate receipt success, transaction shape, payer and network before recording
  server-reported settlement. A valid header is not independent chain confirmation.
- Refuse auth headers, supplied signatures, and requests to unconfigured origins.
  Require HTTPS except for loopback development. The factory disables redirects.
- Attach a stable session identity per transport and the resolved wallet address.
- Preserve request bytes, query, method and timeout on the retry. Restore a finite
  configured timeout when mangroveai 1.16 passes `None` on ordinary service calls.
- Free requests can succeed without a wallet or remaining payment budget.
- Disable SDK automatic retries for this explicit payment client: errors return
  to the caller rather than implicitly starting additional billable exchanges.

## Verification and remaining release gates

Offline integration tests exercise real custody, signing, SQLite accounting and
the installed MangroveAI SDK with an httpx mock receiver. They do not demonstrate
on-chain settlement or live facilitator compatibility.

Run the repository checks from the root:

```sh
.venv/bin/ruff check server/src/ server/tests/
.venv/bin/python scripts/sync-michael-skills.py --verify-manifest
cd server
ENVIRONMENT=test ../.venv/bin/python -m pytest tests/ -q
```

The existing hello-mangrove/server x402 tests need facilitator network access;
the new `tests/integration/test_x402_sync_transport.py` receiver is entirely
offline. Local validation uses the existing Python 3.14 environment; CI uses 3.12.

The original Base Sepolia hello and signals tests succeeded on 2026-09-16:

- hello: `0x9a0ccaf66ec62f2b76e5b43abae555b42200b8fc5d47f78f68db67cee450d32c`
- signals: `0xacd0a197ea693356c3f28ab81429ebbc8e27596f3ae3bfa3fd57a6a98d27edaa`

These are historical pre-hardening results. Subsequent user tests verified hello
(`0xb1b8bdccb56b0e2b3fc4d5cc6daebb73a562487eda2d486b1ce4bf4c4189e96c`) and signals
(`0x410d1461c953c2ecec2e6027c5fa1708367160d0c7283c2df378bcc0cd8df029`).
Those results predate the second audit fixes below. Restart both services before
any new live check. No mainnet readiness is implied.

Domain name/version is now pinned for the two supported USDC deployments:
Base Sepolia `USDC` / `2`; Base mainnet `USD Coin` / `2`. Wrong or missing values
are refused before loading wallet secrets.

B3 merged as PR #173. Its default cap is $25. An oversized quote is refused
without exhausting the entire budget; a consumed budget remains latched until the
user authorizes a new period. A failed HTTP response no longer frees a signature.

## Audit remediation and restart

The audit regressions cover multiprocess overspend, concurrent period resets,
transaction rollback, failed/mismatched receipts, repeated HTTP errors, domain
pinning, sensitive exception text, log redaction, private files, safe secret-entry
transport and upgrade behavior. Final agent validation on Python 3.12 with
`server/requirements.lock`: **825 passed, 2 opt-in live tests skipped**.

Migration `009_x402_uncertain_authorizations.sql` restores historical reservations
released solely for HTTP rejection/error or a later retry. It also demotes old
settled rows without a valid transaction hash to uncertain authorizations. Amounts
and period history are preserved. Available budget may decrease on upgrade; do not
reset it automatically to hide uncertainty. Inspect the chain and ledger first.

Stop and restart every process paying from this database before retesting; an old
payer process still has the old accounting rules. Agent startup applies migration
009. The manual check script is not a substitute for this restart.

The related MangroveAI fix returns HTTP 503 before payment verification/resource
delivery if the replay cache or verifier is unavailable; API-key handling remains
with the existing auth decorators. Restart `mangroveai-mangrove-app-1` to load it.
Redis must be available for paid requests.

The secret-entry CLI now reads privately in Python, never exports the secret, and
sends only to validated loopback origins without proxies or redirects. Invalid
wallet input and signing errors omit dependency messages and sensitive causes.
Local config/state files use owner-only permissions. HTTPX request logging is
suppressed; structured diagnostics mask addresses and strip URL credentials/query.
Diagnostic files contain safe summaries, not raw envelopes or response bodies.
The financial ledger intentionally retains exact public addresses for accounting.

Run the anonymous quote first (no wallet use, no payment):

```sh
cd /Users/neel/Documents/Work/mangrove-agent
python3 server/scripts/watch_x402_quote.py --log agent-data/x402-quotes.jsonl
```

Then use `server/scripts/check_x402_e2e.py --help` for the explicit, capped testnet
payment check. It validates the expected receiver/chain/token/price, checks the
ledger, and confirms the expected USDC Transfer on-chain. The two opt-in live
checks have not been repeated by the remediation tests.


## Second audit: nonce evidence and precise unsigned rollback

Migration 010 adds `authorization_nonce`, `asset` and `valid_after`. New payments
persist these alongside payer, recipient, amount, network and expiry before a
signature is disclosed. Nonces are public authorization identifiers; signatures
and private keys are not stored in these fields. Legacy rows remain NULL and
keep their previous state and budget impact. A migration cannot recover a nonce
that was never recorded.

`release_unsigned` is restricted to the signing-failure path. It runs under the
same IMMEDIATE transaction as budget accounting, does not release settled rows,
and cannot unlock a different period or unlock anything on a repeated call.
Generic release and uncertain HTTP/network outcomes do not use this rollback.
The tests include simultaneous rollback and reset in separate processes.

Read-only inspection (after restarting the agent to apply migrations):

```sh
env -u MANGROVE_AGENT_HOME ENVIRONMENT=local \
.venv/bin/python server/scripts/inspect_x402_payment.py --reservation RESERVATION_ID
```

Run from the repository root. The checker opens SQLite read-only and uses public
RPC reads only. It checks the recorded network, queries a finalized block, then
pins the authorizationState call to that block hash. Unsupported finalized-state
RPCs produce an unavailable result, never a release. A used nonce can mean
settlement or cancellation and is not itself a payment receipt. Expired and
unused finalized state is evidence for review, not an automatic ledger update.
Legacy rows without metadata explicitly remain unresolved.

The second server hardening withholds read responses when settlement fails and
settles non-read methods before execution. Failed paid execution carries its
receipt and is recorded for refund review. A client must retain any uncertain
authorization and must not automatically sign a replacement. Revalidate the live
flow after restart; regression tests send no funds.
