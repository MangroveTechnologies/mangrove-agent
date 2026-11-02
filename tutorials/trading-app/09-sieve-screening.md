# Chapter 09 - Screen strategies with SIEVE before backtesting

*20 minutes. No funds required. An optional side trip you can read any time
after Chapter 04.*

Chapter 04 walked you through composing one strategy and backtesting it.
Composing is cheap and backtesting is not: a backtest can take a minute or more
on a multi-month window, and each one uses part of your allowance or an x402
payment. If you have many candidate strategies, you want to know which are worth
testing first.

This chapter introduces **SIEVE**, a Mangrove model that scores candidate
strategies quickly and answers one question: *will this strategy place trades at
all?*

**SIEVE does not predict performance.** Whether a strategy trades *well* is
what the backtest is for.

## Where these tools live

Every tool in this chapter is a **MangroveAI** tool. The agent keeps no copy of
them: it forwards each call to MangroveAI over MCP, paying per call in x402
wallet mode or using your API key. There is no local screening or sweep service
in the agent. Run `list_tools` or `discover_tools` to see the live catalog and
each tool's current price.

| Tool | What it does |
|---|---|
| `screen_candidates` | Ranks a set of candidate strategies for one asset by how likely each is to trade. |
| `find_sweep_runs` | Searches strategies the Strategy Engine has already measured, filtered by asset, candle size, regime, trade count, Sharpe, return and more. |
| `save_strategy`, `verify_strategy` | Save a candidate as a draft and check it. |
| `run_backtest` and `get_backtest` | Backtest a saved strategy, then read the result once it finishes. |
| `size_sweep`, `create_sweep`, `launch_sweep`, `list_sweeps`, `get_sweep`, `pause_sweep` | Plan, launch and manage a new sweep, a managed experiment that runs many backtests. |

## Screen a candidate set

The value comes from scoring **many** candidates at once. Say you are trying
variations of a MACD-based BTC strategy:

> "Make 30 variations of my MACD strategy with the entry window from 8 to 20
> and the exit window from 20 to 40, then screen them all through SIEVE in one
> call."

The client calls `screen_candidates` with the asset and all the candidates, up
to 99 per call, and gives you the ranking. Use the ranking to decide which
candidates to backtest first.

Two cautions:

- **A high score is real support for spending a backtest.**
- **A low score is weak evidence.** It does not mean the strategy will never
  trade. Do not drop a strategy you care about because SIEVE scored it low;
  backtest it and find out.

## Look at what has already been measured

Before you backtest, look at what already worked. The Strategy Engine holds the
results of completed sweeps, and you can search them:

> "Show me BTC 1h strategies with annualized IRR above 35% that traded at least
> 20 times."

The client calls `find_sweep_runs` with `asset=BTC`, `candle_size=1h`,
`min_irr=35` and `min_trades=20`. Numeric filters come in minimum and maximum
pairs, so a band such as "Sharpe between 1 and 2" is a single call. Results
describe strategies the engine measured, not strategies saved in your account.

## Backtest the best candidates

> "Save the top three candidates and backtest them over the last 90 days."

The client saves each as a draft (`save_strategy`), checks it
(`verify_strategy`) and submits a backtest (`run_backtest`). The run happens on
MangroveAI. Ask "is the backtest done?" a minute or two later and the client
reads the result with `get_backtest`. Each read is a paid call in x402 mode, so
check when you ask rather than in a tight loop.

Rank the candidates by their backtest results, not by their SIEVE score. A
strategy that backtests well and passes is also what Chapter 05 needs before it
can be scheduled for paper evaluation.

## Putting it together

1. **Describe** the candidate strategies you want to try.
2. **`screen_candidates`** to rank them by how likely they are to trade.
3. **`find_sweep_runs`** to see what similar strategies already achieved.
4. **Backtest** the best few and compare the results.
5. **Paper-trade** the finalist (Chapter 05).

## Going further

- Ask for a strategy that fits today's market and the client uses
  `build_strategy_for_regime`, which searches the Strategy Engine's measured
  runs for you (Chapter 04).
- To run a **new sweep** rather than search old ones, the client loads
  MangroveAI's `sweeps` skill with `load_skill` and follows it. To find or
  inspect existing sweep results, it loads `sweep-results`. The client should
  show you the sweep's estimated run count and get your agreement before it
  launches compute, and it must not turn an uncertain launch into a second paid
  launch. Do not ask it to compute sweep sizes or build candidates by itself;
  those belong to MangroveAI's tools.
- The Mangrove knowledge base guides
  [SIEVE end-to-end](https://mangrove.io/docs/guides/sieve-end-to-end-workflow)
  and [Experiments](https://mangrove.io/docs/api-reference/experiments) cover
  the same ground from the SDK side.
