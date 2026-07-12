# Wallet Presentation

Rules for `create_wallet`, `import_wallet`, `get_balances`, and any future wallet tooling.

## Core invariant -- keys are generated, stored, and signed with LOCALLY

Wallet **key generation, storage, and signing all happen on this machine, in
the agent process** -- the private key never crosses the network:

- `create_wallet` mints the keypair **in-process via `eth_account`** (see
  `wallet_manager.create_wallet`). It does NOT call a remote server to generate
  keys. (`sign()` likewise decrypts + signs locally.)
- **`LOCAL_AGENT_URL`** (default `http://localhost:9080`) is this agent's own
  surface -- wallet/secret ops (the SecretVault + `reveal`/`stash`/`confirm`
  scripts) live here. It is distinct from **`MANGROVEMARKETS_BASE_URL`**, the
  *remote, keyless* MangroveMarkets server used only for DEX quotes / routing /
  balances / broadcast. The markets server never receives a private key.

## Core invariant -- agent NEVER sees plaintext keys

After phase-2 security rework, wallet secrets flow out-of-band:

- `create_wallet` returns a `vault_token` (opaque, TTL-bound, single-read). User runs `./scripts/reveal-secret.sh <vault_token>` in a terminal to see plaintext.
- `import_wallet` accepts a `vault_token` (from `./scripts/stash-secret.sh`). Never accepts a raw key.
- Reveal-on-demand: `./scripts/reveal-secret.sh --address <addr>`.

If a user pastes a private key or mnemonic, `.claude/hooks/block-wallet-secrets.sh` intercepts and refuses. Don't work around it.

## Defaults -- testnet-first; mainnet only on explicit ask

For `create_wallet`, default to **testnet** unless the user says "mainnet" / "real money" / "live trading":
- Chain: `evm`
- Network: `testnet`
- Chain ID: `84532` (Base Sepolia)

State: *"Creating your wallet on Base Sepolia (testnet). You can fund it from a free sepolia-ETH faucet and practice the signing + swap flow with zero real-money risk."*

**Mainnet** (only when explicitly asked): chain `evm`, network `mainnet`, chain ID `8453` (Base). Always flag the switch: *"Switching to Base mainnet — this wallet will transact real funds. Start with 1-5 USDC test deposit and use paper mode before any live allocation."*

**Why testnet-first:** new users learn the signing + swap mechanics safely. A compromised key on testnet is free to recover from; on mainnet it costs real USDC (see the 2026-04-24 EIP-7702 drain that prompted the hard signing guard).

## Signing guard -- what the agent can and cannot sign

Hard invariant enforced at `server/src/services/wallet_manager.py::_validate_sign_target`. The agent signs ONLY:

1. Direct calls to known 1inch AggregationRouters (V5 `0x1111111254...A960582`, V6 `0x111111125421...f8842A65`).
2. ERC-20 `approve(spender, amount)` where the spender is a 1inch router (required before a swap).

Anything else -- arbitrary token transfers to EOAs, non-1inch DEX routing, EIP-7702 set-code txs, `authorizationList` fields, EIP-191 `personal_sign` messages -- is refused **before** the private key is decrypted. Defense in depth against SDK compromise, supply-chain attacks, and phishing flows that ask the agent to sign a delegation the user didn't understand.

If the SDK legitimately routes through a different aggregator one day, the `_ONEINCH_ROUTERS` allowlist must be expanded explicitly with review -- never bypass the guard silently.

## `create_wallet` output

### NEVER
- Echo `vault_token` or any wallet secret in prose more than the one tool-response presentation.
- Ask user to paste a private key. Direct them to `stash-secret.sh`.
- Quote/screenshot/save the vault_token after `reveal-secret.sh` -- entry is consumed on reveal; id is useless after.

### ALWAYS
- Display wallet **address** in a copy-friendly code block, clearly labeled.
- Include block explorer link: Base -> `https://basescan.org/address/<ADDRESS>`, Ethereum -> `https://etherscan.io/address/<ADDRESS>`, Arbitrum -> `https://arbiscan.io/address/<ADDRESS>`.
- Tell user to back up secret NOW using `reveal_cmd` from tool response.
- Explain `master_key_source` in plain language so user knows where the encryption key lives on this machine.
- Describe `secret_type` so user picks the right MetaMask import path (`private_key` -> Import Account > Private Key; `mnemonic` -> Import Account > Secret Recovery Phrase).
- Surface `backup_required` flag: live trading gated until `confirm-backup.sh` runs.

### Template

```
Wallet created on Base mainnet.

Address:
{ADDRESS}

Block explorer: https://basescan.org/address/{ADDRESS}

Your secret (type: {SECRET_TYPE}) is encrypted at rest with a Fernet
master key stored in {PLAIN_ENGLISH_MASTER_KEY_SOURCE}. Back it up now:

  {REVEAL_CMD}

Run that in a terminal -- it opens the secret ONCE and only in your
terminal, never in this chat. Save the output in a password manager,
hardware wallet, or paper. After saving, unlock live trading with:

  ./scripts/confirm-backup.sh {ADDRESS}

Deposit 1-5 USDC to start. I'll verify via get_balances before you send more.
```

`{PLAIN_ENGLISH_MASTER_KEY_SOURCE}` from tool's `master_key_source`:

| Field value | Plain English |
|---|---|
| `keyfile` | your local keyfile at `./agent-data/master.key` (chmod 600) |
| `generated_keyfile` | your local keyfile at `./agent-data/master.key` -- just created for you, chmod 600 |
| `keychain` | your OS keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) |

## `import_wallet` output

Only call with `vault_token` from `stash-secret.sh`. If user pastes a raw key, refuse semantically (the hook also blocks upstream).

### Template

```
Wallet imported on Base mainnet.

Address:
{ADDRESS}

Block explorer: https://basescan.org/address/{ADDRESS}

Your secret is already backed up (you typed it into stash-secret.sh --
that's what gave you the vault_token). Backup auto-confirmed; live
trading is unlocked for this wallet.

Run get_balances to verify it's the wallet you expected.
```

## When user wants to import (without pasting yet)

```
I can't accept private keys or mnemonics in chat -- the hook will refuse,
and they'd end up in your transcript file regardless.

To import safely, open a terminal (VSCode integrated terminal works --
Cmd+` / Ctrl+`) and run:

  ./scripts/stash-secret.sh

It prompts with input hidden and prints a short vault_token. Come back
here, say "import wallet vault_token <ID>", and I'll call import_wallet.
Your key never touches this conversation.
```

## Balances

- Show non-zero balances unless user asks for full detail.
- Convert raw amounts to human-readable units (USDC = 6 decimals, most ERC-20s = 18).
- Show token symbols, not contract addresses (address in parentheses for verification).
