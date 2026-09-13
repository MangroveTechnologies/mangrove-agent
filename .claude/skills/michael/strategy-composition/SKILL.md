---
name: strategy-composition
description: >-
  Build high quality trading strategies using the knowledge graph to acquire relevant information,
  judgements, insights, and signals to construct high quality trading strategies. Reach for it when you 
  encounter things like : "build me a strategy", "make me something for ETH", "set up a mean-reversion 
  strategy", ... (etc). Use strategy-management to draft, archive, or deploy.
uses-tools: [query_knowledge, create_strategy_manual, list_strategies, get_strategy, update_strategy_status]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/strategy-composition/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# Build coherent and active strategies

Read the market regime, then load into your context knowledge atoms, relationships, and information that would be beneficial to 
have when reasoning about strategy composition and signal & parameter selection. You must understand the parameter 
values and ranges of the signals as different parameter settings have different effects on when, how often, and how long, a given signal fires or remains true. You must reason about the parameter values and their impact on the underlying signal and how that will change the firing frequency, duration, and location of the signal. 

## Read the regime before you choose, not after

`get_market_regime` for the asset, and let it decide what you compose. If we are in a sideways market
then don't build a trend following strategy. If what they asked for does not fit what the market is doing, 
**explain why and steer them**. If they persist then you may build what they asked for (nothing here enforces a regime check), but NEVER as 
something you do on your own. 

> **Not in mangrove-agent yet: `get_market_regime`.** Read direction and volatility yourself: `get_ohlcv` daily closes over 90, 180 and 365 days (returns, and realised volatility against the asset's own longer history), plus `get_market_data` for today. Say the reading is yours, not a platform regime label.

## Every signal name comes from the knowledge graph

`query_knowledge` first, always. A signal name you did not read out of the graph is a name you invented, and will not
work, period.

`op=ask` for what you want it to do -- "fires when price snaps away from its average", "confirms a
breakout has volume behind it" -- because it searches by meaning. 

`op=find` when you know the words: `role="trigger"`, `kind="volatility"`, `requires="volume"`, `param="window_dev"` (every signal 
taking that parameter), and they intersect. `op=stats` lists every value each filter accepts, so none of them is guessed.

## Signal Roles

Every signal is a `TRIGGER` or a `FILTER`, and which one is a property of the signal. An entry 
needs **exactly one `TRIGGER`** -- the thing that fires the trade -- plus one `FILTER`s per the 
filter-count rule. Do not relabel the signal roles or attempt to alter anything about the role
of a signal. Stacking multiple `FILTER` signals compounds restriction - two filters at 0.5 
admit roughly a fourth of the chart. Check each one before stacking, not after the backtest returns no trades. 
Never select more than 1 entry filter, and generally only select one unless specifically asked by a user. 

## Archetype is what the regime check reads

A signal's CLASS is what it measures -- momentum, volatility, averaging -- and lives in the graph.
Its ARCHETYPE is which kind of strategy it belongs to, and it is a different fact, in a different
place: `${CLAUDE_PLUGIN_ROOT}/.claude/skills/michael/strategy-composition/signal_archetype_map.yaml` (read that file). Read it before composing, but note that 
pattern signals are not included - that does NOT mean they are invalid. You may use pattern signals as well.

What the map tells you that nothing else does:

- **The trigger's archetype is the strategy's.** Filters give context; the trigger is what the
  strategy IS.
- **Some archetypes are decided by a parameter**, so choosing the parameter chooses whether the
  strategy can be saved at all. The map's `conditional_resolution` names them.
- **Some compositions are incoherent** and must not be offered, however good each half looks
  alone. The map's `rules` name those too, non-exhaustively.

## High quality strategies require optimal parameter settings

**Query connected nodes and related concepts** PRIOR to making a decision on what the parameter values should be.
This will potentially enable you to make better decisions with more information, improving the probability of 
generating a high quality trading strategy on first attempt. 

Every signal search result carries the signal's parameters -- each with its type, default, min and max --
so you compare candidates BY their parameters in the same pass that names them. You cannot reason
about a value you have not read, and it is already in front of you. `op=get` on the signal you
chose, before you save it, for the rest: formula, inputs, outputs, warmup.

`query_signal_behavior` tells you how a signal behaves on a real chart at different
parameter settings. Use it for EVERY signal you put in a strategy, trigger and filter
alike, and quote the numbers when you justify the choice. Pass `signal_type`: a
`FILTER` and a `TRIGGER` are measured for different things and the tool will tell you
if you asked under the wrong one.

> **Not in mangrove-agent yet: `query_signal_behavior`.** No measured firing rates or gap distributions are available. Read each signal's params, ranges and warmup with `query_knowledge` op=get, reason about frequency from those, say that the rates are estimates, and let a backtest's `total_trades` be the measurement.

**Every signal has two things to ask about: how much it acts, and how that action is
spread out over the chart.** The two come apart -- settings that cover the chart
identically, or fire at the same rate, still behave completely differently. Ask for
both. For a `FILTER` that is `selectivity` and the length of its true stretches; for a
`TRIGGER` it is `per_1000_bars` and the spacing between firings. The second of each pair
is a search constraint you pass to `pick`, not a property the signal has.

**For a FILTER, ask how often and for how long.** A filter is an ongoing state, so ask how often it
is true (`selectivity`, 0 to 1) and how long it stays true once it turns.

Neither extreme announces itself: a filter true on 99% of bars admits almost every bar,
one true on 0% admits none, and both return a clean boolean with no error. Read the
number and say what it does to the strategy. `describe` gives the filter's measured span
and what its declared default comes out at, which is not always what the name suggests:
`ulcer_low_risk` at its default is true on 99.2% of bars.

**The window decides duration.** The same base rate (percentage of the time a signal
evaluates to true) arrives either as brief blips or as sustained stretches. At a base
rate of 0.19, `rsi_oversold` at window 2 fires 1,267 times for about 2 bars each; at
window 100 it fires 178 times for about 14 bars -- identical coverage of the chart,
different behavior. `max_run_bars` caps how long the average stretch of true lasts
(short blips -- an event marker), `min_run_bars` floors it (sustained stretches -- a
regime gate). Pass whichever bound expresses the shape, then let the threshold set how
much of the chart it covers.

**For a TRIGGER, ask how often it fires and how those firings are spaced.** A trigger
marks a single bar, so it is measured differently.

**Activity is `per_1000_bars` -- a rate, not a fraction of the chart -- and it sets how
often the strategy gets a chance to act.** At their defaults the library spans two
orders of magnitude. `three_white_soldiers_trigger` fires 4.11 times per 1,000 bars and
`bop_cross_up` fires 257.2. Convert the rate into events over the period the user cares 
about and state that number, so trade frequency is part of the decision. 

**Reactivity is how those firings are spaced.** Constrain it with `min_gap_bars` and
`max_gap_bars` to specify the frequency and spacing between firings.

**Read reactivity as a distribution rather than an average.** Two settings can fire at
the same rate and space their firings completely differently. `describe` gives the
median, shortest, longest and tenth-percentile gap, as `median_gap_bars`,
`shortest_gap_bars` and `longest_gap_bars`.

`min_gap_bars` and `max_gap_bars` select on the median, so they shape typical spacing
and thin out clustered settings, but put no floor on the shortest gap: a setting can
clear `min_gap_bars=20` and still fire on two consecutive bars. When the strategy needs
firings that never bunch, `gap_probability` is the check -- for a range of gap lengths
you name, what share of that setting's gaps fall inside it. Ask it when the strategy
cares about a particular spacing rather than a typical one -- for instance how often a
signal goes quiet for five to fifteen bars, which is the window a swing entry has to
work in.

**An entry needs the trigger AND the filter, so multiply across them as a last check.** 
A trigger firing on 5% of bars behind a filter true on 20% of them leaves about 1% of bars carrying an entry.

**Convert it with the bar timeframe, because the same percentage does not mean the same frequency.** 1% of 1h bars is 1.7 entries a week. 1% of 5m bars is 20.2 entries a week. Use the following chart for reference:

| timeframe | target compound rate|
|---|---:|
| 5m | **0.25% - 3%** |
| 15m | **0.50% - 5%** |
| 1h | **2% - 10%** |
| 4h | **4% - 20%** per month |
| 1d | **10 - 40%** per month |

## Find out what they already have first

`list_strategies` before composing anything and make sure you do not build a duplicate or near-duplicate strategy.

## Every signal has its own timeframe

Give each signal a `timeframe`. A signal without one cannot be evaluated -- `Strategy` reads the
field directly -- and a strategy saved without it fails its first backtest with `'timeframe'`
rather than being refused when you saved it.

Different timeframes across signals is a **supported composition**. A strategy carries the set its 
signals need, and a 15m entry under a 1h trend filter is often the better shape -- the trigger times 
the entry, the higher timeframe says whether to take it. This is NOT a rule that you must use a higher
timeframe for `FILTER` signals, you *can* if it makes sense and if you can justify why. 

## Execution and risk management settings

`execution_config` on `create_strategy_manual` is for editing the trading defaults. `get_execution_config_schema` says which 
parameters exist and what they mean. "1% risk per trade", "max 2 positions", "reward 3:1", etc, these go here. 
You must also think about this when creating a strategy as well. When looking at recent market conditions, you 
can assess volatility, and determine if the volatility based stop loss configuration parameters need to be adjusted. 
That is just one example. 

> **Not in mangrove-agent yet: `get_execution_config_schema`.** There is no parameter glossary here. The execution config a strategy runs with (canonical trading defaults merged with its overrides) is on `get_strategy`; describe only parameters you can read there, and do not claim defaults, bounds or effects you have not read.

## Check the strategy before backtesting

There is no separate verification call in this agent. `create_strategy_manual` checks the
composition when it creates the strategy and refuses what it cannot run; read the error and fix
what it names. Then read what is STORED with `get_strategy`: every signal name as the graph has
it, every parameter key among that signal's `params`, and a `timeframe` on every signal. A fault
found here costs nothing; the same fault found by a backtest costs the person a measurement they
never got.

A parameter still sitting at its library default is a judgement you owe the user: change the value,
or say why the default is right for this asset and this horizon. Do not pass it over in silence.

Strategies are not edited in place here. To change a value, create a new strategy with the complete
corrected entry and exit lists, and archive the superseded one with `update_strategy_status`
`status="archived"` -- so every backtest stays attached to the rules it measured.
