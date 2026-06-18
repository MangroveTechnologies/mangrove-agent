---
name: sweep
description: >-
  Use when the user wants to search a parameter SPACE at scale rather
  than test one config — "sweep the RSI window from 7 to 21", "try every
  MACD variation on BTC 1h and rank them", "what's the best config for
  X", "run an experiment", "hyperparameter search". Drives the managed
  Oracle experiment lifecycle (create → validate → launch → poll →
  results): you give it a strategy template + a parameter space, and the
  ENGINE generates and backtests the candidates (grid or random/
  Monte-Carlo), ranking the results. A binary SIEVE pre-filter skips
  dead configs during the sweep. Wraps `oracle_list_datasets` +
  `oracle_list_signals` + `oracle_create_experiment` +
  `oracle_validate_experiment` + `oracle_launch_experiment` +
  `oracle_get_experiment` + `oracle_list_results`; hands the winner to
  /backtest for a confirming verdict before paper/live.
---

# Sweep Skill

This skill exists because **finding a good strategy is a search problem,
not a single guess.** `/backtest` answers "is THIS config good?" A sweep
answers "across this whole parameter space, which config is best?" — you
give it a strategy template plus a parameter space, and the **engine
generates its own candidates** (grid enumeration or random/Monte-Carlo
sampling), backtests each against one or more datasets, and ranks the
results. You author the search space once and let the engine explore it.

**A sweep is NOT "screen a list then run the survivors."** It generates
the candidates itself from the parameter space — you never hand it a
pre-made list of strategies. (That different workflow — score a fixed
shortlist with SIEVE, then backtest the keepers — is `/sieve` →
`/backtest`/`/backtest/bulk`. See "How this differs from /sieve" below.)

The lifecycle is intentionally explicit — **validate before launch,
separate from create** — so you (and Oracle) can confirm the config and
its run-count before paying for the fan-out:

```
create ──▶ [update]* ──▶ validate ──▶ launch ──▶ (running) ──▶ completed
  draft        draft      validated    launched      │
                                                      ├─▶ pause ─▶ relaunch
                                                      └─▶ delete (cancels children)
```

## Trigger

Activate when the user:

- Wants to search a parameter range ("sweep RSI window 7→21",
  "try entry-window 8 to 20 and exit-window 20 to 40")
- Wants the best of many variations, ranked ("try every MACD config on
  BTC 1h and tell me the best", "compare these across timeframes")
- Says "run an experiment" / "do a sweep" / "hyperparameter search" /
  "Monte-Carlo search over the params"

Do NOT activate for:

- A single known config the user wants evaluated → `/backtest`
- Screening/ranking a FIXED set of candidate strategies they already
  have, without a parameter search → `/sieve` (then `/backtest` /
  `/backtest/bulk`). That is a separate workflow, not a pre-step to this one.
- Authoring a single strategy from a loose goal → `/create-strategy`

## How this differs from /sieve (read this — they are NOT a pipeline)

These get conflated; they are different tools:

- **`/sweep` (this skill)** = parameter-space SEARCH. The engine generates
  candidates by sampling the space and backtests them. Its built-in SIEVE
  hook is the **binary** pre-filter (below) that runs *inside* the sweep.
- **`/sieve`** = cheap screening of a **fixed shortlist** you already have
  (≤99 per call), via the standalone `sieve_score` endpoint. It hands
  survivors to `/backtest` / `/backtest/bulk` — **not** to a sweep.

You do not "prune a grid with /sieve and then sweep the survivors." A
sweep makes its own candidates. The only place SIEVE touches a sweep is
the in-sweep binary pre-filter.

## SIEVE binary pre-filter (default ON for every sweep)

The Oracle engine can run each generated candidate through the **binary**
SIEVE head *before* backtesting it, and skip the ones it predicts will
never fire a trade — saving compute on dead-on-arrival configs. Set it in
the config and **default it on at threshold 0.8**:

```json
"pre_filter": {"enabled": true, "confidence_threshold": 0.8}
```

Meaning: skip a candidate if `P(no_trades) > 0.8`. This is the **2-class
(binary) head only** — a will-it-trade gate. The **4-class head
(losing/no_trades/wash/winning) is NOT used in sweeps** — it is a
post-hoc/replay classifier (it labels already-completed runs). Never
describe the sweep as "ranking candidates by P(winning) before running"
— it does not.

## Phase A — Orient with the catalog

Three free (non-billable) metadata calls. Always start here — building a
config from memory produces names the validator rejects.

1. `oracle_list_datasets` — the curated OHLCV snapshots the engine runs
   against. Each carries `asset`, `timeframe`, `file`, `hash`, `rows`,
   `start_date`, `end_date`. **You reference a dataset by passing its
   whole object** (see Phase B rule 1), not by filename. Pick the
   dataset(s) matching the user's asset + timeframe; sort by `end_date`
   and show the date range so they know the window they're sweeping.
2. `oracle_list_signals` — the 223 signals with typed param specs.
   Each: `name`, `type` (`TRIGGER` / `FILTER`), `params` (tunable spec +
   ranges), `requires`, `category` (`momentum` / `trend` / `volume` /
   `volatility` / `patterns`). The source of truth for valid signal names
   and which params are sweepable. **Select signals by category** when the
   user names a style ("momentum sweep" → triggers/filters in `momentum`).
3. `oracle_list_templates` (optional) — predefined strategy templates you
   can seed the experiment from instead of building entry/exit by hand.

## Phase B — Build the experiment config

Two ways to build it:

**(i) Author it directly** — translate the user's intent ("sweep MACD
fast 8→16, slow 20→30 on BTC 1h") into the `ExperimentConfig` yourself,
citing the dataset + signals from Phase A. Do NOT ask the user to
hand-write JSON.

**(ii) Configure via the HTML builder** (`sweep-config.html`, shipped
with this skill) — for when the user wants to drive the setup visually:
1. Fetch `oracle_list_signals` + `oracle_list_datasets`, and embed them
   into the page's `CATALOG` block (the HTML makes **no** API calls — it
   only builds JSON, so the catalog must be injected).
2. Hand the user the file; they pick dataset, signals (filterable by
   category), param ranges, grid/random + size, and **Download
   config.json**.
3. **You** read the downloaded `config.json`, then run validate →
   **confirm `total_runs` + cost with the user** → launch. Never
   auto-launch from a saved config without that confirmation.

The real Oracle `ExperimentConfig` shape (executable SDK shape):

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
       "params": {"window_sign": 9},
       "params_sweep": {"window_fast": {"values": [8, 12, 16]},
                        "window_slow": {"values": [21, 26, 30]}}}
    ],
    "filters": [
      {"name": "is_above_sma", "signal_type": "FILTER",
       "params_sweep": {"window": {"values": [50, 100]}}}
    ],
    "min_filters": 1,
    "max_filters": 1
  },
  "exit_signals": {
    "triggers": [
      {"name": "macd_bearish_cross", "signal_type": "TRIGGER",
       "params": {"window_fast": 12, "window_slow": 26, "window_sign": 9}}
    ],
    "filters": []
  },
  "grid_signals": {"n_param_combos": 24},
  "execution_config": {"base": {}, "sweep_axes": []},
  "pre_filter": {"enabled": true, "confidence_threshold": 0.8}
}
```

> **Server-enforced — verified live. Get these wrong and `create`/`validate` reject:**
>
> 1. **`datasets` holds the whole dataset OBJECTS** returned by
>    `oracle_list_datasets` (dicts with `asset`/`timeframe`/`file`/`hash`/…),
>    NOT bare filename strings. (A bare string → HTTP 422.)
> 2. **Every signal needs `signal_type`** (`"TRIGGER"` or `"FILTER"`) —
>    the value of the catalog's `type` field. Omitting it → HTTP 422.
> 3. **`entry_signals` requires at least one filter** and `min_filters ≥ 1`.
>    An empty entry-filter list → validate fails with
>    `"No entry filter signals selected"`.
> 4. The catalog's `rows` field is often `0`/unpopulated — **don't filter
>    datasets on `rows`**; sort by `end_date` to pick a recent window.

Config rules of thumb:

- **The parameter space lives in `params_sweep`** on each signal — a dict
  of `{param: {"values": [...]}}` or `{param: {"min": .., "max": .., "step": ..}}`.
  Fixed params go in `params`. This is what the engine searches.
- **`search_mode`** — `"grid"` enumerates parameter combinations;
  `"random"` samples (Monte-Carlo). Use `"random"` when the space is huge
  and you want a representative sample rather than exhaustive coverage.
- **Sweep SIZE (how many backtests run) is controlled by you:**
  - **grid:** `grid_signals.n_param_combos` caps the combos drawn from the
    space (it does NOT run the full cross-product by default). Set it to
    the number of runs you want (e.g. 24, 100, 500).
  - **random:** `n_random` = number of samples.
  - Pick the size deliberately and tell the user — this is what they're
    paying compute for.
- **`kind`** — `"single"` (one asset) is the default. `"pair"` is
  continuous two-asset rotation (needs `pairs` + `rotation_params`, random
  mode only) — only for explicit pair-rotation requests.
- **`pre_filter`** — default `{enabled: true, confidence_threshold: 0.8}`
  (binary SIEVE gate; see above). Skips configs predicted not to trade.
- **`execution_config`** — `{"base": {}, "sweep_axes": []}` uses canonical
  trading defaults. Only populate `base` (from the exec-config defaults) if
  the user wants specific risk settings; add `sweep_axes` only to sweep an
  execution parameter too.

## Phase C — create → validate → launch

1. `oracle_create_experiment(config)` → returns `experiment_id`
   (`exp_<timestamp>`), `status: "draft"`.
2. `oracle_validate_experiment(experiment_id)` — **read the response.**
   It returns `{"valid": bool, "total_runs": int, "errors": [...],
   "warnings": [...]}`. Check `valid` and report `total_runs` to the user
   before paying to launch. If `valid` is `false`, the `errors` list says
   why (bad signal name, params out of range, no entry filter, sweep
   exceeds the tier's per-sweep cap); fix with
   `oracle_update_experiment(experiment_id, config)` (**drafts only**) and
   re-validate. Surface errors verbatim.
   - *(`oracle_validate_experiment` returns the validation result, not a
     `status` field — read it straight from the server.)*
3. `oracle_launch_experiment(experiment_id)` — fans the validated search
   out asynchronously; returns `{"status": "preparing", "experiment_id":
   ..., "total_runs": ...}`.

**Two tier caps — know which surfaces where (verified live). Note: there
is NO fixed 99 limit on a sweep — that 99 is SIEVE's per-call cap, not the
sweep's. Sweep size is bounded by your TIER:**

- **Per-sweep size** = `max_backtests_per_sweep` for the caller's tier
  (e.g. Pro 5,000 / Startup 20,000 / Enterprise 100,000). Enforced at
  **validate** → HTTP 403 / `valid: false` if `total_runs` exceeds it.
  Fix by shrinking `n_param_combos`/`n_random`, or the user upgrades tier.
- **Concurrent sweeps in flight** = `concurrent_sweep_cap` (often 1 on
  lower tiers). Enforced at **launch** → HTTP 429 (*"Concurrent sweep
  limit reached…"*). Wait for the running one to finish (poll Phase D),
  then launch. Relay the message plainly; offer to wait or upgrade.

**Cost note:** `create` + `launch` each count as one billable Oracle
experiment call; the fan-out children are NOT billed individually. Tell
the user before launching.

## Phase D — Poll

The fan-out runs in the background (cloud — these run as Cloud Run Jobs on
the prod Oracle, not locally). Track it without hammering the API:

- `oracle_get_experiment(experiment_id)` → `status` + `completed_runs`
  (live progress against `total_runs`).
- `oracle_list_results(experiment_id=...)` → results as they materialize,
  paginated.

**Poll with patience, not a tight loop.** `oracle_list_results` is
billable per call — prefer a larger page every several seconds over rapid
small polls. Tell the user it's a background job and roughly how many runs
to expect.

**If `oracle_list_results` returns a BigQuery / Parquet schema error**
(e.g. *"column 'X' has type INT32 which does not match … BOOL"*), that is
a **server-side corpus issue, not the config** — `get_experiment` still
shows progress. Surface it as an Oracle-side problem, note the experiment
ran, and don't blame the user's sweep.

## Phase E — Rank + verdict + handoff

1. **Rank.** Pull `oracle_list_results(experiment_id=...)`, sort by
   `sortino_ratio` (downside-aware), tie-break on `sharpe_ratio`.
2. **Verdict against the same thresholds `/backtest` uses** (the 6 in
   `server/src/services/data/threshold_spec.json`: sortino ≥ 1.5,
   sharpe ≥ 1.2, calmar ≥ 1.0, irr_annualized ≥ 0.15, max_drawdown ≤ 0.7,
   win_rate ≥ 0.25). A result with `total_trades < 10` is
   `INSUFFICIENT_TRADES`, not a winner.
3. **Benchmark-relative line.** Each row carries `benchmark_asset_return` /
   `benchmark_btc_return`. Report whether the top strategy beat
   buy-and-hold, not just absolute return.
4. **Present the leaderboard** — top 5 by sortino, with threshold pass/fail
   and benchmark deltas — so the user sees the search, not just the winner.
5. **Confirm the winner before promotion.** Sweep runs are fast/coarse;
   register the winning config (`create_strategy_manual`) and hand to
   `/backtest` for a full single-strategy verdict on a properly sized
   window. Only after that PASS does it go to paper (live further gated on
   allocation + backup — see `trading-bot-workflow.md`).

## Lifecycle housekeeping

- `oracle_pause_experiment(id)` stops a running fan-out without losing
  completed results; relaunch to resume.
- `oracle_delete_experiment(id)` removes it and cancels in-flight children.
  Offer cleanup once the user has their winner.

## Prohibited

- **Never** call `launch` without a successful `validate`.
- **Never** mutate a non-draft experiment — `update` only works on `draft`.
- **Never** claim a fixed "99" sweep limit — that's SIEVE's per-call cap.
  Sweep size is the tier's `max_backtests_per_sweep`; control it with
  `n_param_combos` / `n_random`.
- **Never** describe a sweep as "screen with /sieve then run the survivors"
  or "rank candidates by P(winning) before running" — the engine generates
  its own candidates; the only in-sweep SIEVE is the binary pre-filter.
- **Never** promote a sweep winner straight to paper/live on sweep metrics
  alone — confirm with a full `/backtest` first.
- **Never** tight-poll `oracle_list_results` — it's billable per call.
- **Never** invent a metric or a run count; report what
  `oracle_validate_experiment` / `oracle_list_results` returned.

## Summary — Decision Tree

```
User wants to search a parameter space / rank many configs
│
├─ Phase A: catalog — oracle_list_datasets + oracle_list_signals (+templates)
│     → pick dataset object(s) by asset/timeframe; valid signal names by category
│
├─ Phase B: build the ExperimentConfig (author directly OR via sweep-config.html)
│     → params_sweep = the search space; grid (n_param_combos) vs random (n_random)
│     → pre_filter {enabled:true, confidence_threshold:0.8} (binary SIEVE gate)
│     → size it deliberately; tier cap (max_backtests_per_sweep), not 99
│
├─ Phase C: oracle_create_experiment → oracle_validate_experiment → oracle_launch_experiment
│     → READ validate (total_runs / errors / tier cap 403); confirm cost before launch
│     → concurrent cap → 429 at launch: wait, then launch
│
├─ Phase D: poll oracle_get_experiment + oracle_list_results
│     → background cloud job; large pages, not tight loops (results reads bill)
│
└─ Phase E: rank by sortino → verdict vs threshold_spec → benchmark line
      → present top-5 leaderboard
      → register winner (create_strategy_manual) → /backtest to confirm
      → then paper (live further gated); offer delete to clean up
```
