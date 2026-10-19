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

Sage first checks what MangroveAI's Strategy Engine has already measured
for the current market regime; if nothing fits, it composes a fresh
strategy from regime-appropriate signals, saves it as a draft and
backtests it. You get the metrics next to buy-and-hold over the same
window. The build and the backtest both run on MangroveAI
(`build_strategy_for_regime`, or `save_strategy` then `run_backtest` /
`get_backtest`, over MCP).

### Path B — "Find me the best one" (this is the powerful one)

Best when you don't know the right parameters yet — which is usually.

> **You:** "Find me the best MACD strategy for BTC on 1h. Try a bunch of
> window settings."

Here's what Sage does for you, using MangroveAI's tools:

1. **Generates the variations** — e.g. 30 combinations of MACD fast/slow
   windows.
2. **Screens them with SIEVE** (`screen_candidates`) — one call ranks all
   of them by how likely each is to place trades at all. It says nothing
   about profit; it only tells Sage which to backtest first.
3. **Checks what's already been measured** (`find_sweep_runs`) — searches
   the Strategy Engine's completed sweeps for similar strategies and how
   they did.
4. **Backtests the best few** (`save_strategy`, `run_backtest`, then
   `get_backtest` when they finish) — backtests run on MangroveAI.
5. **Shows you the comparison** — the candidates' metrics and how they
   compare to just holding the asset.

You end up with a ranked winner you can promote to paper, having screened
dozens of ideas and backtested only the promising ones.

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

| You want to… | Say something like… | What Sage uses |
|---|---|---|
| See what the bot can do | "Give me a tour." | — |
| Build a strategy for today's market | "Build me a BTC 1h strategy." | `build_strategy_for_regime` (MangroveAI) |
| Build one specific strategy | "Build a mean-reversion strategy for SOL 15m." | `load_skill` + `save_strategy` (MangroveAI) |
| Screen many ideas cheaply | "Score these 40 variations and tell me which are worth testing." | `screen_candidates` (MangroveAI) |
| See what has already worked | "Show me BTC 1h strategies with Sharpe above 1 that traded at least 20 times." | `find_sweep_runs` (MangroveAI) |
| Verdict on one strategy | "Backtest that one over the last 90 days." | `run_backtest` + `get_backtest` (MangroveAI) |
| Start paper trading | "Promote it to paper." | — |
| Check on a strategy | "How are my paper strategies doing?" | — |

You don't need to name the tools — Sage picks the right one from what
you ask. They're listed so you know what's happening under the hood.

---

## Go deeper

- **SIEVE, in depth** — tutorial [chapter 09](../tutorials/trading-app/09-oracle-strategies.md),
  and the KB guides [Filtering with SIEVE](https://mangrove.io/docs/guides/using-sieve-prefilter)
  and [SIEVE end-to-end](https://mangrove.io/docs/guides/sieve-end-to-end-workflow).
- **Sweeps / experiments** — KB [Experiments reference](https://mangrove.io/docs/api-reference/experiments).
- **The full trading workflow** the bot follows — [`trading-bot-workflow.md`](../.claude/rules/trading-bot-workflow.md).
- **Scripting directly** — call MangroveAI's tools yourself over its MCP
  endpoint, or use the `mangroveai` Python package.
