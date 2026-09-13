---
name: sieve
description: >-
  Use when the user has MANY candidate strategies (or wants to try many
  parameter variations) and needs to know which are worth the cost of a
  backtest — "score these", "which of these should I test", "pre-filter
  my candidates", "which of these MACD variations will even trade". Also
  the natural next step after /create-strategy or /custom-signal produces
  a candidate set. Scores up to 99 candidates in ONE millisecond-cheap call
  through the Mangrove SIEVE go/no-go gate and drops the ones SIEVE expects
  never to trade — a cheap SCREEN, never a verdict, and NOT a performance
  ranking. Wraps `sieve_score` + `oracle_list_signals`, hands the survivors
  to /backtest (one) or /backtest/bulk (several). This screens a FIXED
  candidate list; for a parameter-space SEARCH that generates its own
  candidates, use /sweep instead.
---

# SIEVE Skill

This skill exists because **a backtest is expensive and many candidates
never trade at all.** A single Oracle backtest takes 30–120s on a
multi-month window, and a large share of candidate strategies never fire
an entry (unreachable trigger thresholds, filters that are almost never
true). Paying for a backtest only to learn "zero trades" is wasteful.

**SIEVE** is a Mangrove model trained on millions of historical sweep
runs. It scores a candidate in milliseconds and returns one thing:

- **binary (go/no-go)** — `{p_no_trades, p_trades}`: will this strategy
  place trades on real data? If SIEVE expects it never to fire, skip the
  backtest. (This is the same head the `/sweep` engine uses as its inline
  pre-filter — but that's a *different* mechanism; see below.)

**SIEVE does not predict performance.** It used to also return a 4-class
outcome head (`losing / no_trades / wash / winning`). Oracle retired it
(MangroveOracle #422) because its IRR-based "winning" label kept mostly
strategies that underperform plain buy-and-hold. Current responses carry
only `binary`. Never rank candidates by predicted winning/losing, and never
describe a SIEVE score as a quality signal. If an old response still has a
`four_class` field, ignore it.

**SIEVE is a SCREEN, not a verdict.** It tells you what's worth paying to
backtest (what will trade), never whether a strategy is good. Only a real
backtest decides.

**Two different SIEVE uses — don't confuse them:**
- **This skill** = offline screen of a fixed shortlist via the
  `sieve_score` endpoint (≤99 per call), then hand survivors to a backtest.
- **`/sweep`'s pre-filter** = the same binary head running *inside* the
  engine during a parameter search, skipping dead configs as they're
  generated. That's part of `/sweep`, not this skill.

## Trigger

Activate when the user:

- Has many candidate strategies and asks which to test ("score these",
  "which of these is worth backtesting")
- Wants to explore parameter variations cheaply ("try 50 variations of
  my MACD strategy and drop the ones that won't trade")
- Just produced a candidate set via `/create-strategy` (autonomous mode
  emits N candidates) or `/custom-signal` and needs to prune before
  backtesting
- Asks to "pre-filter", "screen", or "narrow down" strategies

Do NOT activate for:

- A single known strategy the user wants to evaluate → `/backtest`
- "Which of these will make money?" → SIEVE can't answer that; backtest
  the candidates (`/backtest/bulk`) and compare the verdicts
- A parameter-space SEARCH where the engine generates and backtests its
  own candidates → `/sweep` (a separate workflow — this skill does NOT
  feed it; `/sweep` has its own built-in binary pre-filter)
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
- **From a set of variations the user wants screened** — generate the
  variations yourself into N candidate objects (≤99) and score them.
  (If the user instead wants a *managed* parameter search — the engine
  generating + backtesting + ranking candidates with persisted results —
  that's `/sweep`, not this. Screening a list ≠ searching a space.)

**Get the signal names + param specs right.** Call `oracle_list_signals`
once and use it to validate every `name`, `signal_type` (`TRIGGER` /
`FILTER`), and `params` key against the real catalog. A typo in a signal
name or param key produces a useless score, not an error.

**The 99 cap is hard.** `sieve_score` accepts 1–99 items per call. If the
user's grid is larger, chunk it into batches of 99 and score each batch
— don't silently truncate. Tell the user how many batches it took.

## Phase B — Score

One `sieve_score(strategies=[...])` call per batch. It returns one
prediction per candidate (input order preserved), plus `model_version`
and `code_version`.

Read a prediction in plain language for the user the first time:

> `p_trades = 0.92` → SIEVE expects this one to fire on real data, so a
> backtest will actually measure something. It says nothing about whether
> those trades make money. If `p_no_trades` were `> 0.5`, we'd drop it
> here and not pay to backtest it.

**Tier + cost note:** each `sieve_score` call is ONE billable unit
regardless of batch size (Beginner ≈ 10 calls/month). Pack batches as
close to 99 as you can — scoring 99 costs the same as scoring 1.

## Phase C — Filter

1. **Go/no-go.** Drop every candidate with `binary.p_no_trades > 0.5`.
   These won't fire entries; a backtest would just confirm zero trades.
2. **Order the survivors** by `binary.p_trades` descending only when you
   have more survivors than backtest budget. That orders them by how
   confident SIEVE is that they'll trade — NOT by expected performance.
   Say so when you present the list.
3. **Keep what the budget allows** and backtest those.

Report it as: "Scored N. M are expected to trade (p_no_trades ≤ 0.5);
N−M dropped as no-trade. Backtesting K: …" with each kept candidate's
`p_trades`.

**Soft-failure mode to watch:** `p_no_trades` close to `1.0` across the
whole batch usually means the TRIGGER thresholds are unreachable (e.g.
RSI < 5) or the filters are too restrictive. The fix is to **widen the
parameter ranges and re-score**, not to delete the strategy. Say so
rather than reporting "everything died."

## Phase D — Handoff

The survivors are ready for real evaluation — a **backtest**, which is the
actual verdict SIEVE only screened for. Route them:

- **One candidate, or the user wants a careful single verdict** →
  `/backtest` on that candidate (register it first with
  `create_strategy_manual` if it isn't a strategy yet).
- **Several survivors to backtest together** → `/backtest/bulk`
  (`oracle_backtest_bulk`) — one call backtests the shortlist with shared
  OHLCV fetches and returns all results. Rank by the backtest verdicts,
  not by SIEVE.

Do NOT route the survivors into `/sweep`. A sweep is a parameter-space
search that generates its own candidates; it does not consume a
pre-screened list. (If the user wants to *search* rather than *screen a
list*, that was a `/sweep` job from the start.)

Always carry the provenance forward: log `model_version` +
`code_version` next to the shortlist, so when SIEVE is retrained you can
tell which snapshot produced the screen.

## Prohibited

- **Never** promote a strategy to paper or live on a SIEVE score alone.
  A real `backtest_strategy` / Oracle backtest ALWAYS gates paper/live.
- **Never** present `p_trades` (or any SIEVE number) as a measure of how
  good a strategy is, and never rank candidates by predicted
  winning/losing. SIEVE predicts whether a strategy trades, not how well.
- **Never** send more than 99 items in one `sieve_score` call — chunk,
  don't truncate.
- **Never** invent probabilities or "round up" a borderline score.
  Report what the model returned.
- **Never** delete a whole batch because `p_no_trades ≈ 1.0` — that's a
  "widen the params and re-score" signal, not a dead end.
- **Never** feed a SIEVE shortlist into `/sweep` — a sweep generates its
  own candidates from a parameter space; this skill screens a fixed list
  and hands it to `/backtest` / `/backtest/bulk`.

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
│     → explain once: go/no-go = will it trade, not how well
│
├─ Phase C: filter
│     → drop p_no_trades > 0.5
│     → over budget? order survivors by p_trades (confidence it trades)
│     → p_no_trades ≈ 1.0 everywhere → widen params, re-score
│
└─ Phase D: handoff (NEVER promote from here; a backtest is the verdict)
      → one candidate / careful verdict → /backtest
      → several survivors → /backtest/bulk, rank by backtest verdicts
      → (NOT /sweep — that searches a space, it doesn't take a screened list)
```
