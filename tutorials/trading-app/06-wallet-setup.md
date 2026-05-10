# Chapter 06 — Wallet setup

*15 minutes. Funds required ($1–5 USDC recommended for the workshop).*

This chapter is the security-critical one. Take it slowly. If any
step feels wrong, stop and ask the facilitator — this is the moment
where mistakes cost actual money.

Goal: create or import a wallet, save its private key somewhere
durable, confirm the backup, and fund it with a small amount of USDC.
By the end you'll have a wallet with a non-zero balance and the
backup gate flipped to "live trading unlocked."

## Before you start

- Make sure you have a Base-network USDC source: MetaMask, a
  centralized exchange (Coinbase / Binance / Kraken) that can
  withdraw on Base, or any other self-custody wallet that supports
  the chain.
- Decide: **create a fresh wallet** (recommended for the workshop)
  or **import an existing wallet** (only if you already have a
  dedicated trading wallet you want to use).
- Have 5 minutes of uninterrupted time. Don't start this between
  two meetings.

**Do NOT use your main wallet.** If you're going to use an existing
wallet, make sure it's one you created specifically for trading,
with a small amount of funds you're comfortable risking. A bug in
this workshop's code (or in your own operation of it) should never
be able to drain your savings.

## Path A — create a fresh wallet (recommended)

In Claude Code:

> "Create a fresh wallet on Base mainnet."

The bot calls `create_wallet(chain="evm", network="mainnet",
chain_id=8453)`. Expected response:

```
Wallet created on Base mainnet.

Address:
0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD

Block explorer: https://basescan.org/address/0x5ff2aAb12Cd34...

Your secret (type: private_key) is encrypted at rest with a Fernet
master key stored in your local keyfile at ./agent-data/master.key
(chmod 600). Back it up now:

  ./scripts/reveal-secret.sh a8f72b3e

Run that in a terminal — it opens the secret ONCE and only in your
terminal, never in this chat. Save the output in a password manager,
hardware wallet, or paper. After saving, unlock live trading for this
wallet with:

  ./scripts/confirm-backup.sh 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD

Deposit 1-5 USDC to start. I'll verify via get_balances before you
send more.
```

**Read carefully what the bot gave you:**

1. **The address** — this is public, safe to share, and where you'll
   send USDC.
2. **The `reveal_cmd`** — a shell command with a short `vault_token`.
   That id has a 5-minute TTL and is single-read. You have one
   shot to see the plaintext.
3. **The `master_key_source`** — where your local encryption key
   lives. "keyfile" means `./agent-data/master.key`; "keychain"
   means your OS keychain.

### A.1 — Save the secret (do this now)

Open a new terminal window (or a tab in VSCode) — **NOT** the Claude
Code session. Run the `reveal_cmd` the bot gave you, copy-pasting
verbatim. Example:

```bash
cd ~/Desktop/mangrove-agent
./scripts/reveal-secret.sh a8f72b3e
```

Output is a single line: your plaintext private key, 64 hex
characters prefixed with `0x`. **Save it now**:

- Paste into a password manager (1Password, Bitwarden, etc.) — best
  option.
- Write it on a piece of paper you can lock up — acceptable backup.
- Put it on a hardware wallet via "Import Account → Private Key" —
  if you have one, great.

**DO NOT**:

- Paste it into Claude Code chat (the hook will block you anyway).
- Email it to yourself.
- Save it in a note on the same laptop without encryption.
- Screenshot it.

After you've saved it, close the terminal window. The vault entry is
consumed after reveal — running the same command again will say "no
such vault_token." That's by design.

### A.2 — Confirm the backup

Back in your terminal (it's fine to reuse the one you used for
reveal):

```bash
./scripts/confirm-backup.sh 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD
```

(Use your actual wallet address.)

Output:

```
✓ Backup confirmed for 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD
  backup_confirmed_at: 2026-04-23T21:04:17Z
  Live trading unlocked.
```

That flips a column in your local database. Without it, any attempt
to `execute_swap` or promote a strategy to live for this wallet will
be refused. With it, the gate is open.

Verify the flip took:

> "List my wallets."

The bot should show your wallet with a non-null `backup_confirmed_at`
timestamp.

### A.3 — Fund the wallet

Send 1–5 USDC (on Base) to your wallet address.

**If you're using MetaMask:**

1. Make sure you're on the Base network. (If you don't see it,
   visit https://chainlist.org, search "Base," and click "Connect
   Wallet → Add to MetaMask.")
2. Confirm your MetaMask holds USDC on Base. If it's on Ethereum
   mainnet, use a bridge (base.org/bridge) to move it — don't send
   Ethereum-mainnet USDC to a Base address.
3. Click "Send" → paste your bot's wallet address → enter 1–5 USDC →
   confirm.

**If you're using Coinbase / Binance / Kraken:**

1. Go to Withdraw → USDC.
2. Network: **Base**. Not Ethereum, not Polygon, not Arbitrum.
   Wrong network means the funds can be stuck or lost.
3. Destination: your bot's address.
4. Amount: 1–5 USDC.

Tx takes 10–30 seconds on Base. You can watch it on basescan:
`https://basescan.org/address/<your_address>`.

Once it lands, verify from Claude Code:

> "Check my balance."

```
Balances for 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD:

  USDC: 2.0000000000  (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
  ETH:  0.0000000000  (native)

Nothing else detected on Base.
```

You now have a funded, backup-confirmed wallet. Skip to "What just
happened" below.

## Path B — import an existing wallet

Only use this if you have a dedicated Base-chain wallet with a
private key you already control. **Your main wallet is not the
right choice** — create a fresh one if in doubt (Path A).

### B.1 — Stash the secret out-of-band

In a terminal (not Claude Code):

```bash
./scripts/stash-secret.sh
```

You'll see:

```
Enter private key or mnemonic. Input is hidden (no echo).
Secret will be stashed with a 5-minute TTL; use the returned
vault_token with import_wallet and DO NOT type it in chat.

Secret:
```

Paste your private key (or type it if you're masochistic). Nothing
will appear as you type — that's hidden input, the whole point.
Press Enter.

Output:

```
✓ Stashed. vault_token: f9a2c14d
  Pass this to import_wallet within 5 minutes.
```

### B.2 — Import via the bot

In Claude Code:

> "Import my existing wallet with vault_token f9a2c14d."

The bot calls `import_wallet(vault_token="f9a2c14d")`. Expected response:

```
Wallet imported on Base mainnet.

Address:
0xDeadbeef1234567890AbCdEf...

Block explorer: https://basescan.org/address/0xDeadbeef1234...

Your secret is already backed up (you just typed it into
stash-secret.sh — that's what gave you the vault_token). The agent
auto-confirmed the backup, so live trading is already unlocked for
this wallet.

Run get_balances to verify it's the wallet you expected.
```

No backup-confirm step needed — importing is proof you already have
the secret somewhere. The bot auto-flips `backup_confirmed_at`.

### B.3 — Verify

> "Check my balance."

Should show whatever funds are on that wallet. If you see nothing
and expected to see something, double-check the address the bot
returned matches the one you thought you imported. If addresses
don't match, the stash-secret + import_wallet flow picked up a
different key than you intended — start over.

## Why the backup gate exists

The story goes like this:

Workshop attendee creates a wallet, sees the secret scrolling by in
a long create_wallet response, doesn't save it. Bot says "backup
confirmed?" attendee says "sure, yes, go ahead." Bot promotes the
strategy to live. Live strategy fires, swaps USDC for ETH. Attendee
closes their laptop and flies home.

Laptop drive dies a week later. The Fernet-encrypted secret in
`agent.db` is useless without the master key (which is... on the
dead drive). Attendee has no backup. The wallet is unrecoverable.

The backup gate makes this failure mode structurally impossible. You
cannot execute a live trade or promote a live strategy until you've
run `confirm-backup.sh`, which requires you to have already saved
the secret (because the reveal is one-shot).

**Annoyance as a feature.** The gate is friction. That's the point.
You'll thank it the first time you almost sent funds to a wallet you
forgot to back up.

## What just happened

You now have:

1. **A wallet** — an address that exists on Base mainnet, visible
   on basescan, holding 1–5 USDC.
2. **A private key** — stored as Fernet-encrypted ciphertext in
   `agent-data/agent.db`, decryptable only with the master key at
   `./agent-data/master.key`. Plaintext also saved in your password
   manager / paper backup from step A.1.
3. **A backup flag** — `wallets.backup_confirmed_at` set to a
   timestamp, which unlocks live trading for this wallet.
4. **Funds on-chain** — USDC visible to the bot via `get_balances`,
   ready to allocate.

## Where your master key actually lives

Worth a sentence on this because attendees get confused:

- **On macOS / Linux with keychain reachable:** the Fernet master
  key is stored in your OS keychain (macOS Keychain app / Linux
  Secret Service). The mangrove-agent reads it at startup and holds it
  in process memory. Your wallet secrets are encrypted with a
  different per-wallet key, derived from the master.
- **Otherwise:** the master key is in `./agent-data/master.key`,
  chmod 600 (only your user can read). Gitignored, so it'll never
  accidentally end up in a commit.

**If you lose the master key**, every wallet in `agent.db` becomes
ciphertext you can't decrypt. The plaintext backups you saved in
step A.1 are your ONLY way back in. Save them well.

**If you lose only your laptop** (not the backups), you import into
a fresh setup using `stash-secret.sh` + `import_wallet`, on any
machine. Your wallet was never tied to this specific laptop — the
laptop just hosts the encrypted store.

## Troubleshooting

### "The reveal_cmd says 'no such vault_token'"

The TTL expired (5 minutes default) OR you already ran the reveal
once. Either way, the single-use vault_token is gone.

Recover via the reveal-by-address path:

```bash
./scripts/reveal-secret.sh --address 0x5ff2aAb12Cd34...
```

This regenerates a fresh reveal session from the encrypted store
— works as long as the master key is intact. Save the output
immediately, then run `confirm-backup.sh` as in step A.2.

### "I sent funds to the wrong network"

If you sent USDC on Ethereum mainnet to a Base address, the funds
are still yours but unreachable from the bot (different chain). Use
a bridge like base.org/bridge to move them to Base.

If you sent something that isn't USDC (e.g., USDT or DAI), same
story — the funds are there, but the bot only knows about USDC by
default. You can still trade them via manual swaps, but it's
out-of-workshop scope.

### "The confirm-backup.sh script says 'unknown address'"

The address you passed doesn't match any wallet in the local DB.
Check: `list_wallets` via the bot, or `sqlite3 agent-data/agent.db
"SELECT address FROM wallets"`. Copy-paste the exact string.

### "I created two wallets by accident"

No harm done. `list_wallets` shows both; use whichever you prefer
for the live allocation. The other just sits there with zero
balance — doesn't cost anything and doesn't interfere.

## What to take away

- The secret-handling scripts (`stash-secret.sh`, `reveal-secret.sh`,
  `confirm-backup.sh`) are deliberately out-of-band. Keys never
  enter the Claude Code conversation.
- `confirm-backup.sh` is the gate. Nothing live executes for a
  wallet until you've run it.
- Fund with 1–5 USDC first time. You can top up later.
- Save the secret to a password manager **before** moving on — not
  after, not "I'll do it later." Now.

→ [Chapter 07 — Going live](07-going-live.md)
