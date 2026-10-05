# Marketplace actions with a local wallet

The agent discovers marketplace tools through the configured Markets MCP
`tools/list` endpoint. Markets supplies descriptions, input schemas and versioned
read/ownership metadata; the agent has no copied listing, offer or search models.
Read tools forward requests. Ownership tools prepare a local approval, and
`marketplace_submit` signs and sends it only after confirmation. The generic
`marketplace_prepare` control remains available for clients using that interface. Markets
owns listings, offers, delivery state, ratings, permissions and replay checks.
The agent owns wallet selection, local approval records and signing.

An optional API key identifies the account and its permissions; a wallet signature proves
control of the participant address required by Markets. Reading public listings
in API-key mode needs no wallet. An empty local wallet store does not prove that
the person lacks ownership of a remote resource.

Markets rejections are preserved during preparation and submission, including
their error code and message. MCP marks these responses as errors. A rejected
preparation creates no local approval and signs nothing. Capability availability
comes from the discovered catalog; an unsupported action is not an ownership
denial. Client instructions guide concise presentation without duplicating
Markets authorization rules.

The same local service is available through authenticated REST:
`POST /api/v1/agent/marketplace/prepare` and `/marketplace/submit`.
Local requests use the existing `X-API-Key` protection. The upstream credential
comes from configuration, never from a tool argument or challenge.

## One-time setup

Use the existing wallet creation/import and backup workflow. Import existing
keys using `scripts/stash-secret.sh`, never through chat. Select a backed-up EVM
wallet on Base Sepolia (84532) or Base (8453). XRPL wallets can also be created locally or imported using an XRPL family seed
through the same terminal-only workflow. Ed25519 and secp256k1 master seeds are
supported; regular-key and multisignature wallets are not.

Run the normal `scripts/setup.sh` workflow and select API-key or x402 access.
API-key writes require Execution Manage permission; wallet-only actions do not
require a MangroveAI account or key. Setup checks Markets ownership discovery without
signing, paying or creating a listing. If Markets is unavailable, setup reports
that marketplace actions are unavailable while leaving other agent tools usable.
The selected wallet's backup and network are checked when preparing an action.

The agent fetches `/.well-known/mangrove-marketplace` from the configured
`MANGROVEMARKETS_BASE_URL`. It validates version 1, the ownership audience and
optional XRPL network. That public ownership document receives no credentials.
MCP tool discovery uses the upstream API key only when API-key mode is selected. Neither
discovery path follows redirects or accepts replacement service URLs. HTTPS is required except
for loopback development endpoints. `MANGROVEAI_BASE_URL` remains the separate
identity authority for API-key access; wallet-only actions do not call it.

No audience or Base chain ID needs to be copied into the agent. Markets discovery
supplies Base mainnet (8453) or Base Sepolia (84532); the selected backed-up wallet
must match it. XRPL wallets must also match the advertised network. Existing
`MARKETPLACE_OWNERSHIP_AUDIENCE`, `MARKETPLACE_CHAIN_ID` and
`MARKETPLACE_XRPL_NETWORK` values remain optional pins; mismatches fail closed.
This ownership selection does not change x402 payment settings.

Markets operators configure a distinct `MARKETPLACE_OWNERSHIP_AUDIENCE` per
production deployment. Explicit `ENVIRONMENT=local` (or `APP_ENV=local`) provides
the local test audience automatically. Production has no default identity.
Discovery and signature verification use the same identity. Before submission,
the agent rediscovers settings and checks the selected wallet against the saved
approval; changes invalidate approval before signing.

For isolated local acceptance, keep the existing local Markets and MangroveAI
URLs. No manual ownership configuration edit is needed. Configuration examples
do not isolate existing wallet or payment data automatically.

## Example conversation

User: "List my weather dataset for 1 USDC using my seller wallet."

Claude chooses the user's wallet from `list_wallets`, asking if the choice is
ambiguous, then calls the discovered `marketplace_create_listing` tool:

```json
{
  "title": "Weather dataset",
  "description": "Daily weather observations",
  "category": "data",
  "price_xrp": 1,
  "chain": "base",
  "currency": "USDC",
  "_agent": {"wallet_address": "<selected local wallet address>"}
}
```

`price_xrp` is the existing Markets argument name; with Base/USDC listing
creation its value is denominated in USDC. This adapter does not alter Markets'
separate offer pricing/settlement policy.

Preparation makes no signature. The response contains the exact expanded action,
wallet, network, endpoint, expiry and an `approval_id`. Claude presents those
details and asks the user to approve. After approval, it calls
`marketplace_submit(approval_id=..., confirm=true)`. Arguments cannot be changed
at submission. The agent checks the current account context again, validates the
entire challenge, signs locally and sends the proof to Markets. Claude receives
the remote result; private keys and the raw signature are not returned to it.

The confirmation flag follows the agent's existing trusted-local-client model.
It is not an independent human-presence guarantee: a client with local agent
access can set it. Keep Claude's tool permissions and local access controls
enabled. There is no mobile-wallet popup in this integration.

## Supported actions

| Operation | Arguments supplied by the client |
| --- | --- |
| `marketplace_create_listing` | title, description, category, price_xrp; optional existing listing metadata |
| `marketplace_make_offer` | listing_id; initial Base/USDC or XRPL/XRP/RLUSD offer only |
| `marketplace_accept_offer` | offer_id; XRP escrow also requires escrow_sequence; other lanes require confirmed payment |
| `marketplace_confirm_delivery` | offer_id |
| `marketplace_rate` | offer_id, score, optional comment |

XRPL arguments default to `chain=xrpl, currency=XRP`; Base defaults remain
`chain=base, currency=USDC`. XRP escrow acceptance verifies the buyer, seller,
amount and release conditions on the configured validated ledger. The local
agent signs the ownership approval, not EscrowCreate/EscrowFinish transactions.
Funding and transaction signing/broadcast remain separate workflows.

The adapter sets the seller/buyer/rater address from the selected local wallet.
It refuses conflicting addresses, caller-supplied ownership proofs, arbitrary
operations and payment payloads. It does not automatically sign or pay an x402
challenge. An initial offer can return payment requirements and reserve a listing;
that result is not proof of purchase. Completing the buyer's testnet payment
requires a separately reviewed payment integration. Do not describe this as a
complete end-to-end purchase flow.

## Safety and recovery

The signer reconstructs the ownership-v1 message, including normalized defaults,
argument hash, user, organization, API-key credential type, audience, wallet,
operation, nonce and expiry. The identity is independently read from the
configured MangroveAI authority. General-purpose message signing remains disabled.
Only version-1 metadata from the configured Markets service is accepted.
Ownership actions must use the supported ownership-v1 protocol. The agent binds
the actor to the selected wallet, applies server-declared defaults and preparation
constraints, and validates the schema without fetching external references.
Discovery never grants approval: every ownership action needs its own local
preview and confirmation. The full discovered contract is hashed into the approval
and checked again before signing and sending. A changed contract requires a new
preview; legacy pending approvals without that binding cannot be submitted.
Completed records and payment recovery data are preserved.

Approvals persist in the existing local SQLite database. An atomic transition
claims each approval before any key is decrypted or signature is transmitted.
Concurrent calls cannot submit that approval twice. Completed calls return their
stored result on repeat. A timeout, crash or result-storage failure leaves the
approval submitted and blocked. Do not prepare a replacement action to discover
whether the first succeeded; inspect the remote record and reconcile it first.
No automatic reconciliation or retry with a fresh signature is implemented.

The approval record contains action details and verified account identifiers,
but no private key, API key or signed proof. Treat the local database as private.
Expired unsigned approvals are pruned in bounded batches; completed and uncertain
records remain available for investigation.

Each approval binds the configured API key using PBKDF2-HMAC-SHA256 with
600,000 iterations and a fresh 16-byte random salt. Submission checks that
binding with a constant-time comparison before signing or returning a cached
result. Changing the API key invalidates the approval.

Approvals created with the earlier plain SHA-256 fingerprint are rejected after
this upgrade. Unsubmitted actions need a fresh preview and approval. For completed
or uncertain actions, inspect the existing Markets record instead of preparing a
replacement. Existing records are retained; no schema migration is required.

Automated tests use test keys, isolated SQLite and synthetic remote services.
Actual Claude Code acceptance and Base Sepolia payment settlement are separate
release checks; no deployment or payment is performed by adding this integration.

## Marketplace reads

Markets currently advertises `marketplace_search` and `marketplace_get_listing`.
The agent exposes their server-owned schemas with a reserved `_agent` object for
local credentials, wallet selection and recovery operation IDs. Those controls are
stripped before forwarding; server business arguments and native MCP results are
preserved. Search freshness guidance is owned by Markets. Their upstream mode follows the same configuration rule as signals:
a configured `MANGROVE_API_KEY` selects API-key access and server-enforced quota;
without a key, the agent uses `pay_mcp()` and its local EVM wallet.
The caller's local agent API key is never forwarded to Markets.

API-key mode does not require a wallet, backup confirmation, encryption key or
payment budget. After tool discovery, it sends an authenticated MCP tool call. Authentication,
quota and payment-required errors do not cause automatic wallet fallback, even if
a wallet argument is supplied. Markets enforces billing and prices; MangroveAI
maintains the shared subscription allowance.

Wallet mode uses the existing signer, spending limits and encrypted payment ledger.
Select a backed-up EVM wallet on `X402_NETWORK`, or configure the existing default
payer wallet. Markets must support anonymous MCP initialization and its standard
x402 challenge, receipt and recovery contract for the two allowed read tools.
Ownership writes retain their separate authentication and confirmation requirements.

Both modes connect only to `MANGROVEMARKETS_BASE_URL`. Endpoints must use HTTPS
(HTTP is allowed only on loopback), without URL credentials, queries or fragments.
Requests do not follow redirects or environment proxies.

For an uncertain wallet payment, retain the returned `operation_id` and repeat the
same read arguments, wallet and Markets endpoint while still in wallet mode. The
payer resends its stored proof and private recovery token without authorizing a
replacement payment. Switching to API-key mode does not recover a pending wallet
payment or return a cached wallet result; use the existing payment status tools to
track it. API-key reads do not use the wallet operation ledger.

For an uncertain quota read, retain the operation ID from the result's
`_meta["mangrove/quota"]` or the error's recovery suggestion. Repeat the same
arguments and `_agent.operation_id` in API-key mode. The agent generates an ID before
sending each new call; Markets returns its saved result and MangroveAI's quota
operation is idempotent. A fresh operation ID requests a new billable read.

## Discovery compatibility

Start the updated Markets service before restarting the agent and reconnecting
Claude. If Markets is unavailable or has incompatible metadata, its dynamic tools
are not advertised; local wallet tools remain available. MCP discovery is bounded
by deadlines, schema size/depth and pagination limits. Tool names cannot replace
local preparation or submission controls. No new endpoint may be supplied by a
remote tool description.

Static agent contract snapshots cover local controls only. Remote schemas are
validated at runtime and exercised through MCP integration tests; they are not
copied into the agent's production registry. Test fixtures contain representative
server contracts solely for isolated signing regression tests.

## Server-owned network binding

Markets discovery supplies the EVM network and chain ID. The selected wallet must
match it; a wallet never chooses the marketplace network. Optional local network
pins must match discovery. The agent verifies the exact network in the signed
authorization and invalidates approvals if discovery changes before submission.
The approval preview includes `settlement_network` (`eip155:84532` for Base Sepolia,
`eip155:8453` for Base mainnet). `price.chain: base` in a listing is a family label;
use its recorded `price.network` to determine the purchase network.

Upgrade Markets and agents together and prepare fresh approvals. Older servers
without network discovery fail closed; there is no wallet-derived fallback.


## Approved USDC purchases

Markets supplies the payment terms in its ownership challenge. The local preview
shows the seller recipient, exact token amount and network before any signature.
`marketplace_submit(confirm=true)` persists the offer response and continues its
standard x402 MCP challenge using the existing payer and durable payment ledger.
The signer rejects any changed recipient, amount, token or network. Only that
approved asset/amount may exceed the generic $1 per-call limit; the total spending
budget and wallet-backup requirement remain enforced.

A repeated submission uses the same approval/payment operation and offer. If a
payment response was lost, it sends the saved payment proof rather than signing
a replacement debit. A settled response is cached. A changed tool contract,
identity or configured endpoint invalidates approval. An expired approval cannot
start a new payment; existing signed-payment recovery remains possible. If initial
offer creation itself was interrupted, use Markets' signed offer history and
prepare payment for the existing offer rather than creating another one.

This continuation supports Base USDC purchases, including Base Sepolia when
configured by Markets. Tool-read fees still go to the configured organization
recipient; dataset payments go to the seller. API-key quota covers reads, not the
purchase price. No private keys or signed payment payloads are included in logs.

Before signing an approved purchase, the agent checks the wallet's USDC balance
using the configured network RPC, verifies the chain ID, and reads the token at a
canonical block hash. It checks before creating the offer and again before payment
signing. Insufficient funds returns `PURCHASE_INSUFFICIENT_FUNDS` with the balance,
price and shortfall; an unverifiable balance returns `PURCHASE_BALANCE_UNAVAILABLE`.
Neither result creates a new payment authorization or budget reservation. If an
offer already exists, retrying uses that offer. Recovery of a previously signed
payment does not require the current balance to cover the purchase again.

The spending cap is permission to spend, not the wallet's token balance. These
checks do not lock on-chain funds: another transaction can still change the
balance, so Markets verification, settlement and durable recovery remain required.
