# Chapter 02 - What you have

*10 minutes. No funds required. Still no commands - this chapter is a map.*

Now that you have the AI client mental model, this chapter zooms out to the
agent itself: what runs where, and who it talks to.

The short version: wallets, signing and local state live on your machine.
Strategy logic, backtests, paper portfolios and market data live at Mangrove,
reached with an API key or x402 wallet payments. Swaps settle on a public
blockchain.

## The picture

```
+--------------------------------------------------------------------+
|  YOUR MACHINE                                                      |
|                                                                    |
|  AI client  ---+                                                   |
|   (terminal)   |                                                   |
|                |  MCP over HTTP (localhost:9080/mcp/)              |
|                v                                                   |
|  +-----------------------------------------------------+           |
|  | mangrove-agent (uvicorn process, port 9080)         |           |
|  |   - local MCP tools + REST API                      |           |
|  |   - forwards MangroveAI tools                       |           |
|  |   - APScheduler (in-process timers)                 |           |
|  |   - Fernet-encrypted wallet keys                    |           |
|  |   - SQLite DB: wallets, swaps, schedules, payments  |           |
|  +--------------+----------------------+---------------+           |
|                 |                      |                           |
|                 | API calls            | signed tx                 |
|                 | (API key or x402)    |                           |
+-----------------+----------------------+---------------------------+
                  |                      |
                  v                      v
+-----------------------------+   +--------------------------------+
| MANGROVE (hosted)           |   | BASE MAINNET (public chain)    |
| - MangroveAI: strategies,   |   | - 1inch router (DEX aggregator)|
|   signals, backtests, paper |   | - USDC, ETH, WBTC, etc.        |
|   portfolios, knowledge base|   | - Your wallet's on-chain state |
| - MangroveMarkets: DEX      |   |                                |
|   quotes, swap preparation, |   |                                |
|   broadcast, balances       |   |                                |
+-----------------------------+   +--------------------------------+
```

The diagram shows a local installation. In a hosted execution environment, the
agent process and its files live on that host instead. Your AI provider
account does not automatically share a database between installations.

## What runs on your machine

### The `mangrove-agent` process

A single Python / FastAPI process. `./scripts/setup.sh` starts it in the
background and it listens on `http://localhost:9080` by default. It does four
things:

1. **Serves REST endpoints** under `/api/v1/agent/*` for wallets, swaps, Kraken
   and Robinhood venues, paper schedules, spend budgets and logs, plus a free
   `/health` endpoint and a free `/api/v1/agent/status`. Your client normally
   uses MCP, but you can call these routes yourself with the local API key.
2. **Serves MCP** at `/mcp/`. This is what your AI client talks to. It
   advertises the agent's own tools and also forwards MangroveAI's tools: the
   agent reads MangroveAI's live tool catalog and passes each call through
   without keeping a copy. Run `list_tools` for the live catalog or
   `discover_tools` for a compact one.
3. **Runs timers.** An in-process APScheduler instance, backed by the same
   SQLite file, runs the paper-schedule dispatcher (every 5 seconds it checks
   for schedules whose next evaluation is due) and the reconciliation workers
   for broker orders and payments. There is no separate daemon and no system
   cron. If the process stops, nothing ticks.
4. **Holds encrypted wallet keys.** When you create or import a wallet, the
   private key is encrypted with a Fernet master key and stored in the SQLite
   file. The master key lives in a file at `./agent-data/master.key` (mode 600)
   or, if no such file exists, in your OS keychain (macOS Keychain, Linux
   Secret Service or Windows Credential Manager). The agent decrypts a key
   in-process only when it needs to sign.

You can see it running with `ps aux | grep uvicorn`, which shows one Python
process with the configured port.

### `agent-data/` - the state directory

Durable state lives in one directory:

```
agent-data/
|-- agent.db     SQLite: wallets, swaps, paper schedules, payment ledger,
|                scheduler jobs, strategy execution records
|-- master.key   Fernet master key (mode 600, gitignored)
|                [only present when you are not using the OS keychain]
|-- bare.pid     uvicorn process ID
|-- bare.log     server log
`-- chat/        working directory for ./scripts/chat.sh
```

Back this up the way you would back up anything important. If you lose
`agent.db`, you lose wallet records and local history. If you lose the master
key, the encrypted wallet secrets in `agent.db` become unreadable. Your own
saved copies of each wallet's secret (Chapter 06) are the recovery path.

### The `.claude/` directory

Introduced in Chapter 01:

```
.claude/
|-- skills/      task playbooks the client loads on demand
|-- rules/       guardrails loaded at session start
|-- hooks/       Claude Code hook scripts (block-wallet-secrets.sh, ...)
|-- agents/      agent personas used in development
`-- settings.json  hook registrations for Claude Code sessions here
```

`.claude/rules/trading-bot-workflow.md` is worth reading once. It tells the
client to treat MangroveAI as the source of truth for strategies: load the
`strategy-composition` skill before building and the `backtesting` skill before
backtesting, use the server's strategy IDs, and never substitute local
templates or remembered instructions. If you wonder why the client did
something while building a strategy, that file and the skill it loaded explain
it.

### Helper scripts you will touch

```
scripts/
|-- setup.sh                - initial setup; idempotent
|-- setup-mcp.sh            - register the MCP server with Claude Code
|-- chat.sh                 - restricted Claude session (Chapter 01)
|-- stash-secret.sh         - import an existing wallet (hidden input)
|-- reveal-secret.sh        - view a wallet's secret in your terminal
|-- confirm-backup.sh       - unlock live swaps for a wallet
|-- stash-kraken-secret.sh  - connect Kraken with your own API key
|-- connect-robinhood.sh    - authorize Robinhood in a browser
`-- verify_quickstart.sh    - sanity-check the whole setup
```

`stash-secret.sh`, `reveal-secret.sh`, `confirm-backup.sh` and
`stash-kraken-secret.sh` handle secrets. They run only in your terminal, and
the AI client never sees what they print. Chapter 06 covers the wallet scripts.

## What runs at Mangrove

The agent reaches Mangrove with either a MangroveAI API key or x402 payments
from a wallet you fund (see [x402 setup](../../docs/setup-x402.md)). Two
hosted services sit behind that access.

### MangroveAI - strategies, signals, backtests, paper portfolios

The core intelligence. It owns:

- **Strategy composition and storage.** `load_skill`, `build_strategy_for_regime`,
  `save_strategy`, `verify_strategy`, `list_strategies` and related tools.
- **Backtests.** `run_backtest` starts a run and `get_backtest` reads it.
- **Paper portfolios.** Server-owned simulated balances, positions and trades
  for a backtested strategy. The agent supplies only the clock (Chapter 05).
- **Signals, market data and on-chain data.** `list_signals`, `get_ohlcv`,
  `get_market_regime` and many more.
- **The knowledge base.** `kb_search`, `kb_get_document` and related tools. A
  strategy's signals and parameters can be grounded in knowledge-base entries.
- **The Strategy Engine and SIEVE.** Searching completed runs, screening
  candidates and launching sweeps (Chapter 09).

Your agent does not reimplement signal math, strategy evaluation or
backtesting. It is a client that forwards these calls and pays for them.

### MangroveMarkets - DEX routing and balances

DEX quotes, swap preparation, transaction broadcast, transaction status and
balances. When you ask for a swap, the agent asks MangroveMarkets for a quote
and an unsigned transaction, signs it locally, and sends the signed bytes back
for broadcast.

**Important:** MangroveMarkets never sees your private key. Signing happens
inside the agent process.

The same SDK also carries the **Kraken** integration. You can connect a Kraken
account with your own API key (the `setup-kraken` skill; the key stays on your
machine) or with keyless OAuth (the `connect-kraken` skill). Robinhood is a
separate venue that you authorize with `./scripts/connect-robinhood.sh`.
Chapter 07 describes what each offers.

## What happens on-chain

When, and only when, you ask for a swap with `execute_swap`, the agent runs a
real transaction on the chain you name (Base mainnet, chain ID 8453, in this
tutorial):

1. It requests a quote for the pair and amount.
2. If the input token needs an ERC-20 allowance for the router, it requests an
   approval transaction, signs it locally, broadcasts it and waits for it to
   confirm.
3. It requests the unsigned swap transaction, with your slippage tolerance.
4. It signs the transaction locally with the decrypted key. The signing
   allowlist permits only 1inch router calls and approvals to them.
5. It broadcasts the signed transaction.
6. It polls the transaction status until it leaves `pending`, then records the
   swap in the local database with its transaction hash.

Everything after step 4 is public and visible on
[basescan.org](https://basescan.org). Everything before it happens on your
machine.

## How this differs from a hosted trading bot

A typical hosted bot takes your exchange API keys or a wallet it controls,
executes server-side, and makes you trust the provider with your capital and
its security. This agent inverts that:

1. You do not give anyone your keys. The agent generates and holds them on your
   machine.
2. Swaps execute from your machine. The hosted services return strategy data,
   market data and unsigned transactions; they do not hold your funds.
3. If Mangrove were unavailable, the worst case is that you cannot get new
   strategy data or quotes. Your wallets and funds are unaffected.

This is also why there are more moving parts on your machine than in a hosted
setup.

## Scope boundaries

- Wallet creation and import support EVM chains and XRPL. The tutorial uses
  Base mainnet (chain ID 8453), the default.
- DEX swaps are routed through 1inch.
- Strategy candle sizes are 5m, 15m, 30m, 1h, 4h and 1D.
- Paper evaluation intervals are 60 seconds, 5 minutes, 15 minutes, 1 hour,
  4 hours or 1 day, and at most four paper evaluations run at once in one
  agent process.
- The agent exposes no MCP tool that deploys a saved strategy to scheduled live
  execution. Chapter 07 explains what live execution is available.

Capabilities follow the live catalog. Do not treat a static tool count or an
old capability list as proof of access; use `list_tools` or `discover_tools`.
Hosted execution is possible, but unattended schedules need a persistent server
and durable storage, not just an open chat.

## What to take away

1. **Your machine is the custody boundary.** Keys live here and signing happens
   here. Anything beyond it is either public (on-chain) or a Mangrove service
   that cannot sign for you.
2. **The mangrove-agent process is the system.** If it is running, local tools
   and paper-schedule requests work. If it stops, schedules stop issuing
   evaluations.
3. **Mangrove provides strategy intelligence; you keep execution and custody.**
   Understanding that split explains what the agent is good at and what it is
   not.

Next: getting the agent running.

[Chapter 03 - Setup](03-setup.md)
