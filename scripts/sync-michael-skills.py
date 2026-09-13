#!/usr/bin/env python3
"""Sync MangroveAI copilot ("Michael") skills into this agent's plugin skills.

MangroveAI is the source of truth for these skills. This script copies them from
a MangroveAI checkout into ``.claude/skills/michael/<skill>/`` and adapts them to
this agent: Michael's tool names become this agent's MCP tool names, passages
that lean on tools this agent does not have are annotated with what to do
instead, and Michael-runtime-only instructions are rewritten. Output is
deterministic, so a re-run over the same source is byte-identical.

Usage (stdlib only, run from anywhere):

    # Render from a local MangroveAI checkout's working tree
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI

    # Render from a committed ref instead (ignores uncommitted edits in the checkout)
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --ref origin/main

    # No local checkout: fetch a snapshot with gh (needs access to the private repo)
    mkdir -p ~/src/mangroveai-snapshot
    gh api repos/MangroveTechnologies/MangroveAI/tarball/main \\
        | tar -xz --strip-components=1 -C ~/src/mangroveai-snapshot
    python scripts/sync-michael-skills.py --source ~/src/mangroveai-snapshot --commit <sha>

    # Fail (exit 1) if the committed copies differ from what the sync would produce
    python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --check

    # CI (no MangroveAI access): committed copies match skills-sync-manifest.json
    python scripts/sync-michael-skills.py --verify-manifest

When upstream wording changes, a patch anchor stops matching and the sync fails
with the anchor it could not find. Update the tables below; never hand-edit the
generated files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEST = REPO_ROOT / ".claude" / "skills" / "michael"
MANIFEST_NAME = "skills-sync-manifest.json"
AGENT_TOOLS_FILE = REPO_ROOT / "server" / "src" / "mcp" / "tools.py"

SOURCE_REPO = "MangroveTechnologies/MangroveAI"
SKILLS_REL = "src/MangroveAI/domains/agent/michael/skills"
TOOLS_REL = "src/MangroveAI/domains/agent/michael/tools"
PLUGIN_ROOT_REL = ".claude/skills/michael"

# Skills that do not apply to a Claude Code plugin.
#   conversation-memory: Claude Code keeps its own transcript; recall_conversation
#   reads Michael's summarised chat store, which has no equivalent here.
DROP_SKILLS = {"conversation-memory"}

# ---------------------------------------------------------------------------
# Tool-name map: Michael tool -> this agent's MCP tool.
# Every Michael tool a synced skill mentions must appear here or in UNAVAILABLE;
# the sync refuses to leave an unknown tool name in a skill.
# ---------------------------------------------------------------------------
TOOL_MAP: dict[str, str] = {
    # -- strategies ------------------------------------------------------------
    "save_strategy": "create_strategy_manual",
    "list_strategies": "list_strategies",
    "get_strategy": "get_strategy",
    # This agent's local store also holds deployment state (paper/live), so the
    # platform-deployment list is the same call here.
    "list_platform_strategies": "list_strategies",
    # Both are status transitions here: status="archived" / status="paper"|"live".
    # Prose that describes Michael's archive flag or deploy button is patched below.
    "archive_strategy": "update_strategy_status",
    "propose_deploy": "update_strategy_status",
    "evaluate_strategy": "evaluate_strategy",
    # -- backtests -------------------------------------------------------------
    "run_backtest": "backtest_strategy",
    "get_backtest": "get_backtest",
    "list_backtests": "list_backtests",
    "get_benchmark": "get_benchmark",
    "screen_candidates": "sieve_score",
    # -- portfolio (MangroveAI's execution record) -----------------------------
    "list_positions": "list_account_positions",
    "list_trades": "list_account_trades",
    # -- market data -----------------------------------------------------------
    "get_market_data": "get_market_data",
    "get_ohlcv": "get_ohlcv",
    "list_approved_assets": "list_approved_assets",
    # -- knowledge -------------------------------------------------------------
    "query_knowledge": "query_knowledge",
    "kb_search": "kb_search",
    "kb_get_document": "kb_get_document",
    "kb_glossary_get": "kb_glossary_get",
    # -- sweeps -> Oracle experiment lifecycle (closest equivalents) ------------
    "list_sweep_markets": "oracle_list_datasets",      # the catalog rows, labels included; no conditional counts
    "size_sweep": "oracle_validate_experiment",        # total_runs for a created draft
    "create_sweep": "oracle_create_experiment",        # then oracle_validate_experiment
    "launch_sweep": "oracle_launch_experiment",
    "get_sweep": "oracle_get_experiment",
    "pause_sweep": "oracle_pause_experiment",
    "list_sweeps": "oracle_list_experiments",
    "find_sweep_runs": "oracle_data_query",            # table=results; server-side filters + order_by
}

# Michael tools with no equivalent in this agent yet. Public endpoints + SDK
# methods for the first five are being added in MangroveAI / the SDK. Each value
# is what the skill should do instead; it is inserted after the first passage
# that mentions the tool, and later mentions are marked inline.
UNAVAILABLE: dict[str, str] = {
    "get_market_regime": (
        "Read direction and volatility yourself: `get_ohlcv` daily closes over 90, 180 and 365 days "
        "(returns, and realised volatility against the asset's own longer history), plus "
        "`get_market_data` for today. Say the reading is yours, not a platform regime label."
    ),
    "classify_market_segment": (
        "For a sweep-catalog window, `oracle_list_datasets` rows carry the same classifier's "
        "`direction`, `volatility`, `trend`, `regime_composite` and `market_era`. For any other "
        "date range there is no classifier here: describe it from `get_ohlcv` and do not present "
        "that as a catalog label."
    ),
    "query_signal_behavior": (
        "No measured firing rates or gap distributions are available. Read each signal's params, "
        "ranges and warmup with `query_knowledge` op=get, reason about frequency from those, say "
        "that the rates are estimates, and let a backtest's `total_trades` be the measurement."
    ),
    "verify_strategy": (
        "There is no dry-run check. `create_strategy_manual` validates the composition when it "
        "creates the strategy; read its error and fix what it names, then read the stored rules "
        "back with `get_strategy` before spending a backtest."
    ),
    "get_execution_config_schema": (
        "There is no parameter glossary here. The execution config a strategy runs with (canonical "
        "trading defaults merged with its overrides) is on `get_strategy`; describe only parameters "
        "you can read there, and do not claim defaults, bounds or effects you have not read."
    ),
    "update_strategy": (
        "Strategies are not edited in place here: create a corrected one with "
        "`create_strategy_manual` and archive the superseded one with `update_strategy_status` "
        "status=\"archived\"."
    ),
    "get_sweep_limits": (
        "Plan limits are not exposed; `oracle_validate_experiment` reports a sweep that is over "
        "one in its `errors`."
    ),
    "list_run_filters": (
        "Read which assets, timeframes and triggers a sweep's runs carry from its "
        "`oracle_list_results` rows before filtering on them."
    ),
    "get_sweep_run": (
        "Each `oracle_list_results` / `oracle_data_query` result row carries the `entry_json` and "
        "`exit_json` the engine drew; rebuild the strategy from them with `create_strategy_manual`, "
        "then backtest it with `backtest_strategy`."
    ),
    "build_market_window": (
        "New windows cannot be added to the catalog from this agent; choose from "
        "`oracle_list_datasets`, or backtest a saved strategy over the exact range with "
        "`backtest_strategy`."
    ),
}

# Agent tools a skill should also declare, beyond what the mapped upstream list gives.
EXTRA_USES: dict[str, list[str]] = {
    "market-intelligence": ["get_ohlcv", "oracle_list_datasets"],
    "portfolio": ["list_trades", "list_all_trades", "list_evaluations"],
    "strategy-composition": ["update_strategy_status"],
    "strategy-management": ["delete_strategy", "list_evaluations", "list_trades"],
    "sweeps": ["oracle_list_signals", "oracle_update_experiment", "backtest_strategy"],
    "sweep-results": ["oracle_list_results", "create_strategy_manual", "backtest_strategy"],
}

# ---------------------------------------------------------------------------
# Adaptations, applied to the UPSTREAM text before tool names are rewritten, so
# anchors read exactly like MangroveAI's file. Write this agent's tool names in
# replacement text as {{tool}} so the renaming pass leaves them alone.
#   ("text", old, new)      old must occur exactly once
#   ("section", heading, new_markdown)  replace a "## " section through the next heading
# ---------------------------------------------------------------------------
_ARCHETYPE_MAP_PATH = f"${{CLAUDE_PLUGIN_ROOT}}/{PLUGIN_ROOT_REL}/strategy-composition/signal_archetype_map.yaml"

PATCHES: dict[str, list[tuple[str, str, str]]] = {
    "backtesting": [
        ("text",
         "  spend a backtest on. Uses run_backtest, screen_candidates, get_backtest, list_backtests,\n"
         "  get_benchmark and get_execution_config_schema.\n",
         "  spend a backtest on. Uses run_backtest, screen_candidates, get_backtest, list_backtests\n"
         "  and get_benchmark.\n"),
        ("text",
         "`run_backtest` when there is genuinely no run for the window they are asking about.\n",
         "`run_backtest` when there is genuinely no run for the window they are asking about.\n\n"
         "Runs are stored against the API key's user, not against this agent's strategy ids: match a\n"
         "stored run to a strategy by `asset`, window and the `strategy_name` {{get_backtest}} returns.\n"),
        ("text",
         "With several candidates and one backtest's worth of patience, `screen_candidates` ranks them by how\n"
         "likely each is to trade at all. One asset per call, because a strategy's signals are measured on\n"
         "that asset's candles.\n",
         "With several candidates and one backtest's worth of patience, {{sieve_score}} scores them, up to\n"
         "99 per call: its binary head (`p_trades`) is how likely each is to trade at all, and that is what\n"
         "to rank by. Keep one asset per call, because a strategy's signals are measured on that asset's\n"
         "candles.\n"),
        ("text",
         "different asset or period, `get_benchmark`.\n",
         "different asset or period, `get_benchmark`. Here the holding return is\n"
         "`benchmark.buy_and_hold_return_pct` and the difference is `benchmark.strategy_minus_benchmark_pct`.\n"
         "When `benchmark.available` is false, say it could not be fetched and why (`reason`) -- never quote\n"
         "the strategy's return alone as though it were the whole answer.\n"),
        ("text",
         "and Calmar are null below `ratio_sample_minimum` daily observations, because a ratio computed from\n",
         "and Calmar are null below roughly 30 daily observations, because a ratio computed from\n"),
        ("text",
         "never \"its Sharpe is 0\".\n",
         "never \"its Sharpe is 0\".\n\n"
         "In this agent, `backtest_strategy` fills a missing `sharpe_ratio`, `win_rate`, `irr_annualized` or\n"
         "`max_drawdown` with `0.0` in its `metrics`. Read `num_days` and `total_trades` before believing a\n"
         "zero there, and when it matters read the stored run with {{get_backtest}}, whose metrics are\n"
         "exactly what the engine returned.\n"),
        ("text",
         "One (strategy, window) is ONE measurement. Asking for the same strategy over the same dates\n"
         "again is refused with the existing run's id -- read that with get_backtest. A new measurement\n",
         "One (strategy, window) is ONE measurement. Nothing here refuses the same strategy over the same\n"
         "dates again -- a repeat is billed and stored as a second run -- so find the existing one with\n"
         "{{list_backtests}} and read it with {{get_backtest}}. A new measurement\n"),
        ("section", "## The check comes before the spend", """## The check comes before the spend

A full backtest costs a unit of the monthly allowance when it is submitted, and nothing checks the
strategy again at that point: a run over a bad signal name or a missing timeframe is billed and
then fails. {{create_strategy_manual}} checks the composition when the strategy is created, so
before a backtest read the stored rules with {{get_strategy}} -- every signal name and parameter key
as {{query_knowledge}} has them, a `timeframe` on every signal -- and fix a fault by creating a
corrected strategy.

A signal parameter still at its library default is a value nobody chose. Either choose it (and
create the strategy with that value) or tell the user you considered the default and stand by it,
and why.

`backtest_strategy` takes a per-run `config` that merges over the canonical trading defaults --
execution parameters and `slippage_pct` / `fee_pct` alike. A result measured with overrides is a
measurement of that config, not of the strategy as stored: name the overrides whenever you quote
it, and prefer storing the execution config on the strategy (`execution_config` at creation) so the
record and the run agree."""),
        ("section", "## The window is not always the one requested", """## The window is not always the one requested

Read `resolved_window`: explicit `start_date` / `end_date` when dates were passed (or derived from
`lookback_days` / `lookback_hours`), or `lookback_months` when the span was chosen from the
strategy's timeframe. `metrics.num_days` is what the data actually supported, which can be less than
the range if history is short. If they asked for a year and got four months, say so before quoting
an annualised figure off it.

`start_date` and `end_date` go together; a lookback is the alternative to them, not an addition."""),
        ("section", "## Long windows fail rather than wait", """## Long windows take longer, and a timeout is not a result

`backtest_strategy` submits the run and polls it, so a wide window -- a multi-month `1h` run,
anything on `1d` -- is slow rather than refused. If polling gives up, the error names the run's
`backtest_id`: it may still finish server-side, so read it later with {{get_backtest}} instead of
submitting it again. No metrics exist until it completes. Never present an unfinished run as a
result, and never guess what it would have said."""),
    ],
    "strategy-composition": [
        ("text",
         "`load_skill resource=\"signal_archetype_map.yaml\"`",
         f"`{_ARCHETYPE_MAP_PATH}` (read that file)"),
        ("text",
         "you may use `regime_override=true`, but NEVER as",
         "you may build what they asked for (nothing here enforces a regime check), but NEVER as"),
        ("text",
         "`constraints` is for editing the trading defaults (aka execution config).",
         "`execution_config` on {{create_strategy_manual}} is for editing the trading defaults."),
        ("section", "## Verify the strategy before backtesting", """## Check the strategy before backtesting

There is no separate verification call in this agent. {{create_strategy_manual}} checks the
composition when it creates the strategy and refuses what it cannot run; read the error and fix
what it names. Then read what is STORED with {{get_strategy}}: every signal name as the graph has
it, every parameter key among that signal's `params`, and a `timeframe` on every signal. A fault
found here costs nothing; the same fault found by a backtest costs the person a measurement they
never got.

A parameter still sitting at its library default is a judgement you owe the user: change the value,
or say why the default is right for this asset and this horizon. Do not pass it over in silence.

Strategies are not edited in place here. To change a value, create a new strategy with the complete
corrected entry and exit lists, and archive the superseded one with {{update_strategy_status}}
`status="archived"` -- so every backtest stays attached to the rules it measured."""),
    ],
    "strategy-management": [
        ("text",
         "and offering a deploy the person has to press.",
         "and deploying to paper or live only on the person's say-so."),
        ("text",
         "and a deploy button is not a deploy.",
         "and a status change is a real change, not an offer."),
        ("text",
         "whether it is live is the platform's answer, above.",
         "in this agent the same record says whether it is live."),
        ("section", "## Two stores, and which one answers which question", """## One record, and it answers deployment too

This agent keeps every strategy it authored in its own local database, and that record is also where
deployment lives: `status` is `draft`, `inactive`, `paper`, `live` or `archived`, and a live strategy
carries its allocation. So "my strategies", "what's running", "what's live" is {{list_strategies}}
(filter with `status`), and "how are they doing" is what they actually did -- {{list_evaluations}} and
{{list_trades}} -- never a backtest."""),
        ("section", "## Draft means unmeasured, and it leaves draft on its own", """## What each status means

Read `status` off {{get_strategy}}; never describe it from memory. `draft` and `inactive` are
created but not running: nothing trades and nothing is scheduled. `paper` evaluates on the strategy's
timeframe with simulated fills and no funds. `live` executes real swaps from its allocation.
`archived` is retired. A strategy moves only through {{update_strategy_status}}, along the transitions
it allows, and a refused transition names the valid ones.

A strategy with no backtest behind it has not been measured, whatever its status. Say that plainly
rather than calling it ready, and back it over a real window before recommending paper. Never
describe a strategy as live, active or running unless its status says so."""),
        ("section", "## Nothing is ever deleted", """## Archive rather than delete

Asked to delete or get rid of a strategy, archive it: {{update_strategy_status}} with
`status="archived"`. Its local record, evaluations and trades stay, and every backtest of it stays
stored. Say so plainly -- "archived, so it's retired but nothing is lost" -- and say that archiving
here is one-way: an archived strategy cannot be reactivated, only rebuilt as a new one. Taking a
strategy off `live` needs `confirm=true`, and that is the user's call.

{{delete_strategy}} exists in this agent and removes the strategy upstream on MangroveAI (the local
audit trail is kept). Use it only when the user explicitly asks for deletion after hearing that
archiving is the non-destructive option."""),
        ("section", "## Deploying is the person's decision, and you only offer it", """## Deploying is the person's decision

There is no button here: {{update_strategy_status}} changes the status the moment you call it. So
never move a strategy to `paper` or `live` on your own initiative -- ask, get a clear yes, then call
it, and report what the response says happened.

`paper` needs nothing more. `live` is gated: the user explicitly asked, the wallet's secret is backed
up, the allocation block is complete, and `confirm=true` -- the full rules are in the `trading-bot`
skill. Do not describe a strategy as live until the call returned it live."""),
        ("section", "## Saving twice does not make two strategies", """## Creating twice makes two strategies

{{create_strategy_manual}} does not look for an existing strategy with the same rules or name: every
call creates a new one. {{list_strategies}} before creating, and if an identical one exists, use it.
Two strategies with the same rules and different ids is a mess the user has to clean up.

A new strategy has not been measured. The next step is a backtest over a real window -- the
backtesting skill -- before any talk of paper or live."""),
    ],
    "portfolio": [
        ("text",
         "Uses list_positions and list_trades.",
         "Uses list_positions and list_trades for MangroveAI's\n"
         "  execution record, and this agent's local {{list_trades}}, {{list_all_trades}} and {{list_evaluations}}."),
        ("text",
         "## Open positions are now; trades are over\n",
         "## Two records in this agent\n\n"
         "{{list_account_positions}} and {{list_account_trades}} read MangroveAI's execution record: activity\n"
         "from strategies deployed through MangroveAI itself. Strategies this agent runs keep their own record\n"
         "locally: {{list_trades}} for one strategy's fills, {{list_all_trades}} across all of them, and\n"
         "{{list_evaluations}} for what each tick saw. For \"how is my paper or live strategy doing\" here, the\n"
         "local record is the answer. When the user runs strategies in both places read both, and say which\n"
         "record every figure came from.\n\n"
         "## Open positions are now; trades are over\n"),
        ("text",
         "Both tools take `strategy_id`. Use it when the person is talking about one strategy, and leave\n"
         "it off when they ask about themselves.",
         "{{list_trades}} takes a `strategy_id` and {{list_all_trades}} covers every local strategy; the\n"
         "account tools filter by `account_id` and `asset`. Scope to one strategy when the person is talking\n"
         "about one, and leave the scope off when they ask about themselves."),
    ],
    "market-intelligence": [
        ("text",
         "  live price, get_market_regime for direction over three horizons plus volatility,\n"
         "  classify_market_segment for what one named stretch of the past was like in the sweep\n"
         "  catalog's own terms, and list_approved_assets for what the platform allows a strategy to\n"
         "  be built on.\n",
         "  live price, get_ohlcv for the price history direction and volatility are read from,\n"
         "  oracle_list_datasets for how the sweep catalog labelled a stretch of the past, and\n"
         "  list_approved_assets for what the platform allows a strategy to be built on.\n"),
        ("text",
         "- No 24h change, and no percentage move over any window shorter than 90 days.\n"
         "- No OHLCV series, no candles, no chart data.\n",
         "- No percentage move over any window shorter than 90 days.\n"
         "- No candles. (In this agent {{get_ohlcv}} does return candles -- provider-native bars, daily by\n"
         "  default -- and {{get_market_data}} carries the 24h change; the limits in this list are the\n"
         "  regime reading's own.)\n"),
    ],
    "knowledge-graph": [
        ("text",
         "then walks a hop along the edges.\n",
         "then walks a hop along the edges.\n\n"
         "> In this agent `ask` has the corpus index but not the pretrained one -- that needs the\n"
         "> `mangrove-kb[semantic]` extra, which is not installed -- and its `note` says so. The figures\n"
         "> below were measured with both; expect fewer paraphrased questions to land, and fall back to\n"
         "> `op=find` sooner.\n"),
    ],
    "sweeps": [
        ("text", "use <tool> to", "use {{oracle_list_datasets}} to"),
        ("text", "matching with <tool>)", "matching with {{query_knowledge}} `op=ask`)"),
        ("section", "## Offer only what their assets actually cover", """## Offer only what their assets actually cover

{{oracle_list_datasets}} after the assets are chosen. It returns the whole catalog -- one row per
window, each with its `asset`, `timeframe`, date span, and the labels the catalog's classifier gave
it (`direction`, `volatility`, `trend`, `regime_composite`, `market_era`) -- so filter it to the chosen
assets yourself, and offer only the regimes, eras and candle sizes those rows carry, with how many
windows each choice would add. An option that covers nothing is never offered, and a person is never
shown a market their assets do not have.

Rows with `coverage_ok: false` have missing bars; leave them out. If they ask for them back, say what
that means: a metric over a half-covered window is not comparable to one over a whole window.

The experiment config takes the chosen rows as whole dataset objects; the `sweep` skill has the
config shape, the signal-pool fields and the SIEVE pre-filter setting."""),
        ("section", "## When the catalog does not cover what they asked for", """## When the catalog does not cover what they asked for

This agent cannot cut a new window into the catalog. Say plainly that the catalog holds no window for
that range and offer the nearest windows it does hold. If they need exactly that range, a sweep is the
wrong tool: backtest a saved strategy over it with {{backtest_strategy}}."""),
        ("section", "## The number they say is not the number that runs", """## The number they say is not the number that runs

This is the single thing that goes wrong, and it goes wrong in one direction: the real size is always
larger. The draw count is PER WINDOW, and one asset at one candle size is many windows.

**You do not work it out.** Create the draft with {{oracle_create_experiment}} -- it runs nothing -- and
{{oracle_validate_experiment}} it: `total_runs` is Oracle's own count of what the plan walks. Report
that number and nothing else as the size. While the size is being settled, change the draft with
{{oracle_update_experiment}} and validate again; only drafts can change.

Plan limits are not exposed here; when a sweep is over one, validation says so in `errors`. Relay
Oracle's own words rather than paraphrasing a validation error into a guess.

The levers, when it is too big: fewer draws, one candle size instead of several, fewer assets or
windows, or a narrower signal pool. Setting a search up runs nothing, and getting it wrong twice is
fine."""),
        ("text",
         "and says\nhow many runs finished, which is what they keep.",
         "and\n{{oracle_get_experiment}} then says how many runs finished, which is what they keep."),
        ("text",
         "`size_sweep` asks the only thing that knows.",
         "{{oracle_validate_experiment}}'s `total_runs` is the only count that knows."),
    ],
    "sweep-results": [
        ("text",
         "`sort_by` is required on `find_sweep_runs`, deliberately. Take it from what they asked\n"
         "for, and **name it whenever you report an order**",
         "Always pass `order_by` to {{oracle_data_query}} (table `results`) -- nothing sorts for you,\n"
         "deliberately. Take it from what they asked for, and **name it whenever you report an order**"),
        ("text",
         "Every numeric filter takes BOTH ends, so a band is one question rather than a page\n"
         "fetched and thinned by hand:",
         "A band is two filter clauses on one column, a lower and an upper bound, in the same\n"
         "{{oracle_data_query}} call -- one question rather than a page fetched and thinned by hand:"),
        ("text",
         "Execution values -- the reward factor a run used, its cooldown, its ATR period -- can only\n"
         "be filtered inside ONE sweep. Naming one without an experiment_id is refused rather than\n"
         "quietly ignored.",
         "Execution values -- the reward factor a run used, its cooldown, its ATR period -- are\n"
         "columns on every result row, but they only compare within one sweep's design: filter on\n"
         "them together with `experiment_id`."),
        ("text",
         "Percent-typed metrics are on a **0-100** scale and arrive as display strings with the\n"
         "unit attached. `0.52%` means 0.52 percent, not 52. Quote them as they come; the raw\n"
         "values ride alongside for anything that computes.",
         "Percent-typed metrics (`total_return`, `win_rate`, `max_drawdown`, `irr_annualized`,\n"
         "`benchmark_asset_return`) are raw numbers on a **0-100** scale with no unit attached:\n"
         "`0.52` means 0.52 percent, not 52. Say the unit in the sentence when you quote one."),
        ("text",
         "Direction, volatility, trend and the\n"
         "full composite label are all available, as is the window's date span.",
         "Result rows do not carry those\n"
         "labels themselves: join a row's `data_file_path` (or its asset, timeframe and dates) to its\n"
         "{{oracle_list_datasets}} row for `direction`, `volatility`, `trend` and `regime_composite`, and\n"
         "filter on the date span directly."),
        ("text",
         "And it\n"
         "carries the character of the window it ran in, so",
         "Its window's\n"
         "character is one join away (its dataset row in {{oracle_list_datasets}}), and"),
    ],
}

# ---------------------------------------------------------------------------
# Machinery
# ---------------------------------------------------------------------------

_SENTINEL = re.compile(r"\{\{([a-z0-9_]+)\}\}")
_FENCE = re.compile(r"^\s*```")
_UNAVAILABLE_MARK = " *(not in this agent yet)*"


class SyncError(RuntimeError):
    pass


def _tok(name: str) -> re.Pattern[str]:
    # A whole tool name, not part of a longer identifier and not inside {{...}}.
    return re.compile(rf"(?<![\w{{]){re.escape(name)}(?![\w}}])")


@dataclass
class Source:
    root: Path
    ref: str | None

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def list_files(self, rel_dir: str) -> list[str]:
        if self.ref:
            out = self._git("ls-tree", "-r", "--name-only", self.ref, "--", rel_dir)
            return sorted(p[len(rel_dir) + 1:] for p in out.splitlines() if p)
        base = self.root / rel_dir
        if not base.is_dir():
            raise SyncError(f"{base} not found; --source must be a MangroveAI checkout")
        return sorted(str(p.relative_to(base)) for p in base.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)

    def read(self, rel_path: str) -> str:
        if self.ref:
            return self._git("show", f"{self.ref}:{rel_path}")
        return (self.root / rel_path).read_text(encoding="utf-8")

    def commit(self, explicit: str | None) -> str | None:
        if explicit:
            return explicit
        try:
            sha = self._git("rev-parse", self.ref or "HEAD").strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        if not self.ref and self._git("status", "--porcelain", "--", SKILLS_REL).strip():
            sha += "+dirty"
        return sha


@dataclass
class Rendered:
    files: dict[str, str] = field(default_factory=dict)          # dest-relative path -> content
    source_hashes: dict[str, str] = field(default_factory=dict)  # source-relative path -> sha256


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def agent_tools() -> set[str]:
    return set(re.findall(r"^\s+async def ([a-z0-9_]+)\(", AGENT_TOOLS_FILE.read_text(), re.M))


def michael_tools(src: Source) -> set[str]:
    names: set[str] = set()
    for rel in src.list_files(TOOLS_REL):
        if rel.endswith(".py"):
            names |= set(re.findall(r'^\s+name = "([a-z0-9_]+)"', src.read(f"{TOOLS_REL}/{rel}"), re.M))
    return names


def _apply_patches(skill: str, text: str) -> str:
    for kind, old, new in PATCHES.get(skill, []):
        if kind == "text":
            count = text.count(old)
            if count != 1:
                raise SyncError(f"{skill}: patch anchor matched {count} times (want 1): {old[:90]!r}")
            text = text.replace(old, new)
        elif kind == "section":
            lines = text.split("\n")
            try:
                start = lines.index(old)
            except ValueError:
                raise SyncError(f"{skill}: section heading not found: {old!r}") from None
            end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2} ", lines[i])), len(lines))
            tail = lines[end:]
            replacement = new.rstrip("\n").split("\n")
            text = "\n".join(lines[:start] + replacement + ([""] + tail if tail else [""]))
        else:  # pragma: no cover - table typo
            raise SyncError(f"{skill}: unknown patch kind {kind!r}")
    return text


def _rename(text: str) -> str:
    for michael, agent in TOOL_MAP.items():
        if michael != agent:
            text = _tok(michael).sub(agent, text)
    return text


def _blocks(body: str) -> list[list[str]]:
    """Split markdown into blocks: fenced code, paragraphs, and single blank lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if in_fence:
            current.append(line)
            if _FENCE.match(line):
                blocks.append(current)
                current, in_fence = [], False
            continue
        if _FENCE.match(line):
            if current:
                blocks.append(current)
            current, in_fence = [line], True
        elif line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            blocks.append([""])
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _annotate_unavailable(body: str) -> str:
    """Full note after the first passage naming an unavailable tool; inline mark after that."""
    seen: set[str] = set()
    out: list[str] = []
    for block in _blocks(body):
        text = "\n".join(block)
        hits = sorted(((m.start(), name) for name in UNAVAILABLE for m in [_tok(name).search(text)] if m))
        names = [name for _, name in hits]
        is_code = bool(block) and bool(_FENCE.match(block[0]))
        if not is_code:
            for name in names:
                if name in seen:
                    text = re.sub(rf"(`?)(?<![\w{{]){re.escape(name)}(?![\w}}])(`?)",
                                  lambda m, n=name: f"{m.group(1)}{n}{m.group(2)}{_UNAVAILABLE_MARK}", text)
        out.append(text)
        fresh = [name for name in names if name not in seen]
        if fresh:
            out.append("")
            for name in fresh:
                out.append(f"> **Not in mangrove-agent yet: `{name}`.** {UNAVAILABLE[name]}")
            seen.update(fresh)
    return "\n".join(out)


def _split_frontmatter(skill: str, text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise SyncError(f"{skill}: SKILL.md has no frontmatter")
    end = text.index("\n---\n", 4)
    return text[4:end], text[end + 5:]


def render_skill(skill: str, text: str, known_agent_tools: set[str], all_michael_tools: set[str]) -> str:
    text = _apply_patches(skill, text.replace("\r\n", "\n"))
    frontmatter, body = _split_frontmatter(skill, text)

    uses_match = re.search(r"^uses-tools:\s*\[(.*?)\]\s*$", frontmatter, re.M)
    upstream_uses = [t.strip() for t in uses_match.group(1).split(",") if t.strip()] if uses_match else []
    uses: list[str] = []
    for tool in upstream_uses:
        if tool in UNAVAILABLE:
            continue
        if tool not in TOOL_MAP:
            raise SyncError(f"{skill}: uses-tools names {tool!r}, which is in neither TOOL_MAP nor UNAVAILABLE")
        uses.append(TOOL_MAP[tool])
    uses += EXTRA_USES.get(skill, [])
    uses = list(dict.fromkeys(uses))
    missing = [t for t in uses if t not in known_agent_tools]
    if missing:
        raise SyncError(f"{skill}: maps to tools this agent does not register: {missing}")

    frontmatter = _rename(frontmatter)
    if uses_match:
        frontmatter = re.sub(r"^uses-tools:.*$", f"uses-tools: [{', '.join(uses)}]", frontmatter, flags=re.M)
    leaked = [name for name in UNAVAILABLE if _tok(name).search(frontmatter)]
    if leaked:
        raise SyncError(f"{skill}: frontmatter still names unavailable tools {leaked}; add a PATCHES entry")

    body = _annotate_unavailable(_rename(body))
    # Tool names read as code in prose, as upstream writes them; the YAML description stays bare.
    body = _SENTINEL.sub(lambda m: f"`{m.group(1)}`", body)
    frontmatter = _SENTINEL.sub(lambda m: m.group(1), frontmatter)

    # Every Michael tool left in the text must be one this agent has, or be annotated.
    for name in sorted(all_michael_tools - known_agent_tools - set(UNAVAILABLE)):
        if _tok(name).search(body) or _tok(name).search(frontmatter):
            raise SyncError(f"{skill}: mentions Michael tool {name!r}, which has no mapping; "
                            "add it to TOOL_MAP or UNAVAILABLE")

    provenance = (
        f"<!-- Synced from {SOURCE_REPO} {SKILLS_REL}/{skill}/SKILL.md by "
        "scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the "
        "script's adaptation tables, and re-run the sync. -->"
    )
    return f"---\n{frontmatter}\n---\n\n{provenance}\n{body.rstrip()}\n"


def render(src: Source) -> Rendered:
    known = agent_tools()
    michael = michael_tools(src)
    if not michael:
        raise SyncError(f"no Michael tools found under {TOOLS_REL}")
    unknown_targets = sorted({t for t in TOOL_MAP.values() if t not in known})
    if unknown_targets:
        raise SyncError(f"TOOL_MAP targets not registered in {AGENT_TOOLS_FILE.name}: {unknown_targets}")

    rendered = Rendered()
    for rel in src.list_files(SKILLS_REL):
        if "/" not in rel:
            continue  # top-level placeholders (e.g. .gitkeep) are not part of any skill
        skill = rel.split("/", 1)[0]
        if skill in DROP_SKILLS:
            continue
        content = src.read(f"{SKILLS_REL}/{rel}")
        rendered.source_hashes[rel] = _sha(content)
        if rel.endswith("/SKILL.md"):
            rendered.files[rel] = render_skill(skill, content, known, michael)
        else:
            # Supporting data (e.g. signal_archetype_map.yaml) is copied verbatim.
            for name in sorted(michael | set(TOOL_MAP)):
                if _tok(name).search(content) and TOOL_MAP.get(name) != name:
                    raise SyncError(f"{rel}: supporting file names tool {name!r}; it would need rewriting")
            rendered.files[rel] = content if content.endswith("\n") else content + "\n"
    unused = sorted(set(PATCHES) - {r.split("/", 1)[0] for r in rendered.files})
    if unused:
        raise SyncError(f"PATCHES reference skills that no longer exist upstream: {unused}")
    return rendered


def manifest_for(rendered: Rendered, commit: str | None, ref: str | None) -> dict:
    return {
        "generator": "scripts/sync-michael-skills.py",
        "source": {"repo": SOURCE_REPO, "path": SKILLS_REL, "commit": commit, "ref": ref},
        "dropped_skills": sorted(DROP_SKILLS),
        "source_files": dict(sorted(rendered.source_hashes.items())),
        "files": {rel: _sha(text) for rel, text in sorted(rendered.files.items())},
    }


def _on_disk() -> dict[str, str]:
    if not DEST.is_dir():
        return {}
    return {str(p.relative_to(DEST)): p.read_text(encoding="utf-8")
            for p in sorted(DEST.rglob("*")) if p.is_file() and p.name != MANIFEST_NAME}


def write(rendered: Rendered, manifest: dict) -> None:
    existing = _on_disk()
    for rel in sorted(set(existing) - set(rendered.files)):
        (DEST / rel).unlink()
        print(f"removed stale {rel}")
    for rel, text in sorted(rendered.files.items()):
        path = DEST / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing.get(rel) != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {rel}")
    for directory in sorted((p for p in DEST.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    (DEST / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST_NAME} ({len(rendered.files)} files)")


def check(rendered: Rendered, manifest: dict) -> int:
    problems: list[str] = []
    existing = _on_disk()
    for rel in sorted(set(rendered.files) | set(existing)):
        if rel not in existing:
            problems.append(f"missing: {rel}")
        elif rel not in rendered.files:
            problems.append(f"not produced by the sync: {rel}")
        elif existing[rel] != rendered.files[rel]:
            problems.append(f"differs: {rel}")
    manifest_path = DEST / MANIFEST_NAME
    if not manifest_path.is_file():
        problems.append(f"missing: {MANIFEST_NAME}")
    else:
        committed = json.loads(manifest_path.read_text())
        for key in ("files", "source_files", "dropped_skills"):
            if committed.get(key) != manifest[key]:
                problems.append(f"{MANIFEST_NAME}: `{key}` is stale")
        if committed.get("source", {}).get("commit") != manifest["source"]["commit"]:
            print(f"note: manifest records source commit {committed.get('source', {}).get('commit')}, "
                  f"this source is {manifest['source']['commit']} (content is what is compared)")
    for p in problems:
        print(p)
    print("check: OK" if not problems else f"check: FAILED ({len(problems)} problem(s)); re-run the sync")
    return 1 if problems else 0


def verify_manifest() -> int:
    manifest_path = DEST / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"missing {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text())
    expected: dict[str, str] = manifest.get("files", {})
    actual = {rel: _sha(text) for rel, text in _on_disk().items()}
    problems = [f"missing: {rel}" for rel in sorted(set(expected) - set(actual))]
    problems += [f"not in manifest: {rel}" for rel in sorted(set(actual) - set(expected))]
    problems += [f"hand-edited or stale: {rel}" for rel in sorted(set(expected) & set(actual))
                 if expected[rel] != actual[rel]]
    for p in problems:
        print(p)
    if problems:
        print("verify-manifest: FAILED -- regenerate with scripts/sync-michael-skills.py --source <MangroveAI>")
        return 1
    print(f"verify-manifest: OK ({len(expected)} files match source commit {manifest['source'].get('commit')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, help="MangroveAI checkout (or extracted snapshot) root")
    parser.add_argument("--ref", help="read the skills at this git ref of --source instead of its working tree")
    parser.add_argument("--commit", help="source commit to record when --source is not a git checkout")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if committed copies differ from a fresh sync")
    mode.add_argument("--verify-manifest", action="store_true",
                      help="exit 1 if committed copies differ from skills-sync-manifest.json (no source needed)")
    args = parser.parse_args(argv)

    if args.verify_manifest:
        return verify_manifest()
    if not args.source:
        parser.error("--source is required unless --verify-manifest is given")
    src = Source(args.source.expanduser().resolve(), args.ref)
    try:
        rendered = render(src)
        manifest = manifest_for(rendered, src.commit(args.commit), args.ref)
    except (SyncError, subprocess.CalledProcessError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2
    if args.check:
        return check(rendered, manifest)
    write(rendered, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
