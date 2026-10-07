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
  spend a backtest on. Uses run_backtest, screen_candidates, get_backtest, list_backtests,
  get_benchmark and get_execution_config_schema.
uses-tools: [run_backtest, screen_candidates, get_backtest, list_backtests, get_benchmark]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/backtesting/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

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

`run_backtest` when there is genuinely no run for the window they are asking about.

## Choosing which candidate to back

With several candidates and one backtest's worth of patience, `screen_candidates` ranks them by how
likely each is to trade at all. One asset per call, because a strategy's signals are measured on
that asset's candles.

Read it as an ordering, not a verdict. It says nothing about whether a strategy will make money --
only a backtest answers that -- and a low score is weak evidence: measured against runs that did
trade, a 0.5 cutoff recovers under half of them. So back the top of the ranking first, and never
tell someone their strategy will not trade because this scored it low. Say which model version
ranked them when it matters; the ranking is that model's opinion, not a property of the strategies.

Screening a single strategy answers nothing worth reporting. If there is one candidate, back it.

## A return with no benchmark is not an answer

Every time you state what a strategy returned, state what holding the asset returned over the same
window. `run_backtest` already includes it in `benchmark` -- use that, don't re-derive it. For a
different asset or period, `get_benchmark`.

A strategy that returned -14.5% while the asset fell -20.5% did its job. Reporting only the -14.5%
tells the user they lost money and hides that they lost less than the alternative. Reporting only
"it beat the market" hides that they lost money. Both facts, always.

## Read the units off the result

`metric_units` travels with every result. Percent-typed metrics are on a **0-100** scale: `0.52`
means 0.52%, not 52%. Never infer the scale from how big a number looks -- that is exactly the
mistake that reported a 0.52%/yr strategy as +52%.

## A null metric is not a zero

`null` means the quantity was not measurable, and it needs a reason, not a number. Sharpe, Sortino
and Calmar are null below `ratio_sample_minimum` daily observations, because a ratio computed from
two weeks of data is noise wearing a decimal point. Say "the window is too short to compute it",
never "its Sharpe is 0".

Zero trades means there is no win rate. Say that -- and then say why, because the result
carries it: `metrics.diagnostics.entry_denials` is how many entries the engine refused and
the reasons (cooldown, principal too small), and `metrics.diagnostics.window_bars` is the
rolling window every signal was evaluated with. "It fired 150 times and every entry was
denied for cooldown" and "it never fired" are different answers, and the person deciding
what to change next needs to know which one they got.

One (strategy, window) is ONE measurement. Asking for the same strategy over the same dates
again is refused with the existing run's id -- read that with get_backtest. A new measurement
needs a different window, or different parameters, and different parameters are a different
strategy.

## The check comes before the spend

A backtest costs a unit of the monthly allowance at submission, so `run_backtest` verifies the
strategy first and a refusal costs nothing. Two kinds of refusal, and they ask different things
of you:

- **A problem** -- a fault that makes the run fail at setup. Fix it (usually by saving corrected
  rules) before asking again.
- **A signal parameter at its library `default`** -- a value nobody chose. `default` and `authored`
  are about SIGNAL PARAMETERS: `authored` means the value was deliberately set when the strategy
  was composed, `default` means it was left as the library ships it. The submission waits until
  you either save the strategy with deliberately chosen values, or pass
  `acknowledge_defaults=true` -- which is a statement to the user that you considered the defaults
  and stand by them, so say that, and why, when you do it.

A backtest always measures a strategy exactly as it is stored -- there is no way to substitute
values for one run. To try a different execution config, save it: update the draft, or save a new
strategy if this one has been measured, then backtest that. Fees and slippage are canon and cannot
be set per strategy. `costs_applied` says what was charged.

`get_execution_config_schema` before you discuss or change any parameter. It carries each one's
default, effect, bounds and status: `tunable` is strategy character, `guardrail` is account safety,
`gated` is inert unless another flag is on, `unused` has no runtime effect at all. Do not advise
tuning a `gated` or `unused` parameter as though it would change anything.

## The window is not always the one requested

Read `window`. `kind` is `explicit` when the user named the dates and `trailing` when the span was
chosen for them from the strategy's timeframe. `days_covered` is what the data actually supported,
which can be less than the range if history is short. If they asked for a year and got four months,
say so before quoting an annualised figure off it.

Pass **both** `start_date` and `end_date` or neither.

## Long windows fail rather than wait

The synchronous surface is deadline-bounded. A wide window -- a multi-month `1h` run, anything on
`1d` -- can overrun it and come back as an error naming the engine warming up. That is a real
failure, not a slow success: no metrics exist. Tell the user the window was too wide for a single
run and offer a narrower one. Never present a timed-out run as a result, and never guess what it
would have said.

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
