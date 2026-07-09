---
name: connect-kraken
description: >-
  Use when the user wants to connect their Kraken account to trade through the
  agent WITHOUT creating or handling an API key — "connect Kraken with OAuth",
  "log in with Kraken", "connect Kraken without a key", "I don't want to manage
  a Kraken key". This is the KEYLESS (OAuth) mode: the user consents once in a
  browser and the Mangrove platform holds the grant and trades on their behalf.
  It is the sibling of the BYOK `setup-kraken` skill — if the user is willing to
  create and hold their own key locally, use setup-kraken instead; if they want
  Mangrove to manage access, use this.
---

# Connect Kraken (keyless, OAuth)

The agent's job here: connect the user's **Kraken account** for trading through
the agent **without the user ever creating, copying, or holding an API key**.

## The model — say this up front

- **No key, ever.** The user consents once on Kraken's own site (a browser
  OAuth screen). Mangrove's platform holds the resulting grant, KMS-encrypted,
  and mints a short-lived, scoped key **per order** — nothing long-lived exists
  anywhere, and no credential is ever pasted in chat or stored on this machine.
- **You choose what you authorize.** At connect time you pick a mode:
  - **view** — read-only: balances and history. (default)
  - **execute** — view **plus** order placement/cancellation.
  The choice drives what Kraken's consent screen grants; Mangrove can never do
  more than you approved. Deposits/withdrawals are never requested.
- **Revocable two ways.** Disconnect in Mangrove, or revoke the app under
  Kraken's Connected Apps settings.
- **Contrast with BYOK (`setup-kraken`):** that mode keeps your own key on your
  machine and talks to Kraken directly, no Mangrove account needed. This mode
  needs a Mangrove account but never asks you to touch a key.

## Step 1 — Start the connect

Call `POST /api/v1/agent/cex/oauth/connect-start` with `{"mode": "view"}` or
`{"mode": "execute"}`. It returns `{authorize_url, state}`.

Show the user the `authorize_url` and tell them: **open this in your browser,
sign in to Kraken, and approve.** If they chose `execute`, the consent screen
will list trading permissions; if `view`, it won't. That screen is the consent
— read it before approving.

## Step 2 — Wait for the connection

Poll `GET /api/v1/agent/cex/oauth/status` until `connected` is true. Confirm the
returned `connection.mode` matches what they intended (`view` vs `execute`).

## Step 3 — Use it

- `GET /api/v1/agent/cex/oauth/balances` — balances (works in either mode).
- `POST /api/v1/agent/cex/oauth/orders` — place or dry-run an order. Always run
  once with `"validate_only": true` first and show the user the venue's own
  description of what would execute; place the live order only on their explicit
  go-ahead. An `execute`-mode connection is required — a `view` connection is
  refused by the platform.

## Guardrails — never

- Never ask the user for a Kraken API key or secret in this flow. If they want
  to provide one, that's the **other** skill (`setup-kraken`), not this one.
- Never place a live order without a preceding validate-only preview the user
  approved.
- Never claim a trade is attributed/placed without echoing the returned
  `tx_ids` and venue description.

## Where this fits

`setup-kraken` (BYOK) and `connect-kraken` (OAuth) are siblings. Pick by the
user's preference: hold-your-own-key-locally vs let-Mangrove-manage-access.
Everything downstream (validate-then-place, telemetry) is the same.
