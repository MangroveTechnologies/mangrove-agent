# Chapter 04 — Your first strategy

*25 minutes. No funds required. The first "do things" chapter.*

Goal: end this chapter with one strategy saved as a draft, a backtest you understand,
and a decision about whether it is worth taking further.

## Where strategies come from

The agent does not build strategies itself. Strategy building and backtesting are
MangroveAI's: the agent loads MangroveAI's own instructions (`load_skill`) and calls
MangroveAI's tools over MCP. So the steps below are the same ones MangroveAI's own
assistant follows, and every check runs on the server.

A strategy has:

1. **A market** — the coin, the currency it is priced in, and the exchange the prices
   come from, e.g. BTC/USDC on Binance.
2. **A candle size** — how often its rules are checked: `5m`, `15m`, `30m`, `1h`,
   `4h` or `1d`. `1h` is a good first choice.
3. **Entry rules** — exactly one *trigger* (the event that opens a trade) plus optional
   *filters* (conditions that must also hold).
4. **Exit rules** — optional exit signals, on top of the stop-loss and profit target
   every strategy has.
5. **Execution settings** — position sizing, stops and limits. Platform defaults apply
   unless you choose otherwise.

## 1. Ask for a strategy

In your Claude Code session from Chapter 03, say:

> "Build me a BTC 1h strategy."

The agent first calls `build_strategy_for_regime`. MangroveAI reads BTC's current
market regime (for example *neutral over 180 days, low volatility*) and searches the
Strategy Engine's completed runs for one that fits it and clears fixed quality floors
(more than 10 trades, Sharpe of at least 1, annual return of at least 10%, beating
BTC and the S&P 500). If one fits, it is saved and backtested in a single step.

Often nothing clears those floors. Then nothing is saved or spent, and the agent asks
which market a fresh strategy should trade:

```
Nothing in the Strategy Engine cleared the floors for BTC 1h in the current regime.
Nothing was saved or spent. A fresh build trades on one market — which one?

1. BTC/USDC on Binance (suggested)
2. BTC/USD on Kraken
3. BTC/USDC on Kraken
4. BTC/USDT on Binance
```

Pick one, e.g. `"the first one"`.

## 2. The agent composes and saves a draft

For a fresh build the agent:

1. Picks signals that suit the regime (`match_signals`). In a neutral market only
   buy-the-dip or breakout entries are allowed; MangroveAI refuses a strategy whose
   entry does not fit the regime.
2. Measures how often they would fire (`query_signal_behavior`), so the strategy trades
   often enough to mean something but not on every bar.
3. Saves it (`save_strategy`) — always as a **draft** — and checks it
   (`verify_strategy`).

It then shows you the strategy: name, market, entry trigger and filter, exit, and that
it is a verified draft. Nothing has been measured yet.

## 3. Backtest it

> "Backtest it."

The agent calls `run_backtest` for the last 90 days. The backtest runs on MangroveAI
in the background: the call returns straight away with a backtest id, and the result
is ready a little later (seconds to a few minutes).

> "Is the backtest done?"

The agent reads it with `get_backtest`, and fetches the buy-and-hold return over the
same window with `get_benchmark` — a return on its own is not an answer; the question
is whether it beat simply holding the coin.

```
Backtest completed.

               Strategy      Hold BTC
Total return   −2.25%        +22.37%
Sharpe         −0.56
Max drawdown   5.59%
Trades         26 (38.5% won)
```

To see what it actually traded:

> "Show me the trades from that backtest."

`get_backtest_trades` lists each simulated trade — entry and exit, profit or loss, and
why it closed. These are backtest trades, not real ones.

## 4. Read the result honestly

- **Compare with holding.** A strategy that made 5% while holding made 20% did worse
  than doing nothing.
- **Enough trades?** A handful of trades says little either way. If there were very
  few, ask for a longer window.
- **Why did it lose?** The trades usually show it: stops hit in a strong trend,
  costs eating small wins, or entries fighting the market's direction.
- **Measured strategies are frozen.** Once a strategy has a backtest, its rules no
  longer change — the result stays attached to the rules that produced it. To try a
  variation, ask for a new strategy.
- **The same backtest is not run twice.** Asking for the same strategy over the same
  dates again is refused and points you to the existing result.

## 5. What you have now

> "Show me my strategies."

`list_strategies` lists your strategies on MangroveAI, including the new draft and
its status. A draft is saved but not running anywhere.

## Common questions

### "What about stop-losses?"

Every strategy has an ATR-based stop-loss and a profit target (2:1 by default). Ask
for different values when you build — `"use a 3:1 reward"` — and the agent passes them
to `save_strategy`.

### "How often does it actually trade?"

The agent measures each signal's firing rate before saving and tells you the expected
number of chances per week. The backtest's trade count over its window is the real
number.

### "Does it cost anything?"

With an API key, backtests count against your monthly allowance. In wallet (x402)
mode each paid call costs a few cents or less — a backtest is $0.10, reading it back
$0.001 — and the agent's spend budget caps the total.

## What to take away

- Strategies are built and backtested on MangroveAI; the agent is the client.
- Every new strategy starts as a draft, checked against the current market regime.
- A backtest is a measurement of the past, always read next to buy-and-hold.

→ [Chapter 05 — Paper mode](05-paper-mode.md)
