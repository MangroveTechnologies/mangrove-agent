---
name: sweeps
description: >-
  Create thousands of strategies across various assets, market data windows, indicators, 
  parameters, and risk settings. Reach for it whenever the question is about searching 
  across many possibilities rather than building something specific: "what works on ETH", 
  "search for a momentum strategy", "find something that holds up in a bear market", "try 
  a lot of variations", "what should I trade", ... . Reading the runs back afterwards is
  sweep-results.
uses-tools: [query_knowledge, oracle_list_datasets, oracle_validate_experiment, oracle_create_experiment, oracle_launch_experiment, oracle_get_experiment, oracle_pause_experiment, oracle_list_signals, oracle_update_experiment, backtest_strategy]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/sweeps/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# The engine builds the candidates, not you

A backtest asks what one strategy did. A sweep asks what a whole space of them does. You
do not write the candidates and you do not hand over a list. The engine draws each run's
strategy itself according to the settings of the experiment, such as what market windows to 
use, what assets to test, what signals to use, what risk management parameters to test, 
and other various knobs and dials. 

The decisions a user makes are:
  1. What asset(s) to choose from. Present the following options:
    a. doesn't matter
    b. popular ones (BTC, ETH, SOL, XRP, DOGE)
    c. choose myself (list them, they will be verified against our approved asset list)
  2. (only offer what their assets actually cover, after asset selection, use `oracle_list_datasets` to 
  get the market regimes and eras that the selected asset(s) cover(s)) What market 
  conditions do you want to test in? Pick as many as you like. 
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
    use fuzzy/semantic matching with `query_knowledge` `op=ask`)
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
  5. Review and launch.
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

## Offer only what their assets actually cover

`oracle_list_datasets` after the assets are chosen. It returns the whole catalog -- one row per
window, each with its `asset`, `timeframe`, date span, and the labels the catalog's classifier gave
it (`direction`, `volatility`, `trend`, `regime_composite`, `market_era`) -- so filter it to the chosen
assets yourself, and offer only the regimes, eras and candle sizes those rows carry, with how many
windows each choice would add. An option that covers nothing is never offered, and a person is never
shown a market their assets do not have.

Rows with `coverage_ok: false` have missing bars; leave them out. If they ask for them back, say what
that means: a metric over a half-covered window is not comparable to one over a whole window.

The experiment config takes the chosen rows as whole dataset objects; the `sweep` skill has the
config shape, the signal-pool fields and the SIEVE pre-filter setting.

## When the catalog does not cover what they asked for

This agent cannot cut a new window into the catalog. Say plainly that the catalog holds no window for
that range and offer the nearest windows it does hold. If they need exactly that range, a sweep is the
wrong tool: backtest a saved strategy over it with `backtest_strategy`.

## The number they say is not the number that runs

This is the single thing that goes wrong, and it goes wrong in one direction: the real size is always
larger. The draw count is PER WINDOW, and one asset at one candle size is many windows.

**You do not work it out.** Create the draft with `oracle_create_experiment` -- it runs nothing -- and
`oracle_validate_experiment` it: `total_runs` is Oracle's own count of what the plan walks. Report
that number and nothing else as the size. While the size is being settled, change the draft with
`oracle_update_experiment` and validate again; only drafts can change.

Plan limits are not exposed here; when a sweep is over one, validation says so in `errors`. Relay
Oracle's own words rather than paraphrasing a validation error into a guess.

The levers, when it is too big: fewer draws, one candle size instead of several, fewer assets or
windows, or a narrower signal pool. Setting a search up runs nothing, and getting it wrong twice is
fine.

## Launching is the only thing that spends

Say the run count and get an answer before calling `oracle_launch_experiment`. A total the
person never saw is not a total they agreed to. This is not a formality: the gap
between "try a few hundred" and what the arithmetic produces is the whole reason
to check.

`preparing` and `queued` both mean it started. Queued means the account already
has a sweep in flight and this one begins when a slot frees. It is not a refusal
and there is nothing to retry.

It returns immediately and with no results. Say it is running, say how big it
is, and stop. Do not poll in a loop waiting for the answer.

`oracle_pause_experiment` stops further work and keeps everything already written, and
`oracle_get_experiment` then says how many runs finished, which is what they keep. A sweep that was only queued
goes back to validated, because it never started, and can be launched again.

## Do not

- **Do not name a signal or a class from memory.** Ask the graph.
- **Do not skip the market-conditions question** and then describe the result as
  though it generalises.
- **Do not work out the run count.** `oracle_validate_experiment`'s `total_runs` is the only count that knows.
- **Do not launch a total they have not seen.**
- **Do not turn a named signal into a backtest.** They asked for a search.
- **Do not poll.** Read progress when they ask.
