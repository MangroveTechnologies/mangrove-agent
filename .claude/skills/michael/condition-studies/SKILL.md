---
name: condition-studies
description: >-
  Answer "how would this strategy do in a bear market" by running it, unchanged, across real
  historical windows of that market and reporting every one. Reach for it whenever the question
  names a kind of market rather than a date range: "how does this hold up in a downturn", "what
  about high volatility", "would this have survived the FTX winter", "is this only good in a bull
  market", "how does it do when things are choppy". Distinct from a backtest, which answers one
  window, and from a sweep, which searches for a strategy. Covers what markets the catalog can
  cover, what a question would run on before it is paid for, and reading the answer back. Uses
  list_market_conditions, preview_condition_study, run_condition_study and get_condition_study.
uses-tools: []
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/condition-studies/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# The strategy does not change; only the market does

A condition study takes one saved strategy and runs it on each of several historical windows that
carry the same market label. The rules, the parameters and the execution config are identical on
every window. Nothing is fitted, nothing is searched, and the windows are chosen by their label
before any backtest runs.

That is the whole reason the answer is worth anything, and it is worth saying out loud when you
report one. A backtest that has been tuned until it looked good says nothing about the future. A
fixed strategy measured across ten separate markets says how it behaved in those markets.

It is not a sweep. A sweep searches thousands of strategies for one that works; a study asks what
one strategy already does. If the person wants "find me something that works in a bear market",
that is `/sweeps`, not this.

## Look before you spend

`list_market_conditions` and `preview_condition_study` are free. `run_condition_study` spends one
backtest per window.

The catalog holds a different amount of history for every asset, and the amount is not guessable.
ETH at 1h has 76 bear-labelled windows; PAXG at 1h has none at all, because it tracks gold and
never fell hard enough to be labelled one. Preview first whenever there is any doubt, and tell the
person what you found before asking whether to run it:

> There are 76 bear-market windows for ETH on the 1h timeframe, and I'll run it across 10 of them —
> 2017 through 2026, with buy-and-hold returns from −15% to −88%. That spends 10 backtests. Go ahead?

When the preview comes back with fewer windows than were asked for, **that is the answer, and you
say it**. The set is never padded to a round number, and "we found four" is a real finding about
how much history exists — not a shortfall to paper over.

When it comes back with none, say so plainly and say why if you can see it. Do not substitute a
different condition and report it as though it were the one asked for.

## A strategy that runs at two timeframes is refused, not rewritten

A study injects one candle size and its daily companion into the engine. A signal asking for a
third finds no data. Rewriting the signal's timeframe to fit would run a different strategy from
the one the person asked about and report the result as theirs, so the tools refuse instead and
name the timeframes involved. Relay that: it is a real limitation with a real reason, not a glitch
to retry.

## Report the spread, never just the middle

The windows are different markets. The point of running ten of them is that they disagree, so an
answer that collapses to one number has thrown away what was bought.

Lead with how many windows were profitable, out of how many. Then the median, then **the worst
window**, named with its dates. The worst window is usually the substance of the answer: a strategy
whose median is −20% but whose floor is −100% is not a strategy that loses 20%.

Always carry the buy-and-hold comparison. A −40% return in a market that fell 60% is a different
fact from a −40% return in a market that fell 10%, and only the comparison distinguishes them.
`beat_buy_and_hold_windows` is in the aggregate for this reason.

Percent-typed values are on a 0–100 scale: 0.7 means 0.7 percent. Never rescale them, and never
infer the scale from the magnitude.

## A strategy that never traded has not been measured

If `windows_with_no_trades` is non-zero, the study carries a `verdict`. Relay it.

A strategy that places no trades returns 0%. In a market that fell 45%, 0% "beats" buy-and-hold in
every window, and the aggregate will say so truthfully — `beat_buy_and_hold_fraction: 1.0` — while
meaning nothing at all. Cash sitting still is not skill. Say that the strategy did not fire, and
treat it as a finding about when it fires rather than about how it performs.

## Reading the answer back

`run_condition_study` returns a submission, not a result: the windows run in parallel and take a
minute or two. Tell the person it is running. Do not call it twice for the same question.

`get_condition_study` is safe to poll, and `windows_run` against `windows_planned` says how far
along it is. Partial results are real results — the windows that finished are the windows that
finished — but say which they are rather than presenting a half-finished study as the whole thing.
