# Chapter 09 — Screen strategies with SIEVE before backtesting

*20 minutes. No funds required.*

Chapter 04 walked you through authoring a strategy and Chapter 05
paper-traded it. But authoring is cheap and backtesting isn't — a
single Oracle backtest can take 30-120 seconds on a multi-month
lookback, and you might want to try 50 parameter variations of a
candidate strategy before committing to one.

This chapter introduces **SIEVE**, a Mangrove model trained on millions
of historical sweep runs. SIEVE scores a candidate strategy in
milliseconds and returns a **go/no-go** answer: `P(no_trades)` vs
`P(trades)`. If the model thinks your strategy will never fire an entry
on real data, you skip the backtest entirely.

**SIEVE does not predict performance.** Earlier versions also returned a
4-class "winning / losing" outcome. Oracle retired it because its
"winning" label mostly kept strategies that underperform plain
buy-and-hold. SIEVE now answers one question: *will this strategy trade?*
Whether it trades *well* is what the backtest is for.

## The pattern

The agent exposes three auth-gated surfaces (REST + MCP):

| Surface | What it does |
|---|---|
| `sieve_score` | Screen 1-99 strategies through SIEVE; returns `p_no_trades` / `p_trades` per item. |
| `oracle_data_query` | Query the curated Oracle corpus for analogues to learn from. |
| `oracle_backtest` | Run a single strategy through Oracle's engine synchronously. |

All three forward through MangroveAI's authenticated proxy at
`/api/v1/oracle/*` to the live MangroveOracle service. Tenancy is
enforced by the proxy — you never see another customer's rows.

## Score one strategy

In Claude Code:

> "Score this strategy through SIEVE: BTC, 1h, MACD bullish cross + SMA(50) filter on entry, MACD bearish cross on exit."

The bot calls `sieve_score` with one Strategy object. The response looks
like this:

```json
{
  "predictions": [{
    "binary": {"p_no_trades": 0.08, "p_trades": 0.92}
  }],
  "count": 1,
  "model_version": "mangrove-sieve:fb26279be5c6",
  "code_version":  "oracle:v2.11.0 ai:v5.4.0 kb:3.3.1 roots:v0.14.0"
}
```

Read it: `P(trades) = 0.92` means SIEVE expects this strategy to fire on
real data, so a backtest will actually measure something. It says
nothing about whether those trades make money.

If `P(no_trades)` had been `> 0.5`, you'd drop the strategy here (or
widen its parameters and re-score) instead of paying for a backtest that
reports zero trades.

## Filter a candidate set

The real value comes from scoring **many** at once. Say you're trying
50 (signal × parameter × timeframe) variations of a MACD-based BTC
strategy:

> "Generate 50 variations of my MACD strategy with the entry-window
> sweeping from 8 to 20 and the exit-window from 20 to 40, then screen
> them all through SIEVE in one call."

The bot calls `sieve_score` with all 50 strategies in a single batch
(SIEVE accepts up to 99 per request), drops everything with
`p_no_trades > 0.5`, and tells you how many survived. If more survive
than you want to backtest, it can order them by `p_trades` — that's how
confident SIEVE is that they'll trade, not how good they are.

## Look at the historical corpus

Before you backtest, look at what already worked. The Oracle corpus
holds millions of completed sweep runs; you can query it for analogues
of your candidate:

> "Show me the top 5 BTC strategies on 1h with annualized IRR above
> 35% that traded at least 20 times."

The bot calls `oracle_data_query` with `table=results`, columns
`experiment_id, asset, timeframe, irr_annualized, total_trades`, and
filters on `asset=BTC`, `timeframe=1h`, `irr_annualized>=35`,
`total_trades>=20`. Tenancy is enforced — only rows your org has
access to come back.

## Backtest the survivors

Backtest the candidates SIEVE expects to trade, optionally cross-
referenced against the corpus pattern:

> "Backtest the surviving BTC MACD candidates over the last 12 months and
> compare them."

The bot calls `oracle_backtest_bulk` (or `oracle_backtest` for one) with
the strategy JSON. The Oracle engine runs the full simulation against
real OHLCV, returning Sharpe, Sortino, IRR, max drawdown, trade history,
etc. Rank the candidates by those results, not by SIEVE.

## Putting it together

The pre-flight workflow:

1. **Author** N candidate strategies (Claude Code generates parameter
   variations).
2. **`sieve_score`** all N in one or two batches. Drop anything with
   `P(no_trades) > 0.5`.
3. **`oracle_data_query`** for analogues — what strategies already
   in the corpus look similar and did well?
4. **Backtest** the survivors and compare the results. Now you're only
   spending compute on candidates that will actually trade.

## What SIEVE is NOT

It is a **fast go/no-go filter**, not a backtest and not a performance
forecast. Don't paper-trade a strategy because SIEVE gave it a high
`P(trades)` — that only means it's likely to place trades. Always
backtest before promoting to paper.

## Going further

- **Search a whole parameter space, not one strategy at a time.** The
  agent exposes the full managed sweep lifecycle as MCP tools
  (`oracle_create_experiment` → `oracle_validate_experiment` →
  `oracle_launch_experiment` → `oracle_get_experiment` →
  `oracle_list_results`). Just ask: *"sweep the MACD windows on BTC 1h
  and rank them."* The guided flow is the **`/sweep`** skill; the cheap
  pre-screen is the **`/sieve`** skill.
- The async / bulk single-strategy backtest variants are also on the
  agent's MCP surface (`oracle_backtest_async`, `oracle_backtest_poll`,
  `oracle_backtest_bulk`) in addition to `client.oracle.*` if you script
  against the SDK directly.
- The API reference for `client.oracle.*` lives in the
  [mangrove-ai SDK docs](https://mangrove.io/docs/sdks/mangroveai),
  and the worked SDK walkthroughs are the KB guides
  [SIEVE end-to-end](https://mangrove.io/docs/guides/sieve-end-to-end-workflow)
  and [Experiments](https://mangrove.io/docs/api-reference/experiments).
- The full corpus schema (98 fields on the `results` table) is
  documented at `MangroveOracle/infra/terraform/schemas/results.json`.
