---
name: trading-bot
description: >-
  Use when the user asks what the trading agent can do, how to use it, or how risk controls work. The mangrove-agent operating rules cover server workflow discovery and local execution, how to
  present and handle wallets without secrets ever entering chat, and which risk controls already
  run in the engine and the agent. Load it before any strategy, backtest, sweep, wallet, balance,
  swap, Kraken or live-trading work, and whenever the user asks what the bot can do or how it
  keeps funds safe. Trigger with phrases such as "what can this bot do", "how does the bot manage risk", or "help me use the trading agent".
version: 0.2.0
author: Mangrove Technologies
license: MIT
allowed-tools: Read, Bash(./scripts/*)
compatibility: Claude Code plugin; requires the local mangrove-agent repository and configured Mangrove services for live discovery.
tags: [mangrove, trading, mcp, risk-management]
---

# Operating the Mangrove trading agent

Follow these three documents. If their complete contents are already loaded by
the native rules loader or shared bundle, do not read them again. Otherwise read
the missing documents in full before trading. They are the contract for every
trading action this agent takes.

1. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/trading-bot-workflow.md` — how to load live server workflow instructions over MCP.
2. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/wallet-presentation.md` — creating, importing and presenting
   wallets; the out-of-band secret flow; testnet-first defaults; the 1inch-only signing guard.
3. `${CLAUDE_PLUGIN_ROOT}/.claude/rules/risk-management.md` — engine-side per-strategy gates and the
   agent-side portfolio kill switch, so you explain blocked trades correctly and never rebuild a
   control that already exists.

## Reading them inside the plugin

Those documents were written for a git clone of the repo. When running as the installed plugin:

- Tool names are `mcp__plugin_mangrove-agent_mangrove-agent__<tool>`, not `mcp__mangrove-agent__<tool>`.
  Search for the tools needed for this task; load their actual schemas without
  importing the entire catalog into context.
- Scripts written as `./scripts/<name>.sh` are at `${CLAUDE_PLUGIN_ROOT}/scripts/<name>.sh`. The user
  runs them in their own terminal, never through you.
- `./agent-data/` and `server/src/config/local-config.json` live under `~/.mangrove-agent/`
  (`agent-data/` and `config/`), which survives plugin updates.
- There is no `./scripts/setup.sh` step: the plugin installs and starts the agent itself at session
  start. If the agent is down, check `~/.mangrove-agent/agent-data/bootstrap.log`.

In a git clone these rules already load automatically from `.claude/rules/`; the paths in them are
relative to the repo root.
