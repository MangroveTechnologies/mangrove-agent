---
name: sweep-results
description: >-
  Search existing Strategy Engine runs: "search for bitcoin strategies", "find a
  momentum strategy", "look for something that holds up in a bear market", "pick
  an existing strategy", "what should I trade", "show me the best ones", or
  "how did that go". Search across all visible experiments unless one is named.
  Filter and sort existing results before proposing a new sweep. New experiment
  creation and launching belong to sweeps.
uses-tools: []
source-uses-tools: [list_backtest_markets, list_sweeps, get_sweep, find_sweep_runs, list_run_filters, get_sweep_run]
unavailable-tools: [list_backtest_markets, list_sweeps, get_sweep, find_sweep_runs, list_run_filters, get_sweep_run]
unreviewed-tools: []
catalog-mode: deployed
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/sweep-results/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. When a remote Oracle experiment/result reader is unavailable, the authenticated local agent_list_oracle_experiments and agent_list_oracle_results tools can discover experiment summaries and read bounded result pages only. They do not provide sweep-specific filtering, ranking, or mutation; never present them as equivalent to a missing remote tool. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

Catalog snapshot note (observed deployed tools only): this skill references tools absent from the recorded catalog snapshot: `list_backtest_markets`, `list_sweeps`, `get_sweep`, `find_sweep_runs`, `list_run_filters`, `get_sweep_run`. Do not call or replace these by assumption. Check the active MCP `tools/list`; use a capability only when that live catalog exposes its schema.

# A sweep has no winner

Return, risk-adjusted return, drawdown, how many trades it took, whether it beat holding
the asset, whether it held up across different windows -- these pull in different
directions, and which of them matters is the person's question, not a property of the
results. So there is no best run and you must not present one as though there were.

`sort_by` is required on `find_sweep_runs`, deliberately. Take it from what they asked
for, and **name it whenever you report an order**: "the ten highest by Sortino", never
"the ten best". If they have not said what they care about, ask before sorting, or show
the same handful of runs against two measures and let them see the disagreement.

## Filter before you sort

`min_trades` is almost always the first thing to set. A ratio computed on four trades is
noise, and unfiltered those rows sit at the top of every ranking.

Every numeric filter takes BOTH ends, so a band is one question rather than a page
fetched and thinned by hand: a delta against the benchmark between 3 and 8, trades
between 10 and 50, a Sharpe between 1 and 3. Ranking within a band is usually the real
question when someone says "good but not suspicious".

The market a run happened in filters too, and it matters more than it looks: what a run
did in one kind of market says little about another. Direction, volatility, trend and the
full composite label are all available, as is the window's date span. Each regime filter
takes several labels as one comma-separated string -- `market_direction="bull,super_bull"`
is "bull or super bull" in one search, not two -- and different regime filters must all
hold. For "what works right now", `get_market_regime` gives the current regime in these
same labels under `window`; strategy-composition has the full protocol for a build.

`list_run_filters` says which assets, candle sizes and entry triggers a sweep's runs
actually carry. Read it before filtering on one of them, because a value nothing carries
comes back as an empty page that reads exactly like a search with nothing good in it.

Use only filters the advertised receiver schema supports. Do not send a filter through an
SDK/REST path unless that caller's published request model exposes it; an accepted method
argument is not evidence that the server applies it. If a requested filter is unavailable,
say that the filtered search cannot be completed here rather than dropping it or ranking an
incomplete page. When the receiver does not advertise experiment/result readers, the
authenticated local `agent_list_oracle_experiments` and `agent_list_oracle_results` tools can
discover experiment summaries and inspect bounded result pages. They do not implement
sweep-run filters, sweep-specific ranking, or sweep-run reconstruction, so they cannot
replace `find_sweep_runs` or `get_sweep_run`.

Before starting a search, inspect the deployed MCP catalog. If `find_sweep_runs` is absent,
do not call it just because it appears in this skill's source list. Use a documented
published SDK/REST path only when its request model supports the full requested filter set
and pagination; otherwise report that search capability as unavailable.

Execution values -- the reward factor a run used, its cooldown, its ATR period -- can only
be filtered inside ONE sweep. Naming one without an experiment_id is refused rather than
quietly ignored.

## What they already have

`list_sweeps` whenever they refer to a search rather than asking for a new one: "how did
that go", "what have I run". Then `get_sweep` for progress on the one that matters, which
also reports the controls it ran with in the same words a new sweep is set up in.

**Leave experiment_id out and `find_sweep_runs` searches every sweep they have at once.**
That is how "have I already found something like this" gets answered without spending a
new search on a question that already has one. A finished sweep is a record.

Results are readable while a sweep is still running. The rows that exist are real runs.
Say how far along it is when you quote them, because an order over a tenth of the search
can change as the rest lands.

## Reading a row honestly

Percent-typed metrics are on a **0-100** scale and arrive as display strings with the
unit attached. `0.52%` means 0.52 percent, not 52. Quote them as they come; the raw
values ride alongside for anything that computes. Never infer the scale from how big a
number looks.

A run whose status says it took no trades did not lose and did not win. It never engaged
with the market. That is a real outcome, not a zero to rank against others, and two such
runs are not tied on performance.

Every row carries what holding the asset did over the same window. A run that made money
in a rising market may have made less than doing nothing, and saying so is the difference
between a result and a number.

Every row carries the entry and exit rules the engine drew. That is the actual answer to
"what did it find" -- a name and a ratio is not a strategy anyone can look at. And it
carries the character of the window it ran in, so a run that looks excellent in one
violent bull market has not been shown to work anywhere else.

## A row is evidence, not a verdict

A run is a fast, coarse measurement of a strategy the engine invented. It says a shape is
worth looking at.

`get_sweep_run` rebuilds an interesting row as the strategy it was, with the parameter
values the engine drew. That is the way out of a sweep: it gets saved as a strategy and
backtested properly over a window the person chose, and the deployment gates after that
are separate again. Say that when you hand one back.

Never describe a sweep as having ranked its candidates by how likely they were to make
money before running them. It does not. The only prediction in the loop is an optional
will-it-trade gate that skips candidates predicted never to open a position, and it says
nothing about whether the survivors are any good.

## Do not

- **Do not report an order without naming the measure it is ordered by.**
- **Do not call the top row the best strategy.** It is the top row by the measure you
  picked.
- **Do not treat a no-trades run as a zero return.**
- **Do not quote a return without what holding the asset did over the same window.**
- **Do not hand back a row as a verdict.** It is evidence; the backtest answers.
- **Do not re-run a search to answer a question a finished one already answered.**

## Saving a sweep pick

Pass its `experiment_id`, `run_index` and `config_hash` to `save_strategy` as
`source_experiment_id`, `source_run_index` and `source_config_hash`, and the same three to
`get_sweep_run`: a relaunched sweep can give one run index to several runs, and the config
hash is what tells them apart. The tool reloads the source run and
preserves its corpus dataset, recorded market and execution settings; copying only
its asset and signals loses that identity. A complete supplied or recorded venue and quote are the defaults; do not ask the user
unnecessary market questions. They can optionally choose another `market_data_venue`
and/or `quote_token` on `save_strategy` or `run_backtest`. Use `list_backtest_markets`
when they ask for alternatives. A changed market uses its own feed and reports that
choice while preserving source provenance.

If venue or quote information is missing, match the supplied dimensions against
available markets and suggest a match. Ask the user to confirm it or choose another
before saving or running. Pass the confirmed venue and quote explicitly. Never infer
confirmation, silently fill missing details, or replace an unavailable supplied market.

`run_backtest` uses the latest engine generation unless the person explicitly asks
for another. An older sweep's metrics are historical evidence, not a promise that
current canon will reproduce identical numbers. Report the generation difference.
