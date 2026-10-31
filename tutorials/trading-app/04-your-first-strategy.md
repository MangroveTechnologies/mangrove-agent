# Chapter 04 - Your first strategy

*25 minutes. No live capital required. The first "do things" chapter.*

Goal: end this chapter with one strategy saved as a draft, a backtest you
understand, and a decision about whether it is worth taking further.

## Where strategies come from

The agent does not build strategies itself. Strategy building and backtesting
belong to MangroveAI: the client calls `load_skill` to fetch MangroveAI's own
instructions (`strategy-composition` before a build, `backtesting` before a
backtest) and then calls MangroveAI's tools through the agent. The agent keeps
no local strategy templates and no copy of those instructions, so the steps
below are the same ones MangroveAI's own assistant follows, and the checks run
on the server. The local `POST /strategies/autonomous` and `/manual` routes
return HTTP 410 because local strategy creation is retired.

The exact behavior of these tools comes from MangroveAI and can change without
a change to this repository. If the output you see differs from the examples
here, follow what the skill and tool results say.

A strategy has:

1. **A market** - the coin, the currency it is priced in, and the exchange the
   prices come from, for example BTC/USDC on Binance.
2. **A candle size** - how often its rules are checked: `5m`, `15m`, `30m`,
   `1h`, `4h` or `1D`. `1h` is a good first choice.
3. **Entry rules** - signals that open a trade, chosen with the help of the
   current market regime.
4. **Exit rules** - optional exit signals on top of the stop-loss and profit
   target that every strategy has.
5. **Execution settings** - position sizing, stops and limits. Platform
   defaults apply unless you choose otherwise.

## 1. Ask for a strategy

In your AI client session from Chapter 03, say:

> "Build me a BTC 1h strategy."

The client calls `build_strategy_for_regime`. MangroveAI reads BTC's current
market regime and searches the Strategy Engine's completed runs for one that
fits it and clears fixed quality floors. If one fits, it is saved and
backtested in a single step.

Often nothing clears those floors. Then nothing is saved or spent, and the
client asks which market a fresh strategy should trade. An illustrative
exchange:

```
Nothing in the Strategy Engine cleared the floors for BTC 1h in the current
regime. Nothing was saved or spent. A fresh build trades on one market. Which
one?

1. BTC/USDC on Binance
2. BTC/USD on Kraken
3. BTC/USDT on Binance
```

Pick one, for example "the first one". The markets offered come from
MangroveAI (`list_backtest_markets`).

## 2. The client composes and saves a draft

For a fresh build the client:

1. Loads the `strategy-composition` skill with `load_skill`.
2. Picks signals that suit the regime (`match_signals`). MangroveAI refuses a
   strategy whose entry does not fit the regime.
3. Measures how often they would fire (`query_signal_behavior`), so the
   strategy trades often enough to mean something but not on every bar.
4. Saves it (`save_strategy`) as a draft and checks it (`verify_strategy`).

It then shows you the strategy: name, market, entry and exit rules, and that it
is a verified draft. Nothing has been measured yet. Note the strategy ID that
MangroveAI assigns; Chapter 05 needs it.

## 3. Backtest it

> "Backtest it."

The client calls `run_backtest` for a recent window such as the last 90 days.
The backtest runs on MangroveAI in the background: the call returns straight
away with a backtest ID, and the result is ready a little later, from seconds
to a few minutes.

> "Is the backtest done?"

The client reads it with `get_backtest` and fetches the buy-and-hold return
over the same window with `get_benchmark`. A return on its own is not an
answer; the question is whether it beat simply holding the coin. Illustrative
shape of a result:

```
Backtest completed.

               Strategy      Hold BTC
Total return   -2.25%        +22.37%
Sharpe         -0.56
Max drawdown   5.59%
Trades         26 (38.5% won)
```

To see what it actually traded:

> "Show me the trades from that backtest."

`get_backtest_trades` lists each simulated trade with entry, exit, profit or
loss and why it closed. These are backtest trades, not real ones.

Each `get_backtest` read is a paid call in x402 mode, so ask once the run has
had time to finish rather than checking repeatedly.

## 4. Read the result honestly

- **Compare with holding.** A strategy that made 5% while holding made 20% did
  worse than doing nothing.
- **Enough trades?** A handful of trades says little either way. If there were
  very few, ask for a longer window.
- **Why did it lose?** The trades usually show it: stops hit in a strong trend,
  costs eating small wins, or entries fighting the market's direction.
- **A backtest belongs to the exact rules and market that produced it.** To try
  a variation, save a new strategy and backtest that. Chapter 05 relies on this:
  paper mode requires a passing backtest of the strategy's exact current
  configuration.

## 5. What you have now

> "Show me my strategies."

`list_strategies` lists your strategies on MangroveAI, including the new draft
and its status. A draft is saved but not running anywhere.

## Common questions

### "What about stop-losses?"

Every strategy has a stop-loss and a profit target. Ask for different values
when you build, for example "use a 3:1 reward", and the client passes them to
`save_strategy`. `get_execution_config_schema` shows the execution settings a
strategy accepts.

### "How often does it actually trade?"

The client measures each signal's firing rate before saving and tells you the
expected number of chances. The backtest's trade count over its window is the
real number.

### "Does it cost anything?"

With an API key, backtests count against your allowance. In wallet (x402) mode
each paid call is priced by MangroveAI, the price appears in the tool's
description, and the agent's spend budget caps the total (`x402_spend_status`
shows it; the default cap is $25).

## What to take away

- Strategies are built and backtested on MangroveAI; the agent is the client.
- Every new strategy starts as a draft, checked against the current market
  regime.
- A backtest is a measurement of the past, always read next to buy-and-hold.

[Chapter 05 - Paper mode](05-paper-mode.md)
