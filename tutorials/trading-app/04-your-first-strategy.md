# Chapter 04 — Your first strategy

*25 minutes. No funds required. The first "do things" chapter.*

Goal: end this chapter with one strategy in your local database, with
a backtest you understand, and a decision about whether it's worth
promoting to paper.

## The mental frame

A "strategy" in this system has five parts:

1. **An asset** — the token you want to trade. This tutorial uses
   ETH.
2. **A timeframe** — the cadence at which the bot evaluates the
   strategy. Supported values: `5m`, `15m`, `30m`, `1h`, `4h`, `1d`.
   Recommended: `1h` (enough signal, not too much
   noise, and backtests complete quickly).
3. **Entry signals** — things that have to be true to open a
   position. Examples: RSI crossed up from oversold, SMA fast
   crossed above SMA slow, ADX shows a strong trend.
4. **Exit signals** — things that trigger closing. Examples: RSI
   crossed down from overbought, trailing stop hit, fixed target.
5. **Risk config** — position size as a fraction of allocation,
   stop-loss %, take-profit %, time-based exits, etc. Sensible
   defaults ship out of the box; you can override anything.

The bot can author a strategy in three modes. We'll use the one
that works best for beginners: **reference-first**.

## The three authoring modes

| Mode | What happens | When to use |
|---|---|---|
| **Reference-first** | Agent searches a curated library of strategies that matched your asset / timeframe / goal, shows you the top 2–3, you pick one. | **First strategy. Always.** |
| **Manual** | You dictate the exact signals and params. Agent must cite the KB for every parameter. | You've read the KB and know what you want. |
| **Autonomous** | Agent picks signals itself. Generates 7 candidates, backtests them all, ranks by performance. | You want a second opinion OR you couldn't find a good reference match. |

Reference-first is the right default because:

- Every reference strategy has been backtested before being added
  to the library. You're not starting from zero.
- The signal choices are ground-truth — the bot copies them
  verbatim, so what you're testing is "does this proven structure
  still work on current market conditions."
- It's fast. 30 seconds to pick a candidate vs. a few minutes for
  autonomous.

We'll use it for the first strategy. Later chapters cover the other
two.

## 1. Start the conversation

In your Claude Code session from Chapter 03, say:

> "Build me a momentum strategy for ETH on 1h. Use a reference."

The bot should:

1. Call `search_reference_strategies(asset="ETH", timeframe="1h",
   goal_hint="momentum")`.
2. Return up to 5 ranked candidates.
3. Show you 2–3 options with a brief description, signal names,
   and category.

Expected response shape (exact names may vary; the search ranks by
specificity — exact asset + timeframe + category first, then loosens):

```
Here are three reference strategies for ETH on 1h, ranked by match:

1. **ref-001 — ETH momentum — MACD bullish cross on 1h** (category: momentum)
   Entry: macd_bullish_cross
   Exit:  macd_bearish_cross
   Notes: Starter config. MACD params can be tuned tighter on 1h
          crypto (e.g. 8,21,5) for more trades.

2. **ref-009 — ETH mean-reversion tight — Stoch oversold bounce on 1h** (category: mean_reversion)
   Entry: rsi_cross_up, stoch_oversold
   Exit:  rsi_cross_down
   Notes: Dual oscillator confluence. High trade count, small edges —
          slippage/fees matter more here.

3. **ref-007 — ETH momentum — ROC positive + above SMA on 4h** (category: momentum)
   Entry: roc_momentum_shift, is_above_sma
   Exit:  roc_momentum_shift
   Notes: Matched on asset + momentum but different timeframe (4h vs 1h).
          Only surfaced because the exact 1h momentum cell is thin.

Which one do you want to build?
```

**A note on coverage.** The curated library is small (12 strategies
across ETH + BTC on 1h / 4h / 1d). For narrow queries the search
will loosen filters to surface related candidates — e.g., different
timeframe but same category, or same timeframe but different style.
Read the category and timeframe of each candidate before picking;
don't assume all three are exact matches for what you asked.

**Pick one.** Say: `"Let's try ref-001."`

## 2. Build + backtest

The bot will now:

1. Call `build_strategy_from_reference(reference_id="ref-001",
   timeframe="1h")` to get a `create_strategy_manual`-compatible
   payload (signals copied exactly, timeframe applied).
2. Call `create_strategy_manual(...)` to persist the strategy in
   your local DB. You'll get a UUID back.
3. Call `backtest_strategy(strategy_id="...", mode="full")` with no
   lookback fields. The backtest service auto-picks the right
   window for your timeframe (3 months for 1h, 6 months for 4h,
   12 months for 1d).

The backtest takes 5–15 seconds depending on the timeframe. When it
comes back, the bot presents the metrics with a verdict.

## 3. Read the backtest

The bot uses six thresholds from
`server/src/services/data/threshold_spec.json`:

| Metric | Threshold | What it means |
|---|---|---|
| **Sortino** | ≥ 1.5 | Risk-adjusted return, downside-only. Higher is better. |
| **Sharpe** | ≥ 1.2 | Risk-adjusted return, total volatility. Higher is better. |
| **Calmar** | ≥ 1.0 | Return vs. max drawdown. Higher is better. |
| **IRR** | ≥ 0.15 | Annualized return. 15% is the floor. |
| **Max drawdown** | ≤ 0.70 | Biggest peak-to-trough dip. Lower is better. |
| **Win rate** | ≥ 0.25 | Fraction of trades that were profitable. |

The verdict rule (simplified):

- **PASS** — all 6 thresholds met.
- **MARGINAL** — 4 or 5 of 6 met; agent will call out which are
  short.
- **FAIL** — 3 or fewer met; strategy is not ready for paper.

A sample PASS response from the bot:

```
Backtest for "ETH momentum with RSI + SMA crossover" (3-month window):

  Sortino:       1.83  ✓ (threshold 1.5)
  Sharpe:        1.42  ✓ (threshold 1.2)
  Calmar:        1.15  ✓ (threshold 1.0)
  IRR:           0.22  ✓ (threshold 0.15)
  Max drawdown:  0.38  ✓ (threshold 0.70)
  Win rate:      0.34  ✓ (threshold 0.25)

Total trades: 47   Avg win: 2.8%   Avg loss: -1.9%   Profit factor: 1.67

Verdict: PASS. Promote to paper, iterate the goal, or reject?
```

## 4. Interpret the verdict

### If PASS

Say: `"Promote to paper."` Proceed to Chapter 05.

### If MARGINAL

The bot will tell you which thresholds are short. Two options:

1. **Accept and paper it anyway.** MARGINAL strategies can be fine
   — the thresholds are conservative defaults, and a paper run
   will give you real signal on whether the strategy works on
   today's market.
2. **Iterate the goal.** Ask the bot for a different reference:
   `"Show me ref-009 instead"` (the mean-reversion alternative) or
   `"What about on 4h?"` (pulls in ref-005, ref-007 for ETH 4h).

### If FAIL

Do NOT paper a failing strategy "to see what happens." A FAIL
usually means the market regime has moved against the strategy's
logic — it will either not fire at all in paper, or fire and lose.

Two options:

1. **Try a different reference.** Go back to the list and pick
   another.
2. **Try a different timeframe.** 1h and 4h often produce different
   results on the same logic — trade rate + noise profile changes.

### If the backtest returns 0 trades (special case)

Sometimes the market just hasn't produced the signal's setup
conditions in the lookback window. You'll see `total_trades == 0`
in the metrics, and the risk-adjusted ratios will be reported as
`None` or `0` (because you can't compute Sharpe with no trades).

The bot will flag this as `INSUFFICIENT_TRADES` — explicitly NOT
a verdict against the strategy, just "no evidence either way."

Three options:

1. **Extend the lookback.** Try `"Same backtest but on a 12-month
   window."` The bot will pass `lookback_months=12`.
2. **Drop to a shorter timeframe.** More bars means more
   opportunities for signals to fire. Retry on 1h or 15m.
3. **Pick a different reference.** The ones in the library with
   `category: volatility` or `mean-reversion` tend to fire more
   often than strict trend-following in quiet markets.

## 5. The autonomous alternative

If none of the references grab you, try:

> "Forget the references — pick me 7 candidates for ETH on 1h
> optimized for sortino, backtest them all."

The bot calls `create_strategy_autonomous(...)` which generates
7 strategy variants internally, backtests all of them, and returns
a ranked list. Takes ~60 seconds vs. ~10 for reference-first.

The bot will surface the top 3 with verdicts. You pick the winner
the same way you would with references.

**Tradeoff:** autonomous candidates sometimes use unusual signal
combinations that weren't curated, so the resulting strategy is
less interpretable ("why did it pick pvo_bullish_cross?"). Fine
for experimenting, but the reference library is the more trustworthy
starting point.

## 6. What's in your DB now

Verify the strategy landed:

```
> "Show me my strategies."
```

The bot calls `list_strategies()` and you should see one row with
`status: "draft"`. Draft = saved but not running on a cron yet.
We'll change that in Chapter 05.

You can also curl it directly if you're curious:

```bash
curl -s -H 'X-API-Key: dev-key-1' \
  http://localhost:9080/api/v1/agent/strategies | python3 -m json.tool
```

## Common questions

### "What if I wanted stop-losses?"

Stop-losses live in the strategy's `risk_management.stop_loss_pct`
config. The reference strategies come with defaults (usually 3–5%).
You can override per-strategy by asking: `"Build the same strategy
but with a 4% stop-loss."` The bot will pass it through to
`create_strategy_manual`.

### "How often does it actually trade?"

Depends on the timeframe and signals. A 1h momentum strategy with
standard RSI/SMA signals typically fires 10–30 times per month on
ETH — call it one trade every 1–3 days. 4h is 2–4x less frequent;
15m is 4–8x more frequent.

You'll see exact numbers from the backtest: `total_trades` / (months
in lookback) gives you a rough per-month rate.

### "Do I have to run backtests only on historical data?"

Yes. Backtests are always historical — they run the strategy logic
against OHLCV bars that have already happened. There is no "future
data leak" even if you could ask for one, because the SDK enforces
it.

Paper mode is where you test on "new" data (post-backtest bars, as
they form). Chapter 05.

### "Can I edit a strategy after creating it?"

Not in-place. The pattern is: create a new strategy, let
the old one age out or archive it. If you want to try "same signals,
different params," ask the bot to `build_strategy_from_reference`
again with overrides, or build manually.

## What to take away

- Reference-first is the default authoring mode for a reason —
  use it for your first strategy.
- The backtest is a hypothesis check, not a prediction. PASS means
  "this used to work"; MARGINAL means "ambiguous"; FAIL means
  "this doesn't work on today's regime."
- `INSUFFICIENT_TRADES` is not a failure, it's a "try again with
  more data."
- A strategy in `draft` state is saved but doing nothing. Promotion
  to paper schedules it.

You now have a strategy and a verdict. Next, we make it tick.

→ [Chapter 05 — Paper mode](05-paper-mode.md)
