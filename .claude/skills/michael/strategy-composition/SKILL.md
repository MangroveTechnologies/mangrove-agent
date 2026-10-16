---
name: strategy-composition
description: >-
  Build a trading strategy, by one of three routes: search the Strategy Engine's completed
  runs for one that fits the current market regime and backtest it on current data; build a
  fresh one from the current regime and the knowledge graph; or adopt a listing from the
  marketplace. A vague ask -- "build me a strategy", "build me a BTC strategy on 1h", "make me
  something for ETH" -- starts in the Strategy Engine. Reach for it on those and on "set up a
  mean-reversion strategy", ... (etc). Use strategy-management to draft, archive, or deploy.
uses-tools: [evaluate_signal, evaluate_multiple_signals_series, validate_signal_params, list_signal_labels, list_playground_datasets, get_playground_data, find_sweep_runs, get_sweep_run, run_backtest, search_marketplace, get_marketplace_strategy, propose_adopt, query_signal_behavior, save_strategy, update_strategy, verify_strategy, list_strategies, get_strategy, get_market_regime]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/strategy-composition/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# Build coherent and active strategies

Read the market regime first, every route. Then load into your context knowledge atoms, relationships, and information that would be beneficial to
have when reasoning about strategy composition and signal & parameter selection. You must understand the parameter
values and ranges of the signals as different parameter settings have different effects on when, how often, and how long, a given signal fires or remains true. You must reason about the parameter values and their impact on the underlying signal and how that will change the firing frequency, duration, and location of the signal.

## Three routes to a strategy

1. **The Strategy Engine.** Search its completed runs for one that already did well in the
   current regime, save it, and backtest it on current market data. The default for a
   vague request.
2. **A fresh build.** Compose a new strategy from the current regime and the knowledge
   graph. Taken when the person names signals or a style, asks for something new, or the
   engine search came back with nothing that fits.
3. **The marketplace.** Adopt a strategy someone already listed. Taken when the person asks
   for it: "something ready-made", "what is on the marketplace", "something I can
   subscribe to".

There is no fourth route.

## A vague request starts in the Strategy Engine

A request that says little more than an asset -- "build me a BTC strategy on 1h", "make me
something for ETH" -- goes to the engine first. Run steps 1 to 5 in ONE turn, without
stopping to ask: the person asked for a strategy, and the search, the save and the backtest
are how you give them one. "Say continue" spends a turn they did not ask to spend.

1. `get_market_regime` for the asset with `lookback_days=90`. Record its `asset`, `timeframe`,
   `classification_venue`, `source_venue`, `history_start`, `history_end`, exact history
   `history_start_timestamp`, `history_end_timestamp`, `as_of`, `as_of_timestamp` when
   available, `gating_horizon_days`, and `gating_band`; these identify the interval, model
   venue, actual source when returned, and closes classified. Keep legacy `venue` as the
   regime-classification label; never infer the provider from it. Use `gating_band` for `save_strategy`'s
   direction-only entry gate. `window.direction` is also direction-only. The complete
   current-window label is `window.direction` + `window.volatility` + `window.trend`; do not
   describe either direction field alone as a full-composite match.
2. `find_sweep_runs` ONCE, across every sweep (leave `experiment_id` out): `asset`,
   `min_trades=11`, `min_sharpe=1`, `min_irr=10`, `beats_benchmarks=true`,
   `market_direction` = `window.direction`, `regime_direction` = `gating_band`,
   `sort_by="irr_annualized"`, `limit=50`. Add `candle_size` only when the person named one.
   First inspect the available MCP tool catalog. If `find_sweep_runs` is not advertised,
   do not call the missing tool or assume the source snapshot's tool count matches the
   deployed catalog. Use the documented SDK or REST search path if it is available to
   this caller, and paginate until the full matching set is covered; otherwise state that
   the sweep search is unavailable in this environment.
   This is a **direction-only** historical match. It does not match the current window's
   volatility or trend. To answer a request for full-composite matches, use the separate
   `regime_composite` filter with the three current `window` fields joined by underscores,
   and label that narrower result full-composite. Never silently substitute one meaning for
   the other. For either query, search every experiment by omitting `experiment_id`, then
   request every page: advance `offset` by the returned row count until the cumulative count
   equals `total_matching`. Report the exact total and any unresolved or unavailable rows;
   do not pick from only the first page.
3. Keep only the rows whose `regime_fit.fits` is true. It is the save's own regime check,
   plus the archetype map's rule that a bearish `role: exit` trigger is not a long entry,
   already applied to every row -- so do not look up signals one by one to decide it. Then
   pick one at random: when the person named a candle size, from the rows on it; when they
   did not, choose a candle size at random from those the kept rows carry, then a row on it
   at random. It is a pick, not a ranking: never call
   it the best or the top one.
4. `save_strategy` with `source_experiment_id`, `source_run_index` and
   `source_config_hash` set to that row's own `experiment_id`, `run_index` and
   `config_hash`, and `regime_direction` = `gating_band`. The save reloads the
   run from those three itself, and the row already carries its rules for you to describe,
   so do not call `get_sweep_run` first, never call `list_sweeps`, `get_sweep` or
   `find_sweep_runs` again to look a row up, and never guess an experiment id. Never
   retype the row's signals into `entry_signals` / `exit_signals`: saving from the source
   keeps the run's exact settings. If it comes back `created: false`, the person already
   has this strategy: say it is their existing draft, by name, rather than presenting it
   as new.
5. `run_backtest` on the saved id over the last 90 days ending today -- the last 30 days
   for 5m candles, because one backtest is capped at 10,000 bars -- so the result is a
   measurement on current market data and not the window the engine ran. It checks the
   strategy before it spends anything, so a separate `verify_strategy` round is not needed
   here. If it is refused because the price series ends earlier, end the window on the
   series' last bar and submit again in the same turn.

A turn has a time budget of a few rounds, and these five steps fit in it only when calls
that do not depend on each other go out together: `get_market_regime` with
`list_strategies` in the first round, then the search, then the save, then the backtest.

Say what you did in one short paragraph: the regime you read, that you searched the
engine's completed runs with those floors, the run you picked and its entry and exit
rules, and that the backtest is running. The engine row's numbers are the old window's;
the backtest is the current one, and you name them as different.

If the search comes back empty, or no row's entry fits the regime, say what you searched
for -- asset, the regime, the floors -- and that nothing met them, then move straight to a
fresh build. Never search again with the floors loosened.

## The marketplace route

When the person asks for the marketplace, call `search_marketplace` ONCE: `asset`,
`candle_size`, `free_only=true` unless they have said they will pay, `sort="sharpe"`,
`limit=5`. Do not pass `risk_level` or any performance floor in the query, and do not call
it a second time with the filters loosened or narrowed -- fit is judged from the rows that
one call returns, never by re-querying.

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

If nothing fits, say what you searched for -- asset, candle_size -- and that nothing fit,
and offer the other two routes. Never loop back and search again with different filters;
one search per turn is the whole of the marketplace step.

## Work in as few rounds as the build allows

Every round -- one reply from you, with its tool calls -- costs the person seconds of waiting, and
a turn has a time budget. Calls that do not depend on each other go out together in ONE round:

1. For a fresh build: `get_market_regime`, `list_strategies`, `get_execution_config_schema`
   and your first `query_knowledge` (`op=ask` or `op=find`) in the same round -- none of them
   waits on another.
2. `query_signal_behavior` for every signal you are weighing, in the same round.
3. `save_strategy`, then `verify_strategy`.

Skip `op=stats` when you already know the filter values you want; it lists the vocabulary and
is only for when you do not. Do not look up a node you already have in this conversation.

## Read the regime before you choose, not after

`get_market_regime` for the asset with `lookback_days=90`, and let it decide what you
compose. The direction gate for a build is `direction["90d"].band`, the 90-day direction.
Pass it to `save_strategy` as `regime_direction` every time: without it the regime check is
skipped, and a strategy composed against the wrong regime gets saved.

`save_strategy` refuses an entry whose trigger's archetype the regime does not admit. This
book is long-only, so the admitted archetypes are:

| 90-day direction | entry archetypes admitted |
|---|---|
| bull, super_bull, mega_bull | trend_following, momentum, breakout |
| neutral | mean_reversion, breakout |
| bear | mean_reversion, breakout (long dip-buys only) |
| super_bear, mega_bear | none -- stand aside |

Choose the trigger from an admitted archetype BEFORE you save -- `signal_archetype_map.yaml`
gives each signal's archetype. Trend-following in a bear is a short, which this book does
not take, and mean-reversion fails in a strong up-trend. In super_bear or mega_bear, say
the market is falling hard and that standing aside is the answer, rather than forcing a
long.

The build gate is the 90-day direction. `window.direction`, `window.volatility` and
`window.trend` describe the full 90-day composite separately. Do not call a direction-only
result a full-composite match.

If what they asked for does not fit what the market is doing, **explain why and steer
them**. If they persist then you may use `regime_override=true`, but NEVER as something you
do on your own. If `save_strategy` refuses on the regime, tell them in plain words what the
market is doing and why the entry does not fit, then recompose with an admitted archetype
or ask whether to override.

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

**Every signal has three things to ask about, and both signal types report all three:
how much it acts, how long each episode lasts, and how long it waits in between.** They
come apart -- settings that cover the chart identically, or fire at the same rate, still
behave completely differently. How much it acts is `selectivity` for a `FILTER` and
`per_1000_bars` for a `TRIGGER`. How long an episode lasts is `mean_run_bars` for a
`FILTER` and `stays_true_bars` for a `TRIGGER`, which is one bar unless the signal
reports a state. How long it waits is `mean_gap_bars` for both, with the median, tightest
and longest gap also given for a `TRIGGER`. The spacing is also a search constraint you
pass to `pick`.

**`runs` is the number of chances to enter, and it is not `fired_bars`.** For a signal
true on more than one bar the two differ, which is the case for the candlestick patterns.
Size a search on `runs`, not on the rate alone, or you will count bars instead of
opportunities.

**For a FILTER, ask how often, for how long, and how long between.** A filter is an
ongoing state, so ask how often it is true (`selectivity`, 0 to 1), how long it stays true
once it turns (`mean_run_bars`), and how long it waits between episodes
(`mean_gap_bars`). A filter that is true a fifth of the time in one long stretch and one
that is true a fifth of the time in two hundred short ones suit completely different
strategies.

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
`mean_gap_bars`, `shortest_gap_bars` and `longest_gap_bars`.

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
carried over from a Strategy Engine run is preserved. `verify_strategy` reports when the saved windows are narrower
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

## Check individual signals before composing

Use `list_signal_labels` to resolve display labels to registered names. Use
`validate_signal_params` to check a signal's parameter object before evaluating it;
validation alone does not run market data.

For supplied bars, use `evaluate_signal` with the signal name, parameters and market
bar objects. For a combined check, use `evaluate_multiple_signals_series` with a
`signals` array of objects containing `name`, `params` and optional `timeframe`.
Choose either a bundled `dataset_key` or a live symbol with start and end dates.
Discover bundled keys with `list_playground_datasets`; fetch their bars with
`get_playground_data` only when the user needs the data itself. A combined evaluation
can use the key directly without a separate dataset fetch.

Report each series' success or error, bars checked and matching-bar count from the
returned result. Do not treat an HTTP success as proof that every series succeeded.
Leave out chart data unless requested. Signal matches are not trades or strategy
returns; use a backtest for those questions. Use `query_signal_behavior` for existing
historical coverage measurements rather than inferring coverage from descriptions.

## Preserve or confirm the market

Use a supplied or recorded venue and quote token without asking the person to choose again.
They can optionally inspect alternatives with `list_backtest_markets` and select another.
If either value is missing, match available markets while retaining any supplied value,
propose a match, and wait for the person to confirm or choose before saving or backtesting.
Never treat an inferred suggestion as their consent, or silently substitute an unavailable market.
