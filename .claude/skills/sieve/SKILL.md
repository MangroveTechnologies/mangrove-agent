---
name: sieve
description: >-
  Use when the user has MANY candidate strategies (or wants to try many
  parameter variations) and needs to know which are worth the cost of a
  backtest — "score these", "which of these should I test", "pre-filter
  my candidates", "rank these MACD variations". Also the natural next
  step after /create-strategy or /custom-signal produces a candidate
  set. Scores up to 99 candidates in ONE millisecond-cheap call through
  the Mangrove SIEVE classifier, drops the dead-on-arrival ones, and
  ranks the survivors by P(winning) before any expensive backtest.
  Wraps `sieve_score` + `oracle_list_signals`, hands off to /backtest
  (one winner) or /sweep (managed many).
---

# SIEVE Skill

This skill exists because **a backtest is expensive and most strategies
fail it.** A single Oracle backtest takes 30–120s on a multi-month
window, and roughly **5 of every 6 candidate strategies fail** — they
never fire a trade, or they wash, or they lose. Paying for 99 backtests
to find the 1 good one is wasteful.

**SIEVE** ("Strategy Indicator Embeddings with Value Estimation") is a
classifier trained on 1.24M historical Mangrove sweep runs. It scores a
candidate in milliseconds and returns two things:

- **binary** — `{p_no_trades, p_trades}`. The go/no-go gate: if SIEVE
  thinks the strategy will never fire on real data, you skip the
  backtest entirely.
- **four_class** — `{losing, no_trades, wash, winning}`. A softmax over
  outcomes. Rank survivors by `P(winning)` and only spend backtest
  compute on the top slice.

A 99-candidate search compresses to ~5 backtests. Same signal quality at
~10% of the cost. **SIEVE is a fast filter, not a backtest** — it never
replaces the real thing (see Prohibited).

## Trigger

Activate when the user:

- Has many candidate strategies and asks which to test ("score these",
  "which of these is worth backtesting", "rank these")
- Wants to explore parameter variations cheaply ("try 50 variations of
  my MACD strategy and tell me the good ones")
- Just produced a candidate set via `/create-strategy` (autonomous mode
  emits N candidates) or `/custom-signal` and needs to prune before
  backtesting
- Asks to "pre-filter", "screen", or "narrow down" strategies

Do NOT activate for:

- A single known strategy the user wants to evaluate → `/backtest`
- A managed, ranked sweep over a parameter grid with persisted results
  → `/sweep` (SIEVE feeds it; this skill is the cheap pre-screen)
- Authoring new strategies from scratch → `/create-strategy`

## Phase A — Assemble candidates (≤ 99)

A candidate is a MangroveAI-shaped Strategy object:

```json
{
  "asset": "BTC",
  "entry": [
    {"name": "macd_bullish_cross", "signal_type": "TRIGGER", "timeframe": "1h",
     "params": {"window_fast": 12, "window_slow": 26, "window_sign": 9}},
    {"name": "is_above_sma", "signal_type": "FILTER", "timeframe": "1h",
     "params": {"window": 50}}
  ],
  "exit": [
    {"name": "macd_bearish_cross", "signal_type": "TRIGGER", "timeframe": "1h",
     "params": {"window_fast": 12, "window_slow": 26, "window_sign": 9}}
  ],
  "execution_config": {"reward_factor": 2.0, "max_risk_per_trade": 0.01}
}
```

Where the candidates come from:

- **From `/create-strategy` or `/custom-signal`** — the candidate set is
  already built; carry it straight in.
- **From a parameter sweep the user describes** — generate the
  variations yourself. "Sweep entry-window 8→20, exit-window 20→40"
  means build the cross-product of those param values into N candidate
  objects.

**Get the signal names + param specs right.** Call `oracle_list_signals`
once and use it to validate every `name`, `signal_type` (`TRIGGER` /
`FILTER`), and `params` key against the real catalog (223 signals across
momentum / trend / volume / volatility / patterns). A typo in a signal
name or param key produces a useless score, not an error.

**The 99 cap is hard.** `sieve_score` accepts 1–99 items per call. If the
user's grid is larger, chunk it into batches of 99 and score each batch
— don't silently truncate. Tell the user how many batches it took.

## Phase B — Score

One `sieve_score(strategies=[...])` call per batch. It returns one
prediction per candidate (input order preserved), plus `model_version`
and `code_version`.

Read a prediction in plain language for the user the first time:

> `p_trades = 0.92` → SIEVE expects this one to fire on real data.
> `P(winning) = 0.50` → its best guess at a profitable outcome — not
> great, not terrible, worth a backtest. If `p_no_trades` were `> 0.5`,
> we'd drop it here and not pay to backtest it.

**Tier + cost note:** each `sieve_score` call is ONE billable unit
regardless of batch size (Beginner ≈ 10 calls/month). Pack batches as
close to 99 as you can — scoring 99 costs the same as scoring 1.

## Phase C — Filter + rank

1. **Binary filter.** Drop every candidate with `binary.p_no_trades > 0.5`.
   These won't fire entries; a backtest would just confirm zero trades.
2. **Rank survivors** by `four_class.winning` descending.
3. **Keep the top slice** — top 5–10, or the top ~10% for large sets.
   That's the shortlist worth real backtest compute.

Report it as: "Scored N. M survive the binary filter. Top K by
P(winning): …" with the K shortlist and their `P(winning)` /
`P(no_trades)`.

**Soft-failure mode to watch:** `p_no_trades` close to `1.0` across the
whole batch usually means the TRIGGER thresholds are unreachable (e.g.
RSI < 5). The fix is to **widen the parameter ranges and re-score**, not
to delete the strategy. Say so rather than reporting "everything died."

## Phase D — Handoff

The shortlist is now ready for real evaluation. Route it:

- **One clear winner, or the user wants a careful single verdict** →
  `/backtest` on that candidate (register it first with
  `create_strategy_manual` if it isn't a strategy yet).
- **Several survivors worth comparing head-to-head with ranked,
  persisted results** → `/sweep` — feed the shortlist as the experiment's
  signal mix.

Always carry the provenance forward: log `model_version` +
`code_version` next to the shortlist, so when SIEVE is retrained you can
tell which snapshot produced the ranking.

## Prohibited

- **Never** promote a strategy to paper or live on a SIEVE score alone.
  SIEVE is an aggregate prediction over millions of runs; your exact
  parameter combo may sit in a corner where the model is overconfident.
  A real `backtest_strategy` / Oracle backtest ALWAYS gates paper/live.
- **Never** send more than 99 items in one `sieve_score` call — chunk,
  don't truncate.
- **Never** invent probabilities or "round up" a borderline score.
  Report what the model returned.
- **Never** treat a high `P(winning)` as a backtest result in a verdict.
  It's a filter signal, full stop.
- **Never** delete a whole batch because `p_no_trades ≈ 1.0` — that's a
  "widen the params and re-score" signal, not a dead end.

## Summary — Decision Tree

```
User has many candidates / wants to try many variations
│
├─ Phase A: assemble ≤99 candidate Strategy objects
│     → validate names + params against oracle_list_signals
│     → chunk into batches of 99 if larger
│
├─ Phase B: sieve_score(each batch)
│     → record model_version + code_version
│     → explain binary vs 4-class once, in plain language
│
├─ Phase C: filter + rank
│     → drop p_no_trades > 0.5
│     → rank survivors by P(winning), keep top 5–10 / top ~10%
│     → p_no_trades ≈ 1.0 everywhere → widen params, re-score
│
└─ Phase D: handoff (NEVER promote from here)
      → one winner / careful verdict → /backtest
      → several to compare, ranked + persisted → /sweep
```
