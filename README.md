<div align="center">
  <a href="https://github.com/MangroveTechnologies/mangrove-agent">
    <img src="assets/icon.png" alt="Mangrove" width="120" height="112">
  </a>

  <h1>mangrove-agent</h1>

  <p>
    <strong>A local MCP server (Model Context Protocol) and Claude Code plugin for AI trading agents.</strong><br>
    Built on the Mangrove API, with a REST API alongside. FastAPI + MCP. Autonomous strategy generation, cron-driven execution, full audit trail.<br>
    CEX trade execution on <strong>Kraken</strong> &nbsp;·&nbsp; DEX execution via <strong>1inch</strong> across 10 EVM chains
  </p>

  <p>
    <a href="https://www.kraken.com"><img src="assets/kraken-logo.png" alt="Kraken" height="28"></a>
    &nbsp;&nbsp;
    <a href="https://1inch.io"><img src="assets/oneinch-logo.png" alt="1inch" height="28"></a>
  </p>

  <p>
    <a href="https://github.com/MangroveTechnologies/mangrove-agent/actions/workflows/ci.yml">
      <img src="https://github.com/MangroveTechnologies/mangrove-agent/actions/workflows/ci.yml/badge.svg" alt="CI">
    </a>
    <a href="https://github.com/MangroveTechnologies/mangrove-agent/blob/main/LICENSE">
      <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
    </a>
    <a href="https://discord.gg/Yycbw6P93B">
      <img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord">
    </a>
  </p>
</div>

---

## What this is

A local AI trading agent that:
- Turns natural-language goals ("momentum on ETH") into backtested, ranked trading strategies via the [MangroveAI API](https://mangrovedeveloper.ai).
- Runs live strategies on APScheduler cron jobs. Same evaluator path for paper and live.
- Executes live DEX swaps through [MangroveMarkets](https://github.com/MangroveTechnologies/MangroveMarkets) — 1inch routing across 10 EVM chains (Ethereum, Base, Arbitrum, Polygon, Optimism, BNB, Avalanche, zkSync Era, Gnosis, Linea; the agent defaults to Base). Client-side signing; the routing server never touches your keys.
- Trades on **Kraken** too, with two connection modes: **bring-your-own-key** (`/setup-kraken` — a least-privilege API key that stays on your machine and talks to Kraken directly) or **keyless OAuth** (`/connect-kraken` — consent once in a browser, no key to manage).
- Logs every evaluation and trade — DEX and CEX — to local SQLite for a full audit trail.

### What you get

- **119 tools** (as of October 2026; `/mcp` in Claude Code showed 119 on 2026-10-20). That is the hosted MangroveAI catalog forwarded by the agent plus local tools. Run `list_tools` against the endpoint for the live catalog.
- **7 agent skills** in `.claude/skills`: `audit-security`, `check-alignment`, `connect-kraken`, `setup-kraken`, `setup-robinhood`, `tool-spec`, `trading-bot`. The Claude Code plugin wires in four of them.
- **REST API** (FastAPI) next to the MCP server, same agent.
- **Claude Code plugin** install, see [Install as a Claude Code plugin](#install-as-a-claude-code-plugin-api-key-required).
- **Works with Codex and any MCP client.** The local MCP endpoint is `http://127.0.0.1:9080/mcp/`. See [Use it from Codex](#use-it-from-codex).
- **Hosted MangroveAI MCP server** at `https://api.mangrovedeveloper.ai/mcp/` (streamable HTTP, answers `initialize` without auth).
- **Pricing:** a free API key at https://mangrovedeveloper.ai, or x402 pay-per-call in USDC.
- **Non-custodial:** execution on Kraken (bring-your-own-key or OAuth), Robinhood, and 1inch across 10 EVM chains. Keys and wallets stay on your machine.

More for agent builders: https://mangrove.ai/agents/ and https://mangrove.ai/llms.txt

Full platform documentation and the trading knowledge base live at **https://mangrove.io/docs** (KB: https://mangrove.io/knowledge-base) — the agent's `kb_search` tool queries the same corpus.

**Real mainnet swap from the agent** (verified April 2026): [0x5c126e...c5565](https://basescan.org/tx/0x5c126e6be26fc736bcb3f11a8f4c699aeee754f6c0bf7e5b7aa2df6a859c5565)

---

## Use Mangrove agent with your AI client

Start with the [user guide](docs/user-guide.md) for installation, choosing a name,
coming back later, and switching clients.

Tell **Claude Code, Codex, Grok Build, or Perplexity Computer**:

> Install https://github.com/MangroveTechnologies/mangrove-agent and follow its AI client installation instructions.

**For the installing agent:** follow [AI client setup](docs/clients.md#install-from-an-agent).
Install the server in your execution environment, load the complete agent
instructions, discover its tools, and call the free `status` tool before
reporting success. Perplexity Computer can use the provided command client;
this installation does not require creating a Perplexity Project, configuring a
custom cloud connector, or installing another desktop app.

The agent's shared identity and operating instructions live in [`SAGE.md`](SAGE.md).
First run asks what to name the agent. That choice is saved locally and reused
across clients; there is no default personal name.
`AGENTS.md` points Codex and compatible clients there; `CLAUDE.md` imports it for
Claude Code. Codex and Grok Build launch helpers, MCP configuration templates,
and the complete Perplexity/web instruction bundle are documented in
[AI client setup](docs/clients.md). Tool access is configured and verified
separately for each client.

## Install with setup.sh (sign-in, API key or x402)

Clone the repository and run `./scripts/setup.sh`. Choose an existing MangroveAI
API key, browser sign-in (OAuth), or wallet payments (x402, no signup). Browser sign-in
requires the OAuth-enabled backend and currently supports a restricted tool catalog;
see [sign-in setup and limitations](docs/oauth-login.md). The script protects local access,
starts the agent and registers Claude when its CLI is available. For x402, a final menu prints wallet steps
for you to run yourself; it never automatically creates/imports a wallet or pays.
See [the complete setup and wallet flow](docs/setup-x402.md).

## Install as a Claude Code plugin (API key required)

For the existing API-key plugin flow, tell your Claude Code agent *"install this trading bot: https://github.com/MangroveTechnologies/mangrove-agent"*, or run it yourself:

```bash
claude plugin marketplace add MangroveTechnologies/mangrove-agent
claude plugin install mangrove-agent@mangrove
```

Inside a Claude Code session the same thing is `/plugin marketplace add MangroveTechnologies/mangrove-agent`, then `/plugin install mangrove-agent@mangrove`. Claude Code asks for your **MangroveAI API key** (free at https://mangrovedeveloper.ai) and stores it as a sensitive setting. Never paste the key into chat.

Start a new session. On first start the plugin installs its Python dependencies and starts the agent in the background (about 1-3 minutes). Then say *"give me the tour"* or *"build me a momentum strategy for ETH"*.

- **Needs:** Python 3.11+ and a bash shell (macOS, Linux, or WSL / Git Bash on Windows).
- **Where your data lives:** `~/.mangrove-agent/` holds the config, the SQLite DB (wallets, strategies, trades) and the wallet master key. It survives plugin updates. Back it up once you hold funds, and never delete it while wallets hold funds.
- **Network:** the agent listens on `127.0.0.1:9080` only. Don't also run a git-clone agent on 9080.
- **Logs:** `~/.mangrove-agent/agent-data/agent.log` (server) and `bootstrap.log` (install/start).

Want to change the code? Clone the repo instead (below). Both paths run the same agent.

### Use it from Codex

The repo ships a Codex marketplace file at `.agents/plugins/marketplace.json`. Add the marketplace from a terminal:

```bash
codex plugin marketplace add MangroveTechnologies/mangrove-agent
```

Per OpenAI's [Codex plugin docs](https://developers.openai.com/codex/plugins/build), the plugin is then installed from the Plugins Directory in the ChatGPT desktop app (restart the app, open the Plugins Directory, choose the `mangrove` marketplace, install `mangrove-agent`). The docs list no CLI install command. To enable it for one project, add this to that repo's `.codex/config.toml`:

```toml
[plugins."mangrove-agent@mangrove"]
enabled = true
```

You can also skip the marketplace and point Codex at the local MCP server (`http://127.0.0.1:9080/mcp/`) with the template in `.codex/config.toml.example`. See [AI client setup](docs/clients.md#codex-and-grok-build).

## Get started (git clone)

This repo is **your own trading agent**: you run it on your machine, your keys never leave it, and it's the reference consumer of the [Mangrove API](https://mangrovedeveloper.ai) — clone it, make it yours, extend it into your own trading system.

1. **Choose your AI client** in the [user guide](docs/user-guide.md). Claude Code, Codex, Grok Build and Perplexity Computer support installation. A MangroveAI API key is optional; a fresh unattended setup defers wallet payments until you configure them.
2. **Follow [`SETUP.md`](SETUP.md)** for manual prerequisites and setup. Its longer Claude walkthrough is optional; other clients do not need Claude or Node.js for the Python agent.
3. **Take the guided walkthrough** at [`tutorials/trading-app/`](tutorials/trading-app/00-index.md) - self-paced chapters from "I just cloned this" to "I have a paper strategy running" to (optionally) "I made a live swap."

Chapters 01-05 avoid live trading capital. Paid API calls can still consume an API-key allowance or require x402 payments, including in paper mode. Chapters 06-08 cover optional live trading.

Already know your way around? Keep reading — the rest of this README is a reference.

## Connect your Kraken account

The agent trades on Kraken (CEX) as well as on-chain. Two ways in — pick by how you want access held:

| | **Bring your own key** — `/setup-kraken` | **Keyless OAuth** — `/connect-kraken` |
|---|---|---|
| How it works | You create a **least-privilege** Kraken API key (trade + query only, **withdrawal disabled**); the SDK talks to `api.kraken.com` directly from your machine | You approve once in a browser (Kraken Connect / OAuth2); no API key to create, copy, or store |
| Who holds access | **You** — the key never leaves your machine and never touches a Mangrove server | **Mangrove platform** holds the OAuth grant and submits on your behalf |
| Best for | Self-custody purists; anyone comfortable making an API key | Fastest path; anyone who doesn't want to manage a key |
| Revoke | Delete the key in Kraken settings | Revoke the app grant in Kraken settings |

Just say **"set up Kraken"** or **"connect Kraken without a key"** in a session and the matching skill walks you through it — including the exact permission set for the BYOK key. Fills sync into the same local SQLite trade log as your DEX swaps (`cex_status`, `cex_balances`, `cex_sync_fills`).

---

## Prerequisites

For marketplace listings and other ownership-protected actions, see
[Marketplace wallet approvals](docs/marketplace-wallets.md). Claude can prepare
an action, show it for approval, then have the local agent sign and submit it to
MangroveMarkets. This requires explicit marketplace configuration and a backed-up
Base or XRPL wallet. Payment settlement is a separate integration.

| Tool | Install | Why |
|---|---|---|
| **VSCode** (optional) | https://code.visualstudio.com/download | Editor with an integrated terminal. Your existing terminal or AI client's execution environment also works. |
| **Python 3.11+** | https://www.python.org/downloads/ | The agent is a Python FastAPI process. 3.11 is the minimum. |
| **Git for Windows** (Windows only) | https://git-scm.com/download/win | Gives you Git Bash, so the `*.sh` scripts in this repo work identically to macOS / Linux. Set VSCode's default terminal to `Git Bash` via the command palette. |
| **AI client** | [Claude, Codex, Grok, or Perplexity setup](docs/clients.md) | A client with command execution or MCP access. Claude Code is optional. |
| **MangroveAI API key** | Free at https://mangrovedeveloper.ai | Optional for setup.sh: choose API-key access or x402 wallet payments. |

Docker is **optional** — see the alternate install path below. Bare-metal is the primary path because the `keyring` library can reach your OS keychain directly when the agent runs natively.

---

## Quick start — bare-metal (recommended)

One command selects API-key or x402 access, creates a venv, installs dependencies, starts the local agent, registers Claude when available, and verifies authenticated local access.

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
cd mangrove-agent
./scripts/setup.sh
```

First run can take a few minutes for dependencies and network discovery. Re-runs preserve configuration and local state; they can restart the server after code or configuration changes. Connecting another client to an already running agent does not require rerunning setup.

When it's finished:
- Agent runs at `http://localhost:9080` (pid in `agent-data/bare.pid`, logs in `agent-data/bare.log`). We bind 9080 externally because `:8080` is commonly squatted by VSCode Helper and other dev tools.
- `./scripts/verify_quickstart.sh --bare` passed: local authentication and required local tools were checked. Unavailable remote tools are reported separately.
- Claude registration is saved when available and not skipped; other clients use the command client or their own MCP connection.

**After setup, continue with your chosen client** using the [user guide](docs/user-guide.md#come-back-or-switch-clients). Claude users can start `./scripts/chat.sh` for a session with shell/file tools disabled and only Mangrove MCP; see [restricted chat](docs/restricted-chat.md). In x402 mode, complete the printed payment-wallet instructions before requesting paid data or backtests. API-key users can paper-trade without a payment wallet. Simulated trades do not make upstream API calls free.

### Useful `./scripts/setup.sh` flags

```
./scripts/setup.sh --yes                          # preserve settings; fresh install uses x402
./scripts/setup.sh --auth oauth                    # sign in and approve access in your browser
./scripts/setup.sh --auth api-key                  # keep existing key or enter a new one (hidden)
./scripts/setup.sh --auth x402                     # explicitly switch to wallet payments
./scripts/setup.sh --foreground                    # run uvicorn in your terminal (Ctrl+C to stop)
./scripts/setup.sh --no-mcp                        # skip Claude Code registration
./scripts/setup.sh --no-verify                     # skip the post-start verify pass
./scripts/setup.sh --docker                        # use Docker instead of bare-metal
BARE_PORT=9085 ./scripts/setup.sh                  # use a different port (9080 busy/squatted)
```

`BARE_PORT` is honored end-to-end (server + health check + MCP registration + verify). If 9080 is occupied, `setup.sh` refuses to start and tells you to free it or set `BARE_PORT` — it will not silently bind-fail or report a false success.

---

## Alternate quick start — Docker

If you can't run Python on the host (corporate restrictions, reproducibility mandate), Docker works the same way. The tradeoff: the container can't reach your OS keychain, so the Fernet master key lives in `./agent-data/master.key` (chmod 600, gitignored) instead of Keychain / Secret Service / Credential Manager.

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
cd mangrove-agent
./scripts/setup.sh --docker
```

State is persisted in the `./agent-data/` directory (bind-mounted into the container). The directory mount avoids the macOS / Windows single-file bind-mount staleness that previously ate DB rows after rebuild.

---

## Your first trade

This section is a fast tour. The full walkthrough is in [`tutorials/trading-app/`](tutorials/trading-app/00-index.md); come back here if you want the shorter reference.

**First connection.** Your chosen client loads the shared policy, discovers tools and calls free status. If the installation has no saved name, it asks you to choose one. Startup does not automatically fetch paid market data, run backtests or configure wallets. Ask for a tour when you want one; paid examples need the appropriate access first.

**Paper trading (payment wallet needed for paid API calls in x402 mode).**

> "Build me a strategy for ETH on 1h."

The agent calls MangroveAI's `build_strategy_for_regime`: the server reads the current market regime, searches the Strategy Engine's completed runs with fixed floors (more than 10 trades, Sharpe at least 1, IRR at least 10%, beats benchmarks) in that regime, picks one at random from those whose entry the regime admits, saves it as a draft and backtests it on the last 90 days. Ask for a *new* strategy to have one composed from regime-allowed signals instead.

> "Promote it to paper."

Schedules the strategy on a cron at the strategy's timeframe. Evaluations fire, paper fills get logged. No funds at risk.

**Going live (optional, needs funds).**

For Robinhood, use the bundled `setup-robinhood` skill and
[execution-venue setup](docs/robinhood-execution.md). It uses broker OAuth and
needs no local trading wallet.

For DEX execution, wallet setup and live trading are in Chapters 06 and 07 of the tutorial:

- Create or import a wallet via `create_wallet` / `stash-secret.sh` + `import_wallet`.
- Save the secret via `./scripts/reveal-secret.sh <vault_token>` in your terminal (never in the chat).
- Run `./scripts/confirm-backup.sh <address>` to flip the backup gate.
- Fund with 1–5 USDC on Base.
- Promote the strategy to live with an allocation block (`confirm=true`, `slippage_pct ≤ 0.0025`).
- Live evaluations fire on the same cron; when the strategy decides to trade, the `mangrovemarkets` SDK routes through 1inch, the agent signs locally, broadcasts, and logs the tx.

## Safety model at a glance

- **Your private keys never touch this chat.** `create_wallet` returns a `vault_token`, not the plaintext. Revealing is a separate CLI (`reveal-secret.sh`) that prints to your terminal only.
- **Harness hooks block key pastes.** If you try to paste a key into Claude Code, `.claude/hooks/block-wallet-secrets.sh` refuses the prompt with an educational message. The hook is in `.claude/settings.json` — the machine owner can change this configuration.
- **Live trading is gated on explicit backup confirmation.** After you save a wallet's secret off-agent, `./scripts/confirm-backup.sh <addr>` flips a flag. `execute_swap` and `update_strategy_status → live` refuse on wallets without it.
- **Master key stays local.** Bare-metal: OS keychain. Docker: `./agent-data/master.key` (chmod 600, gitignored).

---

## What the agent can do

The agent has two kinds of tools. Run `list_tools` for the live, authoritative catalog.

**1. MangroveAI tools (the single source of truth).** Everything about strategies, backtests, signals, market data, on-chain data, the knowledge base and the Strategy Engine lives in MangroveAI. The agent keeps **no copy** of these tools: at startup it reads MangroveAI's live MCP catalog and forwards each call to MangroveAI's `/mcp/` endpoint, paying per call in x402 wallet mode or using your API key. When MangroveAI adds or changes a tool, the agent picks it up without a code change.

| Area (examples, not a fixed list) | MangroveAI tools |
|---|---|
| Strategy building | `load_skill`, `build_strategy_for_regime`, `list_backtest_markets`, `save_strategy`, `update_strategy`, `verify_strategy`, `list_strategies`, `get_strategy` |
| Backtests | `run_backtest` + `get_backtest` (a saved strategy), `get_backtest_trades`, `list_backtests`, `get_benchmark`, `screen_candidates` |
| Sweep results | `find_sweep_runs` |
| Signals & regime | `list_signals`, `get_signal`, `search_signals`, `match_signals`, `query_signal_behavior`, `get_market_regime` |
| Market, on-chain, DeFi, social | `get_ohlcv`, `get_market_data`, `get_trending`, `get_token_holders`, `get_smart_money_sentiment`, `get_chain_tvl`, `get_social_sentiment`, and more |
| Knowledge base | `kb_search`, `kb_glossary_get`, `kb_get_document`, `query_knowledge` |
| Marketplace | `search_marketplace`, `get_marketplace_strategy` |
| Your MangroveAI positions & trades | `list_positions`, `list_trades` |

**2. Local tools (things only your machine can do).** These stay in the agent because they need your keys, your wallet or your local records.

| Category | Tools |
|---|---|
| Discovery (free) | `status`, `list_tools` |
| Naming (local, authenticated) | `set_agent_name` |
| Payment diagnostics | `hello_mangrove` remains directly callable; hidden from normal discovery |
| Wallet custody | `create_wallet`, `import_wallet`, `list_wallets` stay local; balance and portfolio reads are discovered from Markets |
| Kraken (CEX) | `cex_status`, `cex_connect_kraken`, `cex_balances`, `cex_validate_order`, `cex_sync_fills` |
| Markets reads | Server-owned `dex_*`, `oneinch_*` and approved `wallet_*` reads; supports API-key access or $0.01 USDC x402 reads. See [migration and units](docs/markets-read-migration.md). |
| Local DEX execution | `execute_swap` retains local approval, signing and human-unit conversion |
| Server execution history | Discovered MangroveAI trade and evaluation tools |
| x402 spend budget | `x402_spend_status`, `x402_spend_reset` |

Saved strategies are listed and read through MangroveAI MCP `list_strategies` and
`get_strategy`. Local strategy list/get tools and REST routes are removed; local status/evaluation
MCP tools are also removed. Use the server-advertised strategy tools.
Local execution, scheduling and history remain separate from saved-strategy reads.

Local tools also have REST endpoints at `/api/v1/agent/*`, calling the same service layer. MangroveAI tools have **no** local REST copy; call them over the agent's MCP endpoint, or call MangroveAI directly. Authenticated routes take `X-API-Key` set to the first entry of `API_KEYS` in `server/src/config/local-config.json` (a unique key `setup.sh` generates). That is the agent's own local key, **not** your MangroveAI API key. `/health`, `status` and `list_tools` need no key, so a wrong key looks fine until the first authenticated call returns 401.

**Backtests and the Strategy Engine.** Backtests run on MangroveAI, never in the agent. For a saved strategy, `run_backtest` submits the run and `get_backtest` reads the result when it finishes. To search what the Strategy Engine has already measured, use `find_sweep_runs`. Creating and launching new sweeps is not offered over MCP today. Deeper dives: the KB [SIEVE guide](https://mangrove.io/docs/guides/using-sieve-prefilter) and [Experiments reference](https://mangrove.io/docs/api-reference/experiments).

---

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│  Your machine                                               │
│                                                             │
│  Claude Code ─MCP──┐                                        │
│  Python/curl ─REST─┤                                        │
│                    ▼                                        │
│  ┌─ mangrove-agent (single FastAPI process, port 9080) ──┐     │
│  │   • auth middleware (X-API-Key)                   │     │
│  │   • service layer (one for REST + MCP)            │     │
│  │   • APScheduler (in-process cron, SQLite jobstore)│     │
│  │   • local Fernet-encrypted wallets                │     │
│  └───────────────────────────────────────────────────┘     │
│           │                        │                        │
│           ▼                        ▼                        │
│  ┌── SQLite: agent.db ─┐  ┌── OS Keychain (Fernet key) ─┐  │
│  └─────────────────────┘  └──────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
              │                      │
              ▼                      ▼
    MangroveAI /mcp/ +        mangrovemarkets SDK
    mangroveai SDK            (DEX swap, portfolio,
    (every MangroveAI tool,    wallet)
     strategy evaluation)
```

Strategy evaluation happens inside `mangroveai.execution.evaluate()` — the agent does **not** re-implement signal logic, risk gates, position sizing, or cooldowns. It orchestrates: fetch strategy → call SDK → dispatch returned `OrderIntent[]` to the executor → log.

For live trades the agent decrypts your wallet's secret in-process, signs the unsigned transaction returned by `mangrovemarkets`, broadcasts the signed bytes, and zeroes the secret. The SDK never sees your key.

---

## Architecture docs

| Doc | What's in it |
|---|---|
| [docs/api-reference.md](docs/api-reference.md) | The Mangrove API surface we call |
| [docs/user-stories.md](docs/user-stories.md) | 18 user stories + 4 flow diagrams |
| [docs/specification.md](docs/specification.md) | API contracts, Pydantic models, SQLite schema, error codes |
| [docs/architecture.md](docs/architecture.md) | System diagrams, sequence diagrams, file tree |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 24-task phased build plan |

## Development

Extending the agent — adding a new endpoint, MCP tool, signal, or strategy — follows the service-layer pattern documented in [`docs/contributing.md`](docs/contributing.md). Strategy creation and backtesting instructions are loaded live from MangroveAI through the MCP `load_skill` tool. The agent ships no copies of server skills or strategy generators. Contributor skills remain in `.claude/skills/`.

## Tests

```bash
docker run --rm -v "$(pwd)/server:/app" -w /app -e ENVIRONMENT=test \
  $(docker compose build -q app && docker compose images -q app) \
  pytest tests/
```

Or from inside the running container:

```bash
docker compose exec app pytest tests/
```

Expect everything to pass apart from the 2 opt-in live-swap tests, which are skipped.

To run the opt-in live swaps:

```bash
# Testnet (Base Sepolia)
ENABLE_SEPOLIA_TEST=1 BASE_SEPOLIA_PRIVATE_KEY=0x... pytest tests/e2e/test_live_swap.py::test_sepolia_live_swap

# Mainnet — real funds; we tested at 0.10 USDC
ENABLE_MAINNET_TEST=1 BASE_MAINNET_PRIVATE_KEY=0x... pytest tests/e2e/test_live_swap.py::test_mainnet_live_swap
```

## Deployment

Local-only for v1 (Docker Compose). Cloud deployment (Cloud Run with persistent storage, Cloud SQL) is roadmap, not shipped.

## Project layout

```
mangrove-agent/
├── .claude/                  # Claude Code framework (skills, agents, rules)
├── server/
│   ├── src/
│   │   ├── app.py            # FastAPI factory
│   │   ├── config/           # Per-env JSON configs
│   │   ├── api/routes/       # REST routes — one file per resource
│   │   ├── mcp/              # MCP tool registration
│   │   ├── models/           # Pydantic domain + DB models
│   │   ├── services/         # Business logic (wallet, strategy, executor, scheduler, trade_log, …)
│   │   └── shared/           # auth, db/sqlite.py, crypto/fernet.py, clients/mangrove.py, errors, logging
│   └── tests/                # unit / integration / e2e
├── docs/                     # Design docs (requirements, spec, architecture, plan)
├── scripts/verify_quickstart.sh
├── docker-compose.yml
├── .mcp.json.example         # Drop-in Claude Code MCP config
└── SAGE.md                   # Shared identity and operating instructions
```

## License

MIT

### Robinhood execution venue

Mangrove strategies can use Robinhood as their broker execution venue through its
official MCP service. See [connection, allocation, and recovery instructions](docs/robinhood-execution.md).

Server-owned paper portfolios can be evaluated on an agent-owned clock. See
[paper scheduling](docs/paper-scheduling.md) for controls, recovery, costs and rollout.
