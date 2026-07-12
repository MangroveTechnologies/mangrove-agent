# Your First Trading Bot

Welcome. This is a self-paced tutorial for building your own trading
agent on the Mangrove API. By the end, you'll have a Mangrove-powered
trading bot running on your own laptop: authoring strategies, backtesting them,
paper-trading, and (if you choose) executing real swaps with real funds
on Base.

The whole path is designed so you can stop at any chapter and still
have something useful. Chapters 01–05 are **fund-free** — you never
have to put real money on the line. Chapters 06–08 add wallet setup,
funding, and live trading, and are entirely optional.

## What you'll end up with

```
A trading bot that:
  - Runs locally on your machine (not in the cloud)
  - Holds its own keys, encrypted with a local master key
  - Authors strategies using Mangrove's knowledge base of signals
  - Backtests them against historical data
  - Schedules them to evaluate on a cron (5m / 15m / 1h / 4h / 1d)
  - Simulates trades (paper mode) or executes real swaps (live mode)
  - Logs every evaluation + trade for audit
```

You drive it by chatting with Claude Code in your terminal. Under the
hood, Claude Code talks to a local FastAPI server (the "mangrove-agent")
via MCP. The agent talks to Mangrove's hosted API for signals,
strategy logic, and market data.

## Chapters

| # | Title | Funds needed? | Reading time |
|---|---|---|---|
| 01 | [Claude Code + AI safety](01-claude-code-and-ai-safety.md) | no | 15 min |
| 02 | [What you have](02-overview.md) | no | 10 min |
| 03 | [Setup](03-setup.md) | no | 10 min |
| 04 | [Your first strategy](04-your-first-strategy.md) | no | 25 min |
| 05 | [Paper mode](05-paper-mode.md) | no | 15 min |
| 06 | [Wallet setup](06-wallet-setup.md) | yes (~$5 USDC) | 15 min |
| 07 | [Going live](07-going-live.md) | yes | 20 min |
| 08 | [Monitor, troubleshoot, extend](08-monitor-troubleshoot-extend.md) | no | 20 min |
| 09 | [Score strategies with SIEVE before backtesting](09-oracle-strategies.md) | no | 20 min |

This is self-paced — take it in whatever chunks work for you.
Chapters 01–05 fit comfortably in an afternoon.

## Before you start

See [`SETUP.md`](../../SETUP.md) for the prerequisites: VSCode,
Python 3.11, Claude Code CLI, a Mangrove API key (free at
https://mangrovedeveloper.ai), and (if you're on Windows) Git Bash.
If you set up with SETUP.md, everything on that list is already done.

## A note on safety

This bot executes real trades against real DEXes when you tell it to.
That means:

- It can lose money. Markets are unforgiving; a backtest is a
  hypothesis, not a guarantee.
- It is not financial advice. Nothing in this tutorial, the bot's
  responses, or Mangrove's outputs constitutes a recommendation to
  trade any specific asset.
- You are responsible for your own keys. The bot holds your private
  key encrypted on your laptop. If your laptop is compromised, your
  funds are at risk — same as any self-custody wallet.
- Start small. First live allocation should be ~$1–5. You can scale
  up after you've watched the bot execute a few real trades and
  verified it's doing what you expect.

Chapter 01 walks through the three technical safety nets we built to
make accidental mistakes hard. None of them substitute for your own
judgment.

