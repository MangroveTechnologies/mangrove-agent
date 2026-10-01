# Marketplace actions with a local wallet

Claude Code calls the local agent's `marketplace_prepare` and
`marketplace_submit` MCP tools. The agent discovers the supported ownership
schema and invokes the existing MangroveMarkets tools over native MCP. Markets
owns listings, offers, delivery state, ratings, permissions and replay checks.
The agent owns wallet selection, local approval records and signing.

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

Configure these fields in the selected agent environment configuration, then
restart the agent. For isolated local acceptance:

```json
{
  "MANGROVEMARKETS_BASE_URL": "http://127.0.0.1:8081",
  "MANGROVEAI_BASE_URL": "http://localhost:5002/api/v1",
  "MARKETPLACE_OWNERSHIP_AUDIENCE": "mangrove-markets-local-ownership-test",
  "MARKETPLACE_CHAIN_ID": 84532,
  "MARKETPLACE_XRPL_NETWORK": "testnet"
}
```

Use the existing secure setup for `MANGROVE_API_KEY`, with Execution Manage
permission. Do not put credentials in prompts. The audience must exactly match
the Markets deployment. Both URLs must be explicitly trusted; HTTP is permitted
only for loopback. Outside local testing use HTTPS. The chain ID is a local
wallet-selection guard: ownership-v1 messages bind a deployment audience and
`base`, not a numeric chain ID. Configure each deployment with a distinct audience
and the appropriate network. This does not change the remote payment network. For XRPL, set
`MARKETPLACE_XRPL_NETWORK` to match the Markets deployment
`XRPL_NETWORK` (testnet, devnet or mainnet). The selected wallet must match.
The same two tools select Base or XRPL from the stored wallet; the model does
not choose a cryptographic algorithm. Base configuration remains supported
without an XRPL setting, and an XRPL-only setup may omit the Base chain ID.

Missing marketplace configuration disables these actions without affecting
other agent tools. Use a dedicated test agent data directory when testing with
existing funded wallets elsewhere; configuration examples do not isolate data
automatically.

## Example conversation

User: "List my weather dataset for 1 USDC using my seller wallet."

Claude chooses the user's wallet from `list_wallets`, asking if the choice is
ambiguous, then calls:

```json
{
  "operation": "marketplace_create_listing",
  "wallet_address": "<selected local wallet address>",
  "arguments": {
    "title": "Weather dataset",
    "description": "Daily weather observations",
    "category": "data",
    "price_xrp": 1,
    "chain": "base",
    "currency": "USDC"
  }
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
Remote tool discovery is an allowlist compatibility check, not automatic trust
in newly advertised tools. Only the five reviewed contracts are supported.

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

Automated tests use test keys, isolated SQLite and synthetic remote services.
Actual Claude Code acceptance and Base Sepolia payment settlement are separate
release checks; no deployment or payment is performed by adding this integration.
