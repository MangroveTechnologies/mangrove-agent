# Synchronous x402 transport

`X402SyncTransport` (`server/src/shared/x402/sync_transport.py`) lets the synchronous
MangroveAI SDK pay for REST calls with x402. It is an httpx transport that sits below
the SDK, so it sees the HTTP 402 challenge while the response headers are still
available. It uses the official synchronous x402 client together with the agent's
custodial signer, network pin, per-payment limit and agent-wide spend budget. Each
exchange gets its own signer and reservations.

## How it is selected

When no `MANGROVE_API_KEY` is configured, `mangrove_ai_client()`
(`server/src/shared/clients/mangrove.py`) builds its SDK client with this transport.
With a key configured, the client uses key authentication and never pays. Neither mode
falls back to the other on an authentication failure.

`create_x402_mangrove_client(environment=..., base_url=..., kb_base_url=...,
wallet_address=...)` builds a payment client explicitly, for the singleton and for
diagnostic scripts. It injects the transport through the SDK's `httpx_client` argument.
The factory:

- requires `environment` to be `local`, `dev` or `prod`, and explicit service URLs;
- disables dotenv loading, SDK automatic retries and redirects;
- refuses any API key inherited from the environment before sending it, without
  changing process environment variables or SDK internals;
- returns a client that is not cached, so close it after use or use it as a context
  manager.

## Payment behavior

- **One authorization per operation.** The transport sends an unsigned request, then
  at most one new signed attempt after a 402. An unresolved operation keeps its
  authorization and budget reservation, and other requests proceed immediately.
  Identical pending HTTP requests recover the same persisted proof only when the
  receiver advertises idempotency support. They never generate a replacement nonce.
  An explicit operation ID also recovers completed results. See
  [operation recovery](x402-payment-uncertainty.md).
- **Atomic budget.** A reservation is made in an SQLite `IMMEDIATE` transaction on a
  dedicated connection, so cap checks and period resets are serialized across
  processes as well as threads. The default cap is $25 (`X402_SPEND_CAP_USD`), and a
  single payment is limited to $1 by default. An oversized quote is refused without exhausting
  the budget. A consumed budget stays latched until you authorize a new period.
- **Uncertain amounts stay counted.** This covers HTTP 402 and 500 errors and earlier
  attempts. A reservation is released only on proof of non-payment: a rollback when
  signing fails before any signature exists, or audited reconciliation against
  finalized chain data.
- **Receipt validation.** The transport validates receipt success, transaction shape,
  payer and network before recording server-reported settlement. A valid header is not
  independent chain confirmation.
- **Request safety.** It refuses auth headers, supplied signatures and requests to
  origins that are not configured. HTTPS is required except for loopback development.
  Redirects are disabled.
- **Request fidelity.** It attaches a stable session identity per transport and the
  resolved wallet address. It preserves the request's bytes, query, method and
  timeout on the retry, and restores the configured finite timeout when the SDK passes
  `None` on ordinary service calls.
- **Free requests need no wallet.** A free call succeeds without a wallet or remaining
  budget. Backup and budget checks run only after a 402.
- **No implicit retries.** SDK automatic retries are disabled for this client, so an
  error returns to the caller instead of silently starting another billable exchange.

## Signing guard

Signing an x402 payment is a narrow, separate door from swap signing. Before the key
is decrypted, `wallet_manager` checks that the typed data is an EIP-3009
`TransferWithAuthorization` with exactly the canonical fields, that the verifying
contract is the hardcoded USDC deployment for the configured chain, and that the
domain matches the supported token: name `USDC` and version `2` on Base Sepolia, name
`USD Coin` and version `2` on Base mainnet. Wrong or missing values are refused before
any wallet secret is loaded.

Each payment records the payer, recipient, amount, network, expiry, asset, validity
window and authorization nonce before any signature is disclosed. Nonces are public
authorization identifiers; signatures and private keys are never stored in these
fields.

## Manual checks

Run these from the repository root with the agent's Python environment.

Observe an anonymous quote without using a wallet or paying:

```sh
python3 server/scripts/watch_x402_quote.py --log agent-data/x402-quotes.jsonl
```

`--url` selects the endpoint and `--watch SECONDS` repeats the request.

Run an explicit, capped payment check against a receiver you control:

```sh
env -u MANGROVE_AGENT_HOME -u MANGROVE_API_KEY ENVIRONMENT=local \
  .venv/bin/python server/scripts/check_x402_e2e.py --case hello --receiver 0xRECEIVER_ADDRESS
```

Without `--pay` the script only inspects the quote. `--pay` authorizes one payment on
the selected network (`--network sepolia`, the default, or `mainnet`). It checks the
expected receiver, chain, token and price, checks the ledger, and confirms the
expected USDC Transfer on chain. A mainnet payment requires `--allow-mainnet` and
supports only the `signals` case, with a maximum of 0.001 USDC.

To inspect one reservation against finalized chain data, use
`server/scripts/inspect_x402_payment.py`, described in
[payment operations](x402-payment-uncertainty.md).

## Development checks

From the repository root:

```sh
.venv/bin/ruff check server/src/ server/tests/
cd server
ENVIRONMENT=test ../.venv/bin/python -m pytest tests/ -q
```

`server/tests/integration/test_x402_sync_transport.py` exercises real custody,
signing, SQLite accounting and the installed MangroveAI SDK against an httpx mock
receiver. It is entirely offline and does not demonstrate on-chain settlement or live
facilitator compatibility. The existing hello-mangrove x402 tests need facilitator
network access.
