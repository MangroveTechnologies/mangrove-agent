# Getting Started

A clear, no-jargon walkthrough from "I just cloned this" to "I have a
ranked, backtested strategy paper-trading." **No wallet and no real money
are needed for any of this** — paper trading simulates fills at live
market prices.

You don't run commands or write code. You **talk to the bot** (Sage) in
plain English, and it drives the Mangrove tools for you. The examples
below are things you literally type into the chat.

---

## The 60-second mental model

A trading strategy is a set of rules: *when to buy* (entry signals) and
*when to sell* (exit signals). The hard part isn't writing one rule —
it's finding the rule (and the parameter values) that actually works.

Mangrove gives you three tools for that, cheapest first:

| Tool | What it does | When |
|---|---|---|
| **SIEVE** | Scores up to 99 strategy ideas in milliseconds and tells you which are worth testing. | You have *many* ideas and want to skip the duds. |
| **Sweep** | Runs up to 99 backtests in one managed experiment and ranks them. | You want the *best* config out of many. |
| **Backtest** | Carefully simulates *one* strategy over real history. | You want a trustworthy verdict on a single config. |

**Why this order matters:** a real backtest takes 30–120 seconds, and
about **5 of every 6 strategies fail it**. SIEVE finds the 1 worth
testing — so you score 99 ideas for the cost of one, sweep the handful
that survive, and only spend a careful backtest on the finalists.

---

## Two ways to start

### Path A — "Build me one strategy"

Best when you have a specific idea.

> **You:** "Build me a momentum strategy for ETH on the 1-hour chart."

Sage searches the reference library and the knowledge base, builds a
strategy with evidence-backed parameters, and backtests it. You get a
PASS/FAIL verdict against six performance thresholds plus a "did it beat
buy-and-hold?" line. (This is the `/create-strategy` → `/backtest` flow.)

### Path B — "Find me the best one" (this is the powerful one)

Best when you don't know the right parameters yet — which is usually.

> **You:** "Find me the best MACD strategy for BTC on 1h. Try a bunch of
> window settings."

Here's what Sage does for you, automatically:

1. **Generates the variations** — e.g. 50 combinations of MACD fast/slow
   windows.
2. **Runs them through SIEVE** (`/sieve`) — one cheap call scores all 50,
   drops the ones predicted to never trade, and ranks the rest by their
   probability of winning.
3. **Sweeps the survivors** (`/sweep`) — fans the top handful into a
   managed Oracle experiment (`create → validate → launch → results`),
   backtests them all, and ranks by risk-adjusted return (Sortino).
4. **Confirms the winner** — runs the top result through a full backtest
   for a trustworthy verdict.
5. **Shows you the leaderboard** — the top configs with their metrics and
   how they compare to just holding the asset.

You end up with a ranked winner you can promote to paper, having tested
dozens of ideas for a fraction of the compute.

---

## Then: paper-trade it (still no wallet)

> **You:** "Promote the winner to paper."

Paper mode schedules the strategy to evaluate on its timeframe and
simulates trades at live market prices. Nothing touches a blockchain and
no funds are at risk. Watch it with:

> **You:** "Show me the paper evaluations and trades so far."

## Later: go live (this one needs a wallet)

Live trading executes real swaps and is gated for your safety — it needs
a funded wallet with a confirmed backup, an allocation block, and an
explicit confirmation. Sage walks you through connecting a wallet
**only when you ask to go live**, never before. Your private key never
enters the chat (a safety hook blocks it); you back it up in your own
terminal. See [`wallet-presentation.md`](../.claude/rules/wallet-presentation.md).

---

## What to say — a cheat sheet

| You want to… | Say something like… | Skill |
|---|---|---|
| See what the bot can do | "Give me a tour." | — |
| Build one specific strategy | "Build a mean-reversion strategy for SOL 15m." | `/create-strategy` |
| Screen many ideas cheaply | "Score these 40 variations and tell me which are worth testing." | `/sieve` |
| Search for the best config | "Sweep the RSI window from 7 to 21 on BTC 1h and rank them." | `/sweep` |
| Verdict on one strategy | "Backtest that one over the last year." | `/backtest` |
| Start paper trading | "Promote it to paper." | — |
| Check on a strategy | "How are my paper strategies doing?" | — |

You don't need to name the skills — Sage picks the right one from what
you ask. They're listed so you know what's happening under the hood.

---

## Go deeper

- **SIEVE, in depth** — tutorial [chapter 09](../tutorials/trading-app/09-oracle-strategies.md),
  and the KB guides [Filtering with SIEVE](https://docs.mangrovedeveloper.ai/guides/using-sieve-prefilter)
  and [SIEVE end-to-end](https://docs.mangrovedeveloper.ai/guides/sieve-end-to-end-workflow).
- **Sweeps / experiments** — KB [Experiments reference](https://docs.mangrovedeveloper.ai/api-reference/experiments).
- **The full trading workflow** the bot follows — [`trading-bot-workflow.md`](../.claude/rules/trading-bot-workflow.md).
- **The SDK** behind it all (if you want to script directly) — the
  `mangroveai` Python package, `client.oracle.*`.
