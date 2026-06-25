# Chapter 02 — What you have

*10 minutes. No funds required. Still no commands — this chapter is a
map.*

Now that you've got the Claude Code mental model, let's zoom out to
the bot itself. What's running, where, and who it talks to.

The short version: most of it is on your laptop. What isn't lives at
Mangrove (behind an API key), and on the public blockchain.

## The picture

```
┌────────────────────────────────────────────────────────────────────┐
│  YOUR LAPTOP                                                       │
│                                                                    │
│  Claude Code ──┐                                                   │
│   (terminal)   │                                                   │
│                │  MCP over HTTP (localhost:9080/mcp/)              │
│                ▼                                                   │
│  ┌─────────────────────────────────────────────────┐               │
│  │ mangrove-agent (uvicorn process, port 9080)         │               │
│  │   • 95 MCP tools + REST API                     │               │
│  │   • APScheduler (in-process cron)               │               │
│  │   • Fernet-encrypted wallet keys                │               │
│  │   • SQLite DB: strategies, trades, evaluations  │               │
│  └────────────┬──────────────────────┬─────────────┘               │
│               │                      │                             │
│               │ API calls            │ signed tx                   │
│               │ (with API key)       │                             │
└───────────────┼──────────────────────┼─────────────────────────────┘
                │                      │
                ▼                      ▼
┌───────────────────────────┐   ┌──────────────────────────────────┐
│ MANGROVE (hosted)         │   │ BASE MAINNET (public blockchain) │
│ • Signals + strategy      │   │ • 1inch router (DEX aggregator)  │
│   evaluation              │   │ • USDC, ETH, WBTC, etc.          │
│ • Knowledge base          │   │ • Your wallet's on-chain state   │
│ • Market data (OHLCV)     │   │                                  │
│ • Reference strategies    │   │                                  │
└───────────────────────────┘   └──────────────────────────────────┘
```

Everything below the laptop box is someone else's infrastructure.
Everything inside the laptop box is yours.

## What runs on your laptop

### The `mangrove-agent` process

A single Python / FastAPI process. Starts when you run
`./scripts/setup.sh`, listens on `http://localhost:9080`, and does
four things:

1. **Serves REST endpoints** under `/api/v1/agent/*` — you can curl
   these directly from a terminal if you want. There's also a free
   `/health` endpoint for "is it up?"
2. **Serves MCP endpoints** under `/mcp/` — this is what Claude Code
   talks to for the 95 trading tools. Everything the REST API
   exposes, the MCP tools expose too, and vice versa. They share
   the service layer.
3. **Runs the scheduler** — APScheduler, in-process, using the same
   SQLite file as the database. When a strategy promotes to paper
   or live, the scheduler registers a cron job (one of: every 5m /
   15m / 1h / 4h / 1d) that fires `evaluate_strategy` on the
   timeframe. No separate daemon, no system cron. If this process
   dies, strategies stop ticking.
4. **Holds encrypted wallet keys** — when you create or import a
   wallet, the private key is encrypted with a Fernet master key
   and stored in the same SQLite file. The master key itself lives
   either in your OS keychain (macOS Keychain / Linux Secret
   Service / Windows Credential Manager) or a file at
   `./agent-data/master.key` (chmod 600). The agent decrypts
   in-process when it needs to sign, and wipes the plaintext from
   memory immediately after.

You can see it running: `ps aux | grep uvicorn` will show one Python
process with `--port 9080`. That's it.

### `agent-data/` — the state directory

All durable state lives in one directory:

```
agent-data/
├── agent.db         ← SQLite: strategies, trades, evaluations,
│                      wallets, allocations, scheduler jobs
├── master.key       ← Fernet master key (chmod 600, gitignored)
│                      [only present if not using OS keychain]
├── bare.pid         ← uvicorn process ID
└── bare.log         ← server logs
```

Back this up the same way you'd back up anything important. If you
lose `agent.db`, you lose all strategies and trade history. If you
lose `master.key`, you lose access to any wallet secrets encrypted
with it — without the master key, the ciphertext in `agent.db` is
useless. Same property as any encrypted backup.

### The `.claude/` directory

Already introduced in Chapter 01. Quick refresher:

```
.claude/
├── skills/          ← playbooks the agent loads on demand
├── rules/           ← global guardrails loaded at session start
├── hooks/           ← intercept scripts (e.g., block-wallet-secrets.sh)
├── agents/          ← agent personas (e.g., the product owner)
└── settings.json    ← hook registrations
```

Worth pointing at one thing: `.claude/rules/trading-bot-workflow.md`
is the big one. That's the file that makes the agent strategy-first,
eager-loads tools, gates live promotion, and walks through Stage 0–6
when you interact with it. If you ever wonder "why did the bot do
THAT," the answer is usually in that file.

### Two helper scripts you'll touch

```
scripts/
├── setup.sh             ← initial setup; idempotent
├── setup-mcp.sh         ← register the MCP server with Claude Code
├── stash-secret.sh      ← import an existing wallet (hidden-input)
├── reveal-secret.sh     ← view a wallet's secret (one-shot)
├── confirm-backup.sh    ← unlock live trading for a wallet
└── verify_quickstart.sh ← sanity-check the whole setup
```

`stash-secret.sh`, `reveal-secret.sh`, and `confirm-backup.sh` are
the secret-handling scripts — they touch private keys, they run in
your terminal only, and the bot never sees what they print. Chapter
06 covers them in detail.

## What runs at Mangrove

You're pointing at Mangrove's hosted API via the
`MANGROVEMARKETS_BASE_URL` and `MANGROVE_API_KEY` in your local
config. All three of the bot's upstream dependencies live there:

### `mangroveai` — signals, strategies, evaluations

The core intelligence. When you call `backtest_strategy` or
`evaluate_strategy`, the mangrove-agent calls Mangrove, which runs the
strategy logic against historical or current OHLCV, applies the
signals (RSI, MACD, ichimoku, etc.), computes risk/position sizing,
and returns either backtest metrics or a list of order intents.

Your bot does **not** reimplement signal math or strategy evaluation.
It's a thin wrapper that delegates to Mangrove and then executes the
resulting order intents locally.

### `mangrove-kb` — the knowledge base

A curated database of trading signals and patterns with descriptions,
parameter guidance, and backtested examples. When the agent builds a
strategy, it cites specific KB entries for each signal it uses. This
is why the bot's strategies aren't vibes — every parameter choice is
grounded in a KB entry.

You can `kb_search "momentum"` to see what's in it.

### `mangrovemarkets` — market data + DEX routing

OHLCV history, spot prices, gas estimates, and the 1inch DEX
aggregator interface. When a strategy decides to trade, the
executor calls `mangrovemarkets` to get a quote, prepare the swap
transaction, and broadcast it on Base.

**Important:** `mangrovemarkets` never sees your private key. The
agent signs locally (inside the uvicorn process) using
`eth_account`, then hands the signed transaction to
`mangrovemarkets` for broadcast.

## What happens on-chain

When (and only when) a live strategy fires or you explicitly call
`execute_swap`, a real transaction gets broadcast to Base mainnet:

1. Agent asks `mangrovemarkets.dex.get_quote(...)` for a swap price.
2. Agent asks `mangrovemarkets.dex.approve_token(...)` if the token
   needs approval (ERC-20 allowance). Signed locally, broadcast,
   polled until confirmed.
3. Agent asks `mangrovemarkets.dex.prepare_swap(...)` for the
   unsigned swap tx payload (including slippage in the upstream's
   percentage convention).
4. Agent signs the payload locally with `eth_account`, using the
   decrypted private key (only in memory for the duration of the
   sign call).
5. Agent asks `mangrovemarkets.dex.broadcast(...)` to submit the
   signed tx.
6. Agent polls `tx_status` until confirmed, logs a row in the
   `trades` table with the tx hash.

Everything after step 4 is public and auditable on
[basescan.org](https://basescan.org). Everything before it happens
on your laptop.

## Why this is different from a hosted "AI trading bot"

Most hosted AI trading bots you've seen work like this:

1. You give them your API keys or a wallet they control.
2. They execute trades on your behalf, server-side.
3. You trust the provider with both your capital and their own
   security posture (DB breach → your funds).

This setup is the opposite:

1. You never give anyone your keys. The bot generates and holds
   them on your laptop.
2. Trades execute from your machine. The hosted component only
   returns strategy advice — it doesn't touch your funds.
3. If Mangrove gets breached tomorrow, the worst case is "I can't
   ask for new strategy advice until they're back up." Your
   wallets, your trades, your funds, all still yours.

This property — self-custody + local execution — is why we built the
thing this way. It's also why there are more moving parts on your
laptop than in a hosted setup.

## v1 scope boundaries

Things this bot can do now:

- EVM chains (Base, Ethereum, Arbitrum, Polygon, Optimism, BNB,
  Avalanche, zkSync, Gnosis, Linea) for wallet management.
- Base mainnet for live trading (the default throughout the
  workshop).
- Strategies on 5m / 15m / 30m / 1h / 4h / 1d timeframes.
- DEX swaps via 1inch.
- Paper and live trading modes.
- Up to a few strategies running simultaneously (workshop target:
  one).

Things this bot **cannot** do in v1:

- Trade on centralized exchanges (Binance, Coinbase, etc.).
- Trade XRPL, Solana, or other non-EVM chains.
- Sub-5m timeframes.
- Limit orders, stop-losses, or any order type beyond market swaps.
- Cross-strategy portfolio rebalancing.
- Run headless in the cloud (it's local-first; Claude Code needs to
  be able to talk to the MCP server).

These are scope decisions for v1, not permanent limits. The roadmap
evolves.

## What to take away

Three things:

1. **Your laptop is the trust boundary.** Keys live here, trades
   execute from here, state lives here. Anything beyond the laptop
   is either public (on-chain) or an advisor (Mangrove API) that
   can't move your funds.
2. **The mangrove-agent process is the system.** If it's running,
   everything works. If it's not, nothing does. No background
   daemons, no Docker unless you opt in, no phantom state.
3. **Mangrove provides intelligence, not execution.** The strategy
   logic is hosted; the trade execution is local. Understanding
   that split is the key to understanding what this bot is good at
   and what it isn't.

Next up: getting the whole thing running on your laptop.

→ [Chapter 03 — Setup](03-setup.md)
