# Your First Trading Bot

This is a self-paced tutorial for running your own trading agent on the
Mangrove API. By the end you have a Mangrove-powered agent running on your own
machine that composes a strategy, backtests it, runs it in paper mode on a
schedule, and, if you choose, makes a real swap on Base from a wallet you
control.

The path is built so you can stop at any chapter and still have something
useful. Chapters 01-05 involve no live trading capital, although API calls can
still use your API-key allowance or require x402 wallet payments, including
backtests and paper evaluations. Chapters 06 and 07 add a wallet, funding and a
live swap, and are entirely optional.

## What you end up with

```
An agent that:
  - Runs in your chosen local or persistent hosted environment
  - Holds its own wallet keys, encrypted with a local master key
  - Composes strategies with MangroveAI's strategy-composition skill
  - Backtests them on MangroveAI against historical data
  - Evaluates a backtested strategy on a schedule in paper mode
  - Executes a real DEX swap from your wallet when you ask for one
  - Keeps a local record of wallets, swaps and payments
```

You drive it through Claude Code, Codex, Grok Build or Perplexity Computer.
Start with the [user guide](../../docs/user-guide.md) for installation and
naming. Your client talks to a FastAPI server, the mangrove-agent, over MCP,
either through a native connection or the provided command client. The agent
forwards strategy, signal, backtest and market-data calls to Mangrove's hosted
MangroveAI service and keeps wallets, signing and the local database on your
side.

## Chapters

| # | Title | Funds needed? | Reading time |
|---|---|---|---|
| 01 | [AI clients + safety](01-claude-code-and-ai-safety.md) | no | 15 min |
| 02 | [What you have](02-overview.md) | no | 10 min |
| 03 | [Setup](03-setup.md) | no | 10 min |
| 04 | [Your first strategy](04-your-first-strategy.md) | API access; no live capital | 25 min |
| 05 | [Paper mode](05-paper-mode.md) | API access; no live capital | 15 min |
| 06 | [Wallet setup](06-wallet-setup.md) | yes (about $5 USDC) | 15 min |
| 07 | [Going live](07-going-live.md) | yes | 20 min |
| 08 | [Monitor, troubleshoot, extend](08-monitor-troubleshoot-extend.md) | no | 20 min |
| 09 | [Screen strategies with SIEVE before backtesting](09-sieve-screening.md) | API access; no live capital | 20 min |

Take it in whatever chunks work for you. Chapters 01-05 fit comfortably in an
afternoon. Chapter 09 is an optional side trip you can read any time after
Chapter 04.

## Before you start

Use a command-capable AI client, or install Git, Python 3.11+ and a
bash-compatible shell for manual setup. Claude, an editor and a MangroveAI API
key are optional. The [user guide](../../docs/user-guide.md) explains the
choices; [`SETUP.md`](../../SETUP.md) has manual prerequisite instructions.

## A note on safety

The agent can move real funds when you tell it to. That means:

- It can lose money. Markets are unforgiving; a backtest is a hypothesis, not
  a guarantee.
- It is not financial advice. Nothing in this tutorial, the agent's responses,
  or Mangrove's outputs is a recommendation to trade any specific asset.
- You are responsible for your own keys. The agent holds your wallet's private
  key encrypted on the machine it runs on. If that machine is compromised, your
  funds are at risk, as with any self-custody wallet.
- Start small. A first live swap should be roughly $1-5. Scale up only after
  you have watched the agent execute and verified that it does what you expect.

Chapter 01 describes the safety checks built into the agent and the extra
checks that exist only in Claude Code. None of them replaces your own judgment.
