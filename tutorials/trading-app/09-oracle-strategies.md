# Chapter 09 — Screen strategies with SIEVE before backtesting

*20 minutes. No funds required.*

Chapter 04 walked you through building a strategy and Chapter 05
paper-traded it. But building is cheap and backtesting isn't. A single
backtest can take a minute or more on a multi-month window, and it uses
a unit of your monthly backtest allowance. If you have many candidate
strategies, you want to know which are worth testing first.

This chapter introduces **SIEVE**, a Mangrove model trained on millions
of historical Strategy Engine runs. It scores candidate strategies in
milliseconds and answers one question: *will this strategy place
trades at all?*

**SIEVE does not predict performance.** Whether a strategy trades *well*
is what the backtest is for.

## Where these tools live

Every tool in this chapter is a **MangroveAI** tool. The agent keeps no
copy of them: it forwards each call to MangroveAI over MCP, paying per
call in x402 wallet mode or using your API key. Run `list_tools` to see
the live catalog.

| Tool | What it does |
|---|---|
| `screen_candidates` | Ranks up to 99 candidate strategies for one asset by how likely each is to trade. |
| `find_sweep_runs` | Searches strategies the Strategy Engine has already measured, filtered by asset, candle size, regime, trades, Sharpe, IRR and more. |
| `save_strategy`, `verify_strategy` | Save a candidate as a draft and check it. |
| `run_backtest` + `get_backtest` | Backtest a saved strategy, then read the result once it finishes. |

## Screen a candidate set

The value comes from scoring **many** at once. Say you're trying
variations of a MACD-based BTC strategy:

> "Make 30 variations of my MACD strategy with the entry window from 8
> to 20 and the exit window from 20 to 40, then screen them all through
> SIEVE in one call."

The bot calls `screen_candidates` with the asset and all candidates
(up to 99 per call) and gives you the ranking. Use the ranking to pick
which candidates to backtest first.

Two cautions, both from the tool itself:

- **A high score is real support for spending a backtest.**
- **A low score is weak evidence.** It does not mean the strategy will
  never trade. Never drop a strategy you care about because SIEVE
  scored it low; backtest it and find out.

## Look at what has already been measured

Before you backtest, look at what already worked. The Strategy Engine
holds the results of completed sweeps, and you can search them:

> "Show me BTC 1h strategies with annualized IRR above 35% that traded
> at least 20 times."

The bot calls `find_sweep_runs` with `asset=BTC`, `candle_size=1h`,
`min_irr=35` and `min_trades=20`. Every numeric filter has a minimum and
a maximum, so a band like "Sharpe between 1 and 2" is one call.

## Backtest the best candidates

> "Save the top three candidates and backtest them over the last 90 days."

The bot saves each one as a draft (`save_strategy`), checks it
(`verify_strategy`) and submits a backtest (`run_backtest`). The run
happens on MangroveAI; ask "is the backtest done?" a minute or two later
and the bot reads the result with `get_backtest`. Each read is a paid
call in x402 mode, so the bot checks when you ask, not in a tight loop.

Rank the candidates by their backtest results, not by their SIEVE score.

## Putting it together

1. **Describe** the candidate strategies you want to try.
2. **`screen_candidates`** to rank them by how likely they are to trade.
3. **`find_sweep_runs`** to see what similar strategies already achieved.
4. **Backtest** the best few and compare the results.

## Going further

- Ask for a strategy that fits today's market and the bot uses
  `build_strategy_for_regime`, which searches the Strategy Engine's
  measured runs for you.
- Creating and launching new sweeps is not offered over MCP today; use
  `find_sweep_runs` to search the results of existing sweeps.
- The KB guides [SIEVE end-to-end](https://mangrove.io/docs/guides/sieve-end-to-end-workflow)
  and [Experiments](https://mangrove.io/docs/api-reference/experiments)
  cover the same ground from the SDK side.
