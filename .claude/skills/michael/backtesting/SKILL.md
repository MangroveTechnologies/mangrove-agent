---
name: backtesting
description: >-
  Run a saved strategy over history and report what it did honestly: the metrics, the execution
  config the run actually used, which of those values were the strategy's and which were platform
  canon, and the buy-and-hold return over the same window. Reach for it whenever a past-performance
  question is at stake: "how did my strategy do", "backtest this", "what would it have returned",
  "is this any good", "did it beat just holding", "what were its risk settings". Also covers
  reading a run back rather than paying to repeat it, tidying an old run out of the way, and
  discussing any execution-config parameter. Also covers choosing which of several candidates to
  spend a backtest on. Uses backtest_strategy, sieve_score, get_backtest, list_backtests
  and get_benchmark.
uses-tools: [backtest_strategy, sieve_score, get_backtest, list_backtests, get_benchmark]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/backtesting/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# Say what the run actually did

A backtest is the most expensive thing you can do on the user's behalf. It costs them a unit of
their monthly allowance, it takes tens of seconds, and its output is the number they will decide
with. So the standard is not "report the metrics" -- it is that everything you say about a run is
something the run actually told you.

## Never run one you already have

`list_backtests` first whenever the user refers to a result rather than asking for a new one --
"how did it do", "what about that one", "compare those two". Then `get_backtest` for the one you
need. A stored run is the record of what happened; re-running is a fresh answer to a question that
already has one, and it bills them again.

`backtest_strategy` when there is genuinely no run for the window they are asking about.

Runs are stored against the API key's user, not against this agent's strategy ids: match a
stored run to a strategy by `asset`, window and the `strategy_name` `get_backtest` returns.

## Choosing which candidate to back

With several candidates and one backtest's worth of patience, `sieve_score` scores them, up to
99 per call: its binary head (`p_trades`) is how likely each is to trade at all, and that is what
to rank by. Keep one asset per call, because a strategy's signals are measured on that asset's
candles.

Read it as an ordering, not a verdict. It says nothing about whether a strategy will make money --
only a backtest answers that -- and a low score is weak evidence: measured against runs that did
trade, a 0.5 cutoff recovers under half of them. So back the top of the ranking first, and never
tell someone their strategy will not trade because this scored it low. Say which model version
ranked them when it matters; the ranking is that model's opinion, not a property of the strategies.

Screening a single strategy answers nothing worth reporting. If there is one candidate, back it.

## A return with no benchmark is not an answer

Every time you state what a strategy returned, state what holding the asset returned over the same
window. `backtest_strategy` already includes it in `benchmark` -- use that, don't re-derive it. For a
different asset or period, `get_benchmark`. Here the holding return is
`benchmark.buy_and_hold_return_pct` and the difference is `benchmark.strategy_minus_benchmark_pct`.
When `benchmark.available` is false, say it could not be fetched and why (`reason`) -- never quote
the strategy's return alone as though it were the whole answer.

A strategy that returned -14.5% while the asset fell -20.5% did its job. Reporting only the -14.5%
tells the user they lost money and hides that they lost less than the alternative. Reporting only
"it beat the market" hides that they lost money. Both facts, always.

## Read the units off the result

`metric_units` travels with every result. Percent-typed metrics are on a **0-100** scale: `0.52`
means 0.52%, not 52%. Never infer the scale from how big a number looks -- that is exactly the
mistake that reported a 0.52%/yr strategy as +52%.

## A null metric is not a zero

`null` means the quantity was not measurable, and it needs a reason, not a number. Sharpe, Sortino
and Calmar are null below roughly 30 daily observations, because a ratio computed from
two weeks of data is noise wearing a decimal point. Say "the window is too short to compute it",
never "its Sharpe is 0".

In this agent, `backtest_strategy` fills a missing `sharpe_ratio`, `win_rate`, `irr_annualized` or
`max_drawdown` with `0.0` in its `metrics`. Read `num_days` and `total_trades` before believing a
zero there, and when it matters read the stored run with `get_backtest`, whose metrics are
exactly what the engine returned.

Zero trades means there is no win rate. Say that -- and then say why, because the result
carries it: `metrics.diagnostics.entry_denials` is how many entries the engine refused and
the reasons (cooldown, principal too small), and `metrics.diagnostics.window_bars` is the
rolling window every signal was evaluated with. "It fired 150 times and every entry was
denied for cooldown" and "it never fired" are different answers, and the person deciding
what to change next needs to know which one they got.

One (strategy, window) is ONE measurement. Nothing here refuses the same strategy over the same
dates again -- a repeat is billed and stored as a second run -- so find the existing one with
`list_backtests` and read it with `get_backtest`. A new measurement
needs a different window, or different parameters, and different parameters are a different
strategy.

## The check comes before the spend

A full backtest costs a unit of the monthly allowance when it is submitted, and nothing checks the
strategy again at that point: a run over a bad signal name or a missing timeframe is billed and
then fails. `create_strategy_manual` checks the composition when the strategy is created, so
before a backtest read the stored rules with `get_strategy` -- every signal name and parameter key
as `query_knowledge` has them, a `timeframe` on every signal -- and fix a fault by creating a
corrected strategy.

A signal parameter still at its library default is a value nobody chose. Either choose it (and
create the strategy with that value) or tell the user you considered the default and stand by it,
and why.

`backtest_strategy` takes a per-run `config` that merges over the canonical trading defaults --
execution parameters and `slippage_pct` / `fee_pct` alike. A result measured with overrides is a
measurement of that config, not of the strategy as stored: name the overrides whenever you quote
it, and prefer storing the execution config on the strategy (`execution_config` at creation) so the
record and the run agree.

## The window is not always the one requested

Read `resolved_window`: explicit `start_date` / `end_date` when dates were passed (or derived from
`lookback_days` / `lookback_hours`), or `lookback_months` when the span was chosen from the
strategy's timeframe. `metrics.num_days` is what the data actually supported, which can be less than
the range if history is short. If they asked for a year and got four months, say so before quoting
an annualised figure off it.

`start_date` and `end_date` go together; a lookback is the alternative to them, not an addition.

## Long windows take longer, and a timeout is not a result

`backtest_strategy` submits the run and polls it, so a wide window -- a multi-month `1h` run,
anything on `1d` -- is slow rather than refused. If polling gives up, the error names the run's
`backtest_id`: it may still finish server-side, so read it later with `get_backtest` instead of
submitting it again. No metrics exist until it completes. Never present an unfinished run as a
result, and never guess what it would have said.

## When a call fails

A failed call returns no data at all. There is nothing to report from it except that it failed and
why. Do not fill the gap from an earlier run, from the strategy's stored config, or from what a
strategy like this usually does.

## A run is never deleted or put away

There is no way to remove a backtest and nothing to offer instead. A run is the measurement a
decision was made on, so it stays in the history whether it was good or bad. Asked to delete or
clear one out, say that plainly: the record of what a strategy did is not something to tidy up.

A losing run is worth the most of any of them. It is the evidence for why a strategy was
changed.
