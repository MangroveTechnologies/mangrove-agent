<div align="center">
  <a href="https://github.com/MangroveTechnologies/mangrove-agent">
    <img src="assets/icon.png" alt="Mangrove" width="120" height="112">
  </a>

  <h1>mangrove-agent</h1>

  <p>
    <strong>An AI trading bot built on the Mangrove API.</strong><br>
    FastAPI + MCP. Autonomous strategy generation, cron-driven execution, full audit trail.
  </p>

  <p>
    <a href="https://github.com/MangroveTechnologies/mangrove-agent/actions/workflows/ci.yml">
      <img src="https://github.com/MangroveTechnologies/mangrove-agent/actions/workflows/ci.yml/badge.svg" alt="CI">
    </a>
    <a href="https://github.com/MangroveTechnologies/mangrove-agent/blob/main/LICENSE">
      <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
    </a>
  </p>
</div>

---

## What this is

A local AI trading bot that:
- Turns natural-language goals ("momentum on ETH") into backtested, ranked trading strategies via the [MangroveAI API](https://mangrovedeveloper.ai).
- Runs live strategies on APScheduler cron jobs. Same evaluator path for paper and live.
- Executes live swaps through [MangroveMarkets](https://github.com/MangroveTechnologies/MangroveMarkets). Client-side signing; SDK never touches your keys.
- Logs every evaluation and trade to local SQLite for a full audit trail.

**Real mainnet swap from the agent** (verified April 2026): [0x5c126e...c5565](https://basescan.org/tx/0x5c126e6be26fc736bcb3f11a8f4c699aeee754f6c0bf7e5b7aa2df6a859c5565)

---

## Workshop attendee? Start here.

The full guided walkthrough lives in **[`tutorials/trading-app/`](tutorials/trading-app/00-index.md)** — 8 chapters that take you from "I just cloned this" to "I have a paper strategy running" to (optionally) "I made a live swap."

Chapters 01–05 are **fund-free**. Chapters 06–08 are optional and need a small amount of USDC. Budget ~2 hours for the whole path.

Not doing the workshop? Keep reading — the rest of this README is a reference.

---

## Prerequisites

| Tool | Install | Why |
|---|---|---|
| **VSCode** | https://code.visualstudio.com/download | Universal editor + integrated terminal that works the same on macOS / Linux / Windows. Every instruction below assumes you open the repo in VSCode and use its built-in terminal (``Ctrl/Cmd+` ``). |
| **Python 3.11+** | https://www.python.org/downloads/ | The agent is a Python FastAPI process. 3.11 is the minimum. |
| **Git for Windows** (Windows only) | https://git-scm.com/download/win | Gives you Git Bash, so the `*.sh` scripts in this repo work identically to macOS / Linux. Set VSCode's default terminal to `Git Bash` via the command palette. |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` | The chat UX. Optional if you only want the REST API. |
| **MangroveAI API key** | Free at https://mangrovedeveloper.ai | `dev_...` or `prod_...`. The setup script will prompt for it. |

Docker is **optional** — see the alternate install path below. Bare-metal is the primary path because the `keyring` library can reach your OS keychain directly when the agent runs natively.

---

## Quick start — bare-metal (recommended)

One command. It seeds your config (prompts for the API key), creates a venv, pip-installs dependencies, starts uvicorn in the background, registers the MCP server with Claude Code, and verifies `/health`.

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
cd mangrove-agent
./scripts/setup.sh
```

First run takes ~60s (pip install + health wait). Re-runs are idempotent — it detects what's already done and skips.

When it's finished:
- Agent runs at `http://localhost:9080` (pid in `agent-data/bare.pid`, logs in `agent-data/bare.log`). We bind 9080 externally because `:8080` is commonly squatted by VSCode Helper and other dev tools.
- `./scripts/verify_quickstart.sh --bare` passed → the tool catalog returned the expected set.
- Claude Code's MCP registration now knows about `mangrove-agent`.

**Start Claude Code in the repo directory** and the agent runs a short platform tour (status / tools / market data / knowledge base / reference strategies) to prove everything is wired, then offers to help you build a strategy. You can paper-trade without a wallet at all — wallet setup lives in Chapter 06 of the tutorial, right before live trading. See *Your first trade* below.

### Useful `./scripts/setup.sh` flags

```
./scripts/setup.sh --yes --api-key dev_xxx         # fully non-interactive (CI / scripts)
./scripts/setup.sh --foreground                    # run uvicorn in your terminal (Ctrl+C to stop)
./scripts/setup.sh --no-mcp                        # skip Claude Code registration
./scripts/setup.sh --no-verify                     # skip the post-start verify pass
./scripts/setup.sh --docker                        # use Docker instead of bare-metal
```

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

**Stage 0 — platform tour.** Start Claude Code in the repo directory. The agent runs status, list_tools, get_market_data, kb_search, and search_reference_strategies, and offers to help you build a strategy. No wallet needed yet.

**Paper trading, no wallet.**

> "Build me a momentum strategy for ETH on 1h. Use a reference."

Agent searches curated reference strategies, picks a candidate, builds it, backtests with a timeframe-appropriate lookback, reports PASS/MARGINAL/FAIL against 6 thresholds.

> "Promote it to paper."

Schedules the strategy on a cron at the strategy's timeframe. Evaluations fire, paper fills get logged. No funds at risk.

**Going live (optional, needs funds).**

Wallet setup and live trading are in Chapters 06 and 07 of the tutorial. The summary:

- Create or import a wallet via `create_wallet` / `stash-secret.sh` + `import_wallet`.
- Save the secret via `./scripts/reveal-secret.sh <vault_token>` in your terminal (never in the chat).
- Run `./scripts/confirm-backup.sh <address>` to flip the backup gate.
- Fund with 1–5 USDC on Base.
- Promote the strategy to live with an allocation block (`confirm=true`, `slippage_pct ≤ 0.0025`).
- Live evaluations fire on the same cron; when the strategy decides to trade, the `mangrovemarkets` SDK routes through 1inch, the agent signs locally, broadcasts, and logs the tx.

## Safety model at a glance

- **Your private keys never touch this chat.** `create_wallet` returns a `vault_token`, not the plaintext. Revealing is a separate CLI (`reveal-secret.sh`) that prints to your terminal only.
- **Harness hooks block key pastes.** If you try to paste a key into Claude Code, `.claude/hooks/block-wallet-secrets.sh` refuses the prompt with an educational message. The hook is in `.claude/settings.json` — disabling it requires a commit.
- **Live trading is gated on explicit backup confirmation.** After you save a wallet's secret off-agent, `./scripts/confirm-backup.sh <addr>` flips a flag. `execute_swap` and `update_strategy_status → live` refuse on wallets without it.
- **Master key stays local.** Bare-metal: OS keychain. Docker: `./agent-data/master.key` (chmod 600, gitignored).

---

## What the agent can do

41 MCP tools (plus the `hello_mangrove` x402 demo). Rough grouping:

| Category | Tools |
|---|---|
| Discovery (free) | `status`, `list_tools`, `hello_mangrove` |
| Wallet | `create_wallet`, `import_wallet`, `list_wallets`, `get_balances`, `portfolio_value`, `portfolio_pnl` |
| DEX | `list_dex_venues`, `get_swap_quote`, `execute_swap`, `get_tx_status`, `get_token_info`, `get_spot_price`, `get_gas_price`, `get_oneinch_chart` |
| Market / on-chain | `get_ohlcv`, `get_market_data`, `get_crypto_assets`, `get_trending_coins`, `search_crypto_assets`, `get_smart_money_sentiment` |
| Signals | `list_signals`, `kb_list_indicators` |
| Strategy | `create_strategy_autonomous`, `create_strategy_manual`, `list_strategies`, `get_strategy`, `update_strategy_status`, `delete_strategy`, `backtest_strategy`, `evaluate_strategy`, `search_reference_strategies`, `build_strategy_from_reference` |
| Execution | `list_positions`, `get_position`, `list_trade_history` |
| Logs | `list_evaluations`, `list_trades`, `list_all_trades` |
| Knowledge Base | `kb_search`, `list_docs`, `get_doc` |
| DeFi | `get_protocol_tvl` |

Every tool has a mirrored REST endpoint at `/api/v1/agent/*`. Both call the same service layer — pick whichever fits your caller.

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
       mangroveai SDK         mangrovemarkets SDK
       (strategies, backtest, (DEX swap, portfolio,
        signals, market,       wallet)
        KB, on-chain)
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

Extending the agent — adding a new endpoint, MCP tool, signal, or strategy — follows the service-layer pattern documented in [`docs/contributing.md`](docs/contributing.md). Trading-bot-specific skills available in this repo: `/create-strategy`, `/backtest`, `/custom-signal`, `/audit-security`, `/check-alignment`, `/tool-spec`. See `.claude/skills/` for what each one does.

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

Expect: 239 passed, 2 skipped (opt-in live-swap tests).

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
└── CLAUDE.md                 # Project context
```

## License

MIT
