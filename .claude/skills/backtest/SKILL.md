---
name: backtest
description: >-
  Use when the user wants to backtest an existing strategy — "run a backtest
  on X", "how would this have done", "verify this strategy", or after
  /create-strategy produces a draft that needs evaluation. Sizes the
  window from a target bar count (not a fixed month table), caps against
  data availability, reports a verdict against `threshold_spec.json`
  plus benchmark deltas, and recommends the next action (promote,
  iterate, reject). Wraps `backtest_strategy` + `get_market_data` +
  optionally `list_ohlcv_coverage` (when available) for the SDK
  consumer — no manual date math.
---

# Backtest Skill

This skill exists because **picking the backtest window well is more
important than picking the strategy**. A strategy that looks great over
the last 30 days of a grinding uptrend tells you nothing about how it
behaves when the market turns. A window that's too short produces <10
trades and makes every ratio meaningless. A window that's too long
bleeds across regime changes and flatters overfit strategies.

The `/create-strategy` skill hands off here after Phase B-bulk (or
earlier, for single-candidate builds). Users can also invoke `/backtest`
directly on any existing `strategy_id`.

## Trigger

Activate when the user:

- Explicitly asks to backtest ("run a backtest on ref-004 with ETH 1h",
  "how would this have done over the last year", "test this")
- Has just promoted a draft from `/create-strategy` and needs a verdict
- Asks to re-evaluate a paper or live strategy on a new window
- Asks "is this strategy any good"

Do NOT activate for:

- Promotion decisions → `/promote-strategy`
- Live monitoring / evaluations → `/monitor-trades`
- Authoring new strategies → `/create-strategy`

## Phase A — Collect

Required inputs:

1. **strategy_id** — UUID from `list_strategies` or a `create_strategy_manual` response. If the user says "the one I just made" and there's only one draft, infer it. Otherwise call `list_strategies(status="draft")` and present choices.

Optional overrides (skill proposes defaults in Phase B — don't ask the user for these cold):

- **lookback window** — days, hours, or explicit start/end dates. If user volunteers one, respect it.
- **slippage_pct** / **fee_pct** — override `trading_defaults.json`. Only touch if the user mentions they want realistic frictions tuned.

Pull the strategy's asset + timeframe via `get_strategy(strategy_id)` so the window sizing can reason about bar counts.

## Phase B — Size the window

**Target bar count: 2000–5000.** That's the range where ratio metrics
stabilize (enough for 20–100 trades) without the window spanning so much
history that regime drift dominates. Translate to wall-clock via the
timeframe:

| Timeframe | Bars/day | Target window (bars ÷ bars/day) |
|-----------|----------|-----|
| 5m | 288 | **7–17 days** (default: 14d) |
| 15m | 96 | **20–50 days** (default: 30d) |
| 30m | 48 | **40–100 days** (default: 60d) |
| 1h | 24 | **3–7 months** (default: 6mo) |
| 4h | 6 | **12–30 months** (default: 18mo) |
| 1d | 1 | **5–14 years** (default: 5y, capped by provider history) |

> **Upstream timeout ceiling.** A single backtest runs against the cloud
> engine under `MANGROVE_SDK_TIMEOUT_SECONDS` (default 180s). Very long
> intraday windows can exceed it and return a `504`/`SDK_ERROR` — e.g. a
> **12-month 1h** run (~8,760 bars) reliably times out. Staying in the table's
> ranges (≤~6mo on 1h) keeps you well under it; for longer spans use
> `oracle_backtest_async` + poll, or walk several shorter windows.

The table is the default. Override reasons:

- **Data availability caps.** If the asset launched 8 months ago, a 5y
  daily window silently truncates. Before committing, sanity-check via
  `get_market_data(asset, interval, limit=1, end_date=<proposed_start>)`
  — if the response shows no bars before the proposed start, shrink the
  window to match actual coverage.
- **User asked for a specific window.** Respect it, but if their window
  produces <500 bars, warn once: "That window gives ~N bars — ratio
  metrics are unreliable below ~1000. Want me to widen to {default}
  instead?" Then proceed with their pick if they decline.
- **Regime check.** If the window is entirely one direction (pure
  uptrend or pure downtrend against the benchmark), flag it in the
  Phase D verdict — the strategy wasn't stress-tested. Don't auto-widen;
  let the user decide.

Tell the user the resolved window before running: "Backtesting on
`{asset}` `{timeframe}` from `{start}` to `{end}` — ~{bar_count} bars."

## Phase C — Run

Call `backtest_strategy(strategy_id, mode="full", start_date=..., end_date=...)`. Mode is always `"full"` for this skill (quick mode is for the bulk-candidate flow in `/create-strategy` Phase B-bulk).

Set expectations on latency:

- 5m × 14d (~4000 bars): a few seconds
- 1h × 6mo (~4400 bars): a few seconds
- 1d × 5y (~1800 bars): a few seconds
- Anything >10000 bars: disclose "this may take up to ~30s" before the call

If the SDK returns an error, surface the exact message. Don't retry silently — a failed backtest usually means bad data coverage or a strategy config issue, both of which the user needs to see.

## Phase D — Verdict

### Threshold gate

Evaluate against the 6 thresholds in `server/src/services/data/threshold_spec.json`:

| Metric | Threshold | Direction |
|---|---|---|
| `sortino_ratio` | ≥ 1.5 | higher is better |
| `sharpe_ratio` | ≥ 1.2 | higher is better |
| `calmar_ratio` | ≥ 1.0 | higher is better |
| `irr_annualized` | ≥ 0.15 | higher is better |
| `max_drawdown` | ≤ 0.7 | lower is better |
| `win_rate` | ≥ 0.25 | higher is better |

Decision rule:
- **PASS** — all 6 pass
- **MARGINAL** — 4–5 of 6 pass
- **FAIL** — ≤3 of 6 pass
- **INSUFFICIENT_TRADES** — `total_trades < 10`, regardless of ratios. Not a PASS, not a FAIL. Ratios with <10 trades are statistical noise.

### Benchmark-relative line

Every verdict includes a second-class line: **did the strategy beat
buy-and-hold?** Oracle stores `benchmark_asset_return`,
`benchmark_btc_return`, `benchmark_sp500_return` per result row for
exactly this question. Compute:

- `strategy_return - benchmark_asset_return` (same asset, same window)
- `strategy_return - benchmark_btc_return` (crypto-market proxy)

A PASS that loses to buy-and-hold is **still a useful strategy** (lower
drawdown, less correlation) but the user must see that tradeoff. A FAIL
that loses to buy-and-hold is a clear-cut reject.

### Failure-mode advice

Never stop at "FAIL." Always pair the verdict with what to try:

- **Failed on `total_trades` (INSUFFICIENT_TRADES)** → widen the window,
  loosen entry filters, or reconsider the timeframe (maybe 1h is too
  slow for a momentum strategy on this asset).
- **Failed on `sharpe_ratio` / `sortino_ratio`** → the strategy is
  taking risk disproportionate to reward. Try tightening the exit
  (earlier take-profit), lowering `max_risk_per_trade`, or reviewing
  whether the entry signal is firing in noise.
- **Failed on `calmar_ratio` / `max_drawdown`** → one or two losing
  streaks dominated. Try a volatility filter (only trade when ATR is
  below percentile X), or cap `max_open_positions` lower.
- **Failed on `irr_annualized`** → the strategy is boring, not broken.
  Often shows up with tight stops and conservative sizing. Widen
  `reward_factor` or pick a higher-volatility asset.
- **Failed on `win_rate`** → entries are firing too early or in
  counter-trend. Add a trend-filter FILTER signal to the entry group
  (e.g., `is_above_sma` or `adx_strong_trend`).

### Presenting the verdict

1. Verdict label (PASS / MARGINAL / FAIL / INSUFFICIENT_TRADES) in one line
2. Per-threshold table: metric | actual | threshold | ✓/✗
3. Benchmark line: "Strategy return: X%. Buy-and-hold on {asset}: Y%. vs BTC: Z%."
4. Next-step recommendation, chosen from the failure-mode advice above based on which thresholds failed.
5. For PASS: "Promote to paper (unrestricted) or live (requires allocation + backup confirmation). Which?"

## Phase E — Iterate

The user reviews the verdict and picks one of:

- **Accept** → hand off to `/promote-strategy` (skill boundary; don't promote from here)
- **Alt window** → re-run Phase B–D with a different window. Common
  asks: "try YTD", "try last 3 months only", "try the 2023 drawdown".
  Resolve the window, confirm bar count, re-run.
- **Walk-forward** → for a MARGINAL or borderline PASS, offer a
  walk-forward check: split the window into 3 consecutive sub-windows,
  backtest on each, report per-sub-window metrics. If performance holds
  across all three, the strategy is more likely to generalize. If it
  falls off a cliff on one, that's the regime it doesn't handle.
- **Alt reference** → user wants to try a different reference from the
  same Phase A search in `/create-strategy`. Reinvoke `/create-strategy`
  at Phase B-bulk with the shortlist.
- **Reject** → archive the strategy via
  `update_strategy_status(strategy_id, status="archived")` so it stops
  showing up in `list_strategies(status="draft")`. Don't delete — the
  record is useful for the user to remember what they tried.

## Prohibited

- **Never** invent metrics. If `metrics` is missing or null fields, say
  so verbatim: "The SDK response is missing `sharpe_ratio` — I can't
  verdict against the threshold. This usually means too few trades or
  the data provider returned insufficient history."
- **Never** declare PASS with `total_trades < 10`. INSUFFICIENT_TRADES,
  full stop.
- **Never** pick a window based on "feel." Every window choice either
  follows the bar-count table or is user-specified.
- **Never** hide a buy-and-hold loss. If the strategy underperforms the
  asset's buy-and-hold, say so in the verdict — even for PASSes.
- **Never** promote from inside this skill. `/promote-strategy` owns
  that transition.

## Summary — Decision Tree

```
User wants to backtest
│
├─ Phase A: collect strategy_id (+ optional overrides)
│
├─ Phase B: size window from bar-count table
│     → sanity-check provider coverage
│     → warn if <500 bars or regime-homogeneous
│     → disclose resolved window to user
│
├─ Phase C: backtest_strategy(mode="full", resolved window)
│     → disclose latency up front
│     → surface SDK errors verbatim
│
├─ Phase D: verdict
│     → PASS / MARGINAL / FAIL / INSUFFICIENT_TRADES
│     → threshold table + benchmark-relative line
│     → failure-mode advice paired with every non-PASS
│
└─ Phase E: iterate
      → accept (hand off to /promote-strategy)
      → alt window / walk-forward / alt reference
      → reject (archive)
```
