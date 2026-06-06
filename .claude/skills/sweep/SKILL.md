---
name: sweep
description: >-
  Use when the user wants to search a parameter SPACE at scale rather
  than test one config — "sweep the RSI window from 7 to 21", "try every
  MACD variation on BTC 1h and rank them", "what's the best config for
  X", "run an experiment". Drives the managed Oracle experiment
  lifecycle (create → validate → launch → poll → results), fanning a
  strategy template out into up to 99 ranked backtests in one
  experiment. Pair it with /sieve to prune the grid cheaply first. Wraps
  `oracle_list_datasets` + `oracle_list_signals` + `oracle_create_experiment`
  + `oracle_validate_experiment` + `oracle_launch_experiment` +
  `oracle_get_experiment` + `oracle_list_results`; hands the winner to
  /backtest for a confirming verdict before paper/live.
---

# Sweep Skill

This skill exists because **finding a good strategy is a search problem,
not a single guess.** `/backtest` answers "is THIS config good?" A sweep
answers "of these 99 configs, which is best?" — it takes a strategy
template plus a parameter grid (or random search) over signal params,
fans it out into up to 99 individual backtests against one or more
datasets, and ranks the results. You author once and let the engine do
the search.

The lifecycle is intentionally explicit — **validate before launch,
separate from create** — so you (and Oracle) can confirm the config and
its run-count before paying for the fan-out:

```
create ──▶ [update]* ──▶ validate ──▶ launch ──▶ (running) ──▶ completed
  draft        draft      validated    launched      │
                                                      ├─▶ pause ─▶ relaunch
                                                      └─▶ delete (cancels children)
```

**Always prune with `/sieve` first** when the grid is large: SIEVE
scores 99 candidates in one cheap call, so you spend sweep compute only
on the configs it doesn't already predict will die.

## Trigger

Activate when the user:

- Wants to search a parameter range ("sweep RSI window 7→21",
  "try entry-window 8 to 20 and exit-window 20 to 40")
- Wants the best of many variations, ranked ("try every MACD config on
  BTC 1h and tell me the best", "compare these across timeframes")
- Has a shortlist from `/sieve` worth a managed, persisted, ranked run
- Says "run an experiment" / "do a sweep" / "hyperparameter search"

Do NOT activate for:

- A single known config the user wants evaluated → `/backtest`
- Cheaply scoring/screening candidates without persisting → `/sieve`
  (use it first, then come here with the survivors)
- Authoring a single strategy from a loose goal → `/create-strategy`

## Phase A — Orient with the catalog

Three free (non-billable) metadata calls. Always start here — building a
config from memory produces names the validator rejects.

1. `oracle_list_datasets` — the curated OHLCV snapshots the engine runs
   against. Each carries `asset`, `timeframe`, `file`, `rows`,
   `start_date`, `end_date`. **You reference datasets by their `file`
   value** in the config. Pick the dataset(s) matching the user's asset +
   timeframe; show the date range so they know what window they're
   sweeping.
2. `oracle_list_signals` — the 223 signals with typed param specs.
   Each: `name`, `type` (`TRIGGER` / `FILTER`), `params` (tunable spec +
   ranges), `requires`, `category`. This is the source of truth for valid
   signal names and which params are sweepable.
3. `oracle_list_templates` (optional) — predefined strategy templates you
   can seed the experiment from instead of building entry/exit by hand.

## Phase B — Build the experiment config

Build the config dict in the real Oracle `ExperimentConfig` shape (this
is the executable SDK shape, not the older curl-doc shape):

```json
{
  "name": "BTC 1h MACD momentum sweep",
  "description": "Grid over MACD windows; one entry trigger, one exit trigger.",
  "search_mode": "grid",
  "kind": "single",
  "datasets": [ <whole dataset OBJECT from oracle_list_datasets, not the filename> ],
  "entry_signals": {
    "triggers": [
      {"name": "macd_bullish_cross", "signal_type": "TRIGGER",
       "params": {"window_fast": 12, "window_slow": 26}}
    ],
    "filters": [
      {"name": "is_above_sma", "signal_type": "FILTER", "params": {"window": 50}}
    ],
    "min_filters": 1,
    "max_filters": 1
  },
  "exit_signals": {
    "triggers": [
      {"name": "macd_bearish_cross", "signal_type": "TRIGGER",
       "params": {"window_fast": 12, "window_slow": 26}}
    ],
    "filters": []
  },
  "execution_config": {"base": {}, "sweep_axes": []}
}
```

> **These four details are server-enforced — verified live against Oracle
> v0.15.3. Get them wrong and `create`/`validate` reject the config:**
>
> 1. **`datasets` holds the whole dataset OBJECTS** returned by
>    `oracle_list_datasets` (the dicts with `asset`/`timeframe`/`file`/
>    `hash`/…), NOT bare filename strings. (A bare string → HTTP 422.)
> 2. **Every signal needs `signal_type`** (`"TRIGGER"` or `"FILTER"`) —
>    the value of the catalog's `type` field. Omitting it → HTTP 422.
> 3. **`entry_signals` requires at least one filter** and `min_filters ≥ 1`.
>    An empty entry-filter list → validate fails with
>    `"No entry filter signals selected"`.
> 4. The catalog's `rows` field is often `0`/unpopulated — **don't filter
>    datasets on `rows`**; sort by `end_date` to pick a recent window.

Config rules of thumb:

- **`search_mode`** — `"grid"` enumerates every parameter combination
  (deterministic, exhaustive). `"random"` samples N combos (set
  `n_random`) — use it when the full grid would exceed 99.
- **`kind`** — `"single"` (one asset) is the default. `"pair"` is
  continuous two-asset rotation (needs `pairs` + `rotation_params`,
  random mode only) — only reach for it if the user explicitly wants
  pair rotation.
- **Keep the grid ≤ 99.** The engine refuses grids larger than 99
  strategies. If the user's ranges blow past that, either narrow the
  ranges, switch to `"random"` with `n_random ≤ 99`, or **pre-prune with
  `/sieve`** and seed the sweep from the survivors.
- **SIEVE pre-filter.** If supported by the config (`pre_filter` block),
  enabling it lets the engine skip runs it predicts will produce no
  trades — a free compute saving. Default it on for large grids.
- **`execution_config`** — `{"base": {}, "sweep_axes": []}` uses canonical
  trading defaults. Only populate `base` (from `oracle_list_signals`'
  sibling exec-config defaults) if the user wants specific risk settings,
  and only add `sweep_axes` if they want to sweep an execution parameter
  too.

Do NOT ask the user to hand-write this. Translate their intent ("sweep
MACD fast 8→16, slow 20→30 on BTC 1h") into the config yourself, citing
the dataset + signals you pulled in Phase A.

## Phase C — create → validate → launch

1. `oracle_create_experiment(config)` → returns `experiment_id`
   (`exp_<timestamp>`), `status: "draft"`.
2. `oracle_validate_experiment(experiment_id)` — **read the response.**
   It returns `{"valid": bool, "total_runs": int, "errors": [...],
   "warnings": [...]}`. Check `valid` and report `total_runs` to the user
   before paying to launch. If `valid` is `false`, the `errors` list says
   why (bad signal name, params out of range, no entry filter, grid >
   cap); fix the config with `oracle_update_experiment(experiment_id,
   config)` (**only `draft` experiments are mutable**) and re-validate.
   Surface the error verbatim — don't paper over it.
   - *(Note: `oracle_validate_experiment` returns this validation result,
     not a `status` field — the agent reads it straight from the server.)*
3. `oracle_launch_experiment(experiment_id)` — fans the validated grid
   out asynchronously; returns `{"status": "preparing", "experiment_id":
   ..., "total_runs": ...}`.

**Two different caps — know which surfaces where (verified live):**

- **Per-sweep size** (max backtests in one grid) — tier-dependent;
  rejected at **validate** (HTTP 403 / `valid: false`). Shrink the grid,
  switch to `random`, or pre-prune with `/sieve`.
- **Concurrent sweeps in flight** — **plan-dependent, often just 1** on
  the free tier; rejected at **launch** (HTTP 429, *"Concurrent sweep
  limit reached: you have N sweep(s) already in flight; your plan allows
  up to M"*). You can only run one sweep at a time — wait for the current
  one to finish (poll Phase D), then launch the next. Relay the message
  plainly and offer to wait or upgrade.

**Cost note:** `create` + `launch` each count as a billable Oracle
experiment call (1 unit; the fan-out children are NOT billed
individually). Tell the user before launching.

## Phase D — Poll

The fan-out runs in the background. Track it without hammering the API:

- `oracle_get_experiment(experiment_id)` → `status` + `completed_runs`
  (live progress against total).
- `oracle_list_results(experiment_id=...)` → results as they materialize,
  paginated.

**Poll with patience, not a tight loop.** `oracle_list_results` is
billable per call — prefer a larger page every several seconds over
rapid small polls. Tell the user it's a background job and roughly how
many runs to expect.

**If `oracle_list_results` returns a BigQuery / Parquet schema error**
(e.g. *"column 'X' has type INT32 which does not match … BOOL"*), that is
a **server-side corpus issue, not your config** — `get_experiment` still
shows progress. Surface it as an Oracle-side problem, note the experiment
ran, and don't blame the user's sweep. (Graceful downgrade, per
`trading-bot-workflow.md`.)

## Phase E — Rank + verdict + handoff

1. **Rank.** Pull `oracle_list_results(experiment_id=...)`, sort by
   `sortino_ratio` (downside-aware), tie-break on `sharpe_ratio`.
2. **Verdict against the same thresholds `/backtest` uses** (the 6 in
   `server/src/services/data/threshold_spec.json`: sortino ≥ 1.5,
   sharpe ≥ 1.2, calmar ≥ 1.0, irr_annualized ≥ 0.15, max_drawdown ≤ 0.7,
   win_rate ≥ 0.25). A sweep result with `total_trades < 10` is
   `INSUFFICIENT_TRADES`, not a winner — same rule as `/backtest`.
3. **Benchmark-relative line.** Each result row carries
   `benchmark_asset_return` / `benchmark_btc_return`. Report whether the
   top strategy beat buy-and-hold, not just its absolute return.
4. **Present the leaderboard** — top 5 by sortino, with the threshold
   pass/fail and benchmark deltas — so the user can see the search, not
   just the winner.
5. **Confirm the winner before promotion.** Sweep runs are fast/coarse;
   register the winning config (`create_strategy_manual`) and hand to
   `/backtest` for a full single-strategy verdict on a properly sized
   window. Only after that PASS does it go to paper (and live is further
   gated on allocation + backup — see `trading-bot-workflow.md`).

## Lifecycle housekeeping

- `oracle_pause_experiment(id)` stops a running fan-out without losing
  completed results; relaunch to resume.
- `oracle_delete_experiment(id)` removes it and cancels in-flight
  children. Offer cleanup once the user has their winner.

## Prohibited

- **Never** call `launch` without a successful `validate` — the engine
  rejects it, and you'd waste the round-trip.
- **Never** try to mutate a non-draft experiment — `update` only works on
  `draft`. Create a new one instead.
- **Never** build a grid > 99 and hope — narrow it, switch to random, or
  pre-prune with `/sieve`.
- **Never** promote a sweep winner straight to paper/live on the sweep
  metrics alone — confirm with a full `/backtest` first.
- **Never** tight-poll `oracle_list_results` — it's billable per call.
- **Never** invent a metric or a run count; report what
  `oracle_validate_experiment` / `oracle_list_results` returned.

## Summary — Decision Tree

```
User wants to search a parameter space / rank many configs
│
├─ Phase A: catalog — oracle_list_datasets + oracle_list_signals (+templates)
│     → pick dataset(s) by file; confirm valid signal names + params
│
├─ Phase B: build the ExperimentConfig from the user's intent
│     → grid vs random; keep ≤99; enable SIEVE pre-filter on big grids
│     → pre-prune with /sieve if the grid would exceed 99
│
├─ Phase C: oracle_create_experiment → oracle_validate_experiment → oracle_launch_experiment
│     → READ validate response (total_runs / errors / tier caps)
│     → fix-and-revalidate on error (drafts only); surface caps plainly
│
├─ Phase D: poll oracle_get_experiment + oracle_list_results
│     → background job; large pages, not tight loops (results reads bill)
│
└─ Phase E: rank by sortino → verdict vs threshold_spec → benchmark line
      → present top-5 leaderboard
      → register winner (create_strategy_manual) → /backtest to confirm
      → then paper (live further gated); offer delete to clean up
```
