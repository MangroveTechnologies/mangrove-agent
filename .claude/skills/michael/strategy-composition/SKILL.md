---
name: strategy-composition
description: >-
  Build high quality trading strategies -- starting from the marketplace. Find a few listings
  that fit what the person wants and offer them before composing anything new; compose, using
  the knowledge graph to acquire relevant information, judgements, insights, and signals, only
  when nothing on the marketplace fits. Reach for it when you encounter things like : "build me
  a strategy", "make me something for ETH", "set up a mean-reversion strategy", ... (etc). Use
  strategy-management to draft, archive, or deploy.
uses-tools: [query_signal_behavior, save_strategy, update_strategy, verify_strategy, list_strategies, get_strategy, get_market_regime]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/strategy-composition/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# Build coherent and active strategies

Read the market regime, then load into your context knowledge atoms, relationships, and information that would be beneficial to
have when reasoning about strategy composition and signal & parameter selection. You must understand the parameter
values and ranges of the signals as different parameter settings have different effects on when, how often, and how long, a given signal fires or remains true. You must reason about the parameter values and their impact on the underlying signal and how that will change the firing frequency, duration, and location of the signal.

## Start with the marketplace

Composing is not the first move. Once you know what the person wants -- asset and
candle_size, at minimum -- and you have read the regime with `get_market_regime`, call
`search_marketplace` ONCE: `asset`, `candle_size`, `free_only=true` unless they have said
they will pay, `sort="sharpe"`, `limit=5`. In the same round as the regime read where you
can. Do not pass `risk_level` or any performance floor in the query, and do not call it a
second time with the filters loosened or narrowed -- fit is judged from the rows that one
call returns, never by re-querying.

Judge fit from the rows you got, not from the query. Match the person's risk appetite
against each row's `risk_level` after the fact -- that is a read of the row, not a search
filter. A row with fewer than about 20 trades, or a Sharpe above 3, is not trustworthy
evidence: too few trades make the ratio meaningless, and a clean-looking Sharpe built on a
handful of them is noise dressed as a signal. Say so plainly if you keep such a row, or
drop it, rather than presenting it as a fit on the strength of a number that is not one
yet.

Present up to three fits in one short table, numbered 1 to n in the first column: name,
asset, timeframe, risk level, the tracked performance since listing (live where it exists,
otherwise paper), subscriber count, and free or price. Say plainly why each one fits the
ask and the regime, and that the numbers are the tracked record, not the backtest -- if
you also quote a backtest figure, name it as a different window on different data, not a
second version of the same number.

Then offer, by number, and stop: "Say 1, 2 or 3 to try one in paper trading." When they
pick one, call `get_marketplace_strategy` then `propose_adopt` on it in the SAME turn --
do not ask whether to offer it first; picking a number already answered that, and asking
again spends a turn they do not owe you. Pressing the button `propose_adopt` puts up is
still the person's decision: it copies the listing into their account, where it
starts paper trading at once, and nothing happens before they press it.
One button per turn, by design, so resolve their pick to exactly one listing before you
call it. If they pick a paid listing, say so and name its marketplace page;
`propose_adopt` only ever offers a free one, and asking it for a paid listing is refused.

Compose a new strategy only when the person declines every fit, asks for something no
listing covers, or the one search came back with nothing. When it is empty, say what you
searched for -- asset, candle_size -- and that nothing fit, then move straight to
composing. Never loop back and search again with different filters; one search per turn
is the whole of the marketplace step. `list_strategies` still runs first for duplicates of
their OWN compositions; that is a different question from whether the marketplace already
has what they want, and does not replace this step.

## Work in as few rounds as the build allows

Every round -- one reply from you, with its tool calls -- costs the person seconds of waiting, and
a turn has a time budget. Calls that do not depend on each other go out together in ONE round:

1. `get_market_regime`, `search_marketplace`, `list_strategies`, `get_execution_config_schema`
   and your first `query_knowledge` (`op=ask` or `op=find`) in the same round -- the marketplace
   filters follow from what the person already told you, so there is nothing to wait on.
2. `query_signal_behavior` for every signal you are weighing, in the same round.
3. `save_strategy`, then `verify_strategy`.

Skip `op=stats` when you already know the filter values you want; it lists the vocabulary and
is only for when you do not. Do not look up a node you already have in this conversation.

## Read the regime before you choose, not after

`get_market_regime` for the asset, and let it decide what you compose. If we are in a sideways market
then don't build a trend following strategy. If what they asked for does not fit what the market is doing,
**explain why and steer them**. If they persist then you may use `regime_override=true`, but NEVER as
something you do on your own.

## Resolve every signal in the runtime catalog

Use `query_knowledge` and the signal catalog to resolve names. The runtime registry decides which signals exist; the graph describes modelled measurements. A registered but unmodelled or deprecated signal is not unknown. Report its status and reason. Do not choose a signal with `composable=false` for a new strategy; `executable=true` may preserve an existing strategy without making that signal eligible for new composition.

`op=ask` for what you want it to do -- "fires when price snaps away from its average", "confirms a
breakout has volume behind it" -- because it searches by meaning.

`op=find` when you know the words: `role="trigger"`, `kind="volatility"`, `requires="volume"`, `param="window_dev"` (every signal
taking that parameter), and they intersect. `op=stats` lists every value each filter accepts, so none of them is guessed.

## Signal Roles

Every signal is a `TRIGGER` or a `FILTER`, and which one is a property of the signal. Read
`signal_type` on its `query_knowledge` result: a boolean output can represent either an
occurrence or a condition that stays true across bars, so it does not establish the role. An entry
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
generating a high quality trading strategy on first attempt. `query_knowledge op=walk q=... radius=2` pulls a
signal's whole neighbourhood -- what it uses, what reads it, its class and role -- in one call, which is
cheaper than several rounds of `op=neighbors` when you are about to reason about more than one hop.

Every signal search result carries the signal's parameters -- each with its type, default, min and max --
so you compare candidates BY their parameters in the same pass that names them. You cannot reason
about a value you have not read, and it is already in front of you. `op=get` on the signal you
chose, before you save it, for the rest: formula, inputs, outputs, warmup.

`query_signal_behavior` tells you how a signal behaves on a real chart at different
parameter settings. Use it for EVERY signal you put in a strategy, trigger and filter
alike, and quote the numbers when you justify the choice. Pass `signal_type`: a
`FILTER` and a `TRIGGER` are measured for different things and the tool will tell you
if you asked under the wrong one.

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

**An entry needs the trigger AND every filter, so estimate the joint per-bar opportunity rate as a last check.** If a trigger fires on 5% of bars and a filter is true on 20%, multiplying gives a rough 1% joint rate only when their behavior is sufficiently independent. It is a planning estimate, not a measured joint rate; do not claim independence without joint measurements.

**Convert the joint rate with the bar timeframe.** The table uses the explicit target joint opportunity rates and continuous bars per week (7 days). Weekly values are expected trigger/filter coincidences, not executed trades: open positions, cooldowns, sizing, market availability and other constraints can reduce trades, while losses can cluster.

| timeframe | target joint rate per bar | expected joint opportunities / week |
|---|---:|---:|
| 5m | **0.25% - 3%** | 5.04 - 60.48 |
| 15m | **2% - 5%** | 13.44 - 33.60 |
| 1h | **2% - 10%** | 3.36 - 16.80 |
| 4h | **4% - 20%** | 1.68 - 8.40 |
| 1d | **10% - 40%** | 0.70 - 2.80 |

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

`constraints` is for editing the trading defaults (aka execution config). `get_execution_config_schema` says which
parameters exist and what they mean. "1% risk per trade", "max 2 positions", "reward 3:1", etc, these go here.
You must also think about this when creating a strategy as well. When looking at recent market conditions, you
can assess volatility, and determine if the volatility based stop loss configuration parameters need to be adjusted.

When a new composition inherits the platform's default cooldown, its loss-count windows are widened when needed
to cover the selected trigger's exact measured median-gap reference times the configured loss limit. This is a
trigger-only cadence reference, not a promise about joint entries or executed trades: filters and execution
constraints alter realized spacing, and losses can cluster. A cooldown explicitly supplied by the person or
inherited from a reference strategy is preserved. `verify_strategy` reports when the saved windows are narrower
than the measured reference or when the selected trigger configuration has no exact measurement; resolve those
warnings before treating cooldown behavior as validated.
That is just one example.

## Verify the strategy before backtesting

`verify_strategy` on the id you just saved. It reads what is STORED and answers whether the
strategy will run: signal names, parameter keys, roles composing, a timeframe on every signal
and on the strategy.

`conforms: false` means fix what `problems` names and save again. Do not submit a backtest to
find out -- a run spends the person's allowance the moment it is submitted, whether or not it
ever executes a bar, so a fault found here costs nothing and the same fault found by the run
costs them a measurement they never got.

`warnings` do not stop a run. They are the judgements the check cannot make for you -- a
parameter still sitting at its library default. Answer each one: change the value, or say why
the default is right for this asset and this horizon. Do not pass them over in silence.

To change a value, `update_strategy` on the draft -- pass the complete entry and exit lists,
since signals replace rather than merge. It works only while the strategy is a draft nobody
has backtested; after a backtest the rules are frozen so the results stay attached to the
rules they measured, and a change means saving a NEW strategy.
