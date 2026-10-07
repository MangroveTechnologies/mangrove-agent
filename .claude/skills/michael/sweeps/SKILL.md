---
name: sweeps
description: >-
  Create thousands of strategies across various assets, market data windows, indicators,
  parameters, and risk settings. Reach for it whenever the question is about searching
  across many possibilities rather than building something specific: "what works on ETH",
  "search for a momentum strategy", "find something that holds up in a bear market", "try
  a lot of variations", "what should I trade", ... . Reading the runs back afterwards is
  sweep-results.
uses-tools: [list_sweep_markets, build_market_window, get_sweep_limits, size_sweep, create_sweep, launch_sweep, get_sweep, pause_sweep]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/sweeps/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# The engine builds the candidates, not you

A backtest asks what one strategy did. A sweep asks what a whole space of them does. You
do not write the candidates and you do not hand over a list. The engine draws each run's
strategy itself according to the settings of the experiment, such as what market windows to
use, what assets to test, what signals to use, what risk management parameters to test,
and other various knobs and dials.

The decisions a user makes are:
  1. What asset(s) to choose from. Present the following options only when the person
     has not already answered this:
    a. doesn't matter
    b. popular ones (BTC, ETH, SOL, XRP, DOGE)
    c. choose myself (list them, they will be verified against our approved asset list)
     If they say "all markets available in the local catalog", "everything you cover",
     "doesn't matter", or equivalent, that is already an asset choice. Do not ask them
     to name assets or pick from the menu again. Call `list_sweep_markets` without an
     `assets` filter (while applying any regimes/timeframes they already specified),
     then use every returned asset with at least one matching window in `size_sweep`
     and, if later requested, `create_sweep`. `list_sweep_markets` omitted `assets`
     means every catalog asset; `size_sweep` does not infer that choice when its
     required asset list is omitted.
  2. What market conditions to test. Ask only if they have not already said. Pick as
     many as they like, and offer only choices the catalog returns for their selection.
     Use `list_sweep_markets` to get available regimes and eras for the selected assets.
     A request for "current" means `regimes=["current"]`; it is already answered and
     needs no follow-up question. A request to use the local catalog without building
     windows means omit `windows` and do not call `build_market_window`.
     Market regimes:
    a. current (windows with data in the last 30 days)
    b. bear (falling: bear, super_bear, mega_bear)
    c. neutral (sideways)
    d. bull (rising: bull, super_bull, mega_bull)
    e. doesn't matter (every condition)
     Market eras:
    f. covid-bull-alt-season
    g. post-ftx-winter
    h. post-halving
    i. crypto-winter-1
    j. pre-halving-run
    k. pre-covid-accumulation
    l. luna-to-ftx
    m. pre-peak-bull-2025
    n. bear-market-2
    o. peak-plus-correction
    p. election-rally
    q. covid-crash-recovery
    r. current
    s. mania-1
     And/or:
    t. a specific window and timeframe (build one from market data)
  3. What do you want the strategies built from? Pick as many as you like.
    a. specific indicators (they name them; resolve every name through the graph,
    use fuzzy/semantic matching with `query_knowledge`)
    b. specific candlestick patterns (the pattern class, same instructions as above)
    c. indicator types (averaging, flow, momentum, oscillator, pattern, volatility)
    d. mix and match (signal groups)
    e. I don't know what I want (the engine explores the whole library)
  4. What do you want to try different values for? Anything not picked stays at its
     proven default.
    a. risk/reward ratio
    b. stop loss distance
    c. max risk per trade
    d. cooldown and circuit breakers
    e. advanced configuration (every execution parameter)
    f. nothing
  5. Review and launch, only if they asked to create or launch.
    a. name the experiment, give it a description.
    b. confirm the launch
    c. launch when confirmed

## Ask the graph, never your memory

Every signal name and every class comes from the knowledge graph, through
`query_knowledge`. A name you did not read out of it is a name you invented, and
Oracle refuses it. The classes are averaging, flow, momentum, oscillator, pattern
and volatility; the roles are trigger and filter, and a role is a fact about a
signal rather than a choice, so a filter cannot be used as a trigger.

When someone describes what they want rather than naming it -- "something that
fires when price snaps back to its average" -- that is `op=ask`. When they give
you a word, that is `op=find`.

For a category-based sweep, resolve the requested category names without enumerating
all their signals. Pass the categories to `size_sweep`; Oracle expands and validates
the pools. Read individual signal definitions only when the user selects signals or
asks about their behavior. Reuse a completed lookup in the current logical turn;
recall its recorded result if it has left context.

A group's `categories` and `include` define its whole membership. Optional `triggers`
and `filters` each accept `categories`, `include`, and `exclude` and narrow that
membership for the respective role. Put every selected category in the parent group
as well. For example, momentum triggers with averaging filters use:
`{"name":"momentum with averaging","categories":["momentum","averaging"],
"triggers":{"categories":["momentum"]},"filters":{"categories":["averaging"]}}`.
These selections never turn a filter signal into a trigger. A group with more than one
category must set `triggers` and/or `filters`, taken from what the person asked for (ask
them which categories trigger and which filter if unclear); `size_sweep` and
`create_sweep` refuse it otherwise. Preserve the same group
object from sizing through creation.

## Offer only what their assets actually cover

`list_sweep_markets` after an asset subset is chosen, and again after each answer that
changes the selection. If the person already selected every local catalog market, call
it without `assets` to enumerate that complete eligible universe; do not block on an
asset clarification. Filter the returned asset choices by the stated regimes and candle
sizes, then pass all assets with matching windows to `size_sweep` because that tool
requires an explicit nonempty asset list.
It returns the choices with the windows each would ADD, counted against
everything already picked, so an option that covers nothing is never offered and
a person is never shown a market their assets do not have.

Windows with missing bars are left out. If they ask for them back, say what that
means: a metric over a half-covered window is not comparable to one over a whole
window.

## When the catalog does not cover what they asked for

`build_market_window` cuts a window and adds it, for a range the catalog has no
window over. Candle sizes 5m through 1d, any date range, up to 5,000 bars.

```
build_market_window  asset=BTC start_date=2026-06-01 end_date=2026-09-01 candle_size=1h
build_market_window  asset=ETH start_date=2026-06-01 end_date=2026-09-01 source=kraken
```

Call `list_sweep_markets` first. A window that already exists comes back rather
than being refetched, and telling someone you built what they already had is
worse than saying it was there.

A longer range is shortened to the first 5,000 bars, not refused, and the
response says so. Read the dates it returns rather than repeating the ones asked
for: they describe the bars that were stored.

`source` picks the tape. `mangrove` is the instrument-level series. `kraken` is
that venue's own book, and the two carry different volume for the same minute,
because volume belongs to the venue's own liquidity. A strategy meant to execute
on Kraken has to be measured on kraken windows.

It spends, so it is one call per window and not a way to explore. Say what you
are about to build and why the catalog cannot answer it already.

## The number they say is not the number that runs

This is the single thing that goes wrong, and it goes wrong in one direction:
the real size is always larger. The draw count is PER WINDOW, and one asset at
one candle size is many windows.

**You do not work it out. `size_sweep` asks Oracle**, whose counter walks exactly
what the plan walks. It creates nothing, so call it after every change while the
size is being settled. Report `executed_runs` and nothing else as the size.

`possible_runs` comes back beside it and is usually astronomically larger,
because the space counts parameter values and not just which signals were
picked. Quote it only to show what is being sampled from; it is not a cost.

If `capped` comes back true their plan scaled the search down, and `resolved`
says what it settled on. Say that in plan terms rather than repeating the number
they asked for. `get_sweep_limits` says what their plan actually allows.

The levers, when it is too big: fewer draws, one candle size instead of several,
fewer assets, a smaller `param_samples`, or a ceiling. A ceiling has a floor --
Oracle will not run fewer than one draw per window.

If the person gives a total-run cap, pass it as `max_total_runs` on every `size_sweep`
and `create_sweep` call, including when `get_sweep_limits` reports zero or -1. Those
values mean the plan has no enforced cap; they do not remove the person's requested
cap. The sized `executed_runs` is the number Oracle says would run. If Oracle's minimum
of one draw per window means it cannot honor the requested cap, report that exact size and do
not create or launch until the person chooses what to narrow.

When the person has already given the market selection, regimes, candle sizes, signal
groups, execution axes, and a run cap, translate those choices into `list_sweep_markets`,
`get_sweep_limits`, and `size_sweep` calls and size it immediately. Ask only for an input
that is genuinely missing. Sizing is read-only; it does not need launch confirmation.

Setting a search up costs nothing. Getting it wrong twice is fine. If
`create_sweep` comes back invalid, relay Oracle's own words rather than
paraphrasing a validation error into a guess.

## Launching is the only thing that spends

Say the run count and get an answer before calling `launch_sweep`. A total the
person never saw is not a total they agreed to. This is not a formality: the gap
between "try a few hundred" and what the arithmetic produces is the whole reason
to check.

`preparing` and `queued` both mean it started. Queued means the account already
has a sweep in flight and this one begins when a slot frees. It is not a refusal
and there is nothing to retry.

It returns immediately and with no results. Say it is running, say how big it
is, and stop. Do not poll in a loop waiting for the answer.

`pause_sweep` stops further work and keeps everything already written, and says
how many runs finished, which is what they keep. A sweep that was only queued
goes back to validated, because it never started, and can be launched again.

## Do not

- **Do not name a signal or a class from memory.** Ask the graph.
- **Do not omit market conditions from the selection.** Use the person's stated
  conditions when given; ask only when that input is genuinely missing.
- **Do not work out the run count.** `size_sweep` asks the only thing that knows.
- **Do not launch a total they have not seen.**
- **Do not turn a named signal into a backtest.** They asked for a search.
- **Do not poll.** Read progress when they ask.
