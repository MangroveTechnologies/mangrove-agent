---
name: create-strategy
description: >-
  Use when the user wants to build a new trading strategy — "build me a
  momentum play on ETH", "find a strategy for BTC", "I want to trade X",
  "make a strategy", or after Stage 0 greeter when the wallet is ready
  and the user is moving into Stage 2 (Author) of trading-bot-workflow.md.
  Drives the author → backtest path using reference strategies (Mechanism
  2) and KB-grounded parameter choices (Mechanism 1) so parameters are
  evidence-backed, not library-default guesses. Wraps
  search_reference_strategies + build_strategy_from_reference +
  create_strategy_autonomous + create_strategy_manual + kb_search.
---

# Create Strategy Skill

The agent's job in this skill: turn a loose user goal ("momentum on ETH")
into a strategy config with **specific signals and specific parameters
backed by evidence**, not library defaults.

Two mechanisms drive "intuition":

- **Mechanism 2 (reference strategies) — the primary path.** Curated
  known-good configs. The agent searches these FIRST, bulk-backtests
  the top matches onto the user's target, and promotes by ranked
  performance. Zero parameter guessing for the common case.
- **Mechanism 1 (KB-grounded parameters) — the fallback.** When
  references don't fit (unusual asset, unusual goal, or user wants to
  modify), the agent is REQUIRED to call `kb_search` for every signal
  it picks before finalizing params. No library-default fallback.

### Portability — reference strategies are signal combos, not pins

Every reference in the library is a **portable signal combination**: the
asset and timeframe on the reference record are where Oracle *found*
the combo worked, not a constraint on where you can apply it.
`build_strategy_from_reference` accepts both `asset` and `timeframe`
overrides for exactly this reason. If the user asks for a strategy on
AVAX 1h and search returns references recorded on BTC 1h + SOL 5m,
both are valid candidates — retarget them onto (AVAX, 1h) and let the
backtest pick the winner. Never quote the reference's own stored
metrics to the user as if they apply to the retarget.

### Bulk-backtest is the default

When Phase A returns multiple plausible matches (the common case),
**do not ask the user to pick by label or description** — build all of
them onto the user's (asset, timeframe), backtest each, and rank by
actual performance. Picking by label is a KB-grounding regression; the
whole point of the library is measured outcomes. Ask the user to choose
only when the backtests tie, or to pick between equivalent PASSes.

A third mechanism (mined parameter priors from the 1.4M-strategy DB)
will land via MangroveAI endpoint after MangroveOracle issue #156. This
skill will transparently benefit once the SDK exposes it — no rewrite
needed here.

## Trigger

Activate when the user:

- Explicitly asks for a strategy ("build me...", "make a strategy", "find me...")
- Is in Stage 2 of `trading-bot-workflow.md` (wallet ready, wants to author)
- Says "trade X", "start running", "put money to work" and a strategy doesn't yet exist

Do NOT activate if the user is asking questions about an existing strategy (use `/monitor-trades` instead) or wants to promote/pause one (use `/promote-strategy`).

## Input — What to Collect

The agent needs four pieces of info before searching references. ORDER these in the order the user volunteered them; if they gave everything in one message, skip straight to Phase A (FAST ADVANCE):

1. **asset** — symbol (BTC, ETH, SOL, etc.). REQUIRED.
2. **timeframe** — 5m / 15m / 30m / 1h / 4h / 1d. Default 1h if user unclear. **1m is NOT supported** — the server will reject it. If the user asks for 1m, say: "1m isn't supported on the Mangrove data source; the smallest is 5m, but I'd recommend 1h for a first strategy — less noise, faster to backtest."
3. **goal / style** — natural language. Used for category detection (momentum, mean_reversion, trend_following, breakout, volatility).
4. **appetite for trades** — high-frequency (many small edges) vs. swing (fewer, bigger moves). Affects which references to recommend.

FAST ADVANCE: if the user's first message has asset + strategy_type + timeframe, skip the Q&A and go straight to Phase A. Infer the rest (knowledge level, sentiment) from tone — don't ask unnecessary clarifying questions.

Example FAST ADVANCE triggers:
- "Build a momentum strategy for ETH on 1h" → asset=ETH, timeframe=1h, goal=momentum
- "Give me a trend following BTC 4h setup" → asset=BTC, timeframe=4h, goal=trend_following

## Phase A — Search References (ALWAYS DO THIS FIRST)

Call `search_reference_strategies(asset, timeframe, goal_hint)`. You will get back up to 5 ranked candidates, each with:

- `id` (e.g. `ref-004` or `oracle-exp_…-<run_index>`)
- `label` (human-readable)
- `description` (why this works / source lineage)
- `entry_signals` + `exit_signals` (names, types, params)
- `category`
- `notes` (tuning hints — may be empty for Oracle-sourced entries)

If search returns 2+ plausible candidates, **do not ask the user to pick by label** — move straight to Phase B-bulk. If search returns exactly one, proceed to Phase B. If it returns 0 results or only low-score matches, jump to **Phase C (Custom build, KB-grounded)**.

Note: the library spans assets and timeframes beyond just the user's target — Oracle-sourced entries have lineage tied to the experiment where the combo was found, but the combo itself is portable. Treat the returned set as candidates regardless of their recorded asset/TF.

## Phase B — Build from Reference (single match)

Only when Phase A returned exactly one candidate. Call `build_strategy_from_reference(reference_id, asset=<user's>, timeframe=<user's>, name=<optional>)`.

You get back a `create_strategy_manual`-compatible payload with `persisted: false` — **building saves nothing**. There is no strategy_id until you pass the payload to `create_strategy_manual(...)` (REST: `POST /api/v1/agent/strategies/manual`; the `persisted` / `next_step` / `source_reference_id` keys are ignored there). DO NOT modify `entry`, `exit`, or `execution_config`. The whole point of Mechanism 2 is that these values came from strategies that already backtested well.

Only adjustable fields:

- `asset` override (retarget the combo onto the user's asset)
- `timeframe` override (retarget onto the user's TF)
- `name` (cosmetic)

If the user wants to TWEAK the reference (e.g. "but use RSI(9) not RSI(14)"): acknowledge their change, then move to Phase C's KB-citation discipline BEFORE applying the tweak. Don't change params without citation.

## Phase B-bulk — Build + backtest the top N references (common case)

When Phase A returns multiple candidates, bulk-build and bulk-backtest. This is the default path because picking by label means picking by description — and descriptions don't predict backtest performance.

```
for ref in references[:N]:        # N ~= 3-5
    payload = build_strategy_from_reference(
        reference_id=ref.id,
        asset=<user's asset>,     # retarget every ref onto the user's target
        timeframe=<user's TF>,    # same
    )
    strategy = create_strategy_manual(**payload)
    backtest_strategy(strategy.id, mode="full")
```

Then rank the results by the Phase F thresholds (sortino, sharpe, calmar, irr, max_drawdown, win_rate). Present the ranked table to the user:

- Winner (PASS) + 1–2 runners-up with per-threshold verdicts
- The loser(s) shown briefly so the user sees that the top one earned its spot, not picked blindly

Ask: "Promote the winner to paper, iterate on the runners-up, or start over with a different goal?" Do NOT auto-promote — the transition is the user's call.

If every backtest fails (all FAIL against threshold_spec), that's a signal the combos in the library don't fit this (asset, TF, goal). Move to Phase C or Phase D rather than promoting a losing config.

## Phase C — Custom Build (KB-GROUNDED, Mechanism 1)

Use this path when:
- Reference search returned nothing useful
- User explicitly wants something unusual (e.g. "combine Ichimoku and Bollinger breakouts")
- User wants to tweak a reference's parameters

**For each signal you consider, you MUST:**

1. Call `kb_search(query="<signal_name> parameters <asset> <timeframe>")`. Example: `kb_search("MACD parameters ETH 1h")`.
2. Read the top 1-3 results. Extract the recommended parameter range or default.
3. In your response, CITE the KB result verbatim (or paraphrase + attribute): "KB recommends MACD(8, 21, 5) for 1h crypto — tighter than the stock 12/26/9 because 1h bars have less noise than daily."
4. Only then write the param values into the strategy.

**Do NOT use library-default params without KB citation.** If the KB has nothing, say so: "KB doesn't have guidance on this specific (signal, asset, timeframe) combo. I'll use the signal's declared default [X] but flag it — consider running a wider backtest to validate."

Composition rules (TRIGGER vs FILTER) — from MangroveAI signal spec:

- **Entry**: EXACTLY ONE TRIGGER + ZERO OR MORE FILTERS
- **Exit**: ZERO OR ONE TRIGGER + ZERO OR MORE FILTERS
- **NEVER a FILTER without a TRIGGER in the same group**
- If the user doesn't specify exits explicitly, use `exit: []` — the volatility-based stop-loss + take-profit are AUTOMATIC at entry, not exit rules
- Each signal does ONE thing (Single Responsibility). Don't stack two momentum triggers; don't mix concepts.

When the config is ready, call `create_strategy_manual(...)` with it.

## Phase D — Autonomous path (FALLBACK when user says "just pick something")

If the user doesn't want to choose a reference or design custom, call `create_strategy_autonomous(goal, asset, timeframe)` with the user's goal text. The server generates N candidates, backtests them in bulk, and returns the winner.

Autonomous is the "I don't care, you decide" escape hatch. It's NOT the primary path — references are. Use autonomous when:
- User explicitly says "pick for me" / "surprise me" / "you decide"
- Phase A returned nothing AND Phase C is too much friction for the user's patience
- You've tried Phase B/C and the user rejected the results

Under no circumstances go straight to Phase D as the first move. Always try Phase A first.

## Phase E — Hand off to /backtest

Regardless of which phase built the strategy, hand off to the
**`/backtest` skill** for the backtest + verdict + iteration flow. That
skill owns window sizing (bar-count-driven, not months-per-timeframe),
the threshold verdict, the benchmark-relative line, and failure-mode
advice.

For Phase B-bulk: the bulk-backtest loop inside this skill still runs
`backtest_strategy` per candidate with a shared window (so the ranked
table is comparable). Size that shared window via the same bar-count
table `/backtest` uses (2000–5000 bars target). Once the winner is
picked, the user can invoke `/backtest` on the winner alone for deeper
investigation (walk-forward, alternative windows).

Do not duplicate the threshold table or verdict rules here — they live
in `/backtest`. If this skill detects a PASS, tell the user the result
and let them invoke `/promote-strategy` directly; if it detects a FAIL
or MARGINAL, suggest `/backtest` for the iteration path rather than
iterating in-place.

## Prohibited

- **Never** claim a signal is "firing" based on catalog listing alone. Only `evaluate_strategy` output can claim that.
- **Never** fabricate backtest metrics. If `metrics` missing from a tool response, say so.
- **Never** use library-default params in Phase C without KB citation.
- **Never** modify a reference's params in Phase B — move to Phase C if the user wants changes.
- **Never** invent signal names not returned by `list_signals` or a reference strategy. If you don't have a name, call `list_signals` first.
- **Never** place a FILTER without a TRIGGER in the same group (entry or exit).
- **Never** recommend 1m timeframes — server rejects them.

## Never Default to Swap Router

Manual swaps (`get_swap_quote` / `execute_swap`) are a fallback. Only route there if the strategy layer is down (`search_reference_strategies` + `create_strategy_autonomous` both fail), and disclose it.

## Summary — Decision Tree

```
User wants a strategy
│
├─→ Phase A: search_reference_strategies(asset, timeframe, goal_hint)
│       │
│       ├─ ≥2 candidates → Phase B-bulk (build all, backtest all, rank)
│       ├─ 1 candidate   → Phase B (single build + backtest)
│       ├─ 0 candidates  → Phase C
│       └─ user says "just pick" → Phase D (autonomous)
│
├─ Phase B-bulk: loop build_strategy_from_reference → create_strategy_manual
│       → backtest_strategy for each; retarget every ref onto the user's
│       (asset, timeframe); rank by threshold_spec; present ranked table
│
├─ Phase B: build_strategy_from_reference + create_strategy_manual
│       (single match; signals + params copied exactly, asset/tf applied)
│
├─ Phase C: kb_search each signal → cite → create_strategy_manual
│       (custom, evidence-backed)
│
└─ Phase D: create_strategy_autonomous(goal, asset, timeframe)
        (server generates + backtests N candidates, returns winner)

→ /backtest skill (window sizing, verdict, iteration)
→ /promote-strategy skill (inactive → paper → live; new strategies are saved as inactive)
```
