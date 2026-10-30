---
name: setup-kraken
description: >-
  Use when the user wants to connect their own Kraken account to the agent - "set up Kraken", "connect my Kraken", "trade on Kraken",
  "add/use my Kraken API key", "I want to trade on a CEX", or when moving toward
  live centralized-exchange trading on Kraken. Guides the user to create a
  LEAST-PRIVILEGE Kraken API key, store it securely without ever pasting it in
  chat, and explains the local bring-your-own-key (BYOK) model: the client-side
  SDK connects to Kraken DIRECTLY with the user's key - the key stays on the
  user's machine and never reaches a Mangrove server. The keyless OAuth path
  (Mangrove holds the grant and submits on the user's behalf, server-side) is
  the sibling connect-kraken skill, NOT this one - offer it when the user
  doesn't want to create or manage an API key. Trigger with phrases such as "connect Kraken", "set up my Kraken key", or "trade on Kraken".
version: 1.0.0
author: Mangrove Technologies
license: MIT
allowed-tools: Read, Grep, Glob, Bash(./scripts/*)
compatibility: Claude Code plugin; requires the local mangrove-agent repository and a Kraken account for connection setup.
tags: [mangrove, kraken, exchange, trading, byok]
---

# Setup Kraken (local BYOK)

The agent's job in this skill: walk the user through connecting **their own
Kraken account** with **their own Kraken API key kept on their machine**. This
is the **local BYOK** mode. In this mode the agent can check balances, dry-run
orders with `cex_validate_order`, and report fills with `cex_sync_fills`. It does
not place real orders with the user's key; real Kraken orders go through the
keyless `connect-kraken` mode.

## The model - say this up front

- **Your key, your machine, direct to Kraken.** In this mode the client-side
  Mangrove SDK connects to **api.kraken.com directly** with the user's API key.
  The key **never goes to a Mangrove server** and is never sent in chat.
- **Kraken is one venue in a multi-venue aggregator.** The Mangrove Markets API
  spans DEX venues (1inch / XPMarket / Jupiter) and CEX (Kraken). This skill
  covers the Kraken CEX venue via the user's own key.
- **There is a second mode (not this skill): keyless OAuth2.** The user
  authorizes Mangrove in a browser and the Mangrove platform holds the grant and
  submits orders on their behalf. That is the `connect-kraken` skill; offer it
  when the user wants real Kraken orders or does not want to manage a key.

## Security primer - give this unprompted (mirror the wallet Stage-4.5 primer)

1. **Never paste your API key or secret into this chat.** The agent must never
   see it. `.claude/hooks/block-wallet-secrets.sh` guards pasted secrets - don't
   work around it. The key is entered out-of-band (see Step 3).
2. **Least privilege.** Enable only the permissions needed to trade; leave
   **Withdraw Funds OFF**. A trading key should never be able to move funds off
   the exchange.
3. **Real funds.** Kraken spot has **no testnet** - orders execute against real
   balances. Start with a tiny amount and use **validate-only** (dry-run) orders
   before any real fill.
4. **Scope it down.** Set an **IP allowlist** on the key and a **key expiry** if
   the user can; consider a sub-account dedicated to agent trading.

## Step 1 - Create the API key on Kraken

Direct the user (in their browser, signed in to Kraken):

> Kraken -> **Settings -> API -> Create API key**.

**Enable (trading, least privilege):**
- Query Funds
- Query Open Orders & Trades
- Query Closed Orders & Trades
- Create & Modify Orders
- Cancel/Close Orders

**Leave OFF:**
- **Withdraw Funds** (critical - never enable for an agent key)
- Account Management / Account Transfers
- Export Data / WebSockets (unless a later feature needs them)

**Optional hardening:** IP allowlist (the machine running the agent), key
expiry, nonce window. Have the user copy the **API Key** and **Private Key**
(secret) - Kraken shows the secret only once.

## Step 2 - Confirm scope before storing

Read back what they enabled and confirm **Withdraw is OFF**. If withdrawal is
enabled, tell them to delete the key and recreate it without it - do not proceed
with a withdrawal-capable key.

## Step 3 - Store the key securely (never in chat)

The key is stored **encrypted at rest on the user's machine** (Fernet, same
SecretVault model as wallet secrets) and entered out-of-band in their terminal  - 
**not** pasted into this chat. Follow the same pattern as wallet import:

- The user runs the Kraken-key stash command in a terminal (the agent never sees
  the plaintext); the agent receives only an opaque, TTL-bound vault reference.

In a terminal, the user runs:

```bash
./scripts/stash-kraken-secret.sh
```

It prompts for the API key and private key with input hidden, stores them in the
agent's in-process vault, and prints a short-lived `vault_token`. The user gives
you that token, and you call `cex_connect_kraken` with it. The agent then saves
the credentials encrypted at rest and consumes the vault entry.

## Step 4 - Verify

Confirm with `cex_status`, then a read-only `cex_balances` call to prove the key
works and is correctly scoped. Then run one `cex_validate_order` dry run to
confirm the order path without a fill. `cex_sync_fills` sends the account's
Kraken trade statistics to the MangroveMarkets telemetry service; it does not
write them to the local trade log.

## Guardrails - never

- Never ask the user to paste the API key/secret in chat; never echo or store it
  in plaintext, logs, config files, or the DB.
- Never proceed with a key that has **Withdraw Funds** enabled.
- Never place a live order without an explicit user go; default to validate-only
  for the first order and start with a small size.
- Don't conflate this with OAuth2 - if the user wants Mangrove to trade on their
  behalf without holding a key, that's the server-side OAuth2 mode (separate).

## Where this fits

This is the "connect your exchange" step for Kraken. Strategy authoring,
backtests and paper schedules need no Kraken key.
