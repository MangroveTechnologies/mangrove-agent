<div align="center">
  <a href="https://github.com/MangroveTechnologies/mangrove-agent">
    <img src="assets/icon.png" alt="Mangrove" width="120" height="112">
  </a>

  <h1>mangrove-agent</h1>

  <p>
    <strong>A local MCP server (Model Context Protocol) and Claude Code plugin for AI trading agents.</strong><br>
    Built on the Mangrove API, with a REST API alongside. FastAPI + MCP. Strategies are authored and backtested on MangroveAI; this agent supplies the clock, local custody and execution, and a full audit trail.<br>
    Kraken and Robinhood venues, and DEX execution via <strong>1inch</strong> across 10 EVM chains
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

mangrove-agent is an open-source local client for Mangrove services. It gives an AI client (Claude Code, Codex, Grok Build, Perplexity Computer, or any MCP client) local tools and access to configured hosted services. Credential and wallet custody depends on the selected workflow:

- **Author and test strategies.** The agent turns a goal such as "momentum on ETH" into a backtested strategy by calling the [MangroveAI API](https://mangrovedeveloper.ai). Strategy building, backtesting, signals, market data and the knowledge base all live in MangroveAI; the agent forwards those tools and keeps no copy of them.
- **Paper trade.** A paper schedule runs on a clock the agent supplies, while MangroveAI owns the strategy, balances, positions and trades. No funds are at risk.
- **Use the supported execution workflows.** For local DEX execution, the agent signs transactions on the user's machine. Kraken BYOK keeps its API key local; Kraken OAuth is a separate connection where Mangrove holds the authorized grant. Robinhood uses its documented broker connection. Mangrove-owned execution software enforces deterministic risk management based on live market and account conditions. External AI clients and SDKs request operations and preserve Mangrove's decisions, audit details, and errors; they do not replace its risk policy.
- **Keep an audit trail.** Local evaluations, DEX swaps and confirmed Robinhood fills are logged to a local SQLite database. Kraken orders are not added to the local trade log.

The agent exposes the same capabilities two ways: an MCP endpoint at `http://127.0.0.1:9080/mcp/` and a REST API under `/api/v1/agent/`. MangroveAI also runs its own hosted MCP server at `https://api.mangrovedeveloper.ai/mcp/`.

You pay for MangroveAI access with a free API key from https://mangrovedeveloper.ai, or per call in USDC with x402 (no signup). More for agent builders: https://mangrove.ai/agents/ and https://mangrove.ai/llms.txt. Full platform documentation and the trading knowledge base are at https://mangrove.io/docs (knowledge base: https://mangrove.io/knowledge-base); the agent's `kb_search` tool queries the same corpus.

## Choose an install path

| Path | Use it when | Start here |
|---|---|---|
| Ask your AI client | You use Claude Code, Codex, Grok Build or Perplexity Computer and want the client to install and connect itself | [Install with an AI client](#install-with-an-ai-client) |
| Clone and run `setup.sh` | You want the full repository, API-key or x402 access, and the option to change the code | [Quick start](#quick-start-clone-and-run-setupsh) |
| Claude Code plugin | You use Claude Code and have a MangroveAI API key | [Claude Code plugin](docs/clients.md#claude-code) |
| Codex plugin or MCP config | You use Codex | [Codex](docs/clients.md#codex-and-grok-build) |
| Docker | You cannot run Python on the host | [Docker](#alternate-quick-start--docker) |

Every path runs the same agent. Use one path per machine: the agent listens on `127.0.0.1:9080`, so a plugin install and a clone cannot run side by side on that port.

### Install with an AI client

Tell Claude Code, Codex, Grok Build or Perplexity Computer:

> Install https://github.com/MangroveTechnologies/mangrove-agent and follow its AI client installation instructions.

The client installs the server in its own execution environment, loads the shared agent instructions, discovers the tools and calls the free `status` tool before it reports success. The [user guide](docs/user-guide.md) covers installation, choosing the agent's name, coming back later and switching clients. The installing agent follows [Install from an agent](docs/clients.md#install-from-an-agent). Perplexity Computer uses the provided command client and needs no Perplexity Project, custom cloud connector or extra desktop app.

The agent's shared identity and operating instructions live in [`SAGE.md`](SAGE.md). On first run the agent asks what to name itself and saves that choice locally for every client; there is no default personal name. `AGENTS.md` points Codex and compatible clients to `SAGE.md`, and `CLAUDE.md` imports it for Claude Code. Launch helpers, MCP configuration templates and the instruction bundle for hosts without file access are in [AI client setup](docs/clients.md). Tool access is configured and verified separately for each client.

## Install with setup.sh (sign-in, API key or x402)

Clone the repository and run `./scripts/setup.sh`. Choose an existing MangroveAI
API key, browser sign-in (OAuth), or wallet payments (x402, no signup). Browser sign-in
requires the OAuth-enabled backend and currently supports a restricted tool catalog;
see [sign-in setup and limitations](docs/oauth-login.md). The script protects local access,
starts the agent and registers Claude when its CLI is available. For x402, a final menu prints wallet steps
for you to run yourself; it never automatically creates/imports a wallet or pays.
See [the complete setup and wallet flow](docs/setup-x402.md).

[`SETUP.md`](SETUP.md) walks through installing these step by step on macOS and Windows.

### Quick start: clone and run `setup.sh`

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
cd mangrove-agent
./scripts/setup.sh
```

One command selects API-key or x402 access, creates `.venv`, installs dependencies, starts the agent in the background, registers it with Claude Code when the `claude` CLI is present, and verifies authenticated local access. A first run can take a few minutes. Reruns preserve configuration and local state and restart the server only when code or configuration changed. Connecting another client to a running agent does not need a rerun.

When it finishes:

- The agent runs at `http://127.0.0.1:9080`. Its process ID is in `agent-data/bare.pid` and its log is `agent-data/bare.log`.
- `setup.sh` has run `scripts/verify_quickstart.sh`, which checks authenticated local access, scheduler readiness and that the onboarding tools are discoverable.
- Claude registration is saved when the CLI is available and you did not pass `--no-mcp`. Other clients use the command client or their own MCP connection.

Continue with your client using the [user guide](docs/user-guide.md#come-back-or-switch-clients). Claude users can run `./scripts/chat.sh` for a session with shell and file tools disabled and only the Mangrove MCP server; see [restricted chat](docs/restricted-chat.md). In x402 mode, finish the printed payment-wallet steps before you request paid data or backtests ([details](docs/setup-x402.md)). With an API key you can paper trade without a payment wallet. Paper trades are simulated, but the upstream data and evaluation calls behind them still count against your API-key allowance or cost x402 payments.

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
./scripts/setup.sh --docker
```

Docker setup uses port 9080, published on loopback only. State persists in the `./agent-data/` directory, which is bind-mounted into the container as a directory (single-file mounts drift on macOS and Windows). The wallet master key is stored the same way as in a bare-metal install, in `agent-data/master.key` (mode 0600, gitignored).

## Your first strategy and trade

The full walkthrough is the [trading tutorial](tutorials/trading-app/00-index.md); this is the short version. Chapters 01-05 use no live trading capital, and chapters 06-08 cover optional live trading.

**First connection.** Your client loads the shared policy, discovers tools and calls free `status`. If the installation has no saved name, it asks you to choose one. Startup does not fetch paid data, run backtests or configure wallets on its own.

**Build a strategy.**

> "Build me a strategy for ETH on 1h."

The agent calls MangroveAI's `build_strategy_for_regime`. The server reads the current market regime and searches the Strategy Engine's completed runs, using fixed quality floors (more than 10 trades, Sharpe at least 1, annual return at least 10%, beats its benchmarks), for one whose entry the regime admits. It picks one, saves it as a draft and backtests it on the last 90 days. Ask for a *new* strategy to have one composed from regime-allowed signals instead. Strategy building and backtesting follow instructions the agent loads live from MangroveAI with `load_skill`.

**Paper trade it.**

> "Promote it to paper."

The agent calls `agent_start_paper_schedule` with the server strategy ID, an interval (60, 300, 900, 3600, 14400 or 86400 seconds), a maximum number of evaluations (1 to 10000) and `confirm=true`. The agent's clock triggers each evaluation; MangroveAI evaluates the strategy and owns its balances, positions and trades. Use `agent_list_paper_schedules`, `agent_get_paper_schedule`, `agent_pause_paper_schedule` and `agent_reconcile_paper_schedule` to follow and control it. See [paper scheduling](docs/paper-scheduling.md).

**Go live (optional, needs funds).** Live trading is a separate, explicit decision.

- **Robinhood:** use the bundled `setup-robinhood` skill and the [execution venue guide](docs/robinhood-execution.md). It uses broker OAuth and needs no local trading wallet.
- **DEX:** wallet setup and live trading are covered in chapters 06 and 07 of the tutorial. In outline: create or import a wallet (`create_wallet`, or `./scripts/stash-secret.sh` and then `import_wallet`); save its secret in your own terminal with `./scripts/reveal-secret.sh --address WALLET_ADDRESS`; run `./scripts/confirm-backup.sh WALLET_ADDRESS`; fund the wallet with a small amount of USDC on Base; then trade. `execute_swap` needs `confirm=true`, an explicit `slippage_pct` of at most 0.0025 (0.25%) and a backup-confirmed wallet. A strategy moves to `live` through `PATCH /api/v1/agent/strategies/{strategy_id}/status` with `confirm=true` and an allocation (`wallet_address`, `token`, `token_address`, `amount`, `slippage_pct`).
- At swap time the agent signs locally and sends only the signed transaction to MangroveMarkets to broadcast. Swap signing is restricted to calls to the 1inch routers and the ERC-20 approvals that precede them; any other transaction is refused before the key is decrypted.

## Connect your Kraken account

The agent connects to Kraken in one of two modes, chosen by how you want access held. What each mode can do differs:

| | **Bring your own key** (`/setup-kraken`) | **Keyless OAuth** (`/connect-kraken`) |
|---|---|---|
| How it works | You create a least-privilege Kraken API key (query and order permissions, **withdrawal disabled**). The agent talks to `api.kraken.com` directly from your machine. | You approve once in a browser (Kraken Connect, OAuth2). There is no API key to create, copy or store. |
| Who holds access | **You.** The key stays on your machine and never reaches a Mangrove server. | **The Mangrove platform** holds the OAuth grant and submits orders on your behalf. |
| Best for | Self-custody and anyone comfortable making an API key. | The fastest path, or anyone who does not want to manage a key. |
| Orders | Dry-run validation only (`cex_validate_order`). The agent does not place real orders with your key. | Real orders through `POST /api/v1/agent/cex/oauth/orders`. No MCP tool places a Kraken order. |
| Revoke | Delete the key in Kraken settings. | Revoke the app grant in Kraken settings, or disconnect in Mangrove. |

Say **"set up Kraken"** or **"connect Kraken without a key"** in a session and the matching skill walks you through it, including the exact permission set for the key. In bring-your-own-key mode, `cex_sync_fills` sends your Kraken trade statistics to the MangroveMarkets telemetry service; it does not add them to the local trade log.

## Safety model

- **Private keys stay out of the chat.** `create_wallet` returns a `vault_token`, never the plaintext secret. You reveal the secret in your own terminal with `./scripts/reveal-secret.sh`, which refuses to write to anything but a terminal.
- **Claude hooks block key pastes.** In Claude Code sessions, `.claude/hooks/block-wallet-secrets.sh` refuses prompts that contain a private key or recovery phrase, and checks tool output as defense in depth. The hook is configured in `.claude/settings.json`, and the machine owner can change it. Other clients do not inherit it, so never paste a secret into any AI chat.
- **Live trading needs a confirmed backup.** After you save a wallet's secret off the agent, `./scripts/confirm-backup.sh WALLET_ADDRESS` records it. Live swaps and paying x402 from a wallet refuse wallets without it.
- **The master key stays local.** Wallet secrets are encrypted with a Fernet key stored in `agent-data/master.key` (mode 0600, gitignored). If no keyfile exists, the agent reads an OS keychain entry instead; it never silently generates a replacement for a key that is missing.
- **Local access is authenticated.** The agent binds to `127.0.0.1`, and setup generates a unique local credential (`API_KEYS`) for the AI client. That credential is separate from your MangroveAI key.

## What the agent can do

The local agent exposes `list_tools` for its own MCP catalog, including local helpers and tools it currently forwards. This call does not query the hosted MangroveAI catalog directly. For the hosted service, use MCP protocol discovery (`tools/list`) against its `/mcp/` endpoint; when available, `discover_tools` provides a higher-level view of names and descriptions first, with schemas on request. Catalogs can differ by service configuration and can change over time, so do not rely on a fixed total. See [progressive discovery](docs/clients.md#progressive-discovery-of-managed-services).

**1. MangroveAI tools (the source of truth).** Strategies, backtests, sweeps, signals, market data, on-chain data, the knowledge base and the Strategy Engine live in MangroveAI. The agent reads MangroveAI's live MCP catalog (cached for up to a minute) and forwards each call to MangroveAI's `/mcp/` endpoint, paying per call in x402 wallet mode or using your API key. When MangroveAI adds or changes a tool, the agent picks it up without a code change.

| Area (examples, not a fixed list) | MangroveAI tools |
|---|---|
| Strategy building | `load_skill`, `build_strategy_for_regime`, `list_backtest_markets`, `save_strategy`, `update_strategy`, `verify_strategy`, `list_strategies`, `get_strategy` |
| Backtests | `run_backtest`, `get_backtest`, `get_backtest_trades`, `list_backtests`, `get_benchmark`, `screen_candidates` |
| Sweeps | `find_sweep_runs`, `size_sweep`, `create_sweep`, `launch_sweep`, `list_sweeps`, `get_sweep`, `pause_sweep` |
| Signals and regime | `list_signals`, `get_signal`, `search_signals`, `match_signals`, `query_signal_behavior`, `get_market_regime` |
| Market, on-chain, DeFi, social | `get_ohlcv`, `get_market_data`, `get_trending`, `get_token_holders`, `get_smart_money_sentiment`, `get_chain_tvl`, `get_social_sentiment`, and more |
| Knowledge base | `kb_search`, `kb_glossary_get`, `kb_get_document`, `query_knowledge` |
| Marketplace | `search_marketplace`, `get_marketplace_strategy` |
| Your MangroveAI positions and trades | `list_positions`, `list_trades` |

**2. Local tools (what only your machine can do).** These stay in the agent because they need your keys, your wallets or your local records.

| Category | Tools |
|---|---|
| Discovery (free) | `status`, `list_tools` |
| Naming | `set_agent_name` |
| Wallet custody | `create_wallet`, `import_wallet`, `list_wallets` |
| Kraken (CEX) | `cex_status`, `cex_connect_kraken`, `cex_balances`, `cex_validate_order`, `cex_sync_fills` |
| DEX execution | `execute_swap` (local approval, signing and human-unit conversion) |
| Paper scheduling | `agent_start_paper_schedule`, `agent_list_paper_schedules`, `agent_get_paper_schedule`, `agent_pause_paper_schedule`, `agent_reconcile_paper_schedule` |
| Robinhood venue | `agent_robinhood_status`, `agent_configure_execution_venue`, `agent_configure_strategy_market`, `agent_list_venue_orders`, `agent_reconcile_venue_orders`, `agent_cancel_venue_order` |
| Marketplace approvals | `marketplace_prepare`, `marketplace_submit` (see [marketplace wallet approvals](docs/marketplace-wallets.md)) |
| x402 spend budget | `x402_spend_status`, `x402_spend_reset` |
| Payment diagnostics | `hello_mangrove` stays directly callable but is hidden from normal discovery |

The agent also forwards MangroveMarkets read tools (`dex_*`, `oneinch_*` and approved `wallet_*` names) that it discovers from the Markets server. They support API-key access or x402 reads at $0.01 USDC each; see [Markets read tools](docs/markets-read-tools.md) for names and units.

Saved strategies are read through MangroveAI's `list_strategies` and `get_strategy`. The agent has no local strategy creation: the old local create routes return HTTP 410.

Local tools also have REST endpoints under `/api/v1/agent/`, backed by the same service layer. MangroveAI tools have no local REST copy; call them over the agent's MCP endpoint or call MangroveAI directly. Authenticated routes take an `X-API-Key` header set to the first entry of `API_KEYS` in `server/src/config/local-config.json`, the unique local key `setup.sh` generated (not your MangroveAI key). `/health`, `status` and `list_tools` need no key, so a wrong key looks fine until the first authenticated call returns 401.

**Backtests and the Strategy Engine.** Backtests run on MangroveAI, never in the agent. For a saved strategy, `run_backtest` submits the run and `get_backtest` reads the result. `screen_candidates` ranks up to 99 candidates cheaply with the SIEVE model, and `find_sweep_runs` searches what the Strategy Engine has already measured. To size and launch a new sweep, the agent loads MangroveAI's `sweeps` skill, shows the estimated run count and waits for your agreement before it launches compute. Deeper dives: the knowledge base [SIEVE guide](https://mangrove.io/docs/guides/using-sieve-prefilter) and [Experiments reference](https://mangrove.io/docs/api-reference/experiments).

## How it works

```
+-------------------------------------------------------------+
|  Your machine                                               |
|                                                             |
|  AI client --MCP--+                                         |
|  curl/Python -REST+                                         |
|                   v                                         |
|  +- mangrove-agent (one FastAPI process, port 9080) ------+ |
|  |  - auth middleware (X-API-Key)                          | |
|  |  - service layer shared by REST and MCP                 | |
|  |  - APScheduler (paper clock, reconciliation workers)    | |
|  |  - locally encrypted wallets and signing                | |
|  +----------------------------------------------------------+ |
|        |                                  |                 |
|        v                                  v                 |
|  agent-data/agent.db (SQLite)    agent-data/master.key      |
+-------------------------------------------------------------+
        |                                  |
        v                                  v
  MangroveAI /mcp/                   MangroveMarkets
  (every MangroveAI tool:            (DEX quotes, routing,
   strategies, backtests,             broadcast; read tools)
   paper evaluation)
```

The agent does not re-implement signal logic, risk gates, position sizing or cooldowns; those run on MangroveAI. The agent orchestrates: it forwards tools, supplies the paper clock, and handles everything that needs your keys, wallets or local records. For a live trade it decrypts the wallet secret in-process, signs the unsigned transaction that MangroveMarkets prepared, and sends only the signed transaction back to be broadcast. MangroveMarkets never sees your key.

## Documentation

| Doc | What is in it |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | Installing with your AI client, naming the agent, returning and switching clients |
| [docs/clients.md](docs/clients.md) | Client adapters, instruction loading, install-from-an-agent steps, progressive discovery |
| [SETUP.md](SETUP.md) | Step-by-step prerequisites and manual setup for macOS and Windows |
| [docs/setup-x402.md](docs/setup-x402.md) | API-key and x402 access, the wallet commands, spend budget and payment recovery |
| [docs/configuration.md](docs/configuration.md) | Config files, keys, environments and secrets |
| [docs/restricted-chat.md](docs/restricted-chat.md) | The restricted Claude session started by `./scripts/chat.sh` |
| [docs/strategy-lifecycle.md](docs/strategy-lifecycle.md) | Strategy states, transitions, scheduling and execution |
| [docs/paper-scheduling.md](docs/paper-scheduling.md) | Server-owned paper schedules |
| [docs/robinhood-execution.md](docs/robinhood-execution.md) | The Robinhood execution venue |
| [docs/marketplace-wallets.md](docs/marketplace-wallets.md) | Wallet-approved marketplace actions |
| [docs/markets-read-tools.md](docs/markets-read-tools.md) | MangroveMarkets read tools, names and units |
| [docs/defi-pro.md](docs/defi-pro.md) | DeFi analytics tools served by MangroveAI |
| [docs/remote-mcp-payments.md](docs/remote-mcp-payments.md) | How tool calls to MangroveAI and MangroveMarkets are paid in wallet mode |
| [docs/x402-payment-uncertainty.md](docs/x402-payment-uncertainty.md) | What happens when a payment outcome is unknown, and how it is recovered |
| [docs/x402-sync-transport.md](docs/x402-sync-transport.md) | The x402 payment transport used by the MangroveAI client |
| [docs/x402-payment-scripts.md](docs/x402-payment-scripts.md) | Standalone x402 demo and test scripts |
| [docs/api-reference.md](docs/api-reference.md) | The REST API reference |
| [docs/specification.md](docs/specification.md) | API contracts, Pydantic models, SQLite schema, error codes |
| [docs/architecture.md](docs/architecture.md) | System and sequence diagrams, live execution diagram, code layout |
| [docs/mcp-contracts.md](docs/mcp-contracts.md) | Tool ownership and the offline MCP contract check |
| [docs/contributing.md](docs/contributing.md) | How to add a route, tool, migration or config key |
| [docs/incidents/2026-04-24-eip7702-drain.md](docs/incidents/2026-04-24-eip7702-drain.md) | Security post-mortem and the signing defenses it led to |
| [tutorials/trading-app/](tutorials/trading-app/00-index.md) | Self-paced chapters from a fresh clone to a paper strategy and, optionally, a live swap |

## Development

Extending the agent (a new endpoint, MCP tool, signal or strategy) follows the service-layer pattern in [`docs/contributing.md`](docs/contributing.md). Strategy creation and backtesting instructions are loaded live from MangroveAI through the MCP `load_skill` tool; the agent ships no copies of server skills or strategy generators. Contributor skills live in `.claude/skills/`.

### Tests

From a clone that has run `setup.sh`:

```bash
cd server
ENVIRONMENT=test ../.venv/bin/python3 -m pytest tests/ -v
```

CI runs the same suite. The two live-swap tests in `tests/e2e/test_live_swap.py` are skipped unless you opt in:

```bash
# Base Sepolia testnet (run from server/)
ENABLE_SEPOLIA_TEST=1 BASE_SEPOLIA_PRIVATE_KEY=0x... ENVIRONMENT=test ../.venv/bin/python3 -m pytest tests/e2e/test_live_swap.py::test_sepolia_live_swap

# Base mainnet, real funds (budget cap 0.10 USDC per run)
ENABLE_MAINNET_TEST=1 BASE_MAINNET_PRIVATE_KEY=0x... ENVIRONMENT=test ../.venv/bin/python3 -m pytest tests/e2e/test_live_swap.py::test_mainnet_live_swap
```

### Deployment

The agent is a local, single-user service. Cloud deployment is not provided.

### Project layout

```
mangrove-agent/
|-- .claude/                  # Claude Code framework (skills, hooks, rules, agents)
|-- .claude-plugin/           # Claude Code plugin manifest and hooks
|-- .agents/plugins/          # Codex marketplace file
|-- server/
|   |-- src/
|   |   |-- app.py            # FastAPI factory
|   |   |-- config/           # Per-environment JSON configs
|   |   |-- api/routes/       # REST routes, one file per resource
|   |   |-- mcp/              # MCP tool registration and proxies
|   |   |-- models/           # Pydantic domain and DB models
|   |   |-- services/         # Business logic (wallets, execution, scheduling, payments, trade log)
|   |   `-- shared/           # auth, db (SQLite), crypto, clients, x402, errors, logging
|   `-- tests/                # unit, integration, e2e
|-- scripts/                  # setup.sh, chat.sh, verify_quickstart.sh, wallet and client helpers
|-- docs/                     # Design and operations docs
|-- tutorials/trading-app/    # Guided walkthrough
|-- docker-compose.yml
|-- .mcp.json.example         # Drop-in Claude Code MCP config
`-- SAGE.md                   # Shared identity and operating instructions
```

## License

MIT
