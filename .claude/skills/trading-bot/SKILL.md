---
name: trading-bot
description: >-
  The mangrove-agent operating rules: the author -> backtest -> paper -> live workflow, how to
  present and handle wallets without secrets ever entering chat, and which risk controls already
  run in the engine and the agent. Load it before any strategy, backtest, sweep, wallet, balance,
  swap, Kraken or live-trading work, and whenever the user asks what the bot can do or how it
  keeps funds safe.
---

# Operating the Mangrove trading agent

Read these three documents in full and follow them. They are the contract for every trading
action this agent takes.

1. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/trading-bot-workflow.md` — the staged workflow (tour, author,
   search with SIEVE + sweeps, backtest verdict, paper, wallet connection, live promotion, monitoring)
   and what never to do.
2. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/wallet-presentation.md` — creating, importing and presenting
   wallets; the out-of-band secret flow; testnet-first defaults; the 1inch-only signing guard.
3. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/risk-management.md` — engine-side per-strategy gates and the
   agent-side portfolio kill switch, so you explain blocked trades correctly and never rebuild a
   control that already exists.

## Reading them inside the plugin

Those documents were written for a git clone of the repo. When running as the installed plugin:

- Tool names are `mcp__plugin_mangrove-agent_mangrove-agent__<tool>`, not `mcp__mangrove-agent__<tool>`.
  Load them with one ToolSearch call (query `+mangrove-agent`, max_results 100).
- Scripts written as `./scripts/<name>.sh` are at `${CLAUDE_PLUGIN_ROOT}/scripts/<name>.sh`. The user
  runs them in their own terminal, never through you.
- `./agent-data/` and `server/src/config/local-config.json` live under `~/.mangrove-agent/`
  (`agent-data/` and `config/`), which survives plugin updates.
- There is no `./scripts/setup.sh` step: the plugin installs and starts the agent itself at session
  start. If the agent is down, check `~/.mangrove-agent/agent-data/bootstrap.log`.

In a git clone these rules already load automatically from `.claude/rules/`; the paths in them are
relative to the repo root.
