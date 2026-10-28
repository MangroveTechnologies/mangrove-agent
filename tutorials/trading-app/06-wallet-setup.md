# Chapter 06 - Wallet setup

*15 minutes. Funds required ($1-5 USDC recommended to start).*

This chapter is the security-critical one. Take it slowly. If any step feels
wrong, stop and re-read before proceeding; mistakes here cost real money.

Goal: create or import a wallet, save its private key somewhere durable,
confirm the backup, and fund it with a small amount of USDC. By the end you have
a funded wallet whose backup gate is open, so `execute_swap` can sign for it.

## Before you start

- Have a source of USDC on the Base network: MetaMask, an exchange (Coinbase,
  Binance, Kraken) that can withdraw on Base, or another self-custody wallet
  that supports the chain.
- Decide whether to **create a fresh wallet** (recommended) or **import an
  existing wallet** (only if you already have a dedicated trading wallet).
- Set aside five uninterrupted minutes.

**Do not use your main wallet.** If you import one, make sure it is a wallet you
made specifically for this, holding an amount you are comfortable risking. A bug
in this software, or in how you operate it, should never be able to reach your
savings.

If you only want to practice the mechanics, you can create the wallet on Base
Sepolia testnet instead (`network` "testnet", `chain_id` 84532) and use free
test funds. The steps below use Base mainnet, which is the default.

Two separate roles can use a wallet. This tutorial's wallet is the one that
signs your swaps. The agent also has an optional x402 *payment wallet* that
pays for MangroveAI calls; it is selected through `./scripts/setup.sh --wallet`
(see [x402 setup](../../docs/setup-x402.md)) and can be a different wallet.

## Path A - create a fresh wallet (recommended)

In your AI client:

> "Create a fresh wallet on Base mainnet."

The client calls `create_wallet`. Its arguments default to `chain="evm"`,
`network="mainnet"` and `chain_id=8453` (Base). The response contains:

- `address` - public, safe to share, where you send USDC
- `vault_token` and `reveal_cmd` - how to retrieve the secret, described below
- `secret_type` - `private_key` for EVM wallets
- `master_key_source` - where the local encryption key lives
- `secret_ttl_seconds` - how long the vault token lasts (300 by default)
- `backup_required` - `true` until you confirm the backup
- `deposit_instructions` and `safety_note`

The client summarizes this for you, along these lines:

```
Wallet created on Base mainnet.

Address:
0x<your wallet address>

Your secret (type: private_key) is encrypted at rest with a Fernet master key
stored in your local keyfile at ./agent-data/master.key (mode 600). Back it up
now:

  ./scripts/reveal-secret.sh <vault_token>

Run that in a terminal - it shows the secret once, only in your terminal, never
in this chat. Save it in a password manager, hardware wallet or on paper. After
saving, unlock swaps for this wallet with:

  ./scripts/confirm-backup.sh 0x<your wallet address>

Deposit 1-5 USDC to start.
```

The plaintext secret is never returned to the client. It is held in the agent
process's memory under the `vault_token` so you can read it in a separate
terminal. The token is single-use and expires after the TTL.

### A.1 - Save the secret now

Open a new terminal window, **not** your AI client session. Run the
`reveal_cmd` exactly as given, from the repository directory:

```bash
cd mangrove-agent
./scripts/reveal-secret.sh <vault_token>
```

The script refuses to run unless its output is an interactive terminal, so you
cannot redirect the secret to a file or pipe. It prints a short note and then the
secret: for an EVM wallet, the private key as `0x` followed by 64 hex
characters. **Save it now:**

- In a password manager (1Password, Bitwarden and similar) - the best option.
- On paper that you can lock up - acceptable.
- In a hardware wallet via "Import Account" with a private key, if you have one.

**Do not:**

- Paste it into any AI chat. Claude's hook is an extra check, and other clients
  have none.
- Email it to yourself.
- Keep it in an unencrypted note on the same machine.
- Screenshot it.

Clear your terminal scrollback afterward. The vault token is consumed when you
read it, so running the same command again fails with an unknown or expired
token. If that happens, read the secret by address instead (see
Troubleshooting).

### A.2 - Confirm the backup

In a terminal:

```bash
./scripts/confirm-backup.sh 0x<your wallet address>
```

The script prints `Backup confirmed for <address>. No payment was made.` It sets
the wallet's `backup_confirmed_at` timestamp in the local database. Without it,
`execute_swap` refuses to sign for this wallet. With it, the gate is open.

Verify the change took:

> "List my wallets."

`list_wallets` should show your wallet with a non-null `backup_confirmed_at`.

### A.3 - Fund the wallet

Send 1-5 USDC on Base to the wallet address.

**From MetaMask:**

1. Make sure you are on the Base network. If it is missing, add it from
   [chainlist.org](https://chainlist.org).
2. Confirm your MetaMask holds USDC on Base. If it holds USDC on Ethereum
   mainnet, bridge it first. Do not send Ethereum-mainnet USDC to a Base
   address.
3. Choose Send, paste the agent's wallet address, enter 1-5 USDC and confirm.

**From Coinbase, Binance or Kraken:**

1. Go to Withdraw, then USDC.
2. Set the network to **Base**, not Ethereum, Polygon or Arbitrum. The wrong
   network can lose funds.
3. Set the destination to the agent's wallet address and the amount to 1-5 USDC.

Base transfers take seconds to a minute. You can watch the address on
`https://basescan.org/address/<your address>`.

You will also need a few cents of native ETH on Base to pay gas for swaps
(Chapter 07). You can send it the same way.

Once the funds land, check them. The agent reads balances through
MangroveMarkets. Ask your client to use the Markets balance tool that tool
discovery lists (its name starts with `oneinch_`, `wallet_` or `dex_`), or call
the agent's REST route from a terminal in a git clone:

```bash
KEY=$(python3 -c "import json; print(json.load(open('server/src/config/local-config.json'))['API_KEYS'].split(',')[0])")
curl -s -H "X-API-Key: $KEY" \
  "http://localhost:9080/api/v1/agent/wallet/0x<your wallet address>/balances?chain_id=8453" \
  | python3 -m json.tool
```

You should see your USDC (and any ETH) balance. Skip ahead to "What just
happened".

## Path B - import an existing wallet

Use this only for a dedicated Base wallet whose private key you already control.
**Your main wallet is not the right choice**; create a fresh one if in doubt
(Path A).

### B.1 - Stash the secret out-of-band

In a terminal, not your AI client:

```bash
./scripts/stash-secret.sh
```

The script requires an interactive terminal. It prompts for a private key or a
12 or 24 word mnemonic with hidden input (nothing appears as you type), then
sends it to your local agent over the loopback address and receives a
short-lived `vault_token`. It does not print the token. Instead it writes a
private, single-use handoff file in a new temporary directory and prints that
file's path.

### B.2 - Import through the client

Tell your client to read the handoff JSON at the printed path, call
`import_wallet` with its `vault_token`, and then delete the file and its
containing directory. The client must not display the token.

> "Read the handoff file at the path I'll give you, import the wallet with its
> vault_token, then delete the file and its directory."

The client needs file access to do this. The restricted session started by
`./scripts/chat.sh` denies file tools, so use a client that can read local files
for the import. If you decide not to import, delete the handoff file and its
directory yourself; the token also expires on its own.

`import_wallet` defaults to `chain="evm"`, `network="mainnet"`, `chain_id=8453`.
The response gives the address, the chain, `master_key_source` and
`backup_required: false`. A newly imported wallet is marked backup-confirmed
automatically, because you already hold its secret outside the agent. If the
wallet was already stored, the response says so and `backup_required` reflects
its stored state.

### B.3 - Verify

> "List my wallets."

Check that the address matches the wallet you meant to import. If it does not,
the stash step picked up a different key than you intended; stop and start over.
Then check the balance as in step A.3.

## Why the backup gate exists

Picture the failure it prevents. A user creates a wallet, scrolls past the
secret, tells the client "sure, backed up," and funds the wallet. Later the
machine's drive fails. The encrypted secret in `agent.db` is useless without
the master key, which was on the same drive. There is no backup and the wallet
is unrecoverable.

The gate makes that structurally hard. The agent will not sign a swap from a
wallet until you run `confirm-backup.sh`. The script cannot prove you saved the
secret, but the one-time reveal makes the steps hard to skip by accident. The
friction is intentional.

## What just happened

You now have:

1. **A wallet** - an address on Base, visible on basescan, holding 1-5 USDC.
2. **A private key** - stored as Fernet ciphertext in `agent-data/agent.db`,
   decryptable only with the master key, plus your own plaintext backup in a
   password manager or on paper.
3. **A backup flag** - `backup_confirmed_at` set, which opens the gate for
   `execute_swap`.
4. **Funds on-chain** - USDC the agent can see and spend when you ask it to.

## Where your master key lives

- The master key is a Fernet key in `./agent-data/master.key`, mode 600 and
  gitignored (the path is the `MASTER_KEY_PATH` config value). The agent creates
  it when it first needs to encrypt a secret and refuses to read it if it is
  readable by group or others.
- If that file does not exist, the agent looks for a key in your OS keychain
  (macOS Keychain, Linux Secret Service or Windows Credential Manager) under
  the service name from `KEYRING_SERVICE_NAME`. It does not create a new key
  when it needs to decrypt an existing wallet; it fails and tells you to
  restore the original.
- One master key encrypts every wallet secret in the database.

**If you lose the master key**, every wallet in `agent.db` becomes ciphertext you
cannot decrypt. The plaintext backups from step A.1 are your only way back in.
Back up `agent-data/` too, and keep the key file and database together.

**If you lose only the machine**, you can import your saved secret into a fresh
installation with `stash-secret.sh` and `import_wallet`. The wallet is not tied
to any machine; the machine only hosts the encrypted copy.

## Troubleshooting

### "The reveal command says the token is unknown or expired"

The five-minute TTL passed, you already read it once, or the agent restarted
(the vault lives in memory). Read the secret by address instead:

```bash
./scripts/reveal-secret.sh --address 0x<your wallet address>
```

This decrypts the stored ciphertext, so it works any time the master key is
intact and you are in an interactive terminal. Save the output immediately,
then run `confirm-backup.sh` as in step A.2.

### "I sent funds on the wrong network"

USDC sent on Ethereum mainnet to a Base address is still yours but unreachable
from the agent, which only works with the chain you created the wallet for.
Use a bridge such as base.org/bridge to move it to Base. Tokens other than USDC
(for example USDT or DAI) are also visible on-chain but outside this tutorial's
flow.

### "confirm-backup.sh says the wallet is not found"

The address does not match a stored wallet. Run `list_wallets` (or
`sqlite3 agent-data/agent.db "SELECT address FROM wallets"`) and copy the exact
string.

### "I created two wallets by accident"

No harm done. `list_wallets` shows both. Use whichever you prefer; the other
sits unused.

## What to take away

- The secret-handling scripts (`stash-secret.sh`, `reveal-secret.sh`,
  `confirm-backup.sh`) are deliberately out-of-band. Keys never enter the AI
  conversation.
- `confirm-backup.sh` opens the gate. `execute_swap` refuses to sign for a
  wallet until you run it.
- Fund with 1-5 USDC the first time. You can top up later.
- Save the secret before you move on, not after.

[Chapter 07 - Going live](07-going-live.md)
