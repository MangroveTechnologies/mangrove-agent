---
name: sweep-results
description: >-
  Read the runs a sweep produced, across one experiment or every experiment the person
  has. Reach for it whenever the question is about what a search FOUND rather than how to
  set one up: "how did that go", "what did it find", "show me the best ones", "did
  anything work in a bear market", "have I already tried this", "what have I run". Filters
  and sorts server-side over millions of runs, hands back the actual strategy behind a
  row, and refuses to name a winner. Setting a search up and launching it is sweeps.
uses-tools: [oracle_list_experiments, oracle_get_experiment, oracle_data_query, oracle_list_results, create_strategy_manual, backtest_strategy]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/sweep-results/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# A sweep has no winner

Return, risk-adjusted return, drawdown, how many trades it took, whether it beat holding
the asset, whether it held up across different windows -- these pull in different
directions, and which of them matters is the person's question, not a property of the
results. So there is no best run and you must not present one as though there were.

Always pass `order_by` to `oracle_data_query` (table `results`) -- nothing sorts for you,
deliberately. Take it from what they asked for, and **name it whenever you report an order**: "the ten highest by Sortino", never
"the ten best". If they have not said what they care about, ask before sorting, or show
the same handful of runs against two measures and let them see the disagreement.

## Filter before you sort

`min_trades` is almost always the first thing to set. A ratio computed on four trades is
noise, and unfiltered those rows sit at the top of every ranking.

A band is two filter clauses on one column, a lower and an upper bound, in the same
`oracle_data_query` call -- one question rather than a page fetched and thinned by hand: a delta against the benchmark between 3 and 8, trades
between 10 and 50, a Sharpe between 1 and 3. Ranking within a band is usually the real
question when someone says "good but not suspicious".

The market a run happened in filters too, and it matters more than it looks: what a run
did in one kind of market says little about another. Result rows do not carry those
labels themselves: join a row's `data_file_path` (or its asset, timeframe and dates) to its
`oracle_list_datasets` row for `direction`, `volatility`, `trend` and `regime_composite`, and
filter on the date span directly.

`list_run_filters` says which assets, candle sizes and entry triggers a sweep's runs
actually carry. Read it before filtering on one of them, because a value nothing carries
comes back as an empty page that reads exactly like a search with nothing good in it.

> **Not in mangrove-agent yet: `list_run_filters`.** Read which assets, timeframes and triggers a sweep's runs carry from its `oracle_list_results` rows before filtering on them.

Execution values -- the reward factor a run used, its cooldown, its ATR period -- are
columns on every result row, but they only compare within one sweep's design: filter on
them together with `experiment_id`.

## What they already have

`oracle_list_experiments` whenever they refer to a search rather than asking for a new one: "how did
that go", "what have I run". Then `oracle_get_experiment` for progress on the one that matters, which
also reports the controls it ran with in the same words a new sweep is set up in.

**Leave experiment_id out and `oracle_data_query` searches every sweep they have at once.**
That is how "have I already found something like this" gets answered without spending a
new search on a question that already has one. A finished sweep is a record.

Results are readable while a sweep is still running. The rows that exist are real runs.
Say how far along it is when you quote them, because an order over a tenth of the search
can change as the rest lands.

## Reading a row honestly

Percent-typed metrics (`total_return`, `win_rate`, `max_drawdown`, `irr_annualized`,
`benchmark_asset_return`) are raw numbers on a **0-100** scale with no unit attached:
`0.52` means 0.52 percent, not 52. Say the unit in the sentence when you quote one. Never infer the scale from how big a
number looks.

A run whose status says it took no trades did not lose and did not win. It never engaged
with the market. That is a real outcome, not a zero to rank against others, and two such
runs are not tied on performance.

Every row carries what holding the asset did over the same window. A run that made money
in a rising market may have made less than doing nothing, and saying so is the difference
between a result and a number.

Every row carries the entry and exit rules the engine drew. That is the actual answer to
"what did it find" -- a name and a ratio is not a strategy anyone can look at. Its window's
character is one join away (its dataset row in `oracle_list_datasets`), and a run that looks excellent in one
violent bull market has not been shown to work anywhere else.

## A row is evidence, not a verdict

A run is a fast, coarse measurement of a strategy the engine invented. It says a shape is
worth looking at.

`get_sweep_run` rebuilds an interesting row as the strategy it was, with the parameter
values the engine drew. That is the way out of a sweep: it gets saved as a strategy and
backtested properly over a window the person chose, and the deployment gates after that
are separate again. Say that when you hand one back.

> **Not in mangrove-agent yet: `get_sweep_run`.** Each `oracle_list_results` / `oracle_data_query` result row carries the `entry_json` and `exit_json` the engine drew; rebuild the strategy from them with `create_strategy_manual`, then backtest it with `backtest_strategy`.

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
