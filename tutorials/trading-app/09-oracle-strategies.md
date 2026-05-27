# Chapter 09 — Score strategies with SIEVE before backtesting

*20 minutes. No funds required. New as of mangrove-agent vNEXT
(depends on mangrove-ai SDK 1.0).*

Chapter 04 walked you through authoring a strategy and Chapter 05
paper-traded it. But authoring is cheap and backtesting isn't — a
single Oracle backtest can take 30-120 seconds on a multi-month
lookback, and you might want to try 50 parameter variations of a
candidate strategy before committing to one.

This chapter introduces **SIEVE** (Strategy Indicator Embeddings with
Value Estimation), the Mangrove classifier trained on 1.24M historical
sweep runs. SIEVE scores a candidate strategy in milliseconds and
returns:

- A **binary** go/no-go: `P(no_trades)` vs `P(trades)`. If the model
  thinks your strategy will never fire entries on real data, you can
  skip the backtest entirely.
- A **4-class outcome** softmax over `losing` / `no_trades` / `wash` /
  `winning`. Rank candidates by `P(winning)` and backtest only the top
  decile.

**5 out of 6 strategies fail a backtest.** SIEVE tells you which 1.

## The pattern

The agent exposes three new auth-gated surfaces (REST + MCP):

| Surface | What it does |
|---|---|
| `sieve_score` | Score 1-99 strategies through SIEVE; returns binary + 4-class probabilities per item. |
| `oracle_data_query` | Query the curated Oracle corpus for high-IRR analogues to learn from. |
| `oracle_backtest` | Run a single strategy through Oracle's engine synchronously. |

All three forward through MangroveAI's authenticated proxy at
`/api/v1/oracle/*` to the live MangroveOracle service. Tenancy is
enforced by the proxy — you never see another customer's rows.

## Score one strategy

In Claude Code:

> "Score this strategy through SIEVE: BTC, 1h, MACD bullish cross + SMA(50) filter on entry, MACD bearish cross on exit."

The bot calls `sieve_score` with one Strategy object. Expected
response:

```json
{
  "predictions": [{
    "binary": {"p_no_trades": 0.08, "p_trades": 0.92},
    "four_class": {
      "losing":    0.11,
      "no_trades": 0.08,
      "wash":      0.31,
      "winning":   0.50
    }
  }],
  "count": 1,
  "model_version": "mangrove-sieve:0b9a2da0d827",
  "code_version":  "oracle:v0.14.2 ai:v3.10.2 kb:1.0.5 roots:v0.3.0"
}
```

Read it: `P(trades) = 0.92` means SIEVE thinks this strategy will fire
on real data. `P(winning) = 0.50` is its best guess; not great, not
terrible. Maybe worth a backtest.

If `P(no_trades)` had been `> 0.5`, you'd kill the strategy here and
not bother with the next steps.

## Filter a candidate set

The real value comes from scoring **many** at once. Say you're trying
50 (signal × parameter × timeframe) variations of a MACD-based BTC
strategy:

> "Generate 50 variations of my MACD strategy with the entry-window
> sweeping from 8 to 20 and the exit-window from 20 to 40, then score
> them all through SIEVE in one call."

The bot calls `sieve_score` with all 50 strategies in a single batch
(SIEVE accepts up to 99 per request). Then it sorts the results by
`four_class.winning` descending, and keeps only the top 5-10 for the
expensive backtest step.

## Look at the historical corpus

Before you backtest, look at what already worked. The Oracle corpus
holds millions of completed sweep runs; you can query it for high-IRR
analogues of your candidate:

> "Show me the top 5 BTC strategies on 1h with annualized IRR above
> 35% that traded at least 20 times."

The bot calls `oracle_data_query` with `table=results`, columns
`experiment_id, asset, timeframe, irr_annualized, total_trades`, and
filters on `asset=BTC`, `timeframe=1h`, `irr_annualized>=35`,
`total_trades>=20`. Tenancy is enforced — only rows your org has
access to come back.

## Backtest the survivors

Pick the highest-`P(winning)` candidate from SIEVE, optionally cross-
referenced against the corpus pattern, and backtest it:

> "Backtest that BTC MACD candidate over the last 12 months."

The bot calls `oracle_backtest` with the strategy JSON. The Oracle
engine runs the full simulation against real OHLCV, returning Sharpe,
Sortino, IRR, max drawdown, trade history, etc.

## Putting it together

The pre-flight workflow:

1. **Author** N candidate strategies (Claude Code generates parameter
   variations).
2. **`sieve_score`** all N in one or two batches. Drop anything with
   `P(no_trades) > 0.5`. Sort by `P(winning)` descending. Keep top 10%.
3. **`oracle_data_query`** for analogues — what strategies already
   in the corpus look similar and did well?
4. **`oracle_backtest`** the surviving candidates. Now you're spending
   compute on the ones SIEVE and the corpus both endorse.

A 50-strategy search compresses to maybe 5 backtests. Same signal
quality at 10% the cost.

## What SIEVE is NOT

It is a **fast filter**, not a backtest. Don't paper-trade a strategy
SIEVE rated 0.7 without backtesting it first — SIEVE's prediction is
an aggregate over millions of historical runs, but your strategy's
parameter combination might be in a corner of the distribution where
the model is overconfident. Always backtest before promoting to paper.

## Going further

- The API reference for `client.oracle.*` lives in the
  [mangrove-ai SDK docs](https://docs.mangrovedeveloper.ai/sdks/mangroveai).
- For batch / async / bulk backtest variants (long-running, many-
  strategy work), see the `backtest_async` / `backtest_bulk` SDK
  methods — not yet exposed through the agent's MCP surface, but
  reachable via `client.oracle.backtest_async(...)` if you script
  against the SDK directly.
- The full corpus schema (98 fields on the `results` table) is
  documented at `MangroveOracle/infra/terraform/schemas/results.json`.
